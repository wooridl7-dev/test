"""Pure download logic for yt-dlp.

No tkinter, no ``input()``, no direct ``print()``. Presentation is
delegated to callers via two optional hooks:

* ``progress_callback(info: dict)`` - called on every progress update
  parsed from yt-dlp output. See :meth:`ProgressDisplay.parse_progress`
  for the ``info`` keys; note ``percent_known`` is False for live
  streams, whose size is unknown.

* ``log_callback(line: str, level: str)`` - called for every non-progress
  line of yt-dlp output. ``level`` is one of ``"info"`` / ``"warning"`` /
  ``"error"``.

Both are optional; if omitted, output is swallowed (routed to the
``logging`` module at DEBUG).

Authentication is supplied via the :class:`core.auth.Authenticator`
protocol on :attr:`DownloadOptions.auth`. Legacy callers that still pass
``cookie_opts={...}`` are accommodated: the dict is auto-converted via
:func:`core.auth.authenticator_from_legacy_dict` so no existing call
site breaks.

The retry-on-bot-detection behaviour is preserved: if the first attempt
fails with a bot-challenge signature and no cookies were supplied, the
download is retried once with :class:`BrowserCookieAuth("chrome")`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Optional, Union

from core.auth import (
    Authenticator,
    BrowserCookieAuth,
    NoAuth,
    authenticator_from_legacy_dict,
)
from core.cookies import (
    bundled_runtime_dir,
    no_window_kwargs,
    yt_dlp_command,
    yt_dlp_env,
)
# NOTE: ``core.live`` is imported at module top; it only imports
# core.auth and core.cookies, so no circular dependency is introduced.
from core.cookie_health import CookieHealthStore, cookie_identity
from core.failure import (
    FailureKind,
    classify_failure,
    cookies_reported_invalid,
    counts_against_cookie_health,
    failure_hint,
    http_403_error,
    sidecar_unreachable,
    subtitle_download_failed,
)
from core.filename_limit import fit_template
from core.live import (
    DEFAULT_FRAGMENT_RETRIES,
    DEFAULT_STALL_MINUTES,
    LiveOptions,
    fragment_retries_arg,
)
from core.output_claims import OUTPUT_CLAIMS, reserve_output
from core.partial_merge import merge_partial_recording
from core.presets import normalize_audio_lang, prefer_audio_language
from core.run_log import RunLog, activate, current_run_log
from core.version import app_version

__all__ = [
    "ProgressDisplay",
    "DownloadOptions",
    "DownloadResult",
    "download",
    "run_download",
    "check_js_runtime",
]

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], None]
LogCallback = Callable[[str, str], None]
PathLike = Union[str, Path]


class ProgressDisplay:
    """Parses yt-dlp progress output and formats it for display.

    Output contract preserved from the original single-file version:
    a carriage-return-overwritten single line of the form::

        [████░░░░░░░░] 45.2% | 125.3MB | 2.2MB/s | ETA: 00:32

    In the refactored form this class only *parses* and *formats*; the
    actual emission is done by caller-supplied callbacks so that GUI and
    CLI can route the text differently.
    """

    # Passed to yt-dlp as ``--progress-template`` (see _build_cmd). Every
    # field is present while downloading, but any of them may read
    # ``N/A`` / ``Unknown``: live and fragment (HLS/DASH) downloads
    # usually know only ``total_bytes_estimate``, and live ETA is never
    # known. The downloaded-bytes field is what lets the stall watchdog
    # tell real progress apart from a stuck manifest-poll loop.
    TEMPLATE = (
        "[download] %(progress._percent_str)s of %(progress._total_bytes_str)s"
        " ~ %(progress._total_bytes_estimate_str)s"
        " got %(progress._downloaded_bytes_str)s"
        " at %(progress._speed_str)s ETA %(progress._eta_str)s"
    )

    # When several formats download concurrently (e.g. video + audio
    # under --live-from-start), yt-dlp prefixes each line with "N: ".
    _PROGRESS_RE = re.compile(
        r"(?:(?P<stream>\d+):\s+)?\[download\]\s+(?P<pct>\d+(?:\.\d+)?|N/?A)%"
        r"\s+of\s+(?P<total>.+?)\s+~\s+(?P<est>.+?)\s+got\s+(?P<dl>.+?)"
        r"\s+at\s+(?P<speed>.+?)\s+ETA\s+(?P<eta>\S+)"
    )
    # Loose detector for "this is a progress line" even when it does not
    # match the template (e.g. the "100% of X in 00:10" finished line).
    # Used to keep such lines out of the log.
    _PROGRESS_HINT_RE = re.compile(r"\[download\]\s+(?:\d+(?:\.\d+)?|N/?A)%\s+")

    # yt-dlp's format_bytes() emits binary units ("KiB", "MiB", ...).
    # "N/A", "NA" and "Unknown B/s" deliberately do not match.
    _SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGT]?)i?B", re.IGNORECASE)
    _UNIT_POWERS = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4}

    def __init__(self) -> None:
        self.last_progress: str = ""

    @staticmethod
    def format_bytes(bytes_value: float) -> str:
        """Convert a byte count to a human-readable string."""
        value = float(bytes_value)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024.0:
                return f"{value:.1f}{unit}"
            value /= 1024.0
        return f"{value:.1f}TB"

    @staticmethod
    def format_speed(speed_bps: float) -> str:
        """Convert bytes/sec to a human-readable speed string."""
        value = float(speed_bps)
        if value == 0:
            return "0B/s"
        for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
            if value < 1024.0:
                return f"{value:.1f}{unit}"
            value /= 1024.0
        return f"{value:.1f}TB/s"

    @staticmethod
    def create_progress_bar(
        percentage: float, width: int = 50, known: bool = True
    ) -> str:
        """Build a unicode progress bar string."""
        if not known:
            return "[" + "░" * width + "]   N/A"
        filled = int(width * percentage / 100)
        bar = "\u2588" * filled + "\u2591" * (width - filled)
        return f"[{bar}] {percentage:5.1f}%"

    @classmethod
    def _parse_bytes(cls, text: str) -> Optional[float]:
        """``"9.54MiB"`` -> bytes; ``None`` for N/A / Unknown."""
        match = cls._SIZE_RE.search(text)
        if not match:
            return None
        power = cls._UNIT_POWERS[match.group(2).lower()]
        return float(match.group(1)) * (1024 ** power)

    def is_progress_line(self, line: str) -> bool:
        """Return True if *line* looks like a yt-dlp progress update.

        Used to suppress un-parsed progress lines (e.g. the final
        ``100% of X in 00:10`` summary) from the regular log so they
        don't spam the GUI status pane.
        """
        return bool(self._PROGRESS_HINT_RE.search(line))

    def parse_progress(self, line: str) -> Optional[dict]:
        """Parse one line into a progress dict, or ``None`` if no match.

        Keys: ``percentage`` (float, 0.0 when unknown), ``percent_known``,
        ``total_size`` (``"~"``-prefixed when only estimated),
        ``downloaded`` / ``downloaded_bytes``, ``speed``, ``eta``,
        ``bar``, ``stream`` (0-based index when yt-dlp downloads several
        formats concurrently) and ``raw``.
        """
        match = self._PROGRESS_RE.search(line)
        if not match:
            return None

        total = self._parse_bytes(match.group("total"))
        estimate = self._parse_bytes(match.group("est"))

        # With neither size known (typical for live) yt-dlp prints a
        # meaningless "0.0%" rather than "N/A", so require a size too.
        pct_text = match.group("pct")
        percent_known = pct_text[0].isdigit() and bool(total or estimate)
        percentage = float(pct_text) if percent_known else 0.0
        if total:
            total_size = self.format_bytes(total)
        elif estimate:
            total_size = "~" + self.format_bytes(estimate)
        else:
            total_size = "N/A"

        downloaded_bytes = self._parse_bytes(match.group("dl"))
        speed = self._parse_bytes(match.group("speed"))
        eta = match.group("eta")
        if not re.fullmatch(r"\d+(?::\d{2})+", eta):
            eta = "--:--"  # "Unknown" (always, for live) / "NA"

        return {
            "percentage": percentage,
            "percent_known": percent_known,
            "total_size": total_size,
            "downloaded": (
                self.format_bytes(downloaded_bytes)
                if downloaded_bytes is not None
                else "N/A"
            ),
            "downloaded_bytes": downloaded_bytes,
            "speed": self.format_speed(speed) if speed is not None else "N/A",
            "eta": eta,
            "bar": self.create_progress_bar(percentage, known=percent_known),
            "stream": int(match.group("stream") or 1) - 1,
            "raw": line,
        }

    def format_line(self, info: Mapping[str, Any]) -> str:
        """Render the single-line progress string used by the CLI."""
        if not info.get("percent_known", True):
            return f"{info['bar']} | {info['downloaded']} \ubc1b\uc74c | {info['speed']}"
        return (
            f"{info['bar']} | {info['total_size']} | "
            f"{info['speed']} | ETA: {info['eta']}"
        )


@dataclass
class DownloadOptions:
    """Structured options bag for :func:`download`.

    Attributes:
        url: The target URL.
        format_opt: yt-dlp ``-f`` format selector.
        output_template: yt-dlp ``-o`` output template (may include a path).
        auth: :class:`core.auth.Authenticator` describing how to
            authenticate to YouTube. If ``None``, falls back to
            :class:`NoAuth`. Legacy callers may continue to pass
            ``cookie_opts`` instead — see below.
        cookie_opts: **Deprecated.** Legacy dict supported for backward
            compatibility only. If supplied *and* ``auth`` is ``None``,
            it is auto-converted via
            :func:`core.auth.authenticator_from_legacy_dict`. New code
            should set ``auth`` directly.
        subtitle_opts: dict with optional keys ``writesubtitles`` (bool),
            ``writeautomaticsub`` (bool), ``subtitleslangs`` (list[str]).
        audio_opts: dict with optional key ``format`` set to ``"mp3"`` or
            ``"m4a"`` to extract audio, or ``None`` to keep video.
        live_opts: Optional :class:`core.live.LiveOptions` bundle that
            enables YouTube live-stream auto-detection + catchup. When
            ``None`` (the default), the live-probe step is skipped and
            the downloader behaves exactly like the pre-Phase-F version.
        audio_lang: Preferred audio-track language (e.g. ``"ko"``) for
            videos that carry several (dubbed) audio tracks. Falls back
            to the normal choice when the video has no such track.
            ``None`` (the default) keeps yt-dlp's order: original track
            first. See :func:`core.presets.prefer_audio_language`.
        download_archive: Path of a yt-dlp ``--download-archive`` file:
            videos listed there are skipped, finished ones are added.
            Used by :mod:`core.batch` so a batch can be run again.
        cookie_health: Optional :class:`core.cookie_health.CookieHealthStore`.
            When set, a run that confirms its login cookie expired is
            recorded there and a new login that replaces an expired one
            clears the record (``DownloadResult.cookie_event`` says which).
            ``None`` (the default) records nothing and touches no file.
    """

    url: str
    format_opt: str
    output_template: str
    auth: Optional[Authenticator] = None
    cookie_opts: Mapping[str, Any] = field(default_factory=dict)
    subtitle_opts: Mapping[str, Any] = field(default_factory=dict)
    audio_opts: Optional[Mapping[str, Any]] = None
    live_opts: Optional[LiveOptions] = None
    audio_lang: Optional[str] = None
    download_archive: Optional[str] = None
    cookie_health: Optional[CookieHealthStore] = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        # Legacy shim: if caller passed a cookie_opts dict but no auth,
        # translate it on the caller's behalf. This preserves the
        # pre-refactor contract where GUI/CLI/tests all handed us a
        # dict.
        if self.auth is None and self.cookie_opts:
            self.auth = authenticator_from_legacy_dict(self.cookie_opts)
        if self.auth is None:
            self.auth = NoAuth()
        # Fail fast on a bad code rather than after the probe.
        self.audio_lang = normalize_audio_lang(self.audio_lang)


@dataclass
class DownloadResult:
    """Outcome of :func:`run_download`.

    Attributes:
        ok: True when yt-dlp finished successfully.
        cancelled: True when *cancel_event* stopped the download.
        title: Title of the finished video (the playlist's title when a
            playlist finished several), else the title reported by the
            live-status probe, else ``""``.
        files: One dict per finished video with the fields named in
            ``_RESULT_TEMPLATE`` (``id``, ``title``, ``filepath`` - the
            final path after merging / audio extraction - ``language``
            and ``playlist_title``; unknown fields are left out), without
            duplicate paths.
        log_path: The run's log file (see :mod:`core.run_log`); ``""``
            when logging was unavailable.
        skipped: Why nothing was downloaded although nothing failed:
            ``"live"`` (ongoing / upcoming broadcast with
            ``skip_live``), ``"archived"`` (already in
            ``download_archive``), ``"busy"`` (the same video is already
            being downloaded, or its output name cannot be told apart from
            a running download's; ``ok`` is False), else ``""``.
        warnings: Live URL refresh warnings suggesting an incomplete
            recording even when yt-dlp exits successfully. Check the
            recording and log; these do not change ``ok``.
        failure_kind: Why a failed run failed, a
            :class:`core.failure.FailureKind` value (``"other"`` when
            unknown); ``""`` when the run did not fail or was cancelled.
        cookie_event: What the run changed in the cookie health record
            (``DownloadOptions.cookie_health``): ``"invalid"`` when it
            confirmed the login expired for the first time, ``"recovered"``
            when a new login replaced an expired one; else ``""``. Meant for
            a notifier, which should speak only when this is set.
        subtitles_skipped: ``True`` when the video was downloaded but without
            the subtitles that were asked for: yt-dlp stopped at a subtitle
            it could not get (a transient HTTP 429 is the usual cause), so the
            video was fetched again without any. ``ok`` is True; the subtitle
            files are missing.
    """

    ok: bool
    cancelled: bool = False
    title: str = ""
    files: list[dict] = field(default_factory=list)
    log_path: str = ""
    skipped: str = ""
    warnings: list[str] = field(default_factory=list)
    failure_kind: str = ""
    cookie_event: str = ""
    subtitles_skipped: bool = False

    @property
    def subtitle_summary(self) -> str:
        """The video came without the subtitles asked for (see :attr:`subtitles_skipped`)."""
        return "⚠ 자막 없이 받음" if self.subtitles_skipped else ""

    @property
    def warning_summary(self) -> str:
        """A suspected incomplete live recording, not a confirmed failure."""
        return "⚠ 라이브 조기 종료 가능" if self.warnings else ""

    @property
    def cookie_summary(self) -> str:
        """What this run changed about the login cookies, for a status cell;
        ``""`` when it changed nothing (see :attr:`cookie_event`)."""
        if self.cookie_event == "invalid":
            return "⚠ 쿠키 만료 — 다시 추출 필요"
        if self.cookie_event == "recovered":
            return "새 쿠키 확인됨"
        return ""

    @property
    def audio_language(self) -> Optional[str]:
        """Audio language of the first finished file that reports one."""
        for item in self.files:
            if item.get("language"):
                return str(item["language"])
        return None


# Printed by yt-dlp on stdout whenever a video reaches its final path
# (after merging, audio extraction or remux): the marker, then a JSON
# object. The "j" conversion escapes non-ASCII, so the console code page
# cannot garble it. _run_once collects these lines instead of logging
# them. --print would turn on --quiet (hiding progress) without the
# --no-quiet that _build_cmd adds.
_RESULT_MARKER = "[ytdlp-gui-result] "
_RESULT_TEMPLATE = (
    "after_move:" + _RESULT_MARKER + "%(.{id,title,filepath,language,playlist_title})j"
)
# Live only: the final file name and the format(s) being downloaded,
# printed before the download starts, so a recording stopped early can be
# merged (core.partial_merge). The "[download] Destination:" line cannot
# be used: yt-dlp.exe prints it in the console code page, which loses
# characters such as ⧸ and ＂.
_TARGET_MARKER = "[ytdlp-gui-target] "
# yt-dlp's message for a video skipped because of --download-archive.
_ARCHIVED_TEXT = "has already been recorded in the archive"
_TARGET_TEMPLATE = "video:" + _TARGET_MARKER + "%(.{filename,format_id})j"


def _build_cmd(opts: DownloadOptions) -> list[str]:
    """Compose the yt-dlp argv for the given options."""
    live = bool(opts.live_opts and opts.live_opts.effective)
    cmd = list(yt_dlp_command())  # extended below; never mutate the caller's list
    cmd.extend([
        "--remote-components", "ejs:github",  # JS challenge resolver
        "-f", prefer_audio_language(opts.format_opt, opts.audio_lang),
        "-o", opts.output_template,
        # No --no-warnings: yt-dlp's warnings explain runs that end oddly (a
        # recording cut short when the stream turned members-only, ...).
        # _run_once writes them to the run log only; they never reach the
        # GUI log or the output that feeds bot detection.
        # Without --newline yt-dlp separates progress updates with a bare
        # "\r" when stdout is a pipe, so the line-based reader in
        # _run_once would see nothing until the file finished.
        "--newline",
        "--progress-template", ProgressDisplay.TEMPLATE,
        # 30 retries (default 10) lets us ride out short-lived CDN
        # throttling that typically clears within 30–60s instead of
        # giving up at 11% with the default.
        "--retries", "30",
        # One record per finished video for DownloadResult.files.
        "--print", _RESULT_TEMPLATE,
        "--no-quiet",
    ])
    # A broadcast that just ended and that YouTube is still processing
    # (post_live) is downloaded by yt-dlp as the same per-second fragments
    # as a live-from-start recording.
    post_live = bool(
        opts.live_opts
        and opts.live_opts.enabled
        and opts.live_opts._detected_status == "post_live"
    )
    if not live and not post_live:
        # HTTP chunking tuned for YouTube SABR throttling on VOD. 10MB
        # chunks (yt-dlp default) keep the request rate low — too many
        # small chunks (e.g. 1M, 293 reqs for a 1080p H.264 stream) were
        # observed to trigger anti-bot truncation faster than the default.
        # Not for live, nor for post_live: every fragment then took ~20 s
        # (the server holds the ranged request), 0.35 instead of 12
        # fragments/s in a 2026-09-28 test. A post_live download on
        # 2026-10-05 ran at one fragment per ~40 s (21 MB in 68 minutes).
        cmd.extend(["--http-chunk-size", "10M"])

    audio_extract = bool(opts.audio_opts)
    if not audio_extract:
        cmd.extend(["--merge-output-format", "mp4"])

    # Authentication: delegated entirely to the Authenticator.
    cmd.extend(opts.auth.to_yt_dlp_flags())

    # Live-stream catchup (Phase F). Applied only when the detector
    # (or explicit force flag) classifies the URL as an ongoing or
    # upcoming broadcast. ``--live-from-start`` tells yt-dlp to locate
    # the earliest segment inside YouTube's HLS DVR window (typically
    # ~12h) instead of joining at the live edge.
    #
    # ``--concurrent-fragments`` defaults to 1 (LiveOptions). Above 1,
    # yt-dlp (2026.08) feeds the endless live fragment list to
    # Executor.map, which reads the whole list before returning any
    # result: fragments pile up as -FragN files and nothing is appended
    # to the .part file until yt-dlp decides the stream is over. An
    # interrupted recording then has an empty .part. With 1, each
    # fragment is appended as soon as it arrives.
    #
    # ``--fragment-retries`` defaults to 10 (it was ``infinite``): when the
    # broadcast ends, yt-dlp would otherwise spin forever on the next
    # non-existent fragment, requiring a manual kill and hand-merge of
    # the partial .ts. 10 retries with default linear backoff covers
    # transient network blips during a live recording while still letting
    # the process exit on end-of-stream. LiveOptions.fragment_retries sets
    # it: 0 means no retries, and only None passes ``infinite`` (the
    # watchdog is then the way out).
    if live:
        lo = opts.live_opts
        cmd.extend([
            "--print", _TARGET_TEMPLATE,
            "--live-from-start",
            "--concurrent-fragments", str(lo.concurrent_fragments),
            "--hls-use-mpegts",
            "--fragment-retries", fragment_retries_arg(lo.fragment_retries),
        ])
        if lo.wait_minutes > 0:
            # yt-dlp's --wait-for-video expects "min-max" seconds.
            # 60s minimum keeps the first poll cheap; max is the
            # user-configured ceiling.
            cmd.extend(["--wait-for-video", f"60-{lo.wait_minutes * 60}"])
        if lo.remux_to_mp4:
            cmd.extend(["--remux-video", "mp4"])

    if opts.download_archive:
        cmd.extend(["--download-archive", opts.download_archive])

    sub = opts.subtitle_opts or {}
    if sub.get("writesubtitles"):
        cmd.append("--write-subs")
        if sub.get("subtitleslangs"):
            cmd.extend(["--sub-langs", ",".join(sub["subtitleslangs"])])
    if sub.get("writeautomaticsub"):
        cmd.append("--write-auto-subs")

    if audio_extract:
        assert opts.audio_opts is not None
        cmd.extend([
            "-x",
            "--audio-format", opts.audio_opts["format"],
            "--audio-quality", "192K",
        ])

    cmd.append(opts.url)
    return cmd


def _parse_result_line(line: str, marker: str = _RESULT_MARKER) -> Optional[dict]:
    """The JSON object after *marker* in *line*, or ``None`` if malformed."""
    try:
        value = json.loads(line[len(marker):])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _classify_line(line: str) -> str:
    lower = line.lower()
    if "error" in lower or "failed" in lower:
        return "error"
    if "warning" in lower:
        return "warning"
    return "info"


def _noop_log(_line: str, _level: str) -> None:
    logger.debug("yt-dlp: %s", _line)


def _noop_progress(_info: dict) -> None:
    pass


class _Output(list):
    """The yt-dlp lines kept from a run (a ``list[str]``). ``warnings``
    holds its distinct ``WARNING:`` lines, which are not in the list itself:
    ordinary warnings must not reach the bot-detection input or the UI.
    A plain list (a test double) has no ``warnings``."""

    MAX_WARNINGS = 100

    def __init__(self) -> None:
        super().__init__()
        self.warnings: list[str] = []
        #: The largest "got N bytes" figure any progress line of the run reported.
        self.received: float = 0.0

    def add_warning(self, line: str) -> None:
        if len(self.warnings) < self.MAX_WARNINGS and line not in self.warnings:
            self.warnings.append(line)

    def note_received(self, downloaded_bytes: Optional[float]) -> None:
        if downloaded_bytes is not None and downloaded_bytes > self.received:
            self.received = downloaded_bytes


# Python's own failure to start yt-dlp is not an "ERROR:" line: the traceback ends in
# "ModuleNotFoundError: No module named 'yt_dlp'" (a real run log, 2026-10-07) and
# a broken launcher says "Fatal error in launcher".
_STARTUP_FAILURES = ("ERROR:", "ModuleNotFoundError:", "ImportError:", "Fatal error in launcher")


def _failure_lines(output: list[str]) -> list[str]:
    """The lines that say why a run failed: its ERROR lines, the lines saying
    yt-dlp could not start, and the WARNINGs."""
    errors = [line.strip() for line in output if line.lstrip().startswith(_STARTUP_FAILURES)]
    return errors + list(getattr(output, "warnings", ()))


# An HTTP 403 on the first media request was tried again by hand with success in every real
# case with a later try (2026-10-07: 3 min 25 s to 48 min later), and was not a lasting
# state: other videos downloaded fine seconds before and after. How soon after is enough is
# NOT known - every success was minutes later - so this is a guess, and the run log says what
# the retry did, to find out. Seconds; tests set it to 0.
HTTP_403_RETRY_DELAY = 10.0

# "Right at the start": the real 403s came either before yt-dlp printed ``[download]
# Destination:`` (three runs: nothing received) or about two seconds after it, with only the
# first progress line ``0.0% ... got 1.0KB`` printed (two runs). yt-dlp prints progress
# sparsely, so that line says little: in the 20:10 run a ``.part`` of 10,006,282 bytes was on
# disk afterwards, and the next try resumed from it. This is therefore a limit on what the
# progress lines REPORT, not on the bytes on disk. A run whose reports went past it was well
# under way, which is not the case seen so far.
HTTP_403_RETRY_MAX_BYTES = 1 << 20


def _received_little(output: list[str]) -> bool:
    """True while the progress lines of the run reported less than
    :data:`HTTP_403_RETRY_MAX_BYTES` (their largest "got" figure; a run that resumed a big
    ``.part`` reports its size from the first line, so it does not count)."""
    return getattr(output, "received", 0.0) < HTTP_403_RETRY_MAX_BYTES


def _decode_yt_dlp_line(data: bytes) -> str:
    """Decode a single line read from the bundled yt-dlp.exe stdout.

    Despite the child env carrying ``PYTHONIOENCODING=utf-8`` and
    ``PYTHONUTF8=1``, the standalone yt-dlp.exe (PyInstaller-bundled,
    Windows-specific stdio wrappers in yt_dlp.compat) on Korean
    Windows still emits Korean filenames in cp949 / system MBCS rather
    than UTF-8. Reading the pipe in text mode with ``encoding='utf-8',
    errors='replace'`` turned every Korean character into ``\\ufffd``
    (visible as ``�``). Strategy:

    1. Try strict UTF-8 - works on Linux/macOS, and on Windows when the
       env vars do propagate or the line has no non-ASCII bytes.
    2. On Windows, fall back to ``mbcs`` (the active ANSI code page,
       cp949 on Korean Windows, cp1252 on Western, ...) so we recover
       the actual characters yt-dlp wrote.
    3. Last resort: UTF-8 with replacement so we never raise.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if sys.platform == "win32":
            try:
                return data.decode("mbcs")
            except UnicodeDecodeError:
                pass
        return data.decode("utf-8", errors="replace")


_LANG_NAMES = {"ko": "한국어", "en": "영어", "ja": "일본어"}

# yt-dlp's language_preference for the original audio track
# (ORIGINAL_LANG_VALUE in its YouTube extractor).
_ORIGINAL_LANG_PREFERENCE = 10


def _audio_track_note(info: Mapping[str, Any], lang: Optional[str]) -> Optional[str]:
    """One log line naming the audio track that will be used, or ``None``.

    *info* is the probe's ``--dump-json`` dict; *lang* the preferred
    language (``None`` = original track). Silent unless the video has
    at least two audio languages, i.e. almost always.
    """
    # Highest language_preference seen per language code, in first-seen order.
    preference: dict[str, int] = {}
    for fmt in info.get("formats") or []:
        code = fmt.get("language")
        if not code or fmt.get("vcodec") != "none" or fmt.get("acodec") in (None, "none"):
            continue
        pref = fmt.get("language_preference")
        pref = pref if isinstance(pref, int) else -1
        preference[code] = max(pref, preference.get(code, pref))
    if len(preference) < 2:
        return None

    original = next(
        (code for code, pref in preference.items() if pref == _ORIGINAL_LANG_PREFERENCE),
        None,
    )
    head = f"\U0001f50a 오디오 트랙 {len(preference)}개"
    original_label = f"원본({original})" if original else "원본"
    if not lang:
        return f"{head} — {original_label} 선택"
    name = _LANG_NAMES.get(lang, lang)
    # Same match as the -f filter (prefix, no audio description); like
    # yt-dlp's format sort, the highest language_preference wins, and on
    # a tie the later (better-sorted) format.
    matches = [c for c in preference if c.startswith(lang) and not c.endswith("-desc")]
    if not matches:
        return f"{head} — {name} 없음, {original_label}으로 받습니다"
    picked = max(reversed(matches), key=preference.__getitem__)
    return f"{head} — {name}({picked}) 선택"


def _log_live_recording_notes(live_opts: LiveOptions, lcb: LogCallback) -> None:
    """Tell the user where a live recording accumulates, and its limits."""
    lcb(
        "ℹ️ 녹화는 '<제목>.f<번호>.mp4.part' 파일(영상·오디오 각각)에 쌓이고, "
        "끝나면 하나의 .mp4 로 합쳐집니다. 쓰는 중에는 탐색기에서 0 KB 로 "
        "보일 수 있지만 실제로는 커지고 있습니다. 멤버십 전환 등으로 방송 "
        "정보를 다시 받지 못하면 그 시점까지만 녹화됩니다.",
        "info",
    )
    if live_opts.fragment_retries != DEFAULT_FRAGMENT_RETRIES:
        shown = (
            "무한" if live_opts.fragment_retries is None
            else f"{live_opts.fragment_retries}회"
        )
        lcb(f"ℹ️ 조각 재시도 횟수: {shown} (기본 {DEFAULT_FRAGMENT_RETRIES}회)", "info")
    if live_opts.stall_minutes != DEFAULT_STALL_MINUTES:
        shown = "꺼짐" if live_opts.stall_minutes <= 0 else f"{live_opts.stall_minutes}분"
        lcb(f"ℹ️ 진척 없음 자동 정지: {shown} (기본 {DEFAULT_STALL_MINUTES}분)", "info")
    if live_opts.fragment_retries is None and live_opts.stall_minutes <= 0:
        lcb(
            "⚠️ 재시도 무한 + 자동 정지 꺼짐: 방송이 끝난 뒤에도 없는 조각을 영원히 "
            "재시도해 멈추지 않을 수 있습니다 (2026-04-26 사고). 재시도 횟수를 정하거나 "
            "진척 없음 자동 정지(분)를 켜세요.",
            "warning",
        )
    if live_opts.concurrent_fragments > 1:
        lcb(
            f"⚠️ 병렬 {live_opts.concurrent_fragments}: 녹화가 끝날 때까지 조각이 "
            "'-FragN' 파일로만 쌓이고 .part 파일은 0 바이트로 남습니다. 중간에 "
            "멈추면 받은 조각을 직접 이어 붙여야 합니다 (기본값 1 권장).",
            "warning",
        )


def _maybe_warn_live_opts_unused(
    live_opts: Optional[LiveOptions],
    lcb: LogCallback,
) -> None:
    """Emit a one-line note when the user customised live tab options
    but the URL turned out to be archive/VOD, so those settings won't
    take effect. Silent when the user kept defaults.
    """
    if live_opts is None:
        return
    default = LiveOptions()
    customised = []
    if live_opts.concurrent_fragments != default.concurrent_fragments:
        customised.append(f"concurrent_fragments={live_opts.concurrent_fragments}")
    if live_opts.wait_minutes != default.wait_minutes:
        customised.append(f"wait_minutes={live_opts.wait_minutes}")
    if live_opts.remux_to_mp4 != default.remux_to_mp4:
        customised.append(f"remux_to_mp4={live_opts.remux_to_mp4}")
    if live_opts.fragment_retries != default.fragment_retries:
        customised.append(f"fragment_retries={live_opts.fragment_retries}")
    if live_opts.stall_minutes != default.stall_minutes:
        customised.append(f"stall_minutes={live_opts.stall_minutes}")
    if customised:
        lcb(
            "(라이브 옵션 ["
            + ", ".join(customised)
            + "] 은 archive/VOD 영상에는 적용되지 않습니다)",
            "info",
        )


def _kill_process_tree(process: subprocess.Popen) -> None:
    """Stop *process* together with its children. Never raises.

    ``Popen.terminate()`` alone is not enough on Windows: the official
    yt-dlp.exe is a PyInstaller onefile build whose bootloader runs the
    real interpreter as a child process, and yt-dlp itself may spawn
    ffmpeg. TerminateProcess on the top PID leaves those running, still
    holding the output files. ``taskkill /T`` takes the whole tree down.

    Elsewhere :func:`_run_once` starts yt-dlp in its own session, so its
    PID is also its process-group ID and ``killpg`` reaches ffmpeg and
    any other children: SIGTERM first, SIGKILL if still alive after 5 s.
    """
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                **no_window_kwargs(),
            )
        except Exception:
            logger.debug("taskkill failed", exc_info=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            # Not a group leader (started without start_new_session) or
            # already gone; fall through to a plain terminate().
            logger.debug("killpg failed", exc_info=True)
    if process.poll() is None:
        try:
            process.terminate()
        except Exception:
            pass


class StallWatchdog:
    """Force-terminate a stalled yt-dlp subprocess.

    Phase F-2 safety net. DASH live broadcasts occasionally end without
    transitioning their manifest ``@type`` from ``dynamic`` to ``static``;
    yt-dlp then loops between manifest re-fetches and HTTPS read-timeouts
    indefinitely (observed 1.5h+ on 2026-05-13). ``--fragment-retries``
    is a finite count but only applies to HLS fragment-level retries, not
    DASH's manifest-poll loop, so it does not break the cycle.

    Strategy: a daemon thread monitors a monotonic "last progress" clock
    that the stdout loop bumps whenever a parsed progress line reports a
    new downloaded-bytes value (a stuck manifest-poll loop keeps printing
    but never grows the byte count). If the clock ages past
    ``threshold_s`` while the subprocess is still alive, kill the
    process tree once. ``fired`` is read by the main thread after
    ``stop()`` joins, so the user gets a clear post-exit log line.

    Constructed clock starts at ``__init__`` time — i.e. at Popen — so
    the threshold also covers the initial connect / first-fragment grace
    period. yt-dlp emits no parseable progress until bytes actually flow,
    which means a slow-but-progressing start consumes the grace but a
    truly stuck connect ages out.
    """

    def __init__(
        self,
        process: subprocess.Popen,
        threshold_s: float,
        log_callback: LogCallback,
    ) -> None:
        self.process = process
        self.threshold_s = threshold_s
        self.log = log_callback
        self._last_progress = time.monotonic()
        self._stop = threading.Event()
        self._fired = False
        self._thread: Optional[threading.Thread] = None

    @property
    def fired(self) -> bool:
        """True iff this watchdog issued the terminate() call."""
        return self._fired

    def notify_progress(self) -> None:
        """Bump the activity clock. Called from the stdout loop on each
        parsed progress hit. Single-assignment, no lock — atomic on
        CPython under the GIL."""
        self._last_progress = time.monotonic()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="StallWatchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the loop to exit; join briefly. Idempotent."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        # Poll every 10 s. _stop.wait returns True if stop() was called,
        # in which case we exit cleanly without firing.
        while not self._stop.wait(timeout=10):
            idle = time.monotonic() - self._last_progress
            if idle <= self.threshold_s:
                continue
            # Suppress double-fire if the process already exited under us.
            if self.process.poll() is not None or self._fired:
                return
            self._fired = True
            minutes = int(self.threshold_s // 60) or 1
            self.log(
                f"⚠️ {minutes}분간 진척 없음 — 라이브 종료 추정, "
                "yt-dlp 종료 신호 전송 (Phase F-2 워치독)",
                "warning",
            )
            _kill_process_tree(self.process)
            return


# yt-dlp exits with 1 although it merged and moved the file: at the end of a
# live recording the last fragment requests come back empty, which yt-dlp
# reports as an ERROR ("Did not get any data blocks"), skips the fragment
# and carries on. 2026-10-05: a 1:13:59 recording of a 1:14:00 broadcast was
# filed as "failed" because of it.
_EMPTY_FRAGMENT_ERROR = "ERROR: Did not get any data blocks"
_SKIPPED_FRAGMENT = re.compile(r"Skipping fragment (\d+)")


def _skipped_fragments(
    captured: list[str], records: list[dict], targets: list[dict]
) -> Optional[list[int]]:
    """The fragment numbers yt-dlp skipped, when those empty fragments are
    the *only* reason for its non-zero exit code and every video it set out
    to download was finished; ``None`` for any real failure."""
    errors = [line.strip() for line in captured if line.startswith("ERROR:")]
    if not errors or any(error != _EMPTY_FRAGMENT_ERROR for error in errors):
        return None
    skipped = sorted({
        int(match.group(1))
        for line in captured
        if (match := _SKIPPED_FRAGMENT.search(line))
    })
    if not skipped or not records:
        return None

    def same(path: object) -> str:
        return os.path.normcase(os.path.normpath(str(path)))

    finished = {same(record.get("filepath")) for record in records}
    if any(same(target.get("filename")) not in finished for target in targets):
        return None
    return skipped


def _describe_run_environment(cmd: list[str]) -> list[str]:
    """Run-log lines saying where yt-dlp runs and which cookie file it reads
    (``--cookies cookies.txt`` is relative to the working directory, so the
    name alone does not tell). The file's contents are never read."""
    notes = [f"working directory: {os.getcwd()}"]
    if "--cookies" in cmd[:-1]:
        cookie = Path(cmd[cmd.index("--cookies") + 1])
        try:
            resolved = cookie.resolve()
            info = resolved.stat()
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.st_mtime))
            notes.append(f"cookie file: {resolved} ({info.st_size} bytes, modified {when})")
        except OSError:
            notes.append(f"cookie file: {cookie} (not found from {os.getcwd()})")
    return notes


