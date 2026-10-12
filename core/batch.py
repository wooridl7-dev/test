"""Download a list of URLs one after another, sorted into category folders.

The list is a plain text file: ``# <category>`` lines start a category,
the URLs below it are saved to ``<output dir>/<category>/``. URLs before
the first heading go to ``미분류``. Blank lines are ignored, and anything
after the URL on its line (e.g. a note) too::

    # 요리
    https://youtu.be/-OnM0ABAYzM
    https://youtu.be/-mqV2nO4XS4   백종원 김치찌개

    # 투자
    https://youtu.be/09BcoSNaxdk

:func:`run_batch` downloads each URL with :func:`core.downloader.run_download`
(same options, cookie retry and history as a single download). Finished
videos go into a yt-dlp ``--download-archive`` in the output directory,
so running the same list again only fetches what is missing. Ongoing or
upcoming live broadcasts are skipped: recording one would hold up the
rest of the list for hours. Failed and cancelled URLs are written to
``<list>.남은목록.txt`` in the same format, ready to be run again.
Interfaces also keep skipped broadcasts in ``<list>.라이브대기.txt``;
rerun it after the broadcast ends.

Pure logic: no tkinter, no ``print()``.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import random
import re
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

from core.downloader import (
    DownloadOptions,
    DownloadResult,
    LogCallback,
    ProgressCallback,
    run_download,
)

__all__ = [
    "ARCHIVE_NAME",
    "DEFAULT_PAUSE",
    "MAX_PAUSE_SECONDS",
    "UNCATEGORIZED",
    "BatchItem",
    "BatchPlan",
    "BatchSummary",
    "category_template",
    "parse_batch_text",
    "read_batch_file",
    "remaining_path_for",
    "live_path_for",
    "parse_pause",
    "pause_label",
    "run_batch",
    "safe_folder_name",
    "video_id_of",
]

logger = logging.getLogger(__name__)

UNCATEGORIZED = "미분류"
ARCHIVE_NAME = ".batch-archive.txt"
REMAINING_SUFFIX = ".남은목록.txt"

# Seconds to wait between two downloads (random within the range), so a
# long list does not look like a bot to YouTube.
DEFAULT_PAUSE = (5.0, 15.0)
# Longest pause a person may ask for; a bigger number is probably a unit mix-up.
MAX_PAUSE_SECONDS = 600.0
_PAUSE_NUMBER = r"[0-9]+(?:\.[0-9]+)?"  # not \d: that accepts other scripts' digits
_PAUSE_RE = re.compile(rf"({_PAUSE_NUMBER})(?:\s*[-~]\s*({_PAUSE_NUMBER}))?")

# Item states, in the order a run moves through them.
PENDING, RUNNING = "pending", "running"
OK, FAILED, CANCELLED = "ok", "failed", "cancelled"
LIVE, ARCHIVED = "live", "archived"
# States whose URLs go to the remaining list.
_UNFINISHED = (PENDING, FAILED, CANCELLED)

ItemCallback = Callable[[int, "BatchItem", str, Optional[DownloadResult]], None]

_YOUTUBE_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_YOUTUBE_PATH_PREFIXES = ("/live/", "/shorts/", "/embed/", "/v/")
# Characters Windows does not allow in a folder name, plus control codes.
_BAD_NAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)
}


@dataclass(frozen=True)
class BatchItem:
    """One URL of the list."""

    url: str
    category: str = UNCATEGORIZED
    line_no: int = 0
    video_id: str = ""  # YouTube video ID, "" for other URLs


@dataclass
class BatchPlan:
    """A parsed list: what to download and what was left out."""

    items: list[BatchItem] = field(default_factory=list)
    duplicates: list[BatchItem] = field(default_factory=list)
    invalid: list[tuple[int, str]] = field(default_factory=list)  # (line_no, text)

    @property
    def categories(self) -> list[str]:
        """Category names in the order they first appear."""
        return list(dict.fromkeys(item.category for item in self.items))

    def describe(self) -> str:
        """One-line summary, e.g. ``분야 2개 · 118개 · 중복 3 · 잘못된 줄 0``."""
        return (
            f"분야 {len(self.categories)}개 · {len(self.items)}개 · "
            f"중복 {len(self.duplicates)} · 잘못된 줄 {len(self.invalid)}"
        )


@dataclass
class BatchSummary:
    """Outcome of :func:`run_batch`, one state per item."""

    items: list[BatchItem]
    states: list[str]
    results: list[Optional[DownloadResult]]
    remaining_file: Optional[str] = None
    live_file: Optional[str] = None

    @property
    def counts(self) -> Counter:
        return Counter(self.states)

    @property
    def remaining(self) -> list[BatchItem]:
        """Items that failed, were cancelled or never started."""
        return [i for i, s in zip(self.items, self.states) if s in _UNFINISHED]

    @property
    def ok(self) -> bool:
        """True when nothing is left to download."""
        return not self.remaining

    @property
    def subtitles_skipped(self) -> int:
        """Videos that came without the subtitles asked for (their subtitle
        download failed). They are not in the download archive, so running the
        same list again fetches the subtitles and skips the video."""
        return sum(1 for r in self.results if r is not None and r.subtitles_skipped)

    def describe(self) -> str:
        counts = self.counts
        parts = [f"완료 {counts[OK]}" + (f" (자막 없음 {self.subtitles_skipped})" if self.subtitles_skipped else "")]
        for state, label in ((ARCHIVED, "이미 받음"), (LIVE, "라이브라 건너뜀"),
                             (FAILED, "실패"), (CANCELLED, "취소")):
            if counts[state]:
                parts.append(f"{label} {counts[state]}")
        pending = counts[PENDING] + counts[RUNNING]
        if pending:
            parts.append(f"미진행 {pending}")
        return " · ".join(parts)


# --- Parsing ------------------------------------------------------------


def video_id_of(url: str) -> str:
    """The YouTube video ID in *url*, or ``""`` if it has none."""
    try:
        parts = urlsplit(url if "://" in url else "https://" + url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    candidate = ""
    if host == "youtu.be":
        candidate = parts.path.lstrip("/").split("/")[0]
    elif host in _YOUTUBE_HOSTS:
        if parts.path == "/watch":
            candidate = (parse_qs(parts.query).get("v") or [""])[0]
        else:
            for prefix in _YOUTUBE_PATH_PREFIXES:
                if parts.path.startswith(prefix):
                    candidate = parts.path[len(prefix):].split("/")[0]
                    break
    return candidate if _YOUTUBE_ID_RE.fullmatch(candidate) else ""


def safe_folder_name(name: str) -> str:
    """*name* usable as a folder name on Windows too; ``미분류`` if empty."""
    cleaned = _BAD_NAME_CHARS_RE.sub("_", name).strip().rstrip(". ")
    if not cleaned:
        return UNCATEGORIZED
    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned += "_"
    return cleaned


def _as_url(token: str) -> Optional[str]:
    """*token* as a URL, adding ``https://`` to ``youtu.be/...``; else ``None``."""
    lowered = token.lower()
    if lowered.startswith(("http://", "https://")):
        return token
    if re.match(r"(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/", lowered):
        return "https://" + token
    return None


