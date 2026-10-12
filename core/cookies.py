"""Cookie management for yt-dlp.

Pure-logic module: no tkinter, no print(), no input().
Works on both Windows and Linux. Uses yt-dlp's own
``--cookies-from-browser`` + ``--cookies`` combo to materialise a
Netscape-format ``cookies.txt`` file by performing a real (but
download-skipped) request against youtube.com.

Public API:
    yt_dlp_command() -> list[str]
    update_cookies(browser, output_file, profile=None) -> CookieUpdateResult
    load_cookies(file) -> Path
    is_admin_on_windows() -> bool
    CookieUpdateResult  (dataclass)
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Union

__all__ = [
    "CHROMIUM_BROWSERS",
    "CookieUpdateResult",
    "yt_dlp_command",
    "yt_dlp_env",
    "no_window_kwargs",
    "update_cookies",
    "load_cookies",
    "is_admin_on_windows",
]

logger = logging.getLogger(__name__)

PathLike = Union[str, os.PathLike]

# The sentinel URL used to trigger yt-dlp's cookie resolution and
# Netscape-file emission without performing an actual download.
_COOKIE_TRIGGER_URL = "https://www.youtube.com"

# Chromium-family browsers encrypt cookies with DPAPI, which is scoped to
# the non-elevated user: an elevated (admin) process cannot decrypt them.
CHROMIUM_BROWSERS = frozenset(
    {"chrome", "edge", "brave", "chromium", "vivaldi", "opera"}
)


@dataclass
class CookieUpdateResult:
    """Outcome of :func:`update_cookies`.

    Attributes:
        success: ``True`` iff a non-empty cookies file was written.
        error_kind: One of ``"admin_context"``, ``"browser_locked"``,
            ``"browser_not_found"``, ``"yt_dlp_missing"``, ``"other"``, or
            ``None`` on success.
        message: Human-readable message (may contain Korean UI text) that
            explains the result and includes remediation hints.
        file_size: Size in bytes of the written cookie file, or ``None`` on
            failure.
    """

    success: bool
    error_kind: Optional[str]
    message: str
    file_size: Optional[int]


def bundled_runtime_dir() -> Optional[Path]:
    """Return the directory containing PyInstaller-bundled executables.

    In a frozen app, yt-dlp.exe and deno.exe (both bundled by build.ps1)
    live in ``sys._MEIPASS`` (the support folder). Callers should prepend
    this to child-process ``PATH`` so tools like yt-dlp can spawn deno
    without relying on the user having deno installed system-wide.

    Returns ``None`` when not running inside a frozen app.
    """
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(sys.executable).resolve().parent


def yt_dlp_env(overrides: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Environment for a yt-dlp child process.

    * Asks the child for UTF-8 stdio. The standalone yt-dlp.exe ignores
      this on Korean Windows (see ``core.downloader._decode_yt_dlp_line``),
      but Python-based invocations honour it.
    * In a frozen app, prepends :func:`bundled_runtime_dir` to ``PATH`` so
      yt-dlp finds the bundled deno.exe (JS challenge resolver) and
      ffmpeg without a system-wide install.
    * Applies *overrides* last (e.g. :meth:`Authenticator.env_overrides`).
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    bundle_dir = bundled_runtime_dir()
    if bundle_dir is not None:
        env["PATH"] = f"{bundle_dir}{os.pathsep}{env.get('PATH', '')}"
    if overrides:
        env.update(overrides)
    return env


def no_window_kwargs() -> dict[str, Any]:
    """``subprocess`` kwargs that stop a console window flashing up when
    the parent is a windowed (console=False) PyInstaller build."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def yt_dlp_command() -> list[str]:
    """Return the argv prefix to invoke yt-dlp.

    Resolution order:
      1. When frozen (PyInstaller), prefer the bundled ``yt-dlp.exe`` placed
         next to the app binary or inside ``sys._MEIPASS``. ``sys.executable``
         in a frozen build is the host app itself, so the ``-m yt_dlp``
         fallback would pass unknown args to the host's argparse.
      2. Standalone ``yt-dlp`` / ``yt-dlp.exe`` on PATH.
      3. ``<python> -m yt_dlp`` for normal Python environments.
    """
    if getattr(sys, "frozen", False):
        exe_name = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
        candidates: list[Path] = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / exe_name)
        app_dir = Path(sys.executable).resolve().parent
        candidates.append(app_dir / exe_name)
        candidates.append(app_dir / "_internal" / exe_name)
        for candidate in candidates:
            if candidate.is_file():
                return [str(candidate)]
        # Frozen but no bundled yt-dlp: fall through to PATH lookup only.
        binary = shutil.which("yt-dlp")
        if binary:
            return [binary]
        raise FileNotFoundError(
            "yt-dlp 실행 파일을 찾을 수 없습니다. 설치 상태를 확인하거나 "
            "yt-dlp.exe 를 앱 폴더에 두세요."
        )

    binary = shutil.which("yt-dlp")
    if binary:
        return [binary]
    return [sys.executable, "-m", "yt_dlp"]


