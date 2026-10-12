"""Download history shared by the GUI and CLI.

One JSON file holds the most recent downloads, newest first:
``~/.ytdlp-downloader/history.json``, or the path in the
``YTDLP_HISTORY_FILE`` environment variable. Failed and cancelled
downloads are recorded too, so their URL can be found and retried.

Pure logic: no tkinter, no ``print()``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:
    from core.downloader import DownloadResult

__all__ = ["HistoryEntry", "HistoryStore", "default_history_path"]

logger = logging.getLogger(__name__)

HISTORY_ENV = "YTDLP_HISTORY_FILE"
DEFAULT_LIMIT = 500
_FILE_VERSION = 1


def default_history_path() -> Path:
    """``$YTDLP_HISTORY_FILE`` if set, else ``~/.ytdlp-downloader/history.json``."""
    override = os.environ.get(HISTORY_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ytdlp-downloader" / "history.json"


@dataclass
class HistoryEntry:
    """One download attempt.

    Attributes:
        url: The URL as the user entered it.
        title: Video (or playlist) title; ``""`` when unknown.
        status: ``"ok"``, ``"warning"`` (possibly incomplete live recording),
            ``"failed"``, ``"cancelled"`` or ``"skipped"`` (live broadcast or
            already in the batch archive).
        time: Local time, ``YYYY-MM-DD HH:MM:SS``. Defaults to now.
        audio_language: Language of the downloaded audio track
            (e.g. ``"ko"``); ``""`` when yt-dlp did not report one.
        output_dir: Folder the download was saved to.
        files: Final paths of the finished files.
        id: Unique key used to delete the entry. Generated when empty.
        log_file: The run's log file (core.run_log); ``""`` for entries
            made before logs were kept.
    """

    url: str
    title: str = ""
    status: str = "ok"
    time: str = ""
    audio_language: str = ""
    output_dir: str = ""
    files: list[str] = field(default_factory=list)
    id: str = ""
    log_file: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if not self.time:
            self.time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def from_result(
        cls, url: str, result: "DownloadResult", output_dir: str = ""
    ) -> "HistoryEntry":
        """Entry for a :func:`core.downloader.run_download` outcome."""
        if result.skipped:
            status = "skipped"
        elif result.ok:
            # A recording that may have ended early, or a video that came without its subtitles.
            status = "warning" if result.warnings or result.subtitles_skipped else "ok"
        elif result.cancelled:
            status = "cancelled"
        else:
            status = "failed"
        return cls(
            url=url,
            title=result.title,
            status=status,
            audio_language=result.audio_language or "",
            output_dir=output_dir,
            files=[str(item["filepath"]) for item in result.files if item.get("filepath")],
            log_file=result.log_path,
        )

    @classmethod
    def from_dict(cls, data: Any) -> Optional["HistoryEntry"]:
        """Entry from its stored JSON form; ``None`` if it is unusable."""
        if not isinstance(data, dict) or not data.get("url"):
            return None
        try:
            return cls(
                url=str(data["url"]),
                title=str(data.get("title") or ""),
                status=str(data.get("status") or "ok"),
                time=str(data.get("time") or ""),
                audio_language=str(data.get("audio_language") or ""),
                output_dir=str(data.get("output_dir") or ""),
                files=[str(path) for path in data.get("files") or [] if path],
                id=str(data.get("id") or ""),
                log_file=str(data.get("log_file") or ""),
            )
        except (TypeError, ValueError):
            return None


class HistoryStore:
    """Thread-safe JSON file of :class:`HistoryEntry` items, newest first.

    Every call re-reads the file, so entries another process added (the
    CLI while the GUI is open) show up; concurrent writers are
    last-writer-wins. A file that is not valid JSON (or not UTF-8) is
    moved to ``<name>.bak`` and the history starts over instead of
    breaking the app. ``add``, ``remove`` and ``clear`` raise ``OSError``
    when the file cannot be read or written, so a passing read error
    (antivirus, sync client) never makes them overwrite the history.
    """

    def __init__(self, path: Optional[Path] = None, limit: int = DEFAULT_LIMIT) -> None:
        self.path = Path(path) if path is not None else default_history_path()
        self.limit = limit
        self._lock = threading.Lock()

    def entries(self) -> list[HistoryEntry]:
        """All entries, newest first. An unreadable file reads as empty."""
        with self._lock:
            try:
                return self._load()
            except OSError:
                logger.warning("history: cannot read %s", self.path, exc_info=True)
                return []

    def recent(self, count: int = 20) -> list[HistoryEntry]:
        """The newest entry of each URL, at most *count* of them."""
        seen: set[str] = set()
        result: list[HistoryEntry] = []
        for entry in self.entries():
            if entry.url in seen:
                continue
            seen.add(entry.url)
            result.append(entry)
            if len(result) >= count:
                break
        return result

    def add(self, entry: HistoryEntry) -> None:
        """Record *entry* as the newest, dropping the oldest beyond the limit."""
        with self._lock:
            entries = self._load()
            entries.insert(0, entry)
            self._save(entries[: self.limit])

    def remove(self, ids: Iterable[str]) -> None:
        """Delete the entries whose ``id`` is in *ids*."""
        drop = set(ids)
        with self._lock:
            entries = self._load()
            kept = [entry for entry in entries if entry.id not in drop]
            if len(kept) != len(entries):
                self._save(kept)

    def clear(self) -> None:
        """Delete every entry."""
        with self._lock:
            self._save([])

    def _load(self) -> list[HistoryEntry]:
        """Entries on disk; raises ``OSError`` if the file cannot be read."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        try:
            # utf-8-sig: accept a BOM (Notepad). UnicodeDecodeError is a
            # ValueError, so other encodings count as corrupt.
            items = json.loads(raw.decode("utf-8-sig"))["entries"]
            if not isinstance(items, list):
                raise TypeError("entries is not a list")
        except (ValueError, KeyError, TypeError):
            self._move_aside()
            return []
        loaded = (HistoryEntry.from_dict(item) for item in items)
        return [entry for entry in loaded if entry is not None]

    def _move_aside(self) -> None:
        backup = self.path.with_name(self.path.name + ".bak")
        logger.warning("history: %s is unreadable, moved to %s", self.path, backup)
        try:
            os.replace(self.path, backup)
        except OSError:
            logger.debug("history: could not move %s aside", self.path, exc_info=True)

    def _save(self, entries: list[HistoryEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": _FILE_VERSION, "entries": [asdict(e) for e in entries]}
        # Write a uniquely named sibling and swap it in: a crash mid-write
        # never leaves a truncated history, and the GUI and CLI saving at
        # the same moment do not write into the same temp file. Not
        # tempfile.mkstemp: its 0600 mode would carry over to the history
        # file; "x" creates it with the usual umask-based mode.
        tmp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "x", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            _replace(tmp, self.path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def _replace(src: str, dst: Path, attempts: int = 5) -> None:
    """``os.replace``, retried briefly: on Windows it fails while another
    process has *dst* open (e.g. the CLI reading it)."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)
