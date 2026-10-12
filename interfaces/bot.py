"""Telegram bot: send a YouTube link, the PC (or server) downloads it.

``python main.py --bot`` runs a long-polling Telegram bot. Messages from the
chats in ``YTDLP_BOT_ALLOWED_CHATS`` are the only ones it serves:

* a message with a YouTube link downloads it (best quality), and so does
  ``/video <link>`` (the form that also works in a group, where Telegram's
  privacy mode hides plain messages from a bot); ``/audio <link>`` downloads
  the audio as MP3;
* the status message of each download is edited while it runs (percent, speed,
  ETA) and ends with the saved path, or why it failed;
* ``/status`` lists the running and waiting downloads, ``/cancel [#id]``
  cancels one (or all of the chat's); ``/help`` says all this;
* at most ``YTDLP_BOT_MAX_PARALLEL`` run at once, the rest wait in a queue of
  ``YTDLP_BOT_MAX_QUEUED``;
* when a download confirms the login cookies expired, every allowed chat is
  told once (``DownloadResult.cookie_event``).

The dependency direction stays ``interfaces/bot.py -> core/*``; ``core/`` never
imports from here. Telegram itself is :mod:`core.telegram` (standard library
only).

Safety: a bot that downloads whatever it is sent is an open door, so it
refuses to start without an allow-list of chat ids, answers a stranger only
once (with the chat id, which is what the owner needs to put on the list),
and takes only YouTube links unless ``YTDLP_BOT_ALLOWED_HOSTS`` says
otherwise. The bot token is read from ``YTDLP_BOT_TOKEN`` and never logged.

What is NOT implemented yet
---------------------------
* Sending the downloaded file back (the reply gives its path on the machine
  that runs the bot) and the Whisper transcription (see
  :mod:`core.transcriber`, Phase D').
* A queue that survives a restart: waiting downloads are lost when the bot
  stops, running ones are cancelled (a live recording is merged first).
"""

from __future__ import annotations

import collections
import logging
import os
import re
import signal
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

from core import presets
from core.auth import (
    Authenticator,
    BgutilPoTokenAuth,
    BrowserCookieAuth,
    CookieFileAuth,
    NoAuth,
)
from core.cookie_health import CookieHealthStore
from core.downloader import DownloadOptions, DownloadResult, run_download
from core.failure import FailureKind, failure_hint
from core.live import (
    DEFAULT_FRAGMENT_RETRIES,
    DEFAULT_STALL_MINUTES,
    LiveOptions,
    parse_fragment_retries,
    parse_stall_minutes,
)
from core.telegram import TelegramClient, TelegramError
from core.transcriber import Transcriber
from core.version import app_version

__all__ = [
    "BotConfig",
    "BotJob",
    "BotService",
    "config_from_env",
    "handle_url",
    "parse_chat_ids",
    "parse_hosts",
    "run_bot",
]

logger = logging.getLogger(__name__)


# --- Config -----------------------------------------------------------------


_DEFAULT_BGUTIL_URL = "http://127.0.0.1:4416"

#: Links the bot accepts unless ``YTDLP_BOT_ALLOWED_HOSTS`` says otherwise
#: (a host matches itself and its sub-domains).
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
DEFAULT_MAX_PARALLEL = 2
MAX_PARALLEL_LIMIT = 5  # the GUI's cap too
DEFAULT_MAX_QUEUED = 20
MAX_QUEUED_LIMIT = 100
#: Links taken from one message; more are ignored with a note.
MAX_URLS_PER_MESSAGE = 5
#: Seconds between two edits of one status message (Telegram throttles edits).
PROGRESS_INTERVAL = 4.0
#: Seconds an edit that failed is not tried again (the download goes on).
EDIT_COOLDOWN = 30.0
#: Long-poll length. Short enough that Ctrl+C is not left waiting long on Windows.
POLL_SECONDS = 20
#: Strangers' chat ids remembered so each is answered once.
_MAX_REFUSED = 1000
#: Finished downloads kept for the job table (the rest are forgotten).
_KEEP_FINISHED = 100
#: A message that must arrive (how a download ended, the cookie alert) and that
#: Telegram would not take is kept and sent again: after these many seconds the
#: first time, then each later wait (the last one repeats), until it arrives, it
#: cannot arrive (blocked, no such chat) or OUTBOX_GIVE_UP seconds have passed.
OUTBOX_RETRY_DELAYS = (5.0, 10.0, 20.0, 40.0, 60.0)
OUTBOX_GIVE_UP = 3600.0
#: How often the outbox thread looks for a message that is due, and how many it keeps.
OUTBOX_POLL = 1.0
OUTBOX_MAX = 200
#: Error lines of a run remembered, how many a failure message quotes, and how long
#: each may be. The message is the only place an operator on another machine sees why.
_KEEP_ERRORS = 20
_ERROR_LINES = 3
_ERROR_LINE_CHARS = 200


def parse_chat_ids(text: str) -> frozenset[int]:
    """``"123, -100456"`` -> ``{123, -100456}``. Groups have negative ids.

    Raises ``ValueError`` for anything that is not an integer, so a typo does
    not quietly leave a chat off (or let a wrong one in).
    """
    ids = set()
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            raise ValueError(f"not a chat id: {part!r}") from None
    return frozenset(ids)


def parse_hosts(text: str) -> tuple[str, ...]:
    """``"youtube.com, youtu.be"`` -> ``("youtube.com", "youtu.be")``; ``*``
    allows any host. Empty means the YouTube default."""
    hosts = tuple(
        part.strip().lower().lstrip(".")
        for part in text.replace(";", ",").split(",")
        if part.strip()
    )
    for host in hosts:
        if host != "*" and not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", host):
            raise ValueError(f"not a host name: {host!r}")
    return hosts or YOUTUBE_HOSTS