def is_admin_on_windows() -> bool:
    """Return ``True`` iff the current process is elevated on Windows.

    Always returns ``False`` on non-Windows platforms. Any probing failure
    is treated as not-admin, since false negatives are strictly safer than
    false positives here.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive
        logger.debug("IsUserAnAdmin probe failed", exc_info=True)
        return False


def _classify_error(stderr: str, stdout: str) -> str:
    """Bucket yt-dlp failure output into one of the known error kinds."""
    combined = f"{stderr}\n{stdout}".lower()

    # yt-dlp missing / wrong invocation
    if "no module named" in combined and "yt_dlp" in combined:
        return "yt_dlp_missing"

    # Database locked / in-use (Chrome/Edge/Firefox keep their cookie DB open)
    locked_markers = (
        "could not copy chrome cookie database",
        "could not open database",
        "database is locked",
        "unable to open database file",
        "sqlite",  # broad catch, typically indicates DB access trouble
    )
    if any(marker in combined for marker in locked_markers):
        # Only treat generic "sqlite" match as locked if paired with another
        # hint, to avoid over-classification.
        if any(m in combined for m in locked_markers[:-1]) or "locked" in combined:
            return "browser_locked"

    # Profile not found
    not_found_markers = (
        "could not find",
        "no such profile",
        "profile not found",
        "unsupported browser",
    )
    if any(marker in combined for marker in not_found_markers):
        return "browser_not_found"

    return "other"


def update_cookies(
    browser: str,
    output_file: PathLike,
    profile: Optional[str] = None,
) -> CookieUpdateResult:
    """Extract cookies from *browser* into a Netscape-format file.

    This is the canonical, bug-fixed cookie-refresh routine. It invokes
    yt-dlp in a mode that both **loads** cookies from the browser's native
    store and **writes** them out to a Netscape cookies.txt file, by
    hitting the sentinel YouTube URL with ``--skip-download --simulate``.

    Args:
        browser: Browser identifier understood by yt-dlp
            (``chrome`` / ``firefox`` / ``edge`` / ``brave`` / ``safari`` ...).
        output_file: Destination path for the Netscape cookies.txt.
        profile: Optional browser profile name, appended as
            ``browser:profile`` per yt-dlp's ``--cookies-from-browser``
            syntax.

    Returns:
        :class:`CookieUpdateResult` describing success or the classified
        failure.
    """
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-flight: on Windows, Chromium-family browsers cannot be decrypted
    # from an elevated process because DPAPI is scoped to the
    # non-elevated user. Reject fast with an actionable message.
    if is_admin_on_windows() and browser.lower() in CHROMIUM_BROWSERS:
        return CookieUpdateResult(
            success=False,
            error_kind="admin_context",
            message=(
                "관리자 권한으로 실행 중입니다. Chrome/Edge 계열 브라우저는 "
                "DPAPI 제약으로 관리자 모드에서 쿠키를 복호화할 수 없습니다. "
                "관리자가 아닌 일반 PowerShell/CMD 창에서 다시 실행해주세요."
            ),
            file_size=None,
        )

    # Check yt-dlp availability early so we can classify cleanly.
    cmd_prefix = yt_dlp_command()
    if cmd_prefix[0] != sys.executable and shutil.which(cmd_prefix[0]) is None:
        # Only happens if yt-dlp binary got removed between which() and now.
        return CookieUpdateResult(
            success=False,
            error_kind="yt_dlp_missing",
            message="yt-dlp 실행 파일을 찾을 수 없습니다. 'pip install yt-dlp'로 설치해주세요.",
            file_size=None,
        )

    browser_spec = f"{browser}:{profile}" if profile else browser

    # NOTE: `--quiet` is intentionally omitted. It suppresses the very error
    # messages we need to classify failures. `--skip-download --simulate`
    # still makes yt-dlp do a cheap metadata fetch that triggers cookie
    # round-trips so the file actually gets written.
    cmd = cmd_prefix + [
        "--cookies-from-browser", browser_spec,
        "--cookies", str(output_path),
        "--skip-download",
        "--simulate",
        "--no-warnings",
        _COOKIE_TRIGGER_URL,
    ]

    logger.debug("update_cookies cmd: %s", cmd)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=yt_dlp_env(),
            **no_window_kwargs(),
        )
    except FileNotFoundError:
        return CookieUpdateResult(
            success=False,
            error_kind="yt_dlp_missing",
            message="yt-dlp 실행 파일을 찾을 수 없습니다. 'pip install yt-dlp'로 설치해주세요.",
            file_size=None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("update_cookies subprocess failed")
        return CookieUpdateResult(
            success=False,
            error_kind="other",
            message=f"쿠키 업데이트 중 예외 발생: {exc}",
            file_size=None,
        )

    if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
        size = output_path.stat().st_size
        return CookieUpdateResult(
            success=True,
            error_kind=None,
            message=f"쿠키 업데이트 완료: {output_path} ({size} bytes)",
            file_size=size,
        )

    # Failure path: classify.
    error_kind = _classify_error(result.stderr or "", result.stdout or "")

    hint_map = {
        "browser_locked": (
            "브라우저가 실행 중이라 쿠키 DB에 접근할 수 없습니다. "
            f"{browser}를 완전히 종료한 뒤 다시 시도해주세요."
        ),
        "browser_not_found": (
            f"{browser} 프로필을 찾지 못했습니다. 브라우저가 설치되어 있고 "
            "프로필명이 올바른지 확인해주세요."
        ),
        "yt_dlp_missing": (
            "yt-dlp 모듈을 찾을 수 없습니다. 'pip install yt-dlp'로 설치해주세요."
        ),
        "other": "쿠키 업데이트 실패.",
    }

    stderr_tail = (result.stderr or "").strip().splitlines()
    stdout_tail = (result.stdout or "").strip().splitlines()
    diagnostic_lines: list[str] = []
    if stderr_tail:
        diagnostic_lines.extend(stderr_tail[-10:])
    elif stdout_tail:
        diagnostic_lines.append("(stderr 비어있음 - stdout 마지막 줄:)")
        diagnostic_lines.extend(stdout_tail[-5:])
    diagnostic_lines.append(f"returncode={result.returncode}")
    if output_path.exists():
        diagnostic_lines.append(
            f"cookies.txt 기록됨 ({output_path.stat().st_size} bytes) 이지만 "
            "반환 코드가 0이 아님"
        )
    else:
        diagnostic_lines.append("cookies.txt 파일이 생성되지 않음")
    diagnostic_lines.append("실행 명령: " + " ".join(cmd))

    message = hint_map.get(error_kind, hint_map["other"])
    message = f"{message}\n---\n" + "\n".join(diagnostic_lines)

    return CookieUpdateResult(
        success=False,
        error_kind=error_kind,
        message=message,
        file_size=None,
    )


def load_cookies(file: PathLike) -> Path:
    """Validate a cookies file and return it as a :class:`Path`.

    Args:
        file: Path to a Netscape-format cookies.txt.

    Returns:
        The resolved :class:`Path`.

    Raises:
        FileNotFoundError: The file does not exist.
        ValueError: The file exists but is empty.
    """
    path = Path(file)
    if not path.exists():
        raise FileNotFoundError(f"Cookie file not found: {path}")
    if not path.is_file():
        raise ValueError(f"Cookie path is not a regular file: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"Cookie file is empty: {path}")
    return path


def platform_label() -> str:
    """Short platform descriptor useful for diagnostics/logging."""
    return f"{platform.system()} {platform.release()}"
