"""Live stream detection and option assembly for yt-dlp.

Uses a lightweight yt-dlp --dump-json probe to classify a URL before
downloading, so the main pipeline can auto-apply ``--live-from-start``
and catchup flags only when they make sense.

Key behaviour when the user pastes a URL for a broadcast that is already
in progress:

1. :func:`detect_live_status` is called (cheap, metadata-only) and
   returns ``live_status="is_live"``.
2. The caller sets :class:`LiveOptions._detected_status` accordingly.
3. :attr:`LiveOptions.effective` evaluates True, so the downloader
   appends ``--live-from-start --concurrent-fragments N
   --hls-use-mpegts --fragment-retries R`` (N defaults to 1; see
   :attr:`LiveOptions.concurrent_fragments`, R to 10; see
   :attr:`LiveOptions.fragment_retries`). yt-dlp then locates
   the earliest segment inside YouTube's HLS DVR window (typically
   ~12 hours) and downloads forward, catching up to the live edge
   while continuing to record. A finite
   ``--fragment-retries`` (the default was ``infinite``) lets the
   process exit naturally when the broadcast ends; ``infinite`` is a
   choice that leans on the stall watchdog (``stall_minutes``).

Safe fallback: any probe failure (timeout, JSON parse error, non-zero
exit, missing binary) returns a ``"not_live"`` :class:`LiveStatusInfo`
so the caller falls back to the standard download path without
raising.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional

from core.auth import Authenticator, NoAuth
from core.cookies import no_window_kwargs, yt_dlp_command, yt_dlp_env

__all__ = [
    "DEFAULT_FRAGMENT_RETRIES",
    "DEFAULT_STALL_MINUTES",
    "LiveStatusInfo",
    "LiveOptions",
    "MAX_STALL_MINUTES",
    "detect_live_status",
    "fragment_retries_arg",
    "parse_fragment_retries",
    "parse_stall_minutes",
]

logger = logging.getLogger(__name__)


DEFAULT_FRAGMENT_RETRIES = 10
# Minutes without new bytes before a live recording is stopped (0 = never).
DEFAULT_STALL_MINUTES = 5
# What a person may type: a day. A larger number is a unit mix-up (seconds).
MAX_STALL_MINUTES = 24 * 60

_RETRIES_RE = re.compile(r"[0-9]+")  # not \d: that accepts other scripts' digits


def parse_fragment_retries(text: str) -> Optional[int]:
    """A retry count typed by a person (CLI argument, environment variable).

    ``"7"`` -> ``7``; ``"0"`` -> ``0`` (no retries); ``"infinite"`` -> ``None``
    (retry forever). Anything else - a negative or fractional number, a sign,
    another word, an empty string - raises ``ValueError``: a typo must not
    quietly become 0 or infinite.
    """
    value = str(text).strip()
    if value.lower() == "infinite":
        return None
    if _RETRIES_RE.fullmatch(value):
        return int(value)
    raise ValueError(
        f"재시도 횟수는 0 이상의 정수 또는 'infinite' 여야 합니다: {text!r}"
    )


def parse_stall_minutes(text: str) -> int:
    """The stall watchdog's minutes typed by a person (CLI argument,
    environment variable): ``"5"`` -> ``5``; ``"0"`` -> ``0`` (watchdog off).

    A negative or fractional number, a sign, a word, an empty string, or
    more than :data:`MAX_STALL_MINUTES` (a day: probably seconds) raises
    ``ValueError``: a typo must not quietly switch the watchdog off.
    """
    value = str(text).strip()
    if _RETRIES_RE.fullmatch(value) and int(value) <= MAX_STALL_MINUTES:
        return int(value)
    raise ValueError(
        f"자동 정지 시간은 0 이상 {MAX_STALL_MINUTES} 이하의 정수(분)여야 합니다 "
        f"(0 = 끔): {text!r}"
    )


def fragment_retries_arg(retries: Optional[int]) -> str:
    """The value for yt-dlp's ``--fragment-retries``: the number, or the
    keyword ``infinite`` for ``None``. (``0`` is a number, not infinite.)"""
    return "infinite" if retries is None else str(retries)


# Known live_status values emitted by yt-dlp 2024+. Anything else is
# treated as ``"not_live"`` for classification purposes.
_KNOWN_STATUSES = {"is_live", "is_upcoming", "was_live", "not_live", "post_live"}


@dataclass
class LiveStatusInfo:
    """Outcome of a pre-download yt-dlp metadata probe.

    Attributes:
        live_status: One of ``"is_live"``, ``"is_upcoming"``,
            ``"was_live"``, ``"not_live"``, ``"post_live"``. Unknown
            values from yt-dlp are normalised to ``"not_live"``.
        title: Video title (best-effort; may be empty on failure).
        scheduled_start_time: Scheduled start as a unix timestamp for
            ``is_upcoming`` broadcasts; ``None`` otherwise.
        release_timestamp: ``release_timestamp`` field from yt-dlp when
            present.
        duration: Duration in seconds for completed/VOD items.
        raw: Full ``--dump-json`` output dict for debugging.
        probed: ``False`` when the probe could not tell (yt-dlp failed, timed
            out or printed nothing readable). ``live_status`` is then only the
            safe ``"not_live"`` fallback, not something yt-dlp reported.
    """

    live_status: str
    title: str = ""
    scheduled_start_time: Optional[int] = None
    release_timestamp: Optional[int] = None
    duration: Optional[float] = None
    raw: dict = field(default_factory=dict)
    probed: bool = True

    @property
    def needs_catchup(self) -> bool:
        """True when the URL is an ongoing or upcoming live broadcast."""
        return self.live_status in ("is_live", "is_upcoming")


@dataclass
class LiveOptions:
    """User-facing live-stream tuning knobs.

    Attributes:
        enabled: Master auto-detect switch. When False,
            :func:`core.downloader.download` will skip the probe entirely
            and behave exactly like the pre-Phase-F version.
        concurrent_fragments: Value passed to ``--concurrent-fragments``
            during catchup. Default 1: every fragment is appended to the
            ``.part`` file as it arrives (about 4 fragments/s for 1080p
            video + audio, several times real time). Higher values catch
            up faster, but yt-dlp then keeps every fragment as a separate
            ``-FragN`` file and appends nothing until it decides the
            stream is over, so an interrupted recording leaves an empty
            ``.part`` file.
        wait_minutes: Maximum minutes to wait for an ``is_upcoming``
            broadcast to actually start, mapped onto yt-dlp's
            ``--wait-for-video min-max`` flag. ``0`` disables waiting.
        remux_to_mp4: When True, appends ``--remux-video mp4`` so the
            recorded TS segments are repackaged into an MP4 container
            after the stream ends. The TS file is kept intermediately
            so an interrupted recording is still playable.
        force: Apply live-flags even when detection says the URL is not
            live. Debug escape hatch.
        stall_minutes: Force-terminate yt-dlp if no fragment progress is
            observed for this many minutes (Phase F-2 safety net). Covers
            the case where DASH live broadcasts end without their
            manifest transitioning ``@type`` from ``dynamic`` to
            ``static``, leaving yt-dlp in a manifest-poll + HTTPS-timeout
            loop indefinitely. ``--fragment-retries`` does not break this
            cycle because it only applies to HLS fragment-level retries.
            ``0`` disables the watchdog. A negative or non-integer value
            raises ``ValueError``.
        fragment_retries: Value of ``--fragment-retries`` during a live
            recording: how often yt-dlp retries a fragment that does not
            arrive. ``0`` means no retries; ``None`` means ``infinite``.
            Finite (default 10) lets the process exit by itself when the
            broadcast ends; with ``infinite`` yt-dlp keeps asking for the
            fragment after the last one (2026-04-26, it never stopped), so
            only the watchdog (``stall_minutes``) can end the run. A
            negative or non-integer value raises ``ValueError``.
        _detected_status: Populated by
            :func:`core.downloader.download` right after the probe. Not
            intended to be set by callers.
    """

    enabled: bool = True
    concurrent_fragments: int = 1
    wait_minutes: int = 60
    remux_to_mp4: bool = True
    force: bool = False
    stall_minutes: int = DEFAULT_STALL_MINUTES
    fragment_retries: Optional[int] = DEFAULT_FRAGMENT_RETRIES
    _detected_status: Optional[str] = None

    def __post_init__(self) -> None:
        # Fail fast on a bad count rather than when yt-dlp rejects the flag.
        stall = self.stall_minutes
        if isinstance(stall, bool) or not isinstance(stall, int) or stall < 0:
            raise ValueError(f"stall_minutes must be a non-negative integer, not {stall!r}")
        retries = self.fragment_retries
        if retries is not None and (
            isinstance(retries, bool) or not isinstance(retries, int) or retries < 0
        ):
            raise ValueError(
                "fragment_retries must be a non-negative integer or None (infinite), "
                f"not {retries!r}"
            )

    @property
    def effective(self) -> bool:
        """Return True if live-specific yt-dlp flags should be added.

        Three cases:

        * Master switch off -> never effective.
        * Force flag set -> always effective (even for non-live URLs,
          useful for debug).
        * Otherwise effective iff the last probe classified the URL as
          ``is_live`` or ``is_upcoming``. ``was_live`` (VOD archive) and
          ``not_live`` use the standard download path.
        """
        if not self.enabled:
            return False
        if self.force:
            return True
        return self._detected_status in ("is_live", "is_upcoming")


# --- Probe -----------------------------------------------------------------


def _as_int(value: Any) -> Optional[int]:
    """Best-effort ``int`` coercion for unix-timestamp-ish JSON values."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise_status(raw_status: Any) -> str:
    """Map a free-form yt-dlp ``live_status`` value onto our known set."""
    if not isinstance(raw_status, str):
        return "not_live"
    status = raw_status.strip()
    if status in _KNOWN_STATUSES:
        return status
    # yt-dlp historically sometimes emits ``"live"`` instead of
    # ``"is_live"``. Keep the mapping lax so we don't misclassify.
    if status == "live":
        return "is_live"
    if status == "upcoming":
        return "is_upcoming"
    return "not_live"