def parse_batch_text(text: str) -> BatchPlan:
    """Parse a batch list (see the module docstring)."""
    plan = BatchPlan()
    category = UNCATEGORIZED
    seen: set[str] = set()
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            category = safe_folder_name(line.lstrip("#").strip())
            continue
        url = _as_url(line.split()[0])
        if url is None:
            plan.invalid.append((line_no, line))
            continue
        item = BatchItem(url=url, category=category, line_no=line_no, video_id=video_id_of(url))
        key = item.video_id or url
        if key in seen:
            plan.duplicates.append(item)
        else:
            seen.add(key)
            plan.items.append(item)
    return plan


def read_batch_file(path: str | os.PathLike) -> BatchPlan:
    """Read and parse a batch list: UTF-8 (BOM allowed), else cp949."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp949", errors="replace")  # saved as ANSI by Notepad
    return parse_batch_text(text)


def remaining_path_for(list_path: str | os.PathLike) -> Path:
    """Where :func:`run_batch` writes the unfinished URLs of *list_path*.

    Running a remaining list again updates that same file.
    """
    path = Path(list_path)
    if path.name.endswith(REMAINING_SUFFIX):
        return path
    return path.with_name(path.stem + REMAINING_SUFFIX)


def live_path_for(list_path: str | os.PathLike) -> Path:
    """Use the same deferred-live list when retrying either saved list."""
    path = Path(list_path)
    if path.name.endswith(REMAINING_SUFFIX):
        path = path.with_name(path.name.removesuffix(REMAINING_SUFFIX) + ".txt")
    if path.name.endswith(".라이브대기.txt"):
        return path
    return path.with_name(path.stem + ".라이브대기.txt")


# --- Running ------------------------------------------------------------


def check_pause(pause: tuple[float, float]) -> tuple[float, float]:
    """*pause* as a ``(shortest, longest)`` pair of seconds, or ``ValueError``
    unless ``0 <= shortest <= longest <= MAX_PAUSE_SECONDS``."""
    try:
        low, high = pause
        valid = (
            not isinstance(low, bool) and not isinstance(high, bool)
            and 0 <= low <= high <= MAX_PAUSE_SECONDS
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(
            f"휴식 시간은 (최소, 최대) 초로 0 ≤ 최소 ≤ 최대 ≤ {MAX_PAUSE_SECONDS:g} 여야 합니다: "
            f"{pause!r}"
        )
    return float(low), float(high)


def parse_pause(text: str) -> tuple[float, float]:
    """The pause between two downloads typed by a person (CLI argument):
    ``"5-15"`` or ``"5~15"`` -> ``(5.0, 15.0)``; ``"10"`` -> ``(10.0, 10.0)``;
    ``"0"`` -> ``(0.0, 0.0)`` (no pause).

    Anything else - a negative number, a longest shorter than the shortest,
    more than :data:`MAX_PAUSE_SECONDS`, words, an empty string - raises
    ``ValueError``, so a typo cannot quietly remove the pause.
    """
    match = _PAUSE_RE.fullmatch(str(text).strip())
    try:
        if match is None:
            raise ValueError
        low = float(match.group(1))
        return check_pause((low, float(match.group(2)) if match.group(2) else low))
    except ValueError:
        raise ValueError(
            f"휴식 시간은 초 단위의 '최소-최대'(예: 5-15) 또는 하나의 수(예: 10, 0 = 쉬지 않음)여야 "
            f"하고 {MAX_PAUSE_SECONDS:g} 초를 넘을 수 없습니다: {text!r}"
        ) from None


def pause_label(pause: tuple[float, float]) -> str:
    """``(5.0, 15.0)`` -> ``"5~15초"``; ``(0, 0)`` -> ``"쉬지 않음"``."""
    low, high = pause
    if high <= 0:
        return "쉬지 않음"
    if low == high:
        return f"{low:g}초"
    return f"{low:g}~{high:g}초"


def category_template(output_template: str, category: str) -> str:
    """``dir/<name template>`` -> ``dir/<category>/<name template>``."""
    head, tail = os.path.split(output_template)
    # "%" starts a field in yt-dlp's output template.
    folder = safe_folder_name(category).replace("%", "%%")
    return os.path.join(head, folder, tail)


def _read_archive(path: str) -> set[str]:
    """Entries (``"youtube <id>"``) of a yt-dlp download archive."""
    try:
        with open(path, encoding="utf-8") as fh:
            return {line.strip() for line in fh if line.strip()}
    except OSError:
        return set()


def _wait(event: threading.Event, seconds: float) -> bool:
    """``event.wait(seconds)`` in short steps: on Windows a long wait is
    not interrupted by Ctrl+C, so the CLI's handler would run late."""
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        if event.wait(min(left, 0.5)):
            return True
    return event.is_set()


