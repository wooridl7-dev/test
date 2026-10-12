"""Bookkeeping for the GUI's concurrent downloads (no Tk imports).

One :class:`DownloadJob` per started download. The worker thread feeds it
progress; the Tk thread reads it once a second and draws the 기본 tab list
via :func:`job_row`. Keeping this free of tkinter makes it unit-testable.

Past the concurrent cap a new download is not refused: it waits in the queue
(``QUEUED``, first in first out) until :meth:`JobTable.promote` finds a free
slot and hands it back to be started. All table calls that change a job's
place in the queue (``add``, ``promote``, ``cancel``) are made on the Tk thread.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, NamedTuple, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

__all__ = [
    "BATCH",
    "DEFAULT_MAX_PARALLEL",
    "DOWNLOAD",
    "MAX_PARALLEL_LIMIT",
    "MAX_QUEUED",
    "DownloadJob",
    "JobRejected",
    "JobRow",
    "JobTable",
    "ROW_COLUMNS",
    "STALE_SECONDS",
    "format_ago",
    "fit_widths",
    "format_elapsed",
    "job_row",
]

DEFAULT_MAX_PARALLEL = 3
MAX_PARALLEL_LIMIT = 5  # upper bound of the 고급 tab spinbox
# No bytes for this long while a recording is running: flag the row (the
# stall watchdog in core.downloader is what actually stops a dead stream).
STALE_SECONDS = 60
# Finished rows kept for the list; the oldest are dropped past this.
MAX_FINISHED_KEPT = 20
# Downloads that may wait for a free slot; more are refused.
MAX_QUEUED = 30

RUNNING, QUEUED, DONE, FAILED, CANCELLED = (
    "running", "queued", "done", "failed", "cancelled"
)
DOWNLOAD, BATCH = "download", "batch"  # DownloadJob.kind

_FINAL_LABELS = {DONE: "✓ 완료", FAILED: "✗ 실패", CANCELLED: "취소"}
_STREAM_LABELS = {0: "영상", 1: "오디오"}


class JobRejected(Exception):
    """A download cannot start now; the message is shown to the user."""


@dataclass
class DownloadJob:
    id: int
    url: str  # a batch's label ("일괄: list.txt (12개)") instead of a URL
    started_at: float
    kind: str = DOWNLOAD
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    title: str = ""
    badge: str = ""  # "🔴 LIVE", "⏰ 예정 대기", ... (set once the URL is probed)
    state: str = RUNNING
    note: str = ""  # extra text after the final status, e.g. "부분 저장"
    streams: dict[int, dict] = field(default_factory=dict)
    last_update: Optional[float] = None
    ended_at: Optional[float] = None
    # Batch only: the URL it is downloading now ("" between two items).
    active_url: str = ""
    # Starts the worker of a QUEUED job (set by the GUI, called once the job
    # gets a slot); ``None`` for a job that started right away.
    launch: Optional[Callable[[], None]] = field(default=None, repr=False, compare=False)

    @property
    def running(self) -> bool:
        """Holds a download slot (a queued job does not)."""
        return self.state == RUNNING

    @property
    def queued(self) -> bool:
        return self.state == QUEUED

    @property
    def active(self) -> bool:
        """Running, or waiting to run: not finished."""
        return self.state in (RUNNING, QUEUED)

    @property
    def alive(self) -> bool:
        """The worker thread has not finished (merging a stopped
        recording still counts)."""
        if self.thread is None:
            return self.state == RUNNING
        return self.thread.is_alive()

    @property
    def tag(self) -> str:
        return f"[#{self.id}]"

    def record_progress(self, info: dict, now: float) -> None:
        """Called from the worker thread for every parsed progress line.

        Copy-on-write so the Tk thread never iterates a dict that is
        being resized.
        """
        stream = info.get("stream", 0)
        previous = self.streams.get(stream)
        streams = dict(self.streams)
        streams[stream] = info
        self.streams = streams
        # Same rule as core.downloader's stall watchdog: a stuck live
        # manifest-poll loop keeps printing progress lines with an
        # unchanged byte count, which must not look like activity.
        received = info.get("downloaded_bytes")
        if (
            previous is None
            or received is None
            or received != previous.get("downloaded_bytes")
        ):
            self.last_update = now

    def start_item(self, badge: str, url: str) -> None:
        """A batch moved on to its next URL: forget the last one's progress
        (and its "마지막 수신"), so the row shows only what is happening now."""
        self.streams = {}
        self.last_update = None
        self.active_url = url
        self.badge = badge

    def end_item(self) -> None:
        """A batch item ended. Until the next one starts (a 5-15 s pause, or
        items skipped without running) nothing is downloading: the row must
        not look stalled and that URL is free again."""
        self.streams = {}
        self.last_update = None
        self.active_url = ""

    def finish(self, state: str, now: float, title: str = "", note: str = "") -> None:
        if title:
            self.title = title
        self.note = note
        self.active_url = ""
        if self.kind == BATCH:
            self.streams = {}  # the last item's progress says nothing about the batch
        self.ended_at = now
        self.state = state  # last: readers that see it also see ended_at


class JobTable:
    """The set of downloads started from the GUI."""

    def __init__(
        self,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_parallel = max_parallel
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: list[DownloadJob] = []
        self._next_id = 1

    @property
    def jobs(self) -> list[DownloadJob]:
        with self._lock:
            return list(self._jobs)

    def get(self, job_id: int) -> Optional[DownloadJob]:
        return next((j for j in self.jobs if j.id == job_id), None)

    def running_count(self) -> int:
        return sum(1 for j in self.jobs if j.running)

    def queued_count(self) -> int:
        return sum(1 for j in self.jobs if j.queued)

    def active_count(self) -> int:
        return sum(1 for j in self.jobs if j.active)

    def queue_positions(self) -> dict[int, int]:
        """``{job id: 1-based place in the queue}`` of the waiting jobs."""
        return {j.id: n for n, j in enumerate((j for j in self.jobs if j.queued), 1)}

    def any_alive(self) -> bool:
        return any(j.alive for j in self.jobs)

    def add(self, url: str) -> DownloadJob:
        """Register a new download: running if a slot is free, else
        queued (``job.queued``). Raises :class:`JobRejected` for a URL that is
        already downloading or waiting, or when the queue is full."""
        key = _url_key(url)
        with self._lock:
            for job in self._jobs:
                if not job.active:
                    continue
                # A batch's own URL is a label; what it downloads now is active_url.
                busy_url = job.url if job.kind == DOWNLOAD else job.active_url
                if busy_url and _url_key(busy_url) == key:
                    what = "대기열에 있습니다" if job.queued else "받는 중입니다"
                    raise JobRejected(
                        f"같은 주소를 이미 {what} ({job.tag}).\n"
                        "같은 파일에 동시에 쓰면 망가지므로 시작하지 않습니다."
                    )
            return self._admit(DownloadJob(id=self._next_id, url=url, started_at=self._clock()))

    def add_batch(self, label: str) -> DownloadJob:
        """Register a batch (a whole URL list) as one job, running or
        queued like :meth:`add`, or raise :class:`JobRejected`. It counts as
        one download towards the cap."""
        with self._lock:
            for job in self._jobs:
                if job.kind == BATCH and job.active:
                    raise JobRejected(
                        f"일괄 다운로드가 이미 진행 중이거나 대기 중입니다 ({job.tag}).\n"
                        "끝나거나 취소된 뒤 다시 시작하세요."
                    )
            return self._admit(
                DownloadJob(id=self._next_id, url=label, started_at=self._clock(), kind=BATCH)
            )

    def promote(self) -> list[DownloadJob]:
        """Start waiting jobs, oldest first, while slots are free.

        Marks them running and returns them; the caller starts their workers
        (``job.launch()``). Call it whenever a job ended or the cap changed."""
        started: list[DownloadJob] = []
        with self._lock:
            free = max(1, self.max_parallel) - sum(1 for j in self._jobs if j.running)
            for job in self._jobs:
                if free <= 0:
                    break
                if job.queued:
                    job.started_at = self._clock()  # the clock of the list starts now
                    job.state = RUNNING
                    started.append(job)
                    free -= 1
        return started

    def _admit(self, job: DownloadJob) -> DownloadJob:
        # Caller holds the lock. Wait behind earlier waiters even when a slot
        # is free right now (it is about to be handed to them by promote()).
        running = sum(1 for j in self._jobs if j.running)
        waiting = sum(1 for j in self._jobs if j.queued)
        if waiting or running >= max(1, self.max_parallel):
            if waiting >= MAX_QUEUED:
                raise JobRejected(
                    f"대기 중인 다운로드가 이미 {MAX_QUEUED}개입니다.\n"
                    "일부가 시작되거나 취소된 뒤 다시 추가하세요."
                )
            job.state = QUEUED
        return self._append(job)

    def _append(self, job: DownloadJob) -> DownloadJob:
        # Caller holds the lock.
        self._next_id += 1
        self._jobs.append(job)
        self._trim_finished()
        return job

    def cancel(self, job_id: int) -> bool:
        """Ask a running job to stop; a waiting one is dropped from the queue
        at once. False if it is not active or was already asked to."""
        job = self.get(job_id)
        if job is None:
            return False
        with self._lock:
            if job.queued:
                now = self._clock()
                job.cancel_event.set()
                job.started_at = now  # never ran: 00:00:00 in the list
                job.finish(CANCELLED, now, note="대기 중 취소")
                return True
        if not job.running or job.cancel_event.is_set():
            return False
        job.cancel_event.set()
        return True

    def cancel_all(self) -> int:
        return sum(1 for j in self.jobs if self.cancel(j.id))

    def clear_finished(self) -> None:
        with self._lock:
            self._jobs = [j for j in self._jobs if j.active]

    def tag_for(self, job: DownloadJob) -> str:
        """Log prefix: only needed while several downloads share the log."""
        return f"{job.tag} " if self.running_count() > 1 else ""

    def _trim_finished(self) -> None:
        # Caller holds the lock.
        finished = [j for j in self._jobs if not j.active]
        for job in finished[: max(0, len(finished) - MAX_FINISHED_KEPT)]:
            self._jobs.remove(job)


# ---------------------------------------------------------------- rows

ROW_COLUMNS = ("id", "target", "status", "progress", "speed", "elapsed", "seen")


def fit_widths(available: int, columns: Sequence[tuple[int, int, bool]]) -> list[int]:
    """Column widths that fill *available* pixels.

    *columns* holds ``(base width, minimum width, stretches)`` per column.
    Columns that do not stretch keep their base width. The stretching ones
    share what is left in proportion to their base widths: they grow when
    there is room, and shrink, none below its minimum, when there is not.
    If even the minimums do not fit, the minimums win and the table is wider
    than the window.
    """
    widths = [base for base, _low, _stretch in columns]
    flexible = [i for i, (_base, _low, stretch) in enumerate(columns) if stretch]
    if not flexible:
        return widths
    room = available - sum(widths[i] for i in range(len(columns)) if i not in flexible)
    pinned: dict[int, int] = {}  # columns held at their minimum
    scaled = list(flexible)
    while scaled:
        scale = (room - sum(pinned.values())) / sum(columns[i][0] for i in scaled)
        too_small = [i for i in scaled if columns[i][0] * scale < columns[i][1]]
        if not too_small:
            break
        for i in too_small:
            pinned[i] = columns[i][1]
            scaled.remove(i)
    for i, width in pinned.items():
        widths[i] = width
    if scaled:
        exact = {i: columns[i][0] * scale for i in scaled}
        for i in scaled:
            widths[i] = int(exact[i])
        # hand the pixels lost to rounding to the columns that lost the most
        spare = (room - sum(pinned.values())) - sum(widths[i] for i in scaled)
        for i in sorted(scaled, key=lambda i: exact[i] - widths[i], reverse=True)[:max(spare, 0)]:
            widths[i] += 1
    return widths


class JobRow(NamedTuple):
    values: tuple[str, ...]  # same order as ROW_COLUMNS
    stale: bool


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_ago(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}초 전"
    if total < 3600:
        return f"{total // 60}분 전"
    return f"{total // 3600}시간 {total % 3600 // 60}분 전"


def job_row(job: DownloadJob, now: float, queue_position: int = 0) -> JobRow:
    """The list row for *job* at monotonic time *now*. *queue_position* is
    the 1-based place of a waiting job in the queue (0: unknown)."""
    if job.queued:
        waiting = f"대기 중 ({queue_position}번째)" if queue_position else "대기 중"
        return JobRow(
            (str(job.id), job.title or _short_url(job.url), waiting, "대기열", "-", "-", "-"),
            False,
        )
    end = job.ended_at if job.ended_at is not None and not job.running else now
    elapsed = format_elapsed(end - job.started_at)

    streams = job.streams  # replaced wholesale by record_progress
    if job.running:
        status = "취소 중" if job.cancel_event.is_set() else (
            job.badge or ("받는 중" if streams else "시작 중")
        )
    else:
        status = _FINAL_LABELS.get(job.state, job.state)
        if job.note:
            status = f"{status} ({job.note})"

    if streams:
        progress = _progress_text(streams)
        speed = _speed_text(streams) if job.running else "-"
    else:
        progress = "연결/대기 중" if job.running else "-"
        speed = "-"

    stale = False
    seen = "-"
    last_update = job.last_update  # read once: a batch worker may reset it
    if job.running and last_update is not None:
        age = now - last_update
        stale = age > STALE_SECONDS and not _winding_down(job)
        seen = ("⚠ " if stale else "") + format_ago(age)

    target = job.title or _short_url(job.url)
    return JobRow(
        (str(job.id), target, status, progress, speed, elapsed, seen), stale
    )


def _winding_down(job: DownloadJob) -> bool:
    """Quiet on purpose: merging after 100%, or merging what a cancelled
    recording got. No bytes arriving is then expected, not a stall."""
    if job.cancel_event.is_set():
        return True
    return bool(job.streams) and all(
        info.get("percent_known", True) and info["percentage"] >= 100
        for info in job.streams.values()
    )


def _stream_label(index: int, streams: dict[int, dict]) -> str:
    if len(streams) == 1:
        return ""
    if set(streams) == {0, 1}:
        return _STREAM_LABELS[index] + " "
    return f"스트림{index + 1} "


def _progress_text(streams: dict[int, dict]) -> str:
    parts = []
    for index in sorted(streams):
        info = streams[index]
        if info.get("percent_known", True):
            text = f"{info['percentage']:.1f}% / {info['total_size']}"
        else:
            text = f"{info['downloaded']} 받음"
        parts.append(_stream_label(index, streams) + text)
    return " · ".join(parts)


def _speed_text(streams: dict[int, dict]) -> str:
    parts = []
    for index in sorted(streams):
        info = streams[index]
        text = info["speed"]
        eta = info.get("eta", "--:--")
        if info.get("percent_known", True) and eta != "--:--":
            text += f" ETA {eta}"
        parts.append(text)
    return " · ".join(parts)


def _short_url(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url.strip())


# Hosts whose links name a video by id in several different shapes.
_YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_YOUTUBE_PATH_ID = re.compile(r"/(?:live|shorts|embed)/([^/?#]+)")
# Query parameters that never change which media a link points to. Any
# other parameter may (``?p=2`` on a multi-part video, ``?id=7``).
_TRACKING_PARAMS = frozenset(
    {"si", "feature", "t", "start", "time_continue", "pp", "fbclid", "gclid", "igshid", "ref"}
)


def _url_key(url: str) -> tuple:
    """Identity of *url* for the "already downloading" check: tracking
    parameters (``?si=``) and the different YouTube link shapes for one
    video all collapse to the same key."""
    text = url.strip()
    try:
        parts = urlsplit(text if "://" in text else "//" + text)
    except ValueError:  # e.g. "https://[abc/x": compare such text as it is
        return ("raw", text)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    query = parse_qs(parts.query)
    if host in _YOUTUBE_HOSTS:
        if host == "youtu.be":
            video_id = path.lstrip("/").split("/")[0]
        elif path == "/watch":
            video_id = query.get("v", [""])[0]
        else:
            match = _YOUTUBE_PATH_ID.match(path)
            video_id = match.group(1) if match else ""
        if video_id:
            return ("youtube", video_id)
    kept = tuple(
        sorted(
            (k, tuple(values))
            for k, values in query.items()
            if k not in _TRACKING_PARAMS and not k.startswith("utm_")
        )
    )
    return (host, path, kept)