def _bounded_int(text: str, low: int, high: int) -> int:
    try:
        value = int(text.strip())
    except ValueError:
        raise ValueError(f"not a whole number: {text.strip()!r}") from None
    if not low <= value <= high:
        raise ValueError(f"must be {low}..{high}, not {value}")
    return value


@dataclass
class BotConfig:
    """Env-driven config snapshot for the messenger-bot service.

    Attributes:
        auth: The :class:`Authenticator` built from environment variables.
        auth_method: Raw value of ``YTDLP_AUTH_METHOD`` (for logging).
        cookie_file: Resolved path to cookies.txt when relevant, else ``None``.
        bgutil_url: Resolved bgutil provider URL when relevant, else ``None``.
        output_dir: Directory for downloaded media files.
        fragment_retries: ``--fragment-retries`` for live recordings:
            ``0`` means no retries, ``None`` infinite (see
            :attr:`core.live.LiveOptions.fragment_retries`).
        stall_minutes: Stop a live recording that receives no new data for this
            many minutes; ``0`` turns the watchdog off (see
            :attr:`core.live.LiveOptions.stall_minutes`).
        token: Telegram bot token (``YTDLP_BOT_TOKEN``); never shown by
            :meth:`describe` or ``repr``.
        allowed_chats: Chat ids the bot serves (``YTDLP_BOT_ALLOWED_CHATS``).
        allowed_hosts: Hosts a link may point to (``YTDLP_BOT_ALLOWED_HOSTS``).
        max_parallel: Downloads at once (``YTDLP_BOT_MAX_PARALLEL``).
        max_queued: Downloads waiting for a slot (``YTDLP_BOT_MAX_QUEUED``).
        audio_lang: Preferred audio-track language of a dubbed video
            (``YTDLP_AUDIO_LANG``; ``None`` = the original track, the default:
            a language like ``ko`` would pick YouTube's auto-dubbed track of
            a foreign-language video).
    """

    auth: Authenticator
    auth_method: str
    cookie_file: Optional[Path]
    bgutil_url: Optional[str]
    output_dir: Path
    fragment_retries: Optional[int] = DEFAULT_FRAGMENT_RETRIES
    stall_minutes: int = DEFAULT_STALL_MINUTES
    token: str = field(default="", repr=False)
    allowed_chats: frozenset = frozenset()
    allowed_hosts: tuple = YOUTUBE_HOSTS
    max_parallel: int = DEFAULT_MAX_PARALLEL
    max_queued: int = DEFAULT_MAX_QUEUED
    audio_lang: Optional[str] = None

    def live_options(self) -> LiveOptions:
        """The live options this configuration asks for."""
        return LiveOptions(
            fragment_retries=self.fragment_retries, stall_minutes=self.stall_minutes
        )

    def download_options(
        self,
        url: str,
        *,
        audio_only: bool = False,
        cookie_health: Optional[CookieHealthStore] = None,
    ) -> DownloadOptions:
        """What :func:`core.downloader.run_download` needs for *url*: best
        quality (or MP3), the title and video id as the file name, so two
        videos with one title do not skip each other as "already downloaded"."""
        quality = "5" if audio_only else "1"
        return DownloadOptions(
            url=url,
            format_opt=presets.format_for(quality),
            output_template=presets.output_template_for(str(self.output_dir), "4"),
            auth=self.auth,
            audio_opts=presets.audio_opts_for(quality),
            live_opts=self.live_options(),
            audio_lang=self.audio_lang,
            cookie_health=cookie_health,
        )

    def describe(self) -> str:
        return (
            f"BotConfig(method={self.auth_method!r}, auth={self.auth.describe()!r}, "
            f"cookie_file={self.cookie_file}, bgutil_url={self.bgutil_url}, "
            f"output_dir={self.output_dir}, fragment_retries={self.fragment_retries}, "
            f"stall_minutes={self.stall_minutes}, token={'set' if self.token else 'missing'}, "
            f"allowed_chats={len(self.allowed_chats)}, allowed_hosts={list(self.allowed_hosts)}, "
            f"max_parallel={self.max_parallel}, max_queued={self.max_queued}, "
            f"audio_lang={self.audio_lang or 'original'})"
        )