def _state_of(result: DownloadResult) -> str:
    if result.skipped == "live":
        return LIVE
    if result.skipped == "archived":
        return ARCHIVED
    if result.ok:
        return OK
    return CANCELLED if result.cancelled else FAILED


def _write_remaining(path: Path, items: list[BatchItem]) -> None:
    lines: list[str] = []
    category = None
    for item in items:
        if item.category != category:
            category = item.category
            lines += ["", f"# {category}"] if lines else [f"# {category}"]
        lines.append(item.url)
    # A failed write must not truncate the old retry list.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as fh:
            temporary = Path(fh.name)
            fh.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _save_live_list(path: Path, summary: BatchSummary) -> None:
    """Merge new live skips; retain unseen/cancelled entries on partial reruns."""
    try:
        previous = read_batch_file(path).items
    except FileNotFoundError:
        previous = []

    def key(item: BatchItem) -> str:
        return item.video_id or item.url

    finished = {key(item) for item, state in zip(summary.items, summary.states)
                if state in (OK, ARCHIVED)}
    pending = {key(item): item for item in previous if key(item) not in finished}
    for item, state in zip(summary.items, summary.states):
        if state == LIVE:
            pending[key(item)] = item
    if pending:
        _write_remaining(path, list(pending.values()))
        summary.live_file = str(path)
    elif path.exists():
        path.unlink()