class _RunOutcome(NamedTuple):
    success: bool
    captured: list[str]  # yt-dlp output lines
    records: list[dict]  # one per finished video (_RESULT_TEMPLATE)
    targets: list[dict]  # filename + format_id announced (_TARGET_TEMPLATE, live only)


def _run_once(
    opts: DownloadOptions,
    progress_callback: ProgressCallback,
    log_callback: LogCallback,
    cancel_event: Optional[threading.Event] = None,
) -> _RunOutcome:
    """Run yt-dlp once. Returns a :class:`_RunOutcome`.

    The ``_RESULT_MARKER`` / ``_TARGET_MARKER`` lines go to ``records`` /
    ``targets`` and are neither logged nor captured.

    Setting *cancel_event* kills the yt-dlp process tree; the run then
    fails without logging a failure. If yt-dlp had already exited
    successfully, the run still counts as a success.
    """
    try:
        # yt_dlp_command() raises when a frozen build has no yt-dlp.exe.
        cmd = _build_cmd(opts)
    except FileNotFoundError as exc:
        log_callback(f"yt-dlp not found: {exc}", "error")
        return _RunOutcome(False, [], [], [])
    progress = ProgressDisplay()
    captured = _Output()
    records: list[dict] = []
    targets: list[str] = []

    # Authenticator-supplied env vars (e.g. the bgutil provider URL) ride
    # on top of the common yt-dlp child environment. The standalone
    # yt-dlp.exe ignores the UTF-8 request in that environment on Korean
    # Windows, hence the per-line fallback in _decode_yt_dlp_line.
    env_overrides = opts.auth.env_overrides()
    bundle_dir = bundled_runtime_dir()  # for the log line only

    run_log = current_run_log()
    for note in _describe_run_environment(cmd):
        run_log.write(note, "info")
    log_callback(f"yt-dlp command: {' '.join(cmd)}", "info")
    if bundle_dir is not None:
        log_callback(f"번들 경로 PATH 주입: {bundle_dir}", "info")
    if env_overrides:
        override_preview = ", ".join(f"{k}={v}" for k, v in env_overrides.items())
        log_callback(f"환경변수 추가: {override_preview}", "info")

    try:
        # Capture stdout as raw bytes so we can decode each line with a
        # UTF-8 -> MBCS fallback (see _decode_yt_dlp_line). Forcing
        # text=True with encoding="utf-8" here would silently mangle
        # cp949-emitted Korean filenames into U+FFFD mojibake.
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=yt_dlp_env(env_overrides),
            # Own process group on POSIX so _kill_process_tree can killpg
            # yt-dlp together with ffmpeg. (Ctrl+C then no longer reaches
            # the child directly; the KeyboardInterrupt handler kills it.)
            start_new_session=sys.platform != "win32",
            **no_window_kwargs(),
        )
    except FileNotFoundError as exc:
        log_callback(f"yt-dlp not found: {exc}", "error")
        return _RunOutcome(False, [], [], [])

    # Phase F-2: arm the stall watchdog for live downloads. VOD downloads
    # rely on --retries 30 + bounded byte counts; the watchdog would only
    # add overhead. Gated by stall_minutes > 0 so users can disable.
    watchdog: Optional[StallWatchdog] = None
    if (
        opts.live_opts is not None
        and opts.live_opts.effective
        and opts.live_opts.stall_minutes > 0
    ):
        watchdog = StallWatchdog(
            process,
            opts.live_opts.stall_minutes * 60.0,
            log_callback,
        )
        watchdog.start()

    # Cancellation: the stdout loop below blocks on readline, so a
    # separate thread waits for the event and kills the process tree,
    # which closes the pipe and ends the loop.
    stop_canceller = threading.Event()
    if cancel_event is not None:
        def _canceller() -> None:
            while not stop_canceller.is_set():
                if cancel_event.wait(timeout=0.5):
                    _kill_process_tree(process)
                    return

        threading.Thread(
            target=_canceller, name="DownloadCanceller", daemon=True
        ).start()

    # Last downloaded-bytes value per concurrent stream; the watchdog is
    # only fed when this grows.
    last_downloaded: dict[int, Optional[float]] = {}
    in_warning = False

    try:
        assert process.stdout is not None
        for raw in process.stdout:
            if not raw:
                continue
            line = _decode_yt_dlp_line(raw).rstrip("\r\n")
            if not line:
                continue
            if line.startswith(_RESULT_MARKER):
                run_log.write(line, "info")
                record = _parse_result_line(line)
                if record is not None:
                    records.append(record)
                continue
            if line.startswith(_TARGET_MARKER):
                run_log.write(line, "info")
                target = _parse_result_line(line, _TARGET_MARKER)
                if target and isinstance(target.get("filename"), str):
                    targets.append(target)
                continue
            if line.startswith("WARNING:") or (in_warning and line[:1] in (" ", "\t")):
                # Ordinary warnings stay in the file. Live refresh failures
                # also reach the UI, but never the bot-detection input.
                # A multi-line warning's follow-up lines are indented.
                in_warning = True
                run_log.write(line, "warning")
                if line.startswith("WARNING:"):
                    captured.add_warning(line.strip())
                if (opts.live_opts is not None and opts.live_opts.effective
                        and _is_live_end_warning(line)):
                    log_callback(line, "warning")
                continue
            in_warning = False
            captured.append(line)
            info = progress.parse_progress(line)
            if info is not None:
                progress_callback(info)
                downloaded = info["downloaded_bytes"]
                captured.note_received(downloaded)
                if watchdog is not None and (
                    downloaded is None
                    or downloaded != last_downloaded.get(info["stream"])
                ):
                    last_downloaded[info["stream"]] = downloaded
                    watchdog.notify_progress()
            elif progress.is_progress_line(line):
                # Progress-ish line outside the template (e.g. the final
                # "100% of X in 00:10" summary). Drop silently - the
                # progress bar already reflects it.
                pass
            else:
                log_callback(line, _classify_line(line))
        process.wait()
        if process.returncode == 0:
            run_log.write("yt-dlp exited with code 0", "result")
        # A cancel that lands after yt-dlp exited 0 is too late to matter.
        if cancel_event is not None and cancel_event.is_set() and process.returncode != 0:
            return _RunOutcome(False, captured, records, targets)
        if watchdog is not None and watchdog.fired:
            log_callback(
                f"⚠️ 라이브 스트림이 {opts.live_opts.stall_minutes}분간 진척 없음 — "
                "워치독이 종료시켰습니다. 방송 종료로 추정됩니다.",
                "warning",
            )
        if process.returncode == 1 and not (watchdog is not None and watchdog.fired):
            skipped = _skipped_fragments(captured, records, targets)
            if skipped is not None:
                shown = ", ".join(str(n) for n in skipped[:6])
                if len(skipped) > 6:
                    shown += f" 외 {len(skipped) - 6}개"
                log_callback(
                    f"⚠️ yt-dlp 종료 코드 {process.returncode}: 조각 {shown} 번을 받지 못해 "
                    "건너뛰었지만 파일은 완성되었습니다 (방송이 끝날 때 흔한 현상). "
                    "정상 완료로 처리합니다.",
                    "warning",
                )
                run_log.write(
                    f"yt-dlp exited with code {process.returncode}; accepted, the only "
                    f"errors were skipped fragments {skipped}",
                    "result",
                )
                return _RunOutcome(True, captured, records, targets)
        if process.returncode != 0:
            log_callback(f"yt-dlp exit code: {process.returncode}", "error")
            if not captured:
                log_callback(
                    "yt-dlp 출력이 전혀 없습니다. 번들된 yt-dlp.exe 가 손상되었거나 "
                    "deno 런타임(JS challenge 해결용)이 PATH 에 없을 수 있습니다. "
                    "Windows PowerShell 에서 `irm https://deno.land/install.ps1 | iex` "
                    "로 deno 설치 후 재시도하세요.",
                    "error",
                )
    except KeyboardInterrupt:
        log_callback("사용자에 의해 다운로드가 중단되었습니다.", "error")
        _kill_process_tree(process)
        return _RunOutcome(False, captured, records, targets)
    finally:
        stop_canceller.set()
        if watchdog is not None:
            watchdog.stop()

    return _RunOutcome(process.returncode == 0, captured, records, targets)