def config_from_env() -> BotConfig:
    """Build a :class:`BotConfig` from the process environment.

    Environment variables:
        ``YTDLP_AUTH_METHOD`` — one of ``"bgutil"`` (default),
            ``"cookie-file"``, ``"none"``. Unknown values fall back to
            ``"bgutil"`` with a warning.
        ``YTDLP_COOKIE_FILE`` — path to cookies.txt; consulted when
            ``YTDLP_AUTH_METHOD == "cookie-file"``. Defaults to
            ``cookies.txt`` in the current working directory.
        ``YTDLP_GETPOT_BGUTIL_BASEURL`` — base URL of the bgutil
            sidecar; consulted when ``YTDLP_AUTH_METHOD == "bgutil"``.
            Defaults to ``http://127.0.0.1:4416``.
        ``YTDLP_OUTPUT_DIR`` — output directory. Defaults to
            ``downloads`` (relative to CWD).
        ``YTDLP_FRAGMENT_RETRIES`` — retries per live fragment: a
            non-negative integer (``0`` = none) or ``infinite``. Unset or
            empty means 10. Anything else raises ``ValueError``: a typo
            must not quietly become 0 or infinite.
        ``YTDLP_STALL_MINUTES`` — minutes without new data before a live
            recording is stopped: an integer up to 1440, ``0`` = never. Unset
            or empty means 5. Anything else raises ``ValueError``.
        ``YTDLP_BOT_TOKEN`` — the Telegram bot token (from @BotFather).
            Required to run the bot, not to load the config.
        ``YTDLP_BOT_ALLOWED_CHATS`` — comma-separated chat ids the bot
            serves (a group's id is negative). Required to run the bot; a
            non-integer raises ``ValueError``.
        ``YTDLP_BOT_ALLOWED_HOSTS`` — comma-separated hosts a link may point
            to (sub-domains match), ``*`` for any. Unset or empty means YouTube.
        ``YTDLP_BOT_MAX_PARALLEL`` — downloads at once, 1..5. Default 2.
        ``YTDLP_BOT_MAX_QUEUED`` — downloads that may wait, 1..100. Default 20.
        ``YTDLP_AUDIO_LANG`` — preferred audio-track language of a dubbed
            video, e.g. ``ko``; ``original`` keeps the original track. Unset or
            empty means ``original`` (the GUI's default is ``ko``, 한국어 우선).
    """
    method = (os.environ.get("YTDLP_AUTH_METHOD") or "bgutil").strip().lower()
    cookie_file_env = os.environ.get("YTDLP_COOKIE_FILE")
    bgutil_url_env = os.environ.get("YTDLP_GETPOT_BGUTIL_BASEURL", _DEFAULT_BGUTIL_URL)
    output_dir = Path(os.environ.get("YTDLP_OUTPUT_DIR", "downloads"))
    retries_env = (os.environ.get("YTDLP_FRAGMENT_RETRIES") or "").strip()
    try:
        fragment_retries = (
            parse_fragment_retries(retries_env) if retries_env else DEFAULT_FRAGMENT_RETRIES
        )
    except ValueError as exc:
        raise ValueError(f"YTDLP_FRAGMENT_RETRIES: {exc}") from None
    stall_env = (os.environ.get("YTDLP_STALL_MINUTES") or "").strip()
    try:
        stall_minutes = parse_stall_minutes(stall_env) if stall_env else DEFAULT_STALL_MINUTES
    except ValueError as exc:
        raise ValueError(f"YTDLP_STALL_MINUTES: {exc}") from None

    def setting(name: str, parse: Callable[[str], object], default: object) -> object:
        text = (os.environ.get(name) or "").strip()
        if not text:
            return default
        try:
            return parse(text)
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from None

    allowed_chats = setting("YTDLP_BOT_ALLOWED_CHATS", parse_chat_ids, frozenset())
    allowed_hosts = setting("YTDLP_BOT_ALLOWED_HOSTS", parse_hosts, YOUTUBE_HOSTS)
    max_parallel = setting(
        "YTDLP_BOT_MAX_PARALLEL", lambda t: _bounded_int(t, 1, MAX_PARALLEL_LIMIT), DEFAULT_MAX_PARALLEL
    )
    max_queued = setting(
        "YTDLP_BOT_MAX_QUEUED", lambda t: _bounded_int(t, 1, MAX_QUEUED_LIMIT), DEFAULT_MAX_QUEUED
    )
    audio_lang = setting(
        "YTDLP_AUDIO_LANG",
        lambda t: None if t.lower() in ("original", "none") else presets.normalize_audio_lang(t),
        None,
    )

    cookie_file: Optional[Path] = None
    bgutil_url: Optional[str] = None
    auth: Authenticator

    if method == "none":
        auth = NoAuth()
    elif method == "cookie-file":
        cookie_file = Path(cookie_file_env or "cookies.txt")
        auth = CookieFileAuth(path=str(cookie_file))
    elif method == "bgutil":
        bgutil_url = bgutil_url_env
        auth = BgutilPoTokenAuth(provider_url=bgutil_url)
    else:
        logger.warning(
            "Unknown YTDLP_AUTH_METHOD=%r; falling back to bgutil", method
        )
        method = "bgutil"
        bgutil_url = bgutil_url_env
        auth = BgutilPoTokenAuth(provider_url=bgutil_url)

    return BotConfig(
        auth=auth,
        auth_method=method,
        cookie_file=cookie_file,
        bgutil_url=bgutil_url,
        output_dir=output_dir,
        fragment_retries=fragment_retries,
        stall_minutes=stall_minutes,
        token=(os.environ.get("YTDLP_BOT_TOKEN") or "").strip(),
        allowed_chats=allowed_chats,  # type: ignore[arg-type]
        allowed_hosts=allowed_hosts,  # type: ignore[arg-type]
        max_parallel=max_parallel,  # type: ignore[arg-type]
        max_queued=max_queued,  # type: ignore[arg-type]
        audio_lang=audio_lang,  # type: ignore[arg-type]
    )


# --- One download, synchronous ------------------------------------------------


def handle_url(
    url: str,
    *,
    auth: Optional[Authenticator] = None,
    output_dir: Path = Path("downloads"),
    transcriber: Optional[Transcriber] = None,
) -> Path:
    """Download *url* and return the path of the saved media file.

    The simple, blocking form of what the bot does for a message (no progress,
    no cancel); for a script. Raises ``RuntimeError`` when the download fails.

    Args:
        url: YouTube URL to process.
        auth: :class:`Authenticator` strategy (cookie file / browser /
            bgutil PO Token / none). ``None`` means no authentication.
        output_dir: Directory that will hold the downloaded file.
        transcriber: Reserved for the Whisper step (Phase D'); passing one
            raises ``NotImplementedError`` rather than being ignored.

    Returns:
        Path of the downloaded media file.
    """
    if transcriber is not None:
        raise NotImplementedError("transcription is not implemented yet (Phase D')")
    config = BotConfig(
        auth=auth or NoAuth(), auth_method="", cookie_file=None, bgutil_url=None,
        output_dir=output_dir,
    )
    result = run_download(url, config.download_options(url))
    if not result.ok or not result.files:
        raise RuntimeError(f"download failed ({result.failure_kind or 'unknown cause'}): {url}")
    return Path(result.files[0]["filepath"])