def run_batch(
    items: list[BatchItem],
    base_opts: DownloadOptions,
    *,
    progress_callback: Optional[ProgressCallback] = None,
    log_callback: Optional[LogCallback] = None,
    item_callback: Optional[ItemCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    pause: tuple[float, float] = DEFAULT_PAUSE,
    remaining_path: Optional[str | os.PathLike] = None,
    live_path: Optional[str | os.PathLike] = None,
) -> BatchSummary:
    """Download *items* one by one with *base_opts*.

    Each item uses *base_opts* with its URL, its category folder inserted
    into ``output_template`` and the output directory's
    ``.batch-archive.txt`` as download archive. *item_callback* gets
    ``(index, item, state, result)`` when an item starts (``"running"``,
    no result) and when it ends. Setting *cancel_event* stops the current
    download and marks the rest ``"cancelled"``.

    With *remaining_path*, failed and cancelled items are written there
    as a list that can be run again; the file is removed when nothing is
    left.

    With *live_path*, skipped broadcasts are merged into a separate list.
    Successful reruns remove them; cancellation preserves unprocessed entries.

    *pause* is the ``(shortest, longest)`` seconds to wait, at random, between
    two real downloads (``(0, 0)``: none); an invalid pair raises ``ValueError``
    before anything is downloaded.
    """
    pause = check_pause(pause)
    lcb = log_callback or (lambda _line, _level: None)
    notify = item_callback or (lambda *_args: None)
    event = cancel_event or threading.Event()

    output_dir = os.path.dirname(base_opts.output_template)
    archive_path = os.path.join(output_dir, ARCHIVE_NAME)
    archive = _read_archive(archive_path)

    summary = BatchSummary(
        items=list(items), states=[PENDING] * len(items), results=[None] * len(items)
    )
    total = len(items)
    downloaded_before = False  # pause only between real downloads

    def archived(item: BatchItem) -> bool:
        return bool(item.video_id) and f"youtube {item.video_id}" in archive

    for index, item in enumerate(items):
        if event.is_set():
            break
        if archived(item):
            summary.states[index] = ARCHIVED
            notify(index, item, ARCHIVED, None)
            continue
        if downloaded_before:
            delay = random.uniform(*pause)
            if delay > 0:
                lcb(f"다음 영상까지 {delay:.0f}초 쉽니다.", "info")
                if _wait(event, delay):
                    break

        summary.states[index] = RUNNING
        notify(index, item, RUNNING, None)
        lcb(f"━━ [{index + 1}/{total}] {item.category} · {item.url}", "info")
        opts = dataclasses.replace(
            base_opts,
            url=item.url,
            output_template=category_template(base_opts.output_template, item.category),
            download_archive=archive_path,
            # A fresh copy: the probe stores the detected status on it,
            # which must not carry over to the next video.
            live_opts=(
                dataclasses.replace(base_opts.live_opts, _detected_status=None)
                if base_opts.live_opts is not None else None
            ),
        )
        try:
            result = run_download(
                item.url, opts,
                progress_callback=progress_callback,
                log_callback=log_callback,
                cancel_event=event,
                skip_live=True,
            )
        except Exception as exc:  # keep going with the rest of the list
            logger.exception("batch item %s failed", item.url)
            lcb(f"다운로드 중 오류: {exc}", "error")
            # run_download tags the exception with its run log (core.downloader).
            result = DownloadResult(ok=False, log_path=getattr(exc, "log_path", ""))
        state = _state_of(result)
        summary.states[index] = state
        summary.results[index] = result
        downloaded_before = state != LIVE
        notify(index, item, state, result)

    for index, state in enumerate(summary.states):
        if state == PENDING and event.is_set():
            # Already downloaded ones stay off the remaining list.
            state = ARCHIVED if archived(items[index]) else CANCELLED
            summary.states[index] = state
            notify(index, items[index], state, None)

    if remaining_path is not None:
        path = Path(remaining_path)
        remaining = summary.remaining
        try:
            if remaining:
                _write_remaining(path, remaining)
                summary.remaining_file = str(path)
                lcb(f"받지 못한 {len(remaining)}개를 {path.name} 에 저장했습니다. "
                    "이 파일로 다시 실행하면 이어서 받습니다.", "info")
            elif path.exists():
                path.unlink()
        except OSError as exc:
            lcb(f"남은 목록 저장 실패: {exc}", "warning")

    if live_path is not None:
        try:
            _save_live_list(Path(live_path), summary)
            if summary.live_file:
                lcb(f"라이브 대기 목록: {summary.live_file} — 방송 종료 후 이 파일로 다시 실행하세요.", "info")
        except OSError as exc:
            lcb(f"라이브 대기 목록 저장 실패: {exc}", "warning")

    lcb(f"일괄 다운로드 끝: {summary.describe()}", "info")
    return summary
