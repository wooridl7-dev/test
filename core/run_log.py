"""One log file per download run, kept after the window is gone.

The GUI log lives in memory and is only sparsely filled (a progress line
per 10%, or per 30 s for a live recording), so a run that ended oddly could
not be diagnosed afterwards. Each :func:`core.downloader.run_download` now
writes ``~/.ytdlp-downloader/logs/<YYYYmmdd-HHMMSS>_<id>.log`` (or into
``$YTDLP_LOG_DIR``): everything yt-dlp printed, the app's own messages,
a progress line per stream every 30 s, and how the run ended. The newest
``MAX_LOGS`` files are kept.

Logging never breaks a download: when the folder cannot be written the
log is simply disabled.

Pure logic: no tkinter, no ``print()``.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional
from urllib.parse import urlsplit

__all__ = [
    "LOG_DIR_ENV",
    "MAX_LOGS",
    "RunLog",
    "activate",
    "current_run_log",
    "default_log_dir",
]

logger = logging.getLogger(__name__)

LOG_DIR_ENV = "YTDLP_LOG_DIR"
MAX_LOGS = 300  # a batch writes one log per video
# One run never writes more than this many bytes (a runaway loop of identical
# lines), plus RESERVE_BYTES that only the closing lines may use: the exit
# code, errors and the result are what the log exists to keep.
MAX_BYTES = 20 * 1024 * 1024
RESERVE_BYTES = 64 * 1024
_ALWAYS_KEPT = ("result", "error")
# Seconds between progress lines of the same stream.
PROGRESS_EVERY = 30.0

# Names RunLog.create makes; pruning never touches any other file.
_LOG_NAME = re.compile(r"\d{8}-\d{6}_[A-Za-z0-9._-]+\.log")
_VIDEO_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/(?:live|shorts|embed)/)([A-Za-z0-9_-]{11})")


def default_log_dir() -> Path:
    """``$YTDLP_LOG_DIR`` if set, else ``~/.ytdlp-downloader/logs``."""
    override = os.environ.get(LOG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ytdlp-downloader" / "logs"


def _slug(url: str) -> str:
    """Short file-name part naming the video (its id, else the host)."""
    match = _VIDEO_ID.search(url)
    if match:
        return match.group(1)
    try:
        # hostname, not netloc: a "user:pass@" prefix must not reach a file name.
        host = urlsplit(url if "://" in url else "//" + url).hostname or ""
    except ValueError:  # e.g. "https://[abc/x"
        host = ""
    return re.sub(r"[^A-Za-z0-9.-]+", "_", host)[:40] or "download"


class RunLog:
    """Append-only text log of one run. Thread-safe; every line is flushed
    so a killed process still leaves what happened up to then."""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self._clock = clock
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._file = None
        self._written = 0
        self._truncated = False
        self._started = clock()
        self._last_progress: dict[int, float] = {}
        self.path: Optional[Path] = None
        if path is not None:
            try:
                self._file = open(path, "x", encoding="utf-8", errors="replace", buffering=1)
                self.path = path
            except OSError:
                logger.debug("cannot open run log %s", path, exc_info=True)

    @classmethod
    def create(
        cls,
        url: str,
        directory: Optional[Path] = None,
        header: Iterable[str] = (),
        keep: int = MAX_LOGS,
        **kwargs,
    ) -> "RunLog":
        """A new log file for a run on *url*; a disabled log if the folder
        cannot be written. Old logs beyond *keep* are deleted."""
        try:
            return cls._create(url, directory, header, keep, **kwargs)
        except Exception:  # logging must never stop a download
            logger.debug("cannot create run log", exc_info=True)
            return cls(None, **kwargs)

    @classmethod
    def _create(
        cls,
        url: str,
        directory: Optional[Path],
        header: Iterable[str],
        keep: int,
        **kwargs,
    ) -> "RunLog":
        folder = Path(directory) if directory is not None else default_log_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.debug("cannot create log folder %s", folder, exc_info=True)
            return cls(None, **kwargs)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log = cls(None, **kwargs)
        for attempt in range(1, 100):  # two runs of one URL in one second
            suffix = "" if attempt == 1 else f"_{attempt}"
            candidate = folder / f"{stamp}_{_slug(url)}{suffix}.log"
            probe = cls(candidate, **kwargs)
            if probe.path is not None:
                log = probe
                break
            if not candidate.exists():
                break  # not a name clash: the folder is not writable
        if log.path is not None:
            for line in header:
                log.write(line, "info")
            _prune(folder, keep)
        return log

    @property
    def enabled(self) -> bool:
        return self._file is not None

    @property
    def path_str(self) -> str:
        return str(self.path) if self.path is not None else ""

    def write(self, text: str, level: str = "info") -> None:
        """Append one timestamped line (several if *text* has line breaks)."""
        with self._lock:
            if self._file is None:
                return
            stamp = datetime.now().strftime("%H:%M:%S")
            reserve = RESERVE_BYTES if level in _ALWAYS_KEPT else 0
            for line in str(text).replace("\r", "").split("\n"):
                data = f"[{stamp}] [{level}] {line}\n"
                if self._written + len(data.encode("utf-8", "replace")) > self._max_bytes + reserve:
                    if not reserve and not self._truncated:
                        self._truncated = True
                        self._emit(f"[{stamp}] [warning] log size limit reached; only the "
                                   "closing lines (errors, exit code, result) are still saved\n")
                    return
                self._emit(data)

    def _emit(self, data: str) -> None:
        # Caller holds the lock.
        try:
            self._file.write(data)
            self._written += len(data.encode("utf-8", "replace"))
        except (OSError, ValueError):
            logger.debug("run log write failed", exc_info=True)
            self._close()

    def progress(self, info: dict) -> None:
        """A progress line per stream, at most one every ``PROGRESS_EVERY`` s."""
        stream = info.get("stream", 0)
        now = self._clock()
        last = self._last_progress.get(stream)
        if last is not None and now - last < PROGRESS_EVERY:
            return
        self._last_progress[stream] = now
        if info.get("percent_known", True):
            text = (f"progress[{stream}] {info['percentage']:.1f}% of {info['total_size']}"
                    f" got {info['downloaded']} at {info['speed']} ETA {info['eta']}")
        else:
            text = f"progress[{stream}] got {info['downloaded']} at {info['speed']}"
        self.write(text, "progress")

    def wrap(self, callback: Callable[[str, str], None]) -> Callable[[str, str], None]:
        """*callback* (a ``log_callback``) that also writes to this log."""
        def logged(line: str, level: str) -> None:
            self.write(line, level)
            callback(line, level)
        return logged

    def wrap_progress(self, callback: Callable[[dict], None]) -> Callable[[dict], None]:
        """*callback* (a ``progress_callback``) that also logs, throttled."""
        def logged(info: dict) -> None:
            self.progress(info)
            callback(info)
        return logged

    def finish(self, summary: str = "") -> None:
        """Write how the run ended, then close the file. Idempotent."""
        if summary:
            self.write(summary, "result")
        elapsed = int(self._clock() - self._started)
        self.write(f"finished after {elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}",
                   "result")
        with self._lock:
            self._close()

    def _close(self) -> None:
        # Caller holds the lock.
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None


def _prune(folder: Path, keep: int) -> None:
    """Delete the oldest ``*.log`` files beyond *keep* (names sort by time)."""
    try:
        logs = sorted(p for p in folder.iterdir() if _LOG_NAME.fullmatch(p.name) and p.is_file())
    except OSError:
        logger.debug("could not list old run logs", exc_info=True)
        return
    for old in logs[: max(0, len(logs) - keep)]:
        try:
            old.unlink()
        except OSError:  # still open in another run (Windows), or already gone
            logger.debug("could not delete old run log %s", old, exc_info=True)


_DISABLED = RunLog(None)
_current: contextvars.ContextVar[Optional[RunLog]] = contextvars.ContextVar(
    "run_log", default=None
)


def current_run_log() -> RunLog:
    """The log of the :func:`core.downloader.run_download` running in this
    thread; a disabled one (every call a no-op) outside of one."""
    return _current.get() or _DISABLED


@contextlib.contextmanager
def activate(log: RunLog) -> Iterator[RunLog]:
    """Make *log* what :func:`current_run_log` returns inside the block."""
    token = _current.set(log)
    try:
        yield log
    finally:
        _current.reset(token)