# --- The service ----------------------------------------------------------------


_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

_HELP = (
    "YouTube 링크를 보내면 받아 둡니다.\n"
    "• 링크만 보내기, 또는 /video <링크>: 영상(최고 화질)\n"
    "• /audio <링크>: 오디오(MP3)\n"
    "• /status: 받는 중·대기 중인 작업\n"
    "• /cancel [#번호]: 작업 취소 (번호가 없으면 이 채팅의 전부)\n"
    "한 번에 최대 {n}개까지 링크를 보낼 수 있습니다."
)


def extract_urls(text: str) -> list[str]:
    """The ``http(s)`` links in *text*, in order, without trailing punctuation
    and without repeats."""
    seen: dict[str, None] = {}
    for match in _URL_RE.finditer(text):
        seen.setdefault(match.group(0).rstrip(".,;:!?)]}>"), None)
    return list(seen)


def _split_command(text: str) -> tuple[Optional[str], str, Optional[str]]:
    """``"/audio@MyBot  https://..."`` -> ``("audio", "https://...", "mybot")``;
    ``(None, text, None)`` when *text* is not a command."""
    text = text.strip()
    if not text.startswith("/"):
        return None, text, None
    head, _, rest = text.partition(" ")
    name, _, addressee = head[1:].partition("@")
    return name.lower(), rest.strip(), addressee.lower() or None


@dataclass(eq=False)
class BotJob:
    """One requested download."""

    id: int
    chat_id: int
    reply_to: Optional[int]
    url: str
    audio_only: bool = False
    state: str = "queued"  # queued / running / done / failed / cancelled
    message_id: Optional[int] = None  # the status message that is edited
    cancel_event: threading.Event = field(default_factory=threading.Event)
    label: str = ""
    errors: list = field(default_factory=list)  # the run's error-level log lines
    last_text: str = ""
    last_edit: float = float("-inf")
    blocked_until: float = float("-inf")

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")


@dataclass(eq=False)
class _Pending:
    """A message Telegram did not take yet. *job* is set for a job's status
    message (sent or edited), else it is a plain message to *chat_id*."""

    job: Optional[BotJob]
    chat_id: int
    text: str
    reply_to: Optional[int]
    created: float
    due: float
    attempts: int = 0