def detect_live_status(
    url: str,
    auth: Optional[Authenticator] = None,
    timeout: float = 30.0,
) -> LiveStatusInfo:
    """Probe yt-dlp for URL metadata and classify it.

    Uses ``yt-dlp --dump-json --no-download --simulate --no-warnings``
    against *url* and inspects the ``live_status`` field. The probe
    reuses the existing :func:`core.cookies.yt_dlp_command` so it
    behaves consistently in frozen PyInstaller builds, and forwards the
    caller's authenticator so private/age-restricted URLs can be
    classified.

    On any failure (timeout, parse error, non-zero exit, missing
    binary) this function returns a ``"not_live"`` :class:`LiveStatusInfo`
    and logs the cause. It never raises for expected failure modes; the
    caller is expected to simply fall back to the non-live code path.

    Args:
        url: The YouTube URL to classify.
        auth: Optional authenticator. When ``None``, :class:`NoAuth`
            is used.
        timeout: Subprocess timeout in seconds.

    Returns:
        :class:`LiveStatusInfo`.
    """
    safe_fallback = LiveStatusInfo(live_status="not_live", probed=False)
    if not url:
        return safe_fallback

    active_auth: Authenticator = auth if auth is not None else NoAuth()

    try:
        cmd = yt_dlp_command()
    except FileNotFoundError as exc:
        logger.debug("detect_live_status: yt-dlp not found: %s", exc)
        return safe_fallback

    cmd = cmd + [
        "--dump-json",
        "--no-download",
        "--simulate",
        "--no-warnings",
    ]
    try:
        cmd.extend(active_auth.to_yt_dlp_flags())
    except Exception:  # pragma: no cover - defensive
        logger.debug("detect_live_status: auth flag assembly failed", exc_info=True)
    cmd.append(url)

    try:
        env_overrides = active_auth.env_overrides()
    except Exception:  # pragma: no cover - defensive
        env_overrides = {}

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            # Bundled PATH lets yt-dlp reach deno for JS challenges even
            # on this metadata-only probe.
            env=yt_dlp_env(env_overrides),
            timeout=timeout,
            **no_window_kwargs(),
        )
    except FileNotFoundError as exc:
        logger.debug("detect_live_status: subprocess spawn failed: %s", exc)
        return safe_fallback
    except subprocess.TimeoutExpired:
        logger.warning("detect_live_status: probe timed out after %ss", timeout)
        return safe_fallback
    except Exception:  # pragma: no cover - defensive
        logger.exception("detect_live_status: unexpected subprocess failure")
        return safe_fallback

    if proc.returncode != 0:
        # Non-zero is common for geo-blocked / private URLs. Log at
        # debug level and fall back safely.
        logger.debug(
            "detect_live_status: yt-dlp exit=%s stderr_tail=%r",
            proc.returncode,
            (proc.stderr or "").strip().splitlines()[-3:],
        )
        return safe_fallback

    stdout = proc.stdout or ""
    # --dump-json may emit multiple JSON documents for playlists (one per
    # entry). We take the first complete line that parses.
    parsed: Optional[dict] = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if parsed is None:
        logger.debug(
            "detect_live_status: no parseable JSON in stdout (%d bytes)", len(stdout)
        )
        return safe_fallback

    status = _normalise_status(parsed.get("live_status"))
    # Fallback heuristic: some extractors populate ``is_live`` boolean
    # but leave ``live_status`` missing. Honour that for safety.
    if status == "not_live" and parsed.get("is_live") is True:
        status = "is_live"

    return LiveStatusInfo(
        live_status=status,
        title=str(parsed.get("title") or ""),
        scheduled_start_time=_as_int(parsed.get("scheduled_start_time")),
        release_timestamp=_as_int(parsed.get("release_timestamp")),
        duration=_as_float(parsed.get("duration")),
        raw=parsed,
    )