def download(
    url: str,
    opts: Union[DownloadOptions, Mapping[str, Any]],
    *,
    progress_callback: Optional[ProgressCallback] = None,
    log_callback: Optional[LogCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> bool:
    """Download *url* with *opts*. Returns ``True`` on success.

    Same as :func:`run_download`, for callers that only need to know
    whether it worked.
    """
    return run_download(
        url,
        opts,
        progress_callback=progress_callback,
        log_callback=log_callback,
        cancel_event=cancel_event,
    ).ok


def _is_live_end_warning(line: str) -> bool:
    """Known yt-dlp live refresh failure ("Cannot find refreshed url for format").

    The wording comes from yt-dlp's source (youtube/_video.py); no real run log
    has had it yet. Its retry line ("... Retrying (n/3)...") and the final one
    ("... Giving up after 3 retries") both match. "Video is no longer live" is
    how a broadcast normally ends, and ordinary 403, authentication or extractor
    warnings are no evidence of an incomplete recording, so none of them count.
    """
    return "cannot find refreshed url for format" in line.lower()


def _reserve_output_name(
    opts: DownloadOptions,
    info: Mapping[str, Any],
    title: str,
    owner: object,
    lcb: LogCallback,
) -> tuple[DownloadOptions, Optional[DownloadResult]]:
    """Make sure no other running download writes the same output file.

    Returns the options to download with (the file name gets `` (<id>)`` when
    another running download already has the same one) and, instead of
    starting, a ``skipped="busy"`` result when this video is already being
    downloaded or the name cannot be told apart. See :mod:`core.output_claims`.
    """
    name = title or opts.url
    if not info:
        # Probe failed or switched off: the name is unknown, so a clash cannot be seen.
        if OUTPUT_CLAIMS.others_running(owner):
            lcb(
                "다른 다운로드가 진행 중인데 영상 정보를 읽지 못해 파일명이 겹치는지 확인하지 "
                "못했습니다. 제목이 같은 영상이면 파일명을 '제목(ID)'로 하세요.",
                "warning",
            )
        return opts, None
    reservation = reserve_output(OUTPUT_CLAIMS, owner, opts.output_template, info, opts.url)
    if reservation.busy is not None:
        if reservation.same_video:
            lcb(f"같은 영상을 이미 받는 중이라 시작하지 않습니다: {name}", "error")
        else:
            lcb(
                f"같은 파일명을 쓰는 다른 다운로드가 진행 중이고 구분할 방법이 없어 시작하지 "
                f"않습니다: {name}",
                "error",
            )
        return opts, DownloadResult(ok=False, title=title, skipped="busy")
    if reservation.renamed:
        lcb(
            f"같은 파일명을 쓰는 다른 다운로드가 진행 중이라 '제목 (ID)' 형식으로 저장합니다: {name}",
            "warning",
        )
        # The id made the name longer: fit it again.
        opts = dataclasses.replace(
            opts, output_template=fit_template(reservation.template, info).template
        )
    return opts, None


def run_download(
    url: str,
    opts: Union[DownloadOptions, Mapping[str, Any]],
    *,
    progress_callback: Optional[ProgressCallback] = None,
    log_callback: Optional[LogCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    skip_live: bool = False,
) -> DownloadResult:
    """Download *url* with *opts* and report what was saved.

    Every run is also written to its own log file (``result.log_path``,
    see :mod:`core.run_log`): all yt-dlp output, the messages sent to
    *log_callback* and how the run ended.

    Accepts either a :class:`DownloadOptions` instance or a dict with the
    same keys (for convenience when called from interfaces that already
    assemble dicts).

    The function itself is pure in the sense that it performs no stdio
    beyond the callbacks. A missing callback is silently swallowed
    (routed to ``logging`` at DEBUG level for the log_callback).

    On a yt-dlp "bot detection" failure *and* no cookies/PO-Token were
    configured, one retry is attempted with
    :class:`BrowserCookieAuth("chrome")`.

    Setting *cancel_event* (from any thread) kills the running yt-dlp
    process tree; the result then has ``cancelled`` set and no retry
    follows. Partially downloaded files (``.part`` / ``.ts``) are left
    in place.

    With *skip_live*, an ongoing or upcoming broadcast is not recorded:
    the result has ``skipped="live"``. This needs the live probe, so it
    has no effect without ``live_opts`` or with ``live_opts.force``.
    """
    run_log = RunLog.create(
        url or (opts.url if isinstance(opts, DownloadOptions) else str(dict(opts).get("url", ""))),
        header=[
            f"url: {url}",
            f"app version: {app_version()}",
            f"started: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        ],
    )
    warnings: list[str] = []
    sink = run_log.wrap(log_callback or _noop_log)

    def on_log(line: str, level: str) -> None:
        if level == "warning" and _is_live_end_warning(line):
            if line in warnings:
                return
            warnings.append(line)
        sink(line, level)

    # This run, for the output names other running downloads must not reuse.
    claim_owner = object()
    OUTPUT_CLAIMS.enter(claim_owner)
    try:
        with activate(run_log):
            result = _run_download(
                url,
                opts,
                progress_callback=run_log.wrap_progress(progress_callback or _noop_progress),
                log_callback=on_log,
                cancel_event=cancel_event,
                skip_live=skip_live,
                claim_owner=claim_owner,
            )
            result.warnings = warnings
            if warnings:
                sink(result.warning_summary + " — 녹화 길이와 실행 로그를 확인하세요.", "warning")
    except BaseException as exc:
        run_log.finish(f"aborted by {type(exc).__name__}: {exc}")
        try:
            exc.log_path = run_log.path_str  # for callers that show where it is
        except (AttributeError, TypeError):
            pass
        raise
    finally:
        OUTPUT_CLAIMS.leave(claim_owner)
    result.log_path = run_log.path_str
    if result.failure_kind:
        run_log.write(f"failure kind: {result.failure_kind}", "result")
    if result.cookie_event:
        run_log.write(f"cookie health: {result.cookie_event}", "result")
    if result.subtitles_skipped:
        run_log.write("subtitles skipped: the video was downloaded without them", "result")
    run_log.finish(
        f"result: ok={result.ok} cancelled={result.cancelled} "
        f"skipped={result.skipped or '-'} "
        f"files={[item.get('filepath') for item in result.files]}"
    )
    return result


def _run_download(
    url: str,
    opts: Union[DownloadOptions, Mapping[str, Any]],
    *,
    progress_callback: ProgressCallback,
    log_callback: LogCallback,
    cancel_event: Optional[threading.Event],
    skip_live: bool,
    claim_owner: Optional[object] = None,
) -> DownloadResult:
    """The body of :func:`run_download`; its callbacks already write to
    the run log. *claim_owner* is this run's key in ``OUTPUT_CLAIMS`` (``None``:
    do not look for output name clashes)."""
    if isinstance(opts, DownloadOptions):
        download_opts = opts
        if url and url != download_opts.url:
            # Allow caller to pass url separately; prefer explicit argument.
            download_opts = dataclasses.replace(opts, url=url)
    else:
        mapping = dict(opts)
        download_opts = DownloadOptions(
            url=url or mapping.get("url", ""),
            format_opt=mapping.get("format_opt", "bestvideo+bestaudio/best"),
            output_template=mapping.get("output_template") or mapping.get("output_path", ""),
            auth=mapping.get("auth"),
            cookie_opts=mapping.get("cookie_opts") or {},
            subtitle_opts=mapping.get("subtitle_opts") or {},
            audio_opts=mapping.get("audio_opts"),
            live_opts=mapping.get("live_opts"),
            audio_lang=mapping.get("audio_lang"),
            cookie_health=mapping.get("cookie_health"),
        )

    pcb = progress_callback
    lcb = log_callback
    probe_title = ""
    probe_info: Mapping[str, Any] = {}

    # Phase F: classify the URL as live / upcoming / archive / normal
    # before invoking yt-dlp for real, so we can print the right hint
    # and (more importantly) let ``_build_cmd`` decide whether to add
    # ``--live-from-start`` and companion flags. Any probe failure is
    # silently treated as "not live" so the non-live path still works.
    if (
        download_opts.live_opts
        and download_opts.live_opts.enabled
        and not download_opts.live_opts.force
    ):
        try:
            from core.live import detect_live_status

            status = detect_live_status(download_opts.url, download_opts.auth)
            download_opts.live_opts._detected_status = status.live_status
            probe_title = status.title
            probe_info = status.raw
            if skip_live and status.live_status in ("is_live", "is_upcoming"):
                kind = "방송 중인" if status.live_status == "is_live" else "예정된"
                lcb(f"\u23ed {kind} 라이브라 건너뜁니다: {probe_title or download_opts.url}",
                    "warning")
                return DownloadResult(ok=False, title=probe_title, skipped="live")
            if not status.probed:
                # The probe failed (a broken yt-dlp, a timeout, a private video ...): say so
                # rather than call it a normal video that yt-dlp reported.
                lcb(
                    "⚠️ 라이브 여부를 확인하지 못했습니다 — 일반 영상으로 받습니다. "
                    "방송 중인 라이브라면 제대로 받아지지 않을 수 있습니다.",
                    "warning",
                )
            elif status.live_status == "is_live":
                lcb(
                    f"\U0001f534 LIVE 감지 — 첫 부분부터 캐치업 시작 "
                    f"(병렬 {download_opts.live_opts.concurrent_fragments}, "
                    f"{'MP4 재포장' if download_opts.live_opts.remux_to_mp4 else 'TS 유지'})",
                    "info",
                )
            elif status.live_status == "is_upcoming":
                from datetime import datetime

                sched = status.scheduled_start_time
                sched_str = (
                    datetime.fromtimestamp(sched).strftime("%Y-%m-%d %H:%M")
                    if sched
                    else "미정"
                )
                lcb(
                    f"⏰ 예정 라이브 — 시작 예정 {sched_str}, 최대 "
                    f"{download_opts.live_opts.wait_minutes}분 대기",
                    "info",
                )
            elif status.live_status == "was_live":
                lcb("\U0001f4fc 아카이브 라이브 — 일반 다운로드 진행", "info")
                _maybe_warn_live_opts_unused(download_opts.live_opts, lcb)
            elif status.live_status == "post_live":
                lcb("⏳ 종료된 라이브 (YouTube 가 영상을 처리 중) — 처음부터 조각 단위로 받습니다", "info")
                _maybe_warn_live_opts_unused(download_opts.live_opts, lcb)
            else:
                lcb(f"\U0001f4fa 일반 영상 (live_status={status.live_status})", "info")
                _maybe_warn_live_opts_unused(download_opts.live_opts, lcb)
            if status.live_status in ("is_live", "is_upcoming"):
                _log_live_recording_notes(download_opts.live_opts, lcb)
            audio_note = _audio_track_note(status.raw, download_opts.audio_lang)
            if audio_note:
                lcb(audio_note, "info")
        except Exception as exc:
            lcb(f"라이브 감지 실패 (일반 다운로드로 진행): {exc}", "warning")

    # A name over the file system's limit makes yt-dlp fail with "File name too
    # long" (a 98-character Korean title is 248 of Linux's 255 bytes): bound the title.
    fitted = fit_template(download_opts.output_template, probe_info)
    if fitted.template != download_opts.output_template:
        download_opts = dataclasses.replace(download_opts, output_template=fitted.template)
    if fitted.cut:
        lcb(
            f"제목이 길어 파일명이 이 폴더의 한도({fitted.name_max}바이트)를 넘습니다. "
            f"제목을 {fitted.title_bound}바이트까지만 쓰고 줄여서 저장합니다: {probe_title}",
            "warning",
        )

    if claim_owner is not None:
        download_opts, refused = _reserve_output_name(
            download_opts, probe_info, probe_title, claim_owner, lcb
        )
        if refused is not None:
            return refused

    def _cancelled() -> bool:
        if cancel_event is not None and cancel_event.is_set():
            lcb("사용자에 의해 다운로드가 취소되었습니다.", "warning")
            return True
        return False

    health = download_opts.cookie_health
    # Which login the configured cookies hold, read now: yt-dlp rewrites
    # cookies.txt without the login once it sees the login expire. "" for
    # no cookies or a cookie file without a login, which cannot expire.
    cookie_id = cookie_identity(download_opts.auth)
    tracked_id = cookie_id if health is not None else ""
    recovered = [False]
    retried_403 = [False]  # the run was tried again by itself after an HTTP 403

    def _health(action: Callable[[], bool]) -> bool:
        """A store that cannot be read or written never fails a download."""
        try:
            return action()
        except OSError as exc:
            lcb(f"쿠키 상태를 기록하지 못했습니다: {exc}", "warning")
            return False

    def _mark_expired(result: DownloadResult) -> None:
        """Record a confirmed expiry; says so once, when it is new."""
        if health is not None and tracked_id and _health(
            lambda: health.mark_invalid(tracked_id, FailureKind.AUTH_EXPIRED.value)
        ):
            result.cookie_event = "invalid"
            lcb("쿠키 만료로 기록했습니다.", "warning")

    def _result(
        ok: bool, records: list[dict], cancelled: bool = False, skipped: str = ""
    ) -> DownloadResult:
        title = probe_title
        if records:
            first = records[0]
            # A playlist finishes several videos: name the playlist.
            title = (len(records) > 1 and first.get("playlist_title")) or first.get("title") or title
        return DownloadResult(
            ok=ok, cancelled=cancelled, title=str(title or ""), files=records, skipped=skipped,
            cookie_event="recovered" if recovered[0] else "",
        )

    def _archived(records: list[dict], captured: list[str]) -> str:
        """``"archived"`` when a successful run only skipped archived videos."""
        if not records and any(_ARCHIVED_TEXT in line for line in captured):
            return "archived"
        return ""

    def _stopped(records: list[dict], targets: list[dict], cancelled: bool = False) -> DownloadResult:
        """A run that did not finish. A live recording stopped early
        (취소 / 종료, Ctrl+C, watchdog) is merged from its .part files,
        since yt-dlp only merges a recording that ends by itself."""
        live_opts = download_opts.live_opts
        if live_opts is not None and live_opts.effective:
            unique = {(t["filename"], t.get("format_id")): None for t in targets}
            for filename, format_id in unique:
                merged = merge_partial_recording(filename, lcb, format_id)
                if merged:
                    records = records + [{
                        "title": probe_title or Path(filename).stem,
                        "filepath": merged,
                        "partial": True,
                    }]
        return _result(False, records, cancelled=cancelled)

    def _failed(
        result: DownloadResult, kind: FailureKind, output: list[str], *, retry: bool = False
    ) -> DownloadResult:
        """Record why the run failed and tell the user what to check.

        The automatic Chrome retry (*retry*) always uses cookies but is not the
        configured login, so it never touches the health record."""
        if result.cancelled:
            return result
        result.failure_kind = kind.value
        hint = failure_hint(
            kind,
            uses_cookies=retry or bool(cookie_id),
            sidecar_down=sidecar_unreachable(getattr(output, "warnings", ())),
            retried=retried_403[0],
        )
        if hint:
            lcb(hint, "warning")
        if not retry and counts_against_cookie_health(kind, uses_cookies=bool(tracked_id)):
            _mark_expired(result)
        return result

    if _cancelled():
        return _result(False, [], cancelled=True)
    if tracked_id and _health(lambda: health.sync(tracked_id)):
        recovered[0] = True
        lcb("새 쿠키가 확인되어 쿠키 만료 기록을 초기화했습니다.", "info")
    success, captured, records, targets = _run_once(download_opts, pcb, lcb, cancel_event)

    def _cancel_requested() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if (
        not success
        and not _cancel_requested()
        and not records
        and _received_little(captured)
        and http_403_error(_failure_lines(captured))
        and classify_failure(_failure_lines(captured)) is FailureKind.PO_TOKEN
    ):
        # A 403 right at the start (no PO Token provider here; seen five times in real runs,
        # each gone on a later try). Once, after a short wait, from scratch: yt-dlp resumes a
        # ``.part`` the first attempt left. The second attempt replaces the first as the run's
        # outcome, so everything below (success, cancel, the failure's hint) goes on as if it
        # were the only one.
        retried_403[0] = True
        lcb(
            f"HTTP 403 — 받기를 시작한 직후라 {HTTP_403_RETRY_DELAY:g}초 뒤 한 번 다시 시도합니다 "
            "(일시적인 403 은 다시 시도하면 풀린 적이 있습니다).",
            "warning",
        )
        if cancel_event is not None:
            cancel_event.wait(HTTP_403_RETRY_DELAY)
        else:
            time.sleep(HTTP_403_RETRY_DELAY)
        if not _cancel_requested():
            success, captured, records, targets = _run_once(download_opts, pcb, lcb, cancel_event)
            if success:
                lcb("재시도 성공! 처음의 HTTP 403 은 일시적이었습니다.", "info")
            elif not _cancel_requested():
                lcb("재시도도 실패했습니다.", "error")
    if success:
        skipped = _archived(records, captured)
        lcb("이미 받은 영상이라 건너뜀" if skipped else "다운로드 완료", "info")
        result = _result(True, records, skipped=skipped)
        # yt-dlp says so even when the download works (a public video needs no login).
        if cookies_reported_invalid(getattr(captured, "warnings", ())):
            _mark_expired(result)
        return result
    if _cancelled():
        return _stopped(records, targets, cancelled=True)

    kind = classify_failure(_failure_lines(captured))
    auth = download_opts.auth
    # "No auth in effect" for retry purposes means NoAuth. Any active
    # authenticator (including bgutil PO Token) is considered a real
    # attempt and we don't clobber it with Chrome cookies.
    no_auth = isinstance(auth, NoAuth)

    if kind is FailureKind.BOT_BLOCKED and no_auth:
        lcb("봇 감지 발생. Chrome 쿠키로 자동 재시도합니다.", "warning")
        retry_opts = dataclasses.replace(
            download_opts, auth=BrowserCookieAuth(browser="chrome")
        )
        retry_success, retry_output, retry_records, retry_targets = _run_once(
            retry_opts, pcb, lcb, cancel_event
        )
        # Videos finished by the first attempt are reported again when the
        # retry finds them "already downloaded".
        seen = {record.get("filepath") for record in records}
        records = records + [r for r in retry_records if r.get("filepath") not in seen]
        targets = targets + retry_targets
        if retry_success:
            lcb("재시도 성공! 다운로드 완료.", "info")
            return _result(True, records)
        if _cancelled():
            return _stopped(records, targets, cancelled=True)
        lcb("재시도도 실패했습니다.", "error")
        # The retry's own failure (say, Chrome's cookie database being locked)
        # does not undo what the first run showed.
        retry_kind = classify_failure(_failure_lines(retry_output))
        kind = retry_kind if retry_kind is not FailureKind.OTHER else kind
        return _failed(_stopped(records, targets), kind, retry_output, retry=True)

    if download_opts.subtitle_opts and subtitle_download_failed(_failure_lines(captured)):
        # yt-dlp gives up on the whole video when one subtitle cannot be fetched (a transient
        # HTTP 429 is the usual cause). The video is what the user wants: get it without the
        # subtitles, once, and say plainly that they are missing. The retry does not use the
        # download archive: an archived video is skipped whole on the next run (checked in
        # yt-dlp: "has already been recorded in the archive", before any subtitle), so recording
        # it here would leave its subtitles unfetched for good. Left out of the archive, a later
        # run writes the subtitles first and then finds the video already there.
        lcb(
            "⚠️ 자막을 받지 못해 yt-dlp 가 영상까지 중단했습니다 — 자막 없이 영상만 다시 받습니다.",
            "warning",
        )
        retry_opts = dataclasses.replace(download_opts, subtitle_opts={}, download_archive=None)
        retry_success, retry_output, retry_records, retry_targets = _run_once(
            retry_opts, pcb, lcb, cancel_event
        )
        seen = {record.get("filepath") for record in records}
        records = records + [r for r in retry_records if r.get("filepath") not in seen]
        targets = targets + retry_targets
        if retry_success:
            lcb(
                "⚠️ 자막 없이 영상만 받았습니다. 자막이 필요하면 잠시 뒤 이 영상을 다시 받으세요 "
                "(영상은 이미 있어 건너뛰고 자막만 받습니다).",
                "warning",
            )
            result = _result(True, records)
            result.subtitles_skipped = True
            return result
        if _cancelled():
            return _stopped(records, targets, cancelled=True)
        lcb("자막 없이 다시 받았지만 실패했습니다.", "error")
        retry_kind = classify_failure(_failure_lines(retry_output))
        kind = retry_kind if retry_kind is not FailureKind.OTHER else kind
        return _failed(_stopped(records, targets), kind, retry_output)

    lcb("다운로드 실패.", "error")
    return _failed(_stopped(records, targets), kind, captured)


def check_js_runtime() -> bool:
    """Return ``True`` iff a JavaScript runtime (deno) is on PATH.

    Pure check - no prompting or I/O. Callers decide how to surface the
    recommendation.
    """
    return shutil.which("deno") is not None
