"""Authentication strategies for yt-dlp invocations.

This module introduces a pluggable :class:`Authenticator` protocol so
that the CLI / GUI / bot can choose between cookie files, native browser
cookie extraction, bgutil PO-Token provider, or no auth at all, without
each interface needing to hand-roll argv fragments.

Design notes
------------
* The module is cross-platform. Any Windows-specific probe (DPAPI,
  ``ctypes.windll``) is guarded by ``sys.platform == "win32"`` and
  routed through :func:`core.cookies.is_admin_on_windows`.
* No third-party runtime dependency is imported eagerly. In particular
  the presence of ``bgutil-ytdlp-pot-provider`` is checked via
  :func:`importlib.util.find_spec` or a subprocess probe — never via
  ``import bgutil_*`` at module load time.
* :class:`AuthValidationResult` is field-compatible with
  :class:`core.cookies.CookieUpdateResult` (``success`` / ``error_kind``
  / ``message``). ``error_kind`` reuses the existing buckets from
  :func:`core.cookies._classify_error` and adds
  ``"bgutil_unreachable"``, ``"bgutil_plugin_missing"``, and
  ``"yt_dlp_too_old"``.
* :meth:`Authenticator.to_yt_dlp_flags` returns argv tokens.
  :meth:`Authenticator.env_overrides` returns extra environment
  variables for the yt-dlp subprocess (currently unused by every
  strategy; the bgutil plugin only reads its extractor arg).

Public API
----------
* :class:`Authenticator` (Protocol)
* :class:`AuthValidationResult`
* :class:`CookieFileAuth`
* :class:`BrowserCookieAuth`
* :class:`BgutilPoTokenAuth`
* :class:`NoAuth`
* :func:`authenticator_from_legacy_dict`
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from core.cookies import (
    CHROMIUM_BROWSERS,
    is_admin_on_windows,
    no_window_kwargs,
    yt_dlp_command,
    yt_dlp_env,
)

__all__ = [
    "Authenticator",
    "AuthValidationResult",
    "CookieFileAuth",
    "BrowserCookieAuth",
    "BgutilPoTokenAuth",
    "NoAuth",
    "authenticator_from_legacy_dict",
]

logger = logging.getLogger(__name__)


# --- Result type ------------------------------------------------------------


@dataclass
class AuthValidationResult:
    """Outcome of :meth:`Authenticator.validate`.

    Field layout is intentionally compatible with
    :class:`core.cookies.CookieUpdateResult` so callers can treat both
    uniformly.

    Attributes:
        success: ``True`` iff the authenticator is usable.
        error_kind: One of (existing buckets reused from cookies.py)
            ``"admin_context"``, ``"browser_locked"``,
            ``"browser_not_found"``, ``"yt_dlp_missing"``, ``"other"``,
            or one of the auth-specific kinds
            ``"cookie_file_missing"``, ``"cookie_file_empty"``,
            ``"bgutil_unreachable"``, ``"bgutil_plugin_missing"``,
            ``"yt_dlp_too_old"``, or ``None`` on success.
        message: Human-readable message (may contain Korean UI text).
        file_size: Size of cookie file in bytes, when applicable.
    """

    success: bool
    error_kind: Optional[str]
    message: str
    file_size: Optional[int] = None


# --- Protocol ---------------------------------------------------------------


@runtime_checkable
class Authenticator(Protocol):
    """Strategy interface for yt-dlp authentication.

    Implementations must:

    * expose a ``kind`` string tag;
    * produce argv tokens via :meth:`to_yt_dlp_flags` (may be empty);
    * produce extra env vars via :meth:`env_overrides` (may be empty);
    * be able to self-check via :meth:`validate`;
    * describe themselves for UI via :meth:`describe`.
    """

    kind: str

    def to_yt_dlp_flags(self) -> list[str]:
        """Return argv tokens to splice into the yt-dlp command."""
        ...

    def env_overrides(self) -> dict[str, str]:
        """Return environment variable overrides for the child process.

        The downloader merges this dict into ``os.environ`` before
        invoking yt-dlp. An empty dict is the common case.
        """
        ...

    def validate(self) -> AuthValidationResult:
        """Best-effort pre-flight check.

        MUST NOT raise for expected failure conditions; pack them into
        the :class:`AuthValidationResult` instead.
        """
        ...

    def describe(self) -> str:
        """Short Korean-friendly label for UI."""
        ...


# --- Concrete: no-auth ------------------------------------------------------


@dataclass
class NoAuth:
    """No-auth strategy. Emits no flags; warns in :meth:`validate`."""

    kind: str = field(default="none", init=False)

    def to_yt_dlp_flags(self) -> list[str]:
        return []

    def env_overrides(self) -> dict[str, str]:
        return {}

    def validate(self) -> AuthValidationResult:
        return AuthValidationResult(
            success=True,
            error_kind=None,
            message=(
                "\u26a0\ufe0f  인증 없이 진행합니다. YouTube 봇 감지에 걸릴 "
                "가능성이 매우 높습니다."
            ),
        )

    def describe(self) -> str:
        return "인증 안함"


# --- Concrete: cookie file --------------------------------------------------


@dataclass
class CookieFileAuth:
    """Use an existing Netscape-format cookies.txt file."""

    path: str
    kind: str = field(default="cookie-file", init=False)

    def to_yt_dlp_flags(self) -> list[str]:
        return ["--cookies", str(self.path)]

    def env_overrides(self) -> dict[str, str]:
        return {}

    def validate(self) -> AuthValidationResult:
        p = Path(self.path)
        if not p.exists():
            return AuthValidationResult(
                success=False,
                error_kind="cookie_file_missing",
                message=f"쿠키 파일을 찾을 수 없습니다: {p}",
            )
        if not p.is_file():
            return AuthValidationResult(
                success=False,
                error_kind="cookie_file_missing",
                message=f"쿠키 경로가 일반 파일이 아닙니다: {p}",
            )
        size = p.stat().st_size
        if size == 0:
            return AuthValidationResult(
                success=False,
                error_kind="cookie_file_empty",
                message=f"쿠키 파일이 비어 있습니다: {p}",
                file_size=0,
            )
        return AuthValidationResult(
            success=True,
            error_kind=None,
            message=f"쿠키 파일 확인 완료: {p} ({size} bytes)",
            file_size=size,
        )

    def describe(self) -> str:
        return f"쿠키 파일 ({self.path})"


# --- Concrete: browser cookie ----------------------------------------------


@dataclass
class BrowserCookieAuth:
    """Use ``--cookies-from-browser`` to pull cookies live.

    Note: this mode is intended for interactive desktop use. In
    headless Linux / Docker the browser will not be present; callers
    should prefer :class:`CookieFileAuth` or :class:`BgutilPoTokenAuth`
    in those environments.
    """

    browser: str
    profile: Optional[str] = None
    kind: str = field(default="cookie-browser", init=False)

    @property
    def _spec(self) -> str:
        return f"{self.browser}:{self.profile}" if self.profile else self.browser

    def to_yt_dlp_flags(self) -> list[str]:
        return ["--cookies-from-browser", self._spec]

    def env_overrides(self) -> dict[str, str]:
        return {}

    def validate(self) -> AuthValidationResult:
        browser = (self.browser or "").lower()
        if not browser:
            return AuthValidationResult(
                success=False,
                error_kind="browser_not_found",
                message="브라우저 이름이 지정되지 않았습니다.",
            )

        # Windows + Chromium-family + admin context -> DPAPI rejects.
        # Reuse the existing probe from cookies.py so behaviour stays in
        # lock-step with update_cookies().
        if is_admin_on_windows() and browser in CHROMIUM_BROWSERS:
            return AuthValidationResult(
                success=False,
                error_kind="admin_context",
                message=(
                    "관리자 권한으로 실행 중입니다. Chrome/Edge 계열 브라우저는 "
                    "DPAPI 제약으로 관리자 모드에서 쿠키를 복호화할 수 없습니다. "
                    "관리자가 아닌 일반 창에서 다시 실행해주세요."
                ),
            )

        # We don't actually probe the browser's cookie DB here; that is
        # the job of core.cookies.update_cookies(). The common failure
        # mode (admin context) is covered above; anything else is
        # surfaced later when yt-dlp itself runs.
        return AuthValidationResult(
            success=True,
            error_kind=None,
            message=f"브라우저 쿠키 모드 준비 완료: {self._spec}",
        )

    def describe(self) -> str:
        return f"브라우저 쿠키 ({self._spec})"


# --- Concrete: bgutil PO Token ---------------------------------------------


def _bgutil_plugin_available() -> bool:
    """Lazy check for the bgutil-ytdlp-pot-provider yt-dlp plugin.

    We never ``import bgutil_*`` eagerly; we only ask importlib whether
    the spec resolves, which is cheap and has no side effects.

    The upstream package ships under a couple of module names depending
    on version; check the known ones.
    """
    candidates = (
        "bgutil_ytdlp_pot_provider",
        "yt_dlp_plugins.extractor.getpot_bgutil",
        "yt_dlp_plugins.extractor.getpot_bgutil_http",
    )
    for name in candidates:
        try:
            if importlib.util.find_spec(name) is not None:
                return True
        except (ImportError, ValueError):
            # Malformed package cache; treat as not-found.
            continue
    return _plugin_beside_yt_dlp()


# Where a standalone yt-dlp keeps plugins: next to its executable.
_PLUGIN_FILES_BESIDE_EXE = "yt-dlp-plugins/*/yt_dlp_plugins/extractor/getpot_bgutil*.py"


def _plugin_beside_yt_dlp() -> bool:
    """True when the yt-dlp that will actually run has the plugin beside it.

    The standalone ``yt-dlp.exe`` (the bundled one, or any on the PATH) loads plugins from
    ``yt-dlp-plugins/<package>/yt_dlp_plugins`` next to itself, not from the Python
    environment this process runs in. A source run that uses the bundled yt-dlp has the
    plugin only there (the real run log lists it under "Plugin directories"), so looking
    at this interpreter's packages alone called it missing and stopped the pre-flight check
    before it pinged the sidecar."""
    try:
        command = yt_dlp_command()
    except FileNotFoundError:
        return False
    # (For the "python -m yt_dlp" form this looks beside python.exe, where no such folder is.)
    return any(Path(command[0]).resolve().parent.glob(_PLUGIN_FILES_BESIDE_EXE))


@dataclass
class BgutilPoTokenAuth:
    """Delegate PO Token minting to a bgutil sidecar.

    The yt-dlp plugin ``bgutil-ytdlp-pot-provider`` injects the required
    PO Token into each extraction automatically. It learns the provider
    address only from the ``youtubepot-bgutilhttp:base_url`` extractor
    arg; it reads no environment variable and silently falls back to
    ``http://127.0.0.1:4416`` when the arg is missing.
    """

    provider_url: str = "http://127.0.0.1:4416"
    kind: str = field(default="po-token-bgutil", init=False)

    def to_yt_dlp_flags(self) -> list[str]:
        return [
            "--extractor-args",
            f"youtubepot-bgutilhttp:base_url={self.provider_url}",
        ]

    def env_overrides(self) -> dict[str, str]:
        return {}

    def _probe_http(self) -> tuple[bool, str]:
        """Hit the bgutil provider's ping endpoint.

        Returns ``(ok, message)``. ``ok`` is True iff we got a 2xx
        response from *any* reasonable probe path. We try the common
        ``/ping`` first, fall back to a bare GET at ``/`` which bgutil
        also answers.
        """
        base = self.provider_url.rstrip("/")
        for path in ("/ping", "/", "/health"):
            url = f"{base}{path}"
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
                    if 200 <= resp.status < 300:
                        body = resp.read(256)
                        hint = body.decode("utf-8", errors="replace").strip()
                        return True, f"{url} -> HTTP {resp.status} {hint}".strip()
            except urllib.error.HTTPError as exc:
                # 404 on /ping just means wrong path on this version;
                # keep trying.
                if exc.code in (404, 405):
                    continue
                return False, f"{url} -> HTTP {exc.code} {exc.reason}"
            except urllib.error.URLError as exc:
                return False, f"{url} -> {exc.reason}"
            except (TimeoutError, OSError) as exc:
                return False, f"{url} -> {exc}"
        return False, f"{base}: 예상된 엔드포인트(/ping, /, /health) 모두 응답 없음"

    def _yt_dlp_version_ok(self) -> tuple[bool, str]:
        """Check yt-dlp is new enough to support PO Token injection.

        The GetPOT extension requires yt-dlp >= 2024.07.16. We do a
        cheap ``--version`` probe; on failure we just return ``True``
        and let the plugin/yt-dlp surface the real error later.
        """
        cmd = yt_dlp_command() + ["--version"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=yt_dlp_env(),
                timeout=10,
                **no_window_kwargs(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return True, ""  # Defer to yt-dlp itself.
        except Exception:  # pragma: no cover - defensive
            return True, ""

        out = (proc.stdout or "").strip()
        # yt-dlp versions are date-shaped "YYYY.MM.DD" strings; parse
        # lexicographically which works because the format is
        # zero-padded.
        if not out or "." not in out:
            return True, ""
        parts = out.split(".")
        try:
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        except (ValueError, IndexError):
            return True, ""
        # Cut-off: 2024.07.16 — first release with the public GetPOT hook.
        if (y, m, d) < (2024, 7, 16):
            return False, out
        return True, out

    def validate(self) -> AuthValidationResult:
        # Check #1: plugin is installed.
        if not _bgutil_plugin_available():
            return AuthValidationResult(
                success=False,
                error_kind="bgutil_plugin_missing",
                message=(
                    "bgutil-ytdlp-pot-provider 플러그인을 찾을 수 없습니다.\n"
                    "  - 소스 모드: 'uv pip install bgutil-ytdlp-pot-provider' 후 재시도\n"
                    "  - 배포 빌드: build.ps1 을 다시 실행해 새 dist 를 생성"
                ),
            )

        # Check #2: yt-dlp version.
        ok, version = self._yt_dlp_version_ok()
        if not ok:
            return AuthValidationResult(
                success=False,
                error_kind="yt_dlp_too_old",
                message=(
                    f"yt-dlp 버전이 너무 낮습니다 (현재: {version}). "
                    "PO Token 기능을 사용하려면 yt-dlp 2024.07.16 이상이 필요합니다. "
                    "'uv pip install -U yt-dlp' 를 실행해주세요."
                ),
            )

        # Check #3: provider reachable.
        reachable, hint = self._probe_http()
        if not reachable:
            return AuthValidationResult(
                success=False,
                error_kind="bgutil_unreachable",
                message=(
                    f"bgutil provider 에 연결할 수 없습니다: {self.provider_url}\n"
                    f"세부 정보: {hint}\n"
                    "Docker 로 사이드카를 기동해주세요:\n"
                    "  docker run -d -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider:latest"
                ),
            )

        return AuthValidationResult(
            success=True,
            error_kind=None,
            message=f"bgutil provider 연결 성공: {hint}",
        )

    def describe(self) -> str:
        return f"PO Token (bgutil @ {self.provider_url})"


# --- Legacy dict -> Authenticator ------------------------------------------


def authenticator_from_legacy_dict(cookie_opts: Mapping[str, Any]) -> Authenticator:
    """Convert a legacy ``cookie_opts`` dict into an :class:`Authenticator`.

    Kept here so that callers still passing the pre-refactor dict shape
    (``{"cookiefile": "..."}`` / ``{"cookiesfrombrowser": ("chrome",)}``
    / ``{}``) do not break. The GUI previously produced exactly this
    shape; see ``interfaces.gui.DownloaderGUI._collect_options``.

    Accepts:
        * ``{}`` or ``None``                                 -> :class:`NoAuth`
        * ``{"cookiefile": path}``                           -> :class:`CookieFileAuth`
        * ``{"cookiesfrombrowser": (browser,)}`` or just a
          string value                                       -> :class:`BrowserCookieAuth`

    Any unrecognised shape falls through to :class:`NoAuth`.
    """
    if not cookie_opts:
        return NoAuth()

    if "cookiefile" in cookie_opts and cookie_opts["cookiefile"]:
        return CookieFileAuth(path=str(cookie_opts["cookiefile"]))

    if "cookiesfrombrowser" in cookie_opts and cookie_opts["cookiesfrombrowser"]:
        cfb = cookie_opts["cookiesfrombrowser"]
        if isinstance(cfb, (tuple, list)):
            browser = str(cfb[0]) if cfb else ""
            profile = str(cfb[1]) if len(cfb) > 1 and cfb[1] else None
            return BrowserCookieAuth(browser=browser, profile=profile)
        return BrowserCookieAuth(browser=str(cfb))

    logger.debug("authenticator_from_legacy_dict: unrecognised shape %r", cookie_opts)
    return NoAuth()