class BotService:
    """The bot's behaviour, separate from Telegram's transport so it can be
    driven by a fake client.

    Args:
        config: What to serve and how to download.
        client: A :class:`core.telegram.TelegramClient` (or a stand-in with
            ``send_message``, ``edit_message_text``, ``get_updates``, ``get_me``).
        health_store: Where an expired login is recorded
            (:class:`core.cookie_health.CookieHealthStore`); ``None`` records
            nothing and sends no cookie alerts.
        download: Replaces :func:`core.downloader.run_download` (tests).
        clock: Replaces ``time.monotonic`` (tests).
    """

    def __init__(
        self,
        config: BotConfig,
        client: TelegramClient,
        *,
        health_store: Optional[CookieHealthStore] = None,
        download: Optional[Callable[..., DownloadResult]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.client = client
        self.health_store = health_store
        self._download = download
        self._clock = clock
        self.username: Optional[str] = None
        self._lock = threading.Lock()
        self._jobs: dict[int, BotJob] = {}
        self._next_id = 1
        self._queue: "collections.deque[Optional[BotJob]]" = collections.deque()
        self._wake = threading.Condition(self._lock)
        self._workers: list[threading.Thread] = []
        self._refused: set[int] = set()
        # Messages that must arrive but did not yet (see _keep_for_later).
        self._outbox: list[_Pending] = []
        self._flush_lock = threading.Lock()
        self._outbox_stop = threading.Event()
        self._outbox_thread: Optional[threading.Thread] = None

    # --- Telegram -> actions --------------------------------------------------

    def handle_update(self, update: dict) -> None:
        """React to one ``getUpdates`` item (the polling thread)."""
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            return
        text = message.get("text")
        if not isinstance(text, str):
            return  # photos, stickers, ...
        reply_to = message.get("message_id")
        if chat_id not in self.config.allowed_chats:
            self._refuse(chat_id, reply_to)
            return

        command, rest, addressee = _split_command(text)
        if addressee and self.username and addressee != self.username:
            return  # "/audio@OtherBot": meant for another bot in the group
        if command in ("start", "help"):
            self._reply(chat_id, reply_to, _HELP.format(n=MAX_URLS_PER_MESSAGE))
        elif command == "status":
            self._reply(chat_id, reply_to, self._status_text(chat_id))
        elif command == "cancel":
            self._cancel(chat_id, reply_to, rest)
        elif command in ("audio", "video", None):
            self._request(chat_id, reply_to, rest, audio_only=command == "audio")
        else:
            self._reply(chat_id, reply_to, f"알 수 없는 명령입니다: /{command}\n/help 를 보세요.")

    def _refuse(self, chat_id: int, reply_to: Optional[int]) -> None:
        """A chat that is not on the list: answered once, with its id."""
        with self._lock:
            if chat_id in self._refused or len(self._refused) >= _MAX_REFUSED:
                return
            self._refused.add(chat_id)
        logger.warning("message from a chat that is not allowed: %s", chat_id)
        self._reply(
            chat_id, reply_to,
            f"허용되지 않은 채팅입니다 (chat id: {chat_id}). "
            "봇 운영자가 YTDLP_BOT_ALLOWED_CHATS 에 이 id 를 넣어야 합니다.",
        )

    def _request(self, chat_id: int, reply_to: Optional[int], text: str, *, audio_only: bool) -> None:
        urls = extract_urls(text)
        if not urls:
            self._reply(chat_id, reply_to, "받을 링크가 없습니다. YouTube 링크를 보내 주세요 (/help).")
            return
        for url in urls[:MAX_URLS_PER_MESSAGE]:
            problem = self._url_problem(url)
            if problem:
                self._reply(chat_id, reply_to, f"✗ {problem}: {url}")
                continue
            if not self._submit(chat_id, reply_to, url, audio_only):
                break  # the queue is full
        if len(urls) > MAX_URLS_PER_MESSAGE:
            self._reply(chat_id, reply_to, f"링크는 한 번에 {MAX_URLS_PER_MESSAGE}개까지만 받습니다. 나머지는 무시했습니다.")

    def _url_problem(self, url: str) -> str:
        """Why *url* is refused; ``""`` when it is fine."""
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
        except ValueError:
            return "링크를 읽을 수 없습니다"
        allowed = self.config.allowed_hosts
        if "*" in allowed:
            return "" if host else "링크를 읽을 수 없습니다"
        if any(host == name or host.endswith("." + name) for name in allowed):
            return ""
        return "허용되지 않은 사이트입니다"

    def _submit(self, chat_id: int, reply_to: Optional[int], url: str, audio_only: bool) -> bool:
        """Queue one download. ``False`` when the queue is full."""
        job: Optional[BotJob] = None
        with self._lock:
            duplicate = next((j.id for j in self._jobs.values() if j.active and j.url == url), 0)
            waiting = sum(1 for j in self._jobs.values() if j.state == "queued")
            full = not duplicate and waiting >= self.config.max_queued
            if not duplicate and not full:
                self._forget_old_jobs()
                job = BotJob(self._next_id, chat_id, reply_to, url, audio_only)
                self._next_id += 1
                self._jobs[job.id] = job
        if duplicate:
            self._reply(chat_id, reply_to, f"이미 받는 중이거나 대기 중입니다 (#{duplicate}): {url}")
            return True
        if job is None:  # full
            self._reply(chat_id, reply_to, f"대기열이 가득 찼습니다 (최대 {self.config.max_queued}개). 잠시 뒤 다시 보내 주세요.")
            return False
        # The status message first, so a worker that starts at once can edit it.
        self._say(job, f"⏳ 대기 중 #{job.id}: {url}", force=True)
        with self._lock:
            self._queue.append(job)
            self._wake.notify()
        return True

    def _forget_old_jobs(self) -> None:
        """Keep only the newest finished jobs, so a long-running bot does not
        grow without bound (the caller holds the lock)."""
        finished = sorted(j.id for j in self._jobs.values() if not j.active)
        for job_id in finished[: max(0, len(finished) - _KEEP_FINISHED)]:
            del self._jobs[job_id]

    def _status_text(self, chat_id: int) -> str:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.chat_id == chat_id and j.active]
        if not jobs:
            return "받고 있거나 대기 중인 작업이 없습니다."
        return "\n".join(
            f"#{j.id} {'받는 중' if j.state == 'running' else '대기 중'}"
            f"{' · 오디오' if j.audio_only else ''} {j.url}"
            for j in sorted(jobs, key=lambda j: j.id)
        )

    def _cancel(self, chat_id: int, reply_to: Optional[int], rest: str) -> None:
        with self._lock:
            mine = [j for j in self._jobs.values() if j.chat_id == chat_id and j.active]
        if rest.strip():
            try:
                wanted = int(rest.strip().lstrip("#"))
            except ValueError:
                self._reply(chat_id, reply_to, "사용법: /cancel [#번호]")
                return
            mine = [j for j in mine if j.id == wanted]
            if not mine:
                self._reply(chat_id, reply_to, f"#{wanted} 는 이 채팅에서 받는 중이거나 대기 중인 작업이 아닙니다.")
                return
        elif not mine:
            self._reply(chat_id, reply_to, "취소할 작업이 없습니다.")
            return
        for job in mine:
            self._cancel_job(job)

    def _cancel_job(self, job: BotJob) -> None:
        """A waiting job ends at once; a running one is stopped (its worker
        then reports it)."""
        with self._lock:
            waiting = job.state == "queued"
            if waiting:
                job.state = "cancelled"
        job.cancel_event.set()
        if waiting:
            self._say(job, f"✗ 취소됨 #{job.id} (대기 중)", force=True, final=True)

    # --- Telegram out ------------------------------------------------------------

    def _reply(self, chat_id: int, reply_to: Optional[int], text: str) -> None:
        """A message that is not a job's status; a failure is only logged."""
        try:
            self.client.send_message(chat_id, text, reply_to=reply_to)
        except TelegramError as exc:
            logger.warning("could not send a message to %s: %s", chat_id, exc)

    def _say(self, job: BotJob, text: str, *, force: bool = False, final: bool = False) -> None:
        """Show *text* as the job's status message: send it the first time,
        edit it afterwards. Progress is throttled; the first message and the
        end are *force*d. Telegram trouble never fails a download.

        A *final* message (how the download ended) is what the user waits for,
        so when Telegram will not take it for a reason that may pass (no
        connection, flood control, a server error) it is kept and sent again
        later instead of being lost (:meth:`_keep_for_later`)."""
        now = self._clock()
        if not force and (
            text == job.last_text
            or now - job.last_edit < PROGRESS_INTERVAL
            or now < job.blocked_until
        ):
            return
        try:
            self._deliver_status(job, text, anew=force)
            job.last_text, job.last_edit = text, now
        except TelegramError as exc:
            job.blocked_until = now + (exc.retry_after or EDIT_COOLDOWN)
            logger.warning("could not update the status of #%s: %s", job.id, exc)
            if final and self._retryable(exc):
                self._keep_for_later(job, job.chat_id, text, job.reply_to)

    def _deliver_status(self, job: BotJob, text: str, *, anew: bool = False) -> None:
        """Send *text* as the job's status message, or edit the one already
        there. With *anew*, a failed edit falls back to a new message (the old
        one may have been deleted). Raises :class:`TelegramError`."""
        if job.message_id is None:
            self._send_status(job, text)
            return
        try:
            self.client.edit_message_text(job.chat_id, job.message_id, text)
        except TelegramError:
            if not anew:
                raise
            self._send_status(job, text)

    def _send_status(self, job: BotJob, text: str) -> None:
        """Send *text* as the job's (new) status message."""
        sent = self.client.send_message(job.chat_id, text, reply_to=job.reply_to)
        job.message_id = sent.get("message_id") if isinstance(sent, dict) else None

    def _notify_operators(self, text: str) -> None:
        """A message every allowed chat must get, once (the cookie alert is
        never repeated): sent again later when Telegram will not take it now."""
        for chat_id in sorted(self.config.allowed_chats):
            try:
                self.client.send_message(chat_id, text)
            except TelegramError as exc:
                logger.warning("could not send a message to %s: %s", chat_id, exc)
                if self._retryable(exc):
                    self._keep_for_later(None, chat_id, text, None)

    # --- messages that must arrive ----------------------------------------------------

    @staticmethod
    def _retryable(exc: TelegramError) -> bool:
        """Whether a failed send may succeed later: no connection, flood
        control or a Telegram server error. Not a refusal (the user blocked the
        bot, no such chat, a bad request) or a bad token."""
        if exc.fatal:
            return False
        return exc.code is None or exc.code == 429 or exc.code >= 500

    def _keep_for_later(
        self, job: Optional[BotJob], chat_id: int, text: str, reply_to: Optional[int]
    ) -> None:
        """Keep a message Telegram would not take, for the outbox thread to
        send again. A newer message for the same job replaces the older one."""
        now = self._clock()
        with self._lock:
            if job is not None:
                self._outbox[:] = [p for p in self._outbox if p.job is not job]
            if len(self._outbox) >= OUTBOX_MAX:
                lost = self._outbox.pop(0)
                logger.error("too many undelivered messages: dropping the oldest (to chat %s)", lost.chat_id)
            self._outbox.append(
                _Pending(job, chat_id, text, reply_to, created=now, due=now + OUTBOX_RETRY_DELAYS[0])
            )
        logger.warning("will keep trying to deliver a message to chat %s", chat_id)

    def _flush_outbox(self, *, everything: bool = False) -> None:
        """Try again the kept messages that are due. With *everything* (the last
        try before the bot exits) all are tried, but only until one fails: the
        rest would only wait out the same dead connection."""
        with self._flush_lock:
            now = self._clock()
            with self._lock:
                due = [p for p in self._outbox if everything or p.due <= now]
            for pending in due:
                try:
                    if pending.job is not None:
                        self._deliver_status(pending.job, pending.text, anew=True)
                        pending.job.last_text, pending.job.last_edit = pending.text, now
                    else:
                        self.client.send_message(pending.chat_id, pending.text, reply_to=pending.reply_to)
                except TelegramError as exc:
                    pending.attempts += 1
                    if not self._retryable(exc) or now - pending.created > OUTBOX_GIVE_UP:
                        logger.error("giving up on a message to chat %s: %s", pending.chat_id, exc)
                        self._forget(pending)
                    else:
                        step = OUTBOX_RETRY_DELAYS[min(pending.attempts, len(OUTBOX_RETRY_DELAYS) - 1)]
                        pending.due = now + max(exc.retry_after or 0.0, step)
                    if everything:
                        break
                    continue
                logger.info("delivered a message to chat %s that had to wait", pending.chat_id)
                self._forget(pending)

    def _forget(self, pending: _Pending) -> None:
        with self._lock:
            if pending in self._outbox:
                self._outbox.remove(pending)

    def _outbox_loop(self) -> None:
        while not self._outbox_stop.wait(OUTBOX_POLL):
            try:
                self._flush_outbox()
            except Exception:  # never let the outbox thread die
                logger.exception("could not retry the undelivered messages")

    # --- downloads -----------------------------------------------------------------

    def start(self) -> None:
        """Start the worker threads and the one that re-sends kept messages."""
        for number in range(self.config.max_parallel):
            worker = threading.Thread(target=self._work, name=f"bot-worker-{number + 1}", daemon=True)
            worker.start()
            self._workers.append(worker)
        self._outbox_thread = threading.Thread(target=self._outbox_loop, name="bot-outbox", daemon=True)
        self._outbox_thread.start()

    def _work(self) -> None:
        while True:
            with self._lock:
                while not self._queue:
                    self._wake.wait()
                job = self._queue.popleft()
                if job is None:
                    return  # stop sentinel
                if job.state != "queued":
                    continue  # cancelled while it waited
                job.state = "running"
            try:
                self._run_job(job)
            except Exception:  # never let a worker die
                logger.exception("download #%s crashed", job.id)

    def _run_job(self, job: BotJob) -> None:
        self._say(job, f"⏬ 받는 중 #{job.id}: {job.url}", force=True)
        result: Optional[DownloadResult] = None
        error: Optional[Exception] = None
        try:
            opts = self.config.download_options(
                job.url, audio_only=job.audio_only, cookie_health=self.health_store
            )
            download = self._download or run_download
            result = download(
                job.url,
                opts,
                progress_callback=lambda info: self._on_progress(job, info),
                log_callback=lambda line, level: self._on_log(job, line, level),
                cancel_event=job.cancel_event,
            )
        except Exception as exc:  # run_download raises for a missing yt-dlp, ...
            logger.exception("download #%s failed", job.id)
            error = exc
        state, text = self._outcome(job, result, error)
        with self._lock:
            job.state = state
        self._say(job, text, force=True, final=True)
        if result is not None:
            self._cookie_alert(result)

    def _on_progress(self, job: BotJob, info: dict) -> None:
        stream = info.get("stream", 0)
        label = f"[스트림 {stream + 1}] " if stream else ""
        head = f"⏬ #{job.id} {job.label}".rstrip()
        # yt-dlp does not always know the speed or the time left ("N/A", "--:--"): say only
        # what is known, instead of showing the placeholders.
        speed = info.get("speed")
        eta = info.get("eta")
        known_speed = speed if speed not in (None, "", "N/A") else ""
        known_eta = f"ETA {eta}" if eta not in (None, "", "--:--") else ""
        if info.get("percent_known", True):
            parts = [f"{float(info.get('percentage', 0)):.0f}%", info.get("total_size"), known_speed, known_eta]
            line = f"{head} {label}{' · '.join(str(p) for p in parts if p and p != 'N/A')}"
        else:
            downloaded = info.get("downloaded")
            parts = [f"{downloaded} 받음" if downloaded not in (None, "", "N/A") else "", known_speed]
            detail = " · ".join(p for p in parts if p)
            line = f"{head} {label}{detail} (라이브)" if detail else f"{head} {label}(라이브)"
        self._say(job, f"{line}\n{job.url}")

    @staticmethod
    def _on_log(job: BotJob, line: str, level: str) -> None:
        if level == "error":
            job.errors.append(line)
            del job.errors[:-_KEEP_ERRORS]
        if "LIVE 감지" in line:
            job.label = "\U0001f534 라이브"
        elif "예정 라이브" in line:
            job.label = "⏰ 예정 대기"

    def _outcome(
        self, job: BotJob, result: Optional[DownloadResult], error: Optional[Exception]
    ) -> tuple[str, str]:
        """``(state, final message)`` of a finished job."""
        tag = f"#{job.id}"
        if error is not None:
            return "failed", f"✗ 오류 {tag}: {error}"
        assert result is not None
        if result.cancelled:
            saved = [f for f in result.files if f.get("partial")]
            extra = f"\n받은 부분은 '{Path(saved[0]['filepath']).name}' 로 저장했습니다." if saved else ""
            return "cancelled", f"✗ 취소됨 {tag}{extra}"
        if result.skipped == "busy":
            return "failed", f"✗ 같은 영상(또는 같은 파일명)을 이미 받는 중이라 시작하지 않았습니다 {tag}"
        if result.ok:
            lines = [f"✓ 완료 {tag}"]
            if result.title:
                lines.append(result.title)
            if result.files:
                lines.append(f"저장: {result.files[0].get('filepath', '')}")
            if result.warnings:
                lines.append("⚠ 라이브 조기 종료 가능 — 녹화 길이를 확인하세요")
            return "done", "\n".join(lines)
        try:
            kind = FailureKind(result.failure_kind)
        except ValueError:
            kind = FailureKind.OTHER
        uses_cookies = isinstance(self.config.auth, (CookieFileAuth, BrowserCookieAuth))
        lines = [f"✗ 실패 {tag}"]
        hint = failure_hint(kind, uses_cookies=uses_cookies)
        if hint:
            lines.append(hint)
        errors = self._last_errors(job)
        if errors:
            lines.append("마지막 오류:")
            lines.extend(f"• {text}" for text in errors)
        if result.log_path:
            lines.append(f"실행 로그: {result.log_path}")
        return "failed", "\n".join(lines)

    @staticmethod
    def _last_errors(job: BotJob) -> list[str]:
        """The last few error lines of the run for a failure message: what yt-dlp
        said, not the downloader's own summary lines ("다운로드 실패.", the exit
        code), which tell nothing; each cut short, none repeated."""
        shown: list[str] = []
        for line in job.errors:
            text = line.strip()
            if not text or text == "다운로드 실패." or text.startswith("yt-dlp exit code"):
                continue
            text = text[:_ERROR_LINE_CHARS]
            if text not in shown:
                shown.append(text)
        return shown[-_ERROR_LINES:]

    def _cookie_alert(self, result: DownloadResult) -> None:
        """Tell the operators once when the login cookies expire (or are
        replaced): ``cookie_event`` is set only on that run."""
        if result.cookie_event == "invalid":
            self._notify_operators(
                "⚠️ YouTube 쿠키의 로그인이 만료된 것으로 확인되었습니다.\n"
                "로그인이 필요한 영상(멤버 전용 등)은 받지 못합니다. 공개 영상은 쿠키 없이도 받아질 수 있습니다.\n"
                "쿠키 파일을 다시 받아 교체하세요 (이 봇이 도는 곳의 cookies.txt)."
            )
        elif result.cookie_event == "recovered":
            self._notify_operators("✓ 새 쿠키가 확인되었습니다. 쿠키 만료 기록을 초기화했습니다.")

    def stop(self) -> None:
        """Cancel what is running, drop what waits and end the workers, then make
        one last try at the messages that are still waiting to be delivered."""
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.active]
        for job in jobs:
            self._cancel_job(job)
        with self._lock:
            for _ in self._workers:
                self._queue.append(None)
            self._wake.notify_all()
        for worker in self._workers:
            worker.join(timeout=30)
        self._outbox_stop.set()
        if self._outbox_thread is not None:
            self._outbox_thread.join(timeout=5)
        if self._outbox:
            self._flush_outbox(everything=True)
            if self._outbox:
                logger.warning("%d message(s) could not be delivered before the bot stopped", len(self._outbox))

    # --- the polling loop ---------------------------------------------------------------

    def run(self, stop: threading.Event) -> int:
        """Poll Telegram until *stop* is set (or Ctrl+C). Returns the exit code:
        0 after a normal stop, 2 when Telegram refuses the token."""
        try:
            me = self.client.get_me()
        except TelegramError as exc:
            logger.error("cannot reach Telegram with this token: %s", exc)
            return 2
        self.username = str(me.get("username") or "").lower() or None
        logger.info("connected as @%s; serving %d chat(s)", self.username, len(self.config.allowed_chats))
        self.start()
        offset: Optional[int] = None
        backoff = 1.0
        try:
            while not stop.is_set():
                try:
                    updates = self.client.get_updates(offset, timeout=POLL_SECONDS)
                except TelegramError as exc:
                    if exc.fatal:
                        logger.error("Telegram refused the bot: %s", exc)
                        if exc.code == 409:
                            logger.error(
                                "Only one program can receive this bot's messages: another copy of "
                                "the bot is polling it, or a webhook is set for it (the bot does "
                                "not delete a webhook by itself: it may be someone else's)."
                            )
                        return 2
                    logger.warning("polling failed (%s); retrying in %.0fs", exc, backoff)
                    stop.wait(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
                backoff = 1.0
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    try:
                        self.handle_update(update)
                    except Exception:  # one bad message must not stop the bot
                        logger.exception("could not handle an update")
        except KeyboardInterrupt:
            logger.info("stopping")
        finally:
            self.stop()
        return 0


# A variable whose name contains one of these holds a secret: shown as "<set>", never its value.
_SECRET_NAME_WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "KEY", "CREDENTIAL")
_URL_USERINFO = re.compile(r"(://)[^/@\s]+@")
_ID_SEPARATORS = re.compile(r"[,;\s]+")


def environment_lines(environ: Optional[Mapping[str, str]] = None) -> list[str]:
    """The ``YTDLP_*`` variables this process sees, as ``NAME=value`` lines for the console.

    Only the app's own namespace: the rest of the environment (PATH, API keys of other
    tools ...) is none of the log's business. A secret (a name with TOKEN, SECRET, KEY ...
    in it) shows as ``<set>``; the chat ids of the allow-list show as a count, like
    :meth:`BotConfig.describe`; a user name or password inside a URL is cut out.
    Variables the bot does not know are listed too, so a mistyped name is visible."""
    environ = os.environ if environ is None else environ
    lines = []
    for name in sorted(n for n in environ if n.upper().startswith("YTDLP_")):
        value = (environ[name] or "").strip()
        upper = name.upper()
        if not value:
            shown = "<empty>"
        elif any(word in upper for word in _SECRET_NAME_WORDS):
            shown = "<set>"
        elif upper == "YTDLP_BOT_ALLOWED_CHATS":
            ids = [part for part in re.split(_ID_SEPARATORS, value) if part]
            shown = f"<{len(ids)} chat id(s)>"
        else:
            shown = _URL_USERINFO.sub(r"\1***@", value)
        lines.append(f"{name}={shown}")
    return lines


def run_bot() -> int:
    """Entry point for ``python main.py --bot``.

    Loads :class:`BotConfig` from the environment and serves Telegram until
    Ctrl+C / SIGTERM. Exit code 2 when the configuration is wrong (a bad value,
    no token, no allow-list) or Telegram refuses the token.
    """
    # Keep output terse but informative even when logging is not
    # configured by the host process.
    logging.basicConfig(level=logging.INFO, format="[bot] %(message)s")
    # First, so a console (or a pasted log) says which build is running even when the
    # configuration is refused. From source it is the commit at start, "-dirty" if edited.
    logger.info("version: %s", app_version())
    # Before the config is read, so a refused value or a mistyped name shows next to its cause.
    env = environment_lines()
    logger.info("environment: %s", "YTDLP_* only, secrets hidden" if env else "no YTDLP_* variables set")
    for line in env:
        logger.info("  %s", line)

    try:
        cfg = config_from_env()
    except ValueError as exc:
        logger.error("invalid configuration: %s", exc)
        return 2
    logger.info("config loaded: %s", cfg.describe())

    if not cfg.token:
        logger.error(
            "YTDLP_BOT_TOKEN is not set. Create a bot with @BotFather and put its token in "
            "that environment variable (not on the command line)."
        )
        return 2
    if not cfg.allowed_chats:
        logger.error(
            "YTDLP_BOT_ALLOWED_CHATS is empty. The bot serves only the chat ids listed there "
            "(comma separated). Do not know yours yet? Set it to 0 for now, start the bot and "
            "message it: it answers a chat that is not listed with that chat's id."
        )
        return 2

    # Validation is a best-effort pre-flight. We don't gate on it
    # (headless boxes may not have the bgutil sidecar reachable at
    # import time) — we just report.
    try:
        result = cfg.auth.validate()
        level = logging.INFO if result.success else logging.WARNING
        marker = "✓" if result.success else "⚠️"
        logger.log(level, "%s auth.validate: %s", marker, result.message)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("auth.validate raised: %s", exc)

    service = BotService(cfg, TelegramClient(cfg.token), health_store=CookieHealthStore())
    stop = threading.Event()

    def on_term(_signum, _frame) -> None:
        stop.set()
        raise KeyboardInterrupt  # breaks the blocking poll, as Ctrl+C does

    try:
        previous = signal.signal(signal.SIGTERM, on_term)
    except ValueError:  # not the main thread (a test): no handler
        return service.run(stop)
    try:
        return service.run(stop)
    finally:
        signal.signal(signal.SIGTERM, previous)
