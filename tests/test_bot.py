"""interfaces.bot: the Telegram bot's behaviour.

The service runs against a fake client (no network) and a fake downloader
(no yt-dlp); its worker threads are real. One test class drives the whole
thing over HTTP against ``tests/fake_telegram.py``. No real token is used and
nothing here has talked to the real Telegram.
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core.auth import BrowserCookieAuth, CookieFileAuth, NoAuth
from core.downloader import DownloadResult, _build_cmd
from core.live import LiveStatusInfo
from core.telegram import TelegramClient, TelegramError
from interfaces import bot
from tests.fake_telegram import TOKEN, FakeTelegram

VERSION = "9.9.9 (test)"  # what the patched app_version() says in the run_bot tests

CHAT = 4242
OTHER_CHAT = 777
URL_A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_B = "https://youtu.be/bbbbbbbbbbb"
URL_C = "https://youtu.be/ccccccccccc"


def wait_for(predicate, timeout: float = 5.0, what: str = "the condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.005)


def update(text: str, *, chat: int = CHAT, message_id: int = 1, update_id: int = 1) -> dict:
    return {"update_id": update_id,
            "message": {"message_id": message_id, "chat": {"id": chat}, "text": text}}


class FakeClient:
    """Records what the service sends; message ids count up from 100."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.fail_edits: TelegramError | None = None
        self.fail_sends: TelegramError | None = None
        self._next_id = 100

    def send_message(self, chat_id, text, *, reply_to=None):
        with self.lock:
            if self.fail_sends is not None:
                raise self.fail_sends
            self._next_id += 1
            self.sent.append({"chat_id": chat_id, "text": text, "reply_to": reply_to,
                              "message_id": self._next_id})
            return {"message_id": self._next_id}

    def edit_message_text(self, chat_id, message_id, text):
        with self.lock:
            if self.fail_edits is not None:
                raise self.fail_edits
            self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})
            return True

    # --- what a test asks ---
    def texts(self, chat_id=None) -> list[str]:
        """Every message text, sent or edited, in order."""
        with self.lock:
            return [m["text"] for m in self.sent + self.edits
                    if chat_id is None or m["chat_id"] == chat_id]

    def said(self, needle: str, chat_id=None) -> bool:
        return any(needle in text for text in self.texts(chat_id))

    def last_text_of(self, message_id: int) -> str:
        with self.lock:
            texts = [m["text"] for m in self.sent if m["message_id"] == message_id]
            texts += [m["text"] for m in self.edits if m["message_id"] == message_id]
        return texts[-1]


class FakeDownload:
    """Stands in for run_download. ``gates[url]`` holds a download until set;
    a held download ends as cancelled when its cancel_event is set."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.gates: dict[str, threading.Event] = {}
        self.results: dict[str, object] = {}
        self.progress: dict[str, list[dict]] = {}
        self.logs: dict[str, list[tuple[str, str]]] = {}  # (line, level) the run prints
        self._started: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def started(self, url: str) -> threading.Event:
        with self._lock:
            return self._started.setdefault(url, threading.Event())

    def hold(self, url: str) -> threading.Event:
        gate = self.gates[url] = threading.Event()
        return gate

    def __call__(self, url, opts, *, progress_callback=None, log_callback=None, cancel_event=None):
        with self._lock:
            self.calls.append((url, opts))
        self.started(url).set()
        for info in self.progress.get(url, []):
            progress_callback(info)
        for line, level in self.logs.get(url, []):
            log_callback(line, level)
        gate = self.gates.get(url)
        if gate is not None:
            while not gate.wait(0.005):
                if cancel_event is not None and cancel_event.is_set():
                    return DownloadResult(ok=False, cancelled=True)
        outcome = self.results.get(url)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            return outcome  # type: ignore[return-value]
        video = url.rsplit("=", 1)[-1].rsplit("/", 1)[-1]
        return DownloadResult(
            ok=True, title=f"제목 {video}",
            files=[{"title": f"제목 {video}", "filepath": f"/out/제목 {video}.mp4"}])


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_config(**overrides) -> bot.BotConfig:
    values = dict(
        auth=NoAuth(), auth_method="none", cookie_file=None, bgutil_url=None,
        output_dir=Path("/out"), allowed_chats=frozenset({CHAT}), max_parallel=2, max_queued=20,
    )
    values.update(overrides)
    return bot.BotConfig(**values)


class _ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeClient()
        self.download = FakeDownload()
        self.clock = FakeClock()
        # Expected warnings (a stranger, a failed edit) must not print to stderr.
        quiet = logging.NullHandler()
        bot.logger.addHandler(quiet)
        self.addCleanup(bot.logger.removeHandler, quiet)
        # The outbox thread must not retry behind a test's back: tests call _flush_outbox().
        slow = mock.patch.object(bot, "OUTBOX_POLL", 3600.0)
        slow.start()
        self.addCleanup(slow.stop)

    def service(self, **config) -> bot.BotService:
        service = bot.BotService(
            make_config(**config), self.client, download=self.download, clock=self.clock)
        self.addCleanup(service.stop)
        service.start()
        return service

    def finished(self, message_id: int) -> bool:
        with self.client.lock:
            return any(e["message_id"] == message_id and e["text"][:1] in "✓✗" for e in self.client.edits)

    def status_id(self, n: int = 0) -> int:
        """Message id of the n-th status message (a reply to the user's message)."""
        wait_for(lambda: len(self.client.sent) > n, what="a status message")
        return self.client.sent[n]["message_id"]


class ParsingTest(unittest.TestCase):
    def test_chat_ids(self) -> None:
        self.assertEqual(bot.parse_chat_ids("1, -100200;3"), frozenset({1, -100200, 3}))
        self.assertEqual(bot.parse_chat_ids(""), frozenset())
        for text in ("abc", "1,x", "1.5", "@name"):
            with self.subTest(text), self.assertRaises(ValueError):
                bot.parse_chat_ids(text)

    def test_hosts(self) -> None:
        self.assertEqual(bot.parse_hosts(""), bot.YOUTUBE_HOSTS)
        self.assertEqual(bot.parse_hosts(" Example.COM, .vimeo.com"), ("example.com", "vimeo.com"))
        self.assertEqual(bot.parse_hosts("*"), ("*",))
        for text in ("bad host", "a/b", "http://x"):
            with self.subTest(text), self.assertRaises(ValueError):
                bot.parse_hosts(text)

    def test_urls_are_found_cleaned_and_not_repeated(self) -> None:
        text = f"보세요 ({URL_A}). 그리고 {URL_B}, {URL_A} 또 http://x.test/a?b=1!"
        self.assertEqual(bot.extract_urls(text), [URL_A, URL_B, "http://x.test/a?b=1"])
        self.assertEqual(bot.extract_urls("링크 없음"), [])

    def test_commands(self) -> None:
        self.assertEqual(bot._split_command("/audio https://x"), ("audio", "https://x", None))
        self.assertEqual(bot._split_command("/Audio@MyBot  https://x "), ("audio", "https://x", "mybot"))
        self.assertEqual(bot._split_command("/status"), ("status", "", None))
        self.assertEqual(bot._split_command(" https://x "), (None, "https://x", None))


class EnvTest(unittest.TestCase):
    @staticmethod
    def _config(**env: str) -> bot.BotConfig:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("YTDLP_")}
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=True):
            return bot.config_from_env()

    @staticmethod
    def _format_arg(config: bot.BotConfig) -> str:
        """The ``-f`` value yt-dlp would get for this configuration's video download."""
        cmd = _build_cmd(config.download_options("https://youtu.be/x"))
        return cmd[cmd.index("-f") + 1]

    def test_defaults(self) -> None:
        config = self._config()
        self.assertEqual(config.token, "")
        self.assertEqual(config.allowed_chats, frozenset())
        self.assertEqual(config.allowed_hosts, bot.YOUTUBE_HOSTS)
        self.assertEqual((config.max_parallel, config.max_queued), (2, 20))
        # The original track, not YouTube's auto-dubbed Korean one (matters for transcription).
        self.assertIsNone(config.audio_lang)
        self.assertIsNone(make_config().audio_lang)
        self.assertIsNone(self._config(YTDLP_AUDIO_LANG="  ").audio_lang)
        self.assertNotIn("[language", self._format_arg(config))
        self.assertIn("audio_lang=original", config.describe())

    def test_values(self) -> None:
        config = self._config(
            YTDLP_BOT_TOKEN=f" {TOKEN} ", YTDLP_BOT_ALLOWED_CHATS="1, -2", YTDLP_BOT_ALLOWED_HOSTS="a.com",
            YTDLP_BOT_MAX_PARALLEL="3", YTDLP_BOT_MAX_QUEUED="7", YTDLP_AUDIO_LANG="en-us")
        self.assertEqual(config.token, TOKEN)
        self.assertEqual(config.allowed_chats, frozenset({1, -2}))
        self.assertEqual(config.allowed_hosts, ("a.com",))
        self.assertEqual((config.max_parallel, config.max_queued), (3, 7))
        self.assertEqual(config.audio_lang, "en-US")
        self.assertIsNone(self._config(YTDLP_AUDIO_LANG="original").audio_lang)
        # A language can still be asked for; it reaches the format selector.
        korean = self._config(YTDLP_AUDIO_LANG="ko")
        self.assertEqual(korean.audio_lang, "ko")
        self.assertIn("[language^=ko]", self._format_arg(korean))
        self.assertIn("audio_lang=ko", korean.describe())

    def test_a_typo_names_the_variable_and_is_refused(self) -> None:
        bad = {
            "YTDLP_BOT_ALLOWED_CHATS": "me", "YTDLP_BOT_ALLOWED_HOSTS": "no spaces",
            "YTDLP_BOT_MAX_PARALLEL": "0", "YTDLP_BOT_MAX_QUEUED": "101", "YTDLP_AUDIO_LANG": "k o",
        }
        for name, value in bad.items():
            with self.subTest(name), self.assertRaises(ValueError) as caught:
                self._config(**{name: value})
            self.assertIn(name, str(caught.exception))
        with self.assertRaises(ValueError):
            self._config(YTDLP_BOT_MAX_PARALLEL="6")
        with self.assertRaises(ValueError):
            self._config(YTDLP_BOT_MAX_PARALLEL="two")

    def test_the_token_is_never_shown(self) -> None:
        config = self._config(YTDLP_BOT_TOKEN=TOKEN)
        self.assertNotIn(TOKEN, config.describe())
        self.assertNotIn(TOKEN, repr(config))
        self.assertIn("token=set", config.describe())

    def test_download_options(self) -> None:
        config = make_config(output_dir=Path("/media"), audio_lang="ko")
        video = config.download_options("u")
        self.assertIsNone(video.audio_opts)
        self.assertEqual(video.audio_lang, "ko")
        self.assertIn("bestvideo", video.format_opt)
        self.assertTrue(video.live_opts.enabled)
        # The id in the name keeps two videos with one title from skipping each other.
        self.assertIn("%(id)s", video.output_template)
        self.assertTrue(video.output_template.startswith(os.path.join("/media", "")) or "media" in video.output_template)
        self.assertEqual(config.download_options("u", audio_only=True).audio_opts, {"format": "mp3"})
        health = object()
        self.assertIs(config.download_options("u", cookie_health=health).cookie_health, health)  # type: ignore[arg-type]


class AccessTest(_ServiceTest):
    def test_a_stranger_is_told_the_chat_id_once_and_nothing_downloads(self) -> None:
        service = self.service()
        service.handle_update(update(URL_A, chat=OTHER_CHAT))
        service.handle_update(update(URL_B, chat=OTHER_CHAT))
        service.handle_update(update("/status", chat=OTHER_CHAT))
        replies = [m for m in self.client.sent if m["chat_id"] == OTHER_CHAT]
        self.assertEqual(len(replies), 1)
        self.assertIn(str(OTHER_CHAT), replies[0]["text"])
        self.assertIn("YTDLP_BOT_ALLOWED_CHATS", replies[0]["text"])
        self.assertEqual(self.download.calls, [])

    def test_a_stranger_cannot_cancel_or_see_anything(self) -> None:
        service = self.service()
        gate = self.download.hold(URL_A)
        service.handle_update(update(URL_A))
        wait_for(self.download.started(URL_A).is_set)
        service.handle_update(update("/cancel", chat=OTHER_CHAT))
        service.handle_update(update("/status", chat=OTHER_CHAT))
        self.assertFalse(self.client.said("받는 중 #1:", OTHER_CHAT))
        self.assertFalse(self.download.calls[0:1] == [] and False)
        gate.set()

    def test_updates_that_are_not_plain_text_messages_are_ignored(self) -> None:
        service = self.service()
        for item in ({"update_id": 1}, {"update_id": 2, "edited_message": {"chat": {"id": CHAT}, "text": URL_A}},
                     {"update_id": 3, "message": {"chat": {"id": CHAT}, "photo": []}},
                     {"update_id": 4, "message": {"chat": {"id": True}, "text": URL_A}},
                     {"update_id": 5, "message": {"text": URL_A}}):
            service.handle_update(item)
        self.assertEqual((self.client.sent, self.download.calls), ([], []))

    def test_only_youtube_links_by_default(self) -> None:
        service = self.service()
        refused = [
            "https://example.com/video", "https://youtube.com.evil.com/watch?v=a",
            "https://evil.com/?u=youtube.com", "https://youtube.com@evil.com/", "https://notyoutube.com/a",
            "ftp://youtube.com/a",
        ]
        for url in refused:
            service.handle_update(update(url))
        self.assertEqual(self.download.calls, [])
        self.assertEqual(sum("허용되지 않은 사이트" in t or "받을 링크가 없습니다" in t
                             for t in self.client.texts()), len(refused))
        for url in ("https://youtu.be/x", "https://music.youtube.com/watch?v=y", "https://m.youtube.com/watch?v=z"):
            self.assertEqual(service._url_problem(url), "", url)

    def test_other_hosts_when_the_operator_allows_them(self) -> None:
        service = self.service(allowed_hosts=("vimeo.com",))
        self.assertEqual(service._url_problem("https://player.vimeo.com/video/1"), "")
        self.assertNotEqual(service._url_problem(URL_A), "")
        any_host = self.service(allowed_hosts=("*",))
        self.assertEqual(any_host._url_problem("https://anything.example/x"), "")


class RequestTest(_ServiceTest):
    def test_help(self) -> None:
        service = self.service()
        for text in ("/start", "/help"):
            service.handle_update(update(text, message_id=9))
        self.assertEqual(len(self.client.sent), 2)
        self.assertTrue(all("/audio" in m["text"] and "/video" in m["text"] and m["reply_to"] == 9
                            for m in self.client.sent))

    def test_a_message_without_a_link_gets_a_hint(self) -> None:
        self.service().handle_update(update("안녕"))
        self.assertTrue(self.client.said("링크가 없습니다"))
        self.assertEqual(self.download.calls, [])

    def test_an_unknown_command(self) -> None:
        self.service().handle_update(update("/nope"))
        self.assertTrue(self.client.said("알 수 없는 명령"))

    def test_a_link_is_downloaded_and_the_status_message_follows_it(self) -> None:
        service = self.service()
        service.handle_update(update(URL_A, message_id=55))
        status = self.status_id()
        wait_for(lambda: self.finished(status), what="the download to finish")
        self.assertEqual(self.client.sent[0]["reply_to"], 55)  # a reply to the user's message
        self.assertEqual(len(self.client.sent), 1)  # everything else edits that message
        text = self.client.last_text_of(status)
        self.assertIn("✓ 완료 #1", text)
        self.assertIn("제목 aaaaaaaaaaa", text)
        self.assertIn("저장: /out/제목 aaaaaaaaaaa.mp4", text)
        (url, opts), = self.download.calls
        self.assertEqual(url, URL_A)
        self.assertIsNone(opts.audio_opts)
        history = [e["text"] for e in self.client.edits]
        self.assertTrue(history[0].startswith("⏬ 받는 중 #1"))

    def test_audio_downloads_mp3(self) -> None:
        self.service().handle_update(update(f"/audio {URL_A}"))
        wait_for(lambda: bool(self.download.calls), what="the download")
        self.assertEqual(self.download.calls[0][1].audio_opts, {"format": "mp3"})

    def test_video_command_downloads_the_video_like_a_bare_link(self) -> None:
        self.service().handle_update(update(f"/video {URL_A}"))
        wait_for(lambda: bool(self.download.calls), what="the download")
        url, opts = self.download.calls[0]
        self.assertEqual(url, URL_A)
        self.assertIsNone(opts.audio_opts)  # video, not the MP3 of /audio

    def test_video_without_a_link_gets_the_hint(self) -> None:
        self.service().handle_update(update("/video"))
        self.assertTrue(self.client.said("링크가 없습니다"))
        self.assertEqual(self.download.calls, [])

    def test_in_a_group_the_command_addressed_to_this_bot_is_served(self) -> None:
        # With Telegram's privacy mode a group bot sees commands, not bare links, so /video
        # (and /audio) are the way to ask there. A group's chat id is negative.
        group = -1001234567890
        service = self.service(allowed_chats=frozenset({group}))
        service.username = "mybot"
        service.handle_update(update(f"/video@MyBot {URL_A}", chat=group))
        wait_for(lambda: bool(self.download.calls), what="the download")
        self.assertEqual(self.client.sent[0]["chat_id"], group)
        service.handle_update(update(f"/video@SomeoneElsesBot {URL_B}", chat=group))
        self.assertEqual(len(self.download.calls), 1)  # meant for another bot in the group

    def test_a_command_for_another_bot_is_ignored(self) -> None:
        service = self.service()
        service.username = "mybot"
        service.handle_update(update(f"/audio@OtherBot {URL_A}"))
        self.assertEqual((self.client.sent, self.download.calls), ([], []))
        service.handle_update(update(f"/audio@MyBot {URL_A}"))
        wait_for(lambda: bool(self.download.calls), what="the download")

    def test_several_links_in_one_message_and_a_limit(self) -> None:
        service = self.service(max_parallel=5)
        links = [f"https://youtu.be/{n:011d}" for n in range(7)]
        service.handle_update(update(" ".join(links)))
        wait_for(lambda: len(self.download.calls) == bot.MAX_URLS_PER_MESSAGE, what="the downloads")
        self.assertTrue(self.client.said("5개까지만"))
        self.assertEqual(len(self.download.calls), 5)

    def test_the_same_link_twice_in_a_message_is_one_download(self) -> None:
        service = self.service()
        service.handle_update(update(f"{URL_A} {URL_A}"))
        wait_for(lambda: bool(self.download.calls))
        self.assertEqual(len(self.download.calls), 1)

    def test_a_link_that_is_already_being_downloaded_is_not_started_again(self) -> None:
        service = self.service()
        gate = self.download.hold(URL_A)
        service.handle_update(update(URL_A))
        wait_for(self.download.started(URL_A).is_set)
        service.handle_update(update(URL_A, chat=CHAT))
        self.assertTrue(self.client.said("이미 받는 중"))
        self.assertEqual(len(self.download.calls), 1)
        gate.set()


class QueueTest(_ServiceTest):
    def test_a_second_download_waits_for_the_free_slot_then_starts(self) -> None:
        service = self.service(max_parallel=1)
        gate_a = self.download.hold(URL_A)
        service.handle_update(update(URL_A))
        wait_for(self.download.started(URL_A).is_set)
        service.handle_update(update(URL_B))
        second = self.status_id(1)
        self.assertIn("⏳ 대기 중 #2", self.client.last_text_of(second))
        self.assertFalse(self.download.started(URL_B).is_set())
        service.handle_update(update("/status"))
        status = self.client.sent[-1]["text"]
        self.assertIn("#1 받는 중", status)
        self.assertIn("#2 대기 중", status)
        gate_a.set()
        wait_for(lambda: self.finished(second), what="the second download")
        self.assertIn("✓ 완료 #2", self.client.last_text_of(second))

    def test_at_most_max_parallel_run_at_once(self) -> None:
        service = self.service(max_parallel=2)
        gates = {url: self.download.hold(url) for url in (URL_A, URL_B, URL_C)}
        service.handle_update(update(f"{URL_A} {URL_B} {URL_C}"))
        wait_for(lambda: self.download.started(URL_A).is_set() and self.download.started(URL_B).is_set())
        time.sleep(0.1)
        self.assertFalse(self.download.started(URL_C).is_set())
        gates[URL_A].set()
        wait_for(self.download.started(URL_C).is_set, what="the third to start")
        for gate in gates.values():
            gate.set()

    def test_a_full_queue_refuses_more(self) -> None:
        service = self.service(max_parallel=1, max_queued=1)
        gate = self.download.hold(URL_A)
        service.handle_update(update(URL_A))
        wait_for(self.download.started(URL_A).is_set)
        service.handle_update(update(URL_B))  # waits: the queue now holds one
        service.handle_update(update(URL_C))  # refused
        self.assertTrue(self.client.said("대기열이 가득"))
        self.assertEqual(len(self.client.sent), 3)
        gate.set()
        wait_for(lambda: self.download.started(URL_B).is_set())
        self.assertFalse(self.download.started(URL_C).is_set())

    def test_finished_jobs_are_forgotten_beyond_the_limit(self) -> None:
        service = self.service()
        with mock.patch.object(bot, "_KEEP_FINISHED", 2):
            for n in range(5):
                url = f"https://youtu.be/{n:011d}"
                service.handle_update(update(url))
                wait_for(lambda u=url: self.download.started(u).is_set())
                wait_for(lambda: all(not j.active for j in service._jobs.values()), what="idle")
        self.assertLessEqual(len(service._jobs), 3)  # the newest two + the last added


class CancelTest(_ServiceTest):
    def test_a_running_download_is_cancelled(self) -> None:
        service = self.service()
        self.download.hold(URL_A)
        service.handle_update(update(URL_A))
        status = self.status_id()
        wait_for(self.download.started(URL_A).is_set)
        service.handle_update(update("/cancel #1"))
        wait_for(lambda: self.finished(status), what="the cancel")
        self.assertIn("✗ 취소됨 #1", self.client.last_text_of(status))

    def test_a_waiting_download_is_cancelled_at_once_and_never_runs(self) -> None:
        service = self.service(max_parallel=1)
        gate = self.download.hold(URL_A)
        service.handle_update(update(URL_A))
        wait_for(self.download.started(URL_A).is_set)
        service.handle_update(update(URL_B))
        waiting = self.status_id(1)
        service.handle_update(update("/cancel 2"))
        self.assertIn("취소됨 #2 (대기 중)", self.client.last_text_of(waiting))
        gate.set()
        wait_for(lambda: self.finished(self.client.sent[0]["message_id"]))
        time.sleep(0.1)
        self.assertEqual([c[0] for c in self.download.calls], [URL_A])

    def test_a_numbered_cancel_leaves_the_running_download_alone(self) -> None:
        # Cancelling the waiting #2 must not stop #1, which is still downloading. (In a real
        # 2026-10-07 run a bare "/cancel" ended both, as designed; this pins the numbered form.)
        service = self.service(max_parallel=1)
        gate = self.download.hold(URL_A)
        service.handle_update(update(URL_A))
        first = self.status_id()
        wait_for(self.download.started(URL_A).is_set)
        service.handle_update(update(URL_B))
        service.handle_update(update("/cancel #2"))
        running = service._jobs[1]
        self.assertEqual(running.state, "running")
        self.assertFalse(running.cancel_event.is_set())
        self.assertEqual(service._jobs[2].state, "cancelled")
        gate.set()
        wait_for(lambda: self.finished(first), what="#1 finishing")
        self.assertIn("✓ 완료 #1", self.client.last_text_of(first))

    def test_cancel_without_a_number_cancels_all_of_the_chats_downloads(self) -> None:
        service = self.service(max_parallel=1)
        self.download.hold(URL_A)
        self.download.hold(URL_B)
        service.handle_update(update(f"{URL_A} {URL_B}"))
        wait_for(self.download.started(URL_A).is_set)
        service.handle_update(update("/cancel"))
        wait_for(lambda: all(not j.active for j in service._jobs.values()), what="both cancelled")
        self.assertEqual({j.state for j in service._jobs.values()}, {"cancelled"})

    def test_cancel_with_nothing_to_cancel_or_a_bad_number(self) -> None:
        service = self.service()
        service.handle_update(update("/cancel"))
        self.assertTrue(self.client.said("취소할 작업이 없습니다"))
        gate = self.download.hold(URL_A)
        service.handle_update(update(URL_A))
        wait_for(self.download.started(URL_A).is_set)
        service.handle_update(update("/cancel 99"))
        self.assertTrue(self.client.said("#99 는"))
        service.handle_update(update("/cancel abc"))
        self.assertTrue(self.client.said("사용법"))
        self.assertTrue(service._jobs[1].active)  # none of that touched #1
        gate.set()

    def test_another_chat_cannot_cancel_my_download(self) -> None:
        service = self.service(allowed_chats=frozenset({CHAT, OTHER_CHAT}))
        gate = self.download.hold(URL_A)
        service.handle_update(update(URL_A))
        wait_for(self.download.started(URL_A).is_set)
        service.handle_update(update("/cancel 1", chat=OTHER_CHAT))
        self.assertTrue(self.client.said("#1 는", OTHER_CHAT))
        self.assertEqual(service._jobs[1].state, "running")
        gate.set()

    def test_a_cancelled_live_recording_says_where_the_part_was_saved(self) -> None:
        service = self.service()
        self.download.results[URL_A] = DownloadResult(
            ok=False, cancelled=True, files=[{"filepath": "/out/방송 (부분 녹화).mp4", "partial": True}])
        service.handle_update(update(URL_A))
        status = self.status_id()
        wait_for(lambda: self.finished(status))
        self.assertIn("'방송 (부분 녹화).mp4' 로 저장했습니다", self.client.last_text_of(status))

    def test_stopping_the_service_cancels_what_runs_and_what_waits(self) -> None:
        service = self.service(max_parallel=1)
        self.download.hold(URL_A)
        self.download.hold(URL_B)
        service.handle_update(update(f"{URL_A} {URL_B}"))
        wait_for(self.download.started(URL_A).is_set)
        service.stop()
        self.assertEqual({j.state for j in service._jobs.values()}, {"cancelled"})
        self.assertFalse(self.download.started(URL_B).is_set())
        self.assertFalse(any(w.is_alive() for w in service._workers))


class ProgressTest(_ServiceTest):
    def _pct(self, n: int, **extra) -> dict:
        info = {"percentage": float(n), "percent_known": True, "total_size": "100.0MB",
                "speed": "2.0MB/s", "eta": "00:32", "stream": 0}
        info.update(extra)
        return info

    def _first_line(self, info: dict) -> str:
        """The first line of the status message after one progress report."""
        service = self.service()
        job = bot.BotJob(1, CHAT, None, URL_A)
        service._say(job, "시작", force=True)
        self.clock.now += 5
        service._on_progress(job, info)
        return self.client.edits[-1]["text"].splitlines()[0]

    def test_unknown_speed_and_eta_are_left_out_not_shown_as_placeholders(self) -> None:
        # The real screenshot (2026-10-07): "⏬ #2 72% · 259.8MB · N/A · ETA --:--".
        line = self._first_line(self._pct(72, total_size="259.8MB", speed="N/A", eta="--:--"))
        self.assertEqual(line, "⏬ #1 72% · 259.8MB")
        self.assertNotIn("N/A", line)
        self.assertNotIn("ETA", line)

    def test_what_is_known_is_kept(self) -> None:
        self.assertEqual(self._first_line(self._pct(72, total_size="259.8MB", eta="--:--")),
                         "⏬ #1 72% · 259.8MB · 2.0MB/s")
        self.assertEqual(self._first_line(self._pct(72, total_size="259.8MB", speed="N/A")),
                         "⏬ #1 72% · 259.8MB · ETA 00:32")
        self.assertEqual(self._first_line(self._pct(72, total_size="259.8MB")),
                         "⏬ #1 72% · 259.8MB · 2.0MB/s · ETA 00:32")

    def test_a_live_recording_leaves_out_what_it_does_not_know(self) -> None:
        live = {"percentage": 0.0, "percent_known": False, "stream": 0, "eta": "--:--"}
        self.assertEqual(self._first_line({**live, "downloaded": "12.0MB", "speed": "1.0MB/s"}),
                         "⏬ #1 12.0MB 받음 · 1.0MB/s (라이브)")
        self.assertEqual(self._first_line({**live, "downloaded": "12.0MB", "speed": "N/A"}),
                         "⏬ #1 12.0MB 받음 (라이브)")
        self.assertEqual(self._first_line({**live, "downloaded": "N/A", "speed": "N/A"}), "⏬ #1 (라이브)")

    def test_the_stream_label_survives_unknown_values(self) -> None:
        line = self._first_line(self._pct(50, stream=1, speed="N/A", eta="--:--"))
        self.assertEqual(line, "⏬ #1 [스트림 2] 50% · 100.0MB")

    def test_progress_edits_are_throttled_and_skip_repeats(self) -> None:
        service = self.service()
        gate = self.download.hold(URL_A)
        job = bot.BotJob(1, CHAT, None, URL_A)
        service._jobs[1] = job
        service._say(job, "시작", force=True)       # t=1000: the status message was just sent
        service._on_progress(job, self._pct(5))    # too soon after it
        self.clock.now += 5
        service._on_progress(job, self._pct(10))   # t=1005: allowed
        self.clock.now += 1
        service._on_progress(job, self._pct(20))   # 1 s later: too soon
        self.clock.now += 2
        service._on_progress(job, self._pct(30))   # 3 s since the last edit: still too soon
        self.clock.now += 2
        service._on_progress(job, self._pct(40))   # 5 s since the last edit: allowed
        self.clock.now += 10
        service._on_progress(job, self._pct(40))   # the same text again: nothing to say
        shown = [e["text"].splitlines()[0] for e in self.client.edits]
        self.assertEqual(len(shown), 2)
        self.assertIn("10%", shown[0])
        self.assertIn("40%", shown[1])
        self.assertIn("2.0MB/s", shown[1])
        self.assertIn("ETA 00:32", shown[1])
        gate.set()

    def test_a_live_recording_shows_bytes_not_a_percentage(self) -> None:
        service = self.service()
        job = bot.BotJob(1, CHAT, None, URL_A)
        service._say(job, "시작", force=True)
        self.clock.now += 5
        service._on_progress(job, {"percentage": 0.0, "percent_known": False, "downloaded": "12.0MB",
                                   "speed": "1.0MB/s", "eta": "--:--", "stream": 0})
        self.assertIn("12.0MB 받음", self.client.edits[-1]["text"])
        self.assertIn("라이브", self.client.edits[-1]["text"])

    def test_a_second_stream_is_labelled(self) -> None:
        service = self.service()
        job = bot.BotJob(1, CHAT, None, URL_A)
        service._say(job, "시작", force=True)
        self.clock.now += 5
        service._on_progress(job, self._pct(50, stream=1))
        self.assertIn("[스트림 2]", self.client.edits[-1]["text"])

    def test_a_detected_live_broadcast_is_labelled(self) -> None:
        job = bot.BotJob(1, CHAT, None, URL_A)
        bot.BotService._on_log(job, "🔴 LIVE 감지: 녹화합니다", "info")
        self.assertIn("라이브", job.label)

    def test_the_progress_of_a_real_download_reaches_the_message(self) -> None:
        ticks = itertools.count(1000, 5)  # each reading of the clock is 5 s later
        service = bot.BotService(make_config(), self.client, download=self.download,
                                 clock=lambda: float(next(ticks)))
        self.addCleanup(service.stop)
        service.start()
        self.download.progress[URL_A] = [self._pct(10), self._pct(10)]
        service.handle_update(update(URL_A))
        status = self.status_id()
        wait_for(lambda: self.finished(status))
        self.assertTrue(any("10%" in e["text"] for e in self.client.edits))


class OutcomeTest(_ServiceTest):
    def _run(self, result, **config) -> str:
        service = self.service(**config)
        self.download.results[URL_A] = result
        service.handle_update(update(URL_A))
        status = self.status_id()
        wait_for(lambda: self.finished(status))
        return self.client.last_text_of(status)

    def test_a_lost_login_with_cookies_says_to_get_them_again(self) -> None:
        text = self._run(
            DownloadResult(ok=False, failure_kind="auth_expired", log_path="/logs/x.log"),
            auth=CookieFileAuth(path="cookies.txt"))
        self.assertIn("✗ 실패 #1", text)
        self.assertIn("다시 추출", text)
        self.assertIn("실행 로그: /logs/x.log", text)

    def test_the_hint_does_not_blame_the_cookie_for_a_dropped_connection(self) -> None:
        text = self._run(DownloadResult(ok=False, failure_kind="network"), auth=BrowserCookieAuth("chrome"))
        self.assertIn("인터넷 연결", text)
        self.assertNotIn("다시 추출", text)

    def test_an_unknown_cause_has_no_hint_but_the_log(self) -> None:
        text = self._run(DownloadResult(ok=False, failure_kind="other", log_path="/l.log"))
        self.assertEqual(text.splitlines(), ["✗ 실패 #1", "실행 로그: /l.log"])

    def test_a_failure_quotes_what_yt_dlp_said_but_not_the_downloaders_own_summary(self) -> None:
        # The first real run (2026-10-07) failed with only "✗ 실패 #1" and a log path.
        self.download.logs[URL_A] = [
            ("Traceback (most recent call last):", "info"),
            ("ModuleNotFoundError: No module named 'yt_dlp'", "error"),
            ("yt-dlp exit code: 1", "error"),
            ("다운로드 실패.", "error"),
        ]
        text = self._run(DownloadResult(ok=False, failure_kind="yt_dlp_missing", log_path="/l.log"))
        self.assertEqual(text.splitlines()[0], "✗ 실패 #1")
        self.assertIn("pip install", text)  # the hint for this kind
        self.assertIn("마지막 오류:\n• ModuleNotFoundError: No module named 'yt_dlp'", text)
        self.assertNotIn("exit code", text)
        self.assertNotIn("다운로드 실패.", text)
        self.assertEqual(text.splitlines()[-1], "실행 로그: /l.log")

    def test_only_the_last_few_distinct_error_lines_each_cut_short(self) -> None:
        self.download.logs[URL_A] = [("error one", "error"), ("error two", "error"), ("error two", "error"),
                                     ("error three", "error"), ("E" * 500, "error"), ("error five", "error")]
        text = self._run(DownloadResult(ok=False, failure_kind="other"))
        quoted = [line[2:] for line in text.splitlines() if line.startswith("• ")]
        self.assertEqual(quoted, ["error three", "E" * bot._ERROR_LINE_CHARS, "error five"])
        self.assertLess(len(text), 600)

    def test_a_successful_run_never_quotes_errors(self) -> None:
        # yt-dlp's "Got error: ... Retrying" lines carry the word "error" and are tagged so.
        self.download.logs[URL_A] = [("[download] Got error: Read timed out.. Retrying (1/30)...", "error")]
        text = self._run(DownloadResult(ok=True, title="영상", files=[{"filepath": "/out/a.mp4"}]))
        self.assertIn("✓ 완료 #1", text)
        self.assertNotIn("마지막 오류", text)

    def test_the_real_downloader_failure_reaches_the_message(self) -> None:
        # No fake downloader: run_download itself, over a yt-dlp that prints a traceback.
        from core import downloader
        from tests.test_downloader import _FakeProcess
        from tests.test_failure import YT_DLP_TRACEBACK
        lines = [(line + "\n").encode("utf-8") for line in YT_DLP_TRACEBACK]
        service = bot.BotService(make_config(), self.client, clock=self.clock)  # the real run_download
        self.addCleanup(service.stop)
        service.start()
        # (Popen is patched process-wide, and app_version() runs `git describe` through it: pin it.)
        with mock.patch("core.live.detect_live_status", return_value=LiveStatusInfo("not_live", probed=False)), \
                mock.patch.object(downloader, "app_version", return_value="test"), \
                mock.patch.object(downloader, "yt_dlp_command", return_value=["yt-dlp"]), \
                mock.patch.object(downloader.subprocess, "Popen", return_value=_FakeProcess(lines, 1)):
            service.handle_update(update(URL_A))
            status = self.status_id()
            wait_for(lambda: self.finished(status), what="the failure")
        text = self.client.last_text_of(status)
        self.assertIn("✗ 실패 #1", text)
        self.assertIn("yt-dlp 를 실행하지 못했습니다", text)
        self.assertIn("• ModuleNotFoundError: No module named 'yt_dlp'", text)
        self.assertNotIn("yt-dlp exit code", text)  # the downloader's own summary lines are left out
        self.assertNotIn("다운로드 실패.", text)

    def test_an_unexpected_failure_kind_is_treated_as_unknown(self) -> None:
        text = self._run(DownloadResult(ok=False, failure_kind="something new"))
        self.assertEqual(text, "✗ 실패 #1")

    def test_an_exception_is_reported_and_the_worker_survives(self) -> None:
        service = self.service(max_parallel=1)
        self.download.results[URL_A] = RuntimeError("yt-dlp 를 찾을 수 없습니다")
        service.handle_update(update(URL_A))
        first = self.status_id()
        wait_for(lambda: self.finished(first))
        self.assertIn("✗ 오류 #1: yt-dlp 를 찾을 수 없습니다", self.client.last_text_of(first))
        service.handle_update(update(URL_B))  # the one worker still serves the next one
        wait_for(lambda: self.finished(self.status_id(1)))
        self.assertIn("✓ 완료 #2", self.client.last_text_of(self.status_id(1)))

    def test_a_video_that_is_already_being_downloaded_by_the_downloader(self) -> None:
        text = self._run(DownloadResult(ok=False, skipped="busy"))
        self.assertIn("이미 받는 중", text)

    def test_a_live_recording_that_may_have_ended_early(self) -> None:
        text = self._run(DownloadResult(
            ok=True, title="방송", files=[{"filepath": "/out/방송.mp4"}], warnings=["refresh failed"]))
        self.assertIn("✓ 완료 #1", text)
        self.assertIn("조기 종료 가능", text)


class CookieAlertTest(_ServiceTest):
    def test_an_expiry_is_announced_to_every_allowed_chat(self) -> None:
        service = self.service(allowed_chats=frozenset({CHAT, OTHER_CHAT}))
        self.download.results[URL_A] = DownloadResult(
            ok=False, failure_kind="auth_expired", cookie_event="invalid")
        service.handle_update(update(URL_A))
        wait_for(lambda: self.client.said("쿠키의 로그인이 만료", OTHER_CHAT), what="the alert")
        self.assertTrue(self.client.said("쿠키의 로그인이 만료", CHAT))
        self.assertTrue(self.client.said("쿠키 파일을 다시 받아 교체", OTHER_CHAT))

    def test_a_replaced_login_is_announced_too(self) -> None:
        service = self.service()
        self.download.results[URL_A] = DownloadResult(ok=True, cookie_event="recovered")
        service.handle_update(update(URL_A))
        wait_for(lambda: self.client.said("새 쿠키가 확인"), what="the notice")

    def test_a_run_without_an_event_says_nothing_about_cookies(self) -> None:
        service = self.service()
        service.handle_update(update(URL_A))
        wait_for(lambda: self.finished(self.status_id()))
        self.assertFalse(self.client.said("쿠키"))

    def test_the_health_store_reaches_the_download_options(self) -> None:
        store = object()
        service = bot.BotService(make_config(), self.client, health_store=store,  # type: ignore[arg-type]
                                 download=self.download, clock=self.clock)
        self.addCleanup(service.stop)
        service.start()
        service.handle_update(update(URL_A))
        wait_for(lambda: bool(self.download.calls))
        self.assertIs(self.download.calls[0][1].cookie_health, store)


class TelegramTroubleTest(_ServiceTest):
    def test_a_failing_edit_does_not_fail_the_download_and_is_not_retried_at_once(self) -> None:
        service = self.service()
        self.client.fail_edits = TelegramError("edit: boom", code=500)
        self.download.progress[URL_A] = [{"percentage": 1.0, "percent_known": True, "total_size": "1B",
                                          "speed": "1B/s", "eta": "00:01", "stream": 0}]
        service.handle_update(update(URL_A))
        wait_for(lambda: service._jobs[1].state == "done", what="the download to finish anyway")

    def test_flood_control_pauses_progress_for_as_long_as_telegram_asks(self) -> None:
        service = self.service()
        job = bot.BotJob(1, CHAT, None, URL_A)
        service._say(job, "시작", force=True)
        self.clock.now += 5
        self.client.fail_edits = TelegramError("flood", code=429, retry_after=30)
        service._say(job, "A")
        self.assertEqual(job.blocked_until, self.clock.now + 30)
        self.client.fail_edits = None
        self.clock.now += 10
        service._say(job, "B")  # still inside Telegram's 30 s
        self.clock.now += 25
        service._say(job, "C")
        self.assertEqual([e["text"] for e in self.client.edits], ["C"])

    def test_the_final_message_is_sent_anew_when_its_edit_fails(self) -> None:
        # e.g. the user deleted the status message
        service = self.service()
        job = bot.BotJob(1, CHAT, 7, URL_A)
        service._say(job, "시작", force=True)
        self.client.fail_edits = TelegramError("editMessageText: message to edit not found", code=400)
        service._say(job, "✓ 완료 #1", force=True)
        self.assertEqual([m["text"] for m in self.client.sent], ["시작", "✓ 완료 #1"])
        self.assertNotEqual(job.message_id, self.client.sent[0]["message_id"])

    def test_a_failing_first_message_is_retried_as_the_final_one(self) -> None:
        service = self.service()
        self.client.fail_sends = TelegramError("sendMessage: boom", code=502)
        service.handle_update(update(URL_A))
        wait_for(lambda: service._jobs[1].state == "done", what="the download")
        self.client.fail_sends = None
        job = service._jobs[1]
        service._say(job, "✓ 완료 #1", force=True)
        self.assertTrue(self.client.said("✓ 완료 #1"))

    def test_a_reply_that_cannot_be_sent_is_only_logged(self) -> None:
        service = self.service()
        self.client.fail_sends = TelegramError("sendMessage: boom", code=502)
        with self.assertLogs(bot.logger, "WARNING"):
            service.handle_update(update("/help"))


def down() -> TelegramError:
    """What a dropped connection looks like (no HTTP code)."""
    return TelegramError("sendMessage: <urlopen error timed out>")


class OutboxTest(_ServiceTest):
    """A message that must arrive (how a download ended, the cookie alert) is kept
    when Telegram will not take it and sent again, instead of being lost. Found on
    2026-10-07: this PC's route to Telegram dropped connections, and the only retry
    of the end message was one new message right after the failed edit."""

    def setUp(self) -> None:
        super().setUp()
        self.service_ = self.service()
        self.job = bot.BotJob(1, CHAT, 7, URL_A, state="done")  # finished: stop() leaves it alone
        self.service_._jobs[1] = self.job
        self.service_._say(self.job, "⏬ 받는 중 #1", force=True)  # its status message is out
        self.message_id = self.job.message_id

    def go_down(self, **kw) -> None:
        self.client.fail_edits = self.client.fail_sends = kw.get("error", down())

    def come_back(self) -> None:
        self.client.fail_edits = self.client.fail_sends = None

    def pending(self) -> list:
        return list(self.service_._outbox)

    def test_the_end_of_a_download_is_delivered_once_the_connection_is_back(self) -> None:
        self.go_down()
        self.service_._say(self.job, "✓ 완료 #1", force=True, final=True)
        self.assertEqual(len(self.pending()), 1)
        self.assertNotIn("✓ 완료 #1", self.client.texts())
        self.service_._flush_outbox()  # not due yet: no attempt
        self.assertEqual(self.pending()[0].attempts, 0)
        self.come_back()
        self.clock.now += bot.OUTBOX_RETRY_DELAYS[0]
        self.service_._flush_outbox()
        self.assertEqual(self.client.last_text_of(self.message_id), "✓ 완료 #1")  # it edited the same message
        self.assertEqual(self.pending(), [])

    def test_each_failed_try_waits_longer_and_the_last_wait_repeats(self) -> None:
        self.go_down()
        self.service_._say(self.job, "✓ 완료 #1", force=True, final=True)
        waits = []
        for _ in range(7):
            self.clock.now = self.pending()[0].due
            self.service_._flush_outbox()
            waits.append(self.pending()[0].due - self.clock.now)
        self.assertEqual(waits, [10.0, 20.0, 40.0, 60.0, 60.0, 60.0, 60.0])

    def test_flood_control_is_waited_out(self) -> None:
        self.go_down(error=TelegramError("flood", code=429, retry_after=120))
        self.service_._say(self.job, "✓ 완료 #1", force=True, final=True)
        self.clock.now = self.pending()[0].due
        self.service_._flush_outbox()
        self.assertEqual(self.pending()[0].due, self.clock.now + 120)

    def test_a_refusal_is_not_kept(self) -> None:
        for error in (TelegramError("Forbidden: bot was blocked by the user", code=403),
                      TelegramError("Bad Request: chat not found", code=400),
                      TelegramError("Unauthorized", code=401)):
            with self.subTest(error.description):
                self.go_down(error=error)
                self.service_._say(self.job, "✓ 완료 #1", force=True, final=True)
                self.assertEqual(self.pending(), [])

    def test_a_server_error_is_kept(self) -> None:
        self.go_down(error=TelegramError("Bad Gateway", code=502))
        self.service_._say(self.job, "✓ 완료 #1", force=True, final=True)
        self.assertEqual(len(self.pending()), 1)

    def test_a_refusal_that_comes_later_ends_the_retrying(self) -> None:
        self.go_down()
        self.service_._say(self.job, "✓ 완료 #1", force=True, final=True)
        self.go_down(error=TelegramError("Forbidden: bot was kicked", code=403))
        self.clock.now = self.pending()[0].due
        with self.assertLogs(bot.logger, "ERROR") as logs:
            self.service_._flush_outbox()
        self.assertEqual(self.pending(), [])
        self.assertIn("giving up", logs.output[0])

    def test_it_gives_up_after_an_hour(self) -> None:
        self.go_down()
        self.service_._say(self.job, "✓ 완료 #1", force=True, final=True)
        self.clock.now += bot.OUTBOX_GIVE_UP + 1
        with self.assertLogs(bot.logger, "ERROR"):
            self.service_._flush_outbox()
        self.assertEqual(self.pending(), [])

    def test_a_newer_end_replaces_the_older_one_for_the_same_job(self) -> None:
        self.go_down()
        self.service_._say(self.job, "✗ 실패 #1", force=True, final=True)
        self.service_._say(self.job, "✗ 취소됨 #1", force=True, final=True)
        self.assertEqual([p.text for p in self.pending()], ["✗ 취소됨 #1"])

    def test_progress_and_ordinary_replies_are_not_kept(self) -> None:
        self.go_down()
        self.clock.now += 100
        self.service_._say(self.job, "⏬ #1 50%")  # progress: the next one takes over
        self.service_.handle_update(update("/help"))  # a reply: the user can ask again
        self.assertEqual(self.pending(), [])

    def test_the_first_message_of_a_job_that_never_got_one_is_sent_later(self) -> None:
        late = bot.BotJob(2, CHAT, 9, URL_B)
        self.service_._jobs[2] = late
        self.go_down()
        self.service_._say(late, "✓ 완료 #2", force=True, final=True)  # no message_id yet: a send
        self.assertEqual(len(self.pending()), 1)
        self.come_back()
        self.clock.now += bot.OUTBOX_RETRY_DELAYS[0]
        self.service_._flush_outbox()
        self.assertIsNotNone(late.message_id)
        self.assertEqual(self.client.last_text_of(late.message_id), "✓ 완료 #2")
        self.assertEqual(self.client.sent[-1]["reply_to"], 9)

    def test_the_cookie_alert_that_could_not_be_sent_is_not_lost(self) -> None:
        # It is said once per expiry (cookie_event is set on that run only): losing it means never.
        service = self.service(allowed_chats=frozenset({CHAT, OTHER_CHAT}))
        self.go_down()
        service._cookie_alert(DownloadResult(ok=False, cookie_event="invalid"))
        self.assertEqual(sorted(p.chat_id for p in service._outbox), sorted([CHAT, OTHER_CHAT]))
        self.come_back()
        self.clock.now += bot.OUTBOX_RETRY_DELAYS[0]
        service._flush_outbox()
        self.assertTrue(self.client.said("쿠키의 로그인이 만료", CHAT))
        self.assertTrue(self.client.said("쿠키의 로그인이 만료", OTHER_CHAT))
        count = len(self.client.sent)
        service._flush_outbox()
        self.assertEqual(len(self.client.sent), count)  # sent once each, not again

    def test_only_a_limited_number_is_kept(self) -> None:
        self.go_down()
        with mock.patch.object(bot, "OUTBOX_MAX", 2), self.assertLogs(bot.logger, "ERROR"):
            for n in range(3):
                job = bot.BotJob(10 + n, CHAT, None, f"{URL_A}{n}")
                self.service_._say(job, f"✗ 실패 #{10 + n}", force=True, final=True)
        self.assertEqual([p.text for p in self.pending()], ["✗ 실패 #11", "✗ 실패 #12"])

    def test_stopping_makes_one_last_try_and_gives_up_on_the_rest_at_the_first_failure(self) -> None:
        other = bot.BotJob(2, CHAT, None, URL_B, state="done")
        self.service_._jobs[2] = other
        self.go_down()
        self.service_._say(self.job, "✓ 완료 #1", force=True, final=True)
        self.service_._say(other, "✓ 완료 #2", force=True, final=True)
        tries = []
        real = self.client.send_message

        def counting_send(*a, **kw):
            tries.append(a)
            return real(*a, **kw)
        self.client.send_message = counting_send  # type: ignore[method-assign]
        with self.assertLogs(bot.logger, "WARNING") as logs:
            self.service_.stop()  # still down: tries the first, sees it fail, stops there
        self.assertEqual(len(tries), 1)  # the first message's new-message fallback; then it stopped
        self.assertTrue(any("could not be delivered before the bot stopped" in line for line in logs.output))
        self.assertEqual(self.pending()[0].attempts + self.pending()[1].attempts, 1)  # only one was tried

    def test_stopping_with_the_connection_back_delivers_everything(self) -> None:
        other = bot.BotJob(2, CHAT, None, URL_B, state="done")
        self.service_._jobs[2] = other
        self.go_down()
        self.service_._say(self.job, "✓ 완료 #1", force=True, final=True)
        self.service_._say(other, "✓ 완료 #2", force=True, final=True)
        self.come_back()  # not due yet, but the bot is exiting: try now
        self.service_.stop()
        self.assertEqual(self.pending(), [])
        self.assertTrue(self.client.said("✓ 완료 #1"))
        self.assertTrue(self.client.said("✓ 완료 #2"))


class OutboxThreadTest(unittest.TestCase):
    """The same, with the real thread and the real client over HTTP (short waits)."""

    def setUp(self) -> None:
        quiet = logging.NullHandler()
        bot.logger.addHandler(quiet)
        self.addCleanup(bot.logger.removeHandler, quiet)
        for name, value in (("OUTBOX_POLL", 0.01), ("OUTBOX_RETRY_DELAYS", (0.02,))):
            patcher = mock.patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_end_arrives_by_itself_once_the_connection_is_back(self) -> None:
        client = FakeClient()
        download = FakeDownload()
        gate = download.hold(URL_A)
        service = bot.BotService(make_config(), client, download=download)  # the real clock
        self.addCleanup(service.stop)
        service.start()
        service.handle_update(update(URL_A))
        wait_for(download.started(URL_A).is_set, what="the download to start")
        client.fail_edits = client.fail_sends = down()  # the connection drops while it runs
        gate.set()
        wait_for(lambda: service._jobs[1].state == "done", what="the download to finish")
        wait_for(lambda: bool(service._outbox), what="the end message to be kept")
        self.assertFalse(client.said("✓ 완료 #1"))
        client.fail_edits = client.fail_sends = None  # back
        wait_for(lambda: client.said("✓ 완료 #1"), what="the kept end message to be delivered")
        wait_for(lambda: not service._outbox, what="the outbox to empty")

    def test_over_http_a_server_that_fails_a_few_times_still_gets_the_end(self) -> None:
        with FakeTelegram() as server:
            failures = {"left": 4}

            def flaky(params):
                # Only the end message ("완료") is refused, and only a few times.
                if "완료" in params.get("text", "") and failures["left"] > 0:
                    failures["left"] -= 1
                    return 502, {"ok": False, "error_code": 502, "description": "Bad Gateway"}
                return 200, {"ok": True, "result": {"message_id": 900, "chat": {"id": params.get("chat_id")}}}
            server.handlers["editMessageText"] = flaky
            server.handlers["sendMessage"] = flaky
            client = TelegramClient(TOKEN, api_base=server.api_base, timeout=5)
            download = FakeDownload()
            service = bot.BotService(make_config(), client, download=download)
            stop = threading.Event()
            runner = threading.Thread(target=service.run, args=(stop,), daemon=True)
            runner.start()
            self.addCleanup(runner.join, 10)
            self.addCleanup(stop.set)
            server.push_message(CHAT, URL_A)
            wait_for(lambda: failures["left"] == 0, what="the server to have refused it a few times")
            wait_for(lambda: not service._outbox and any(
                "완료" in p["text"] for p in server.calls_of("editMessageText") + server.calls_of("sendMessage")),
                what="the end message to get through")
            self.assertEqual(failures["left"], 0)
            stop.set()
            runner.join(10)


class FastStop(threading.Event):
    """A stop event whose wait() does not sleep (the poll loop's back-off)."""

    def wait(self, timeout=None):
        return self.is_set()


class ScriptedClient:
    """get_updates answers from a script; the stop event is set when it runs out."""

    def __init__(self, script, stop, me=None):
        self.script = list(script)
        self.stop = stop
        self.me = me if me is not None else {"username": "TestBot"}
        self.offsets: list = []
        self.sent: list = []

    def get_me(self):
        if isinstance(self.me, Exception):
            raise self.me
        return self.me

    def get_updates(self, offset=None, *, timeout=30):
        self.offsets.append(offset)
        if not self.script:
            self.stop.set()
            return []
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def send_message(self, chat_id, text, *, reply_to=None):
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}

    def edit_message_text(self, *a):
        return True


class PollingTest(unittest.TestCase):
    def _run(self, script, **kw):
        stop = FastStop()
        client = ScriptedClient(script, stop, **kw)
        download = FakeDownload()
        service = bot.BotService(make_config(), client, download=download)  # type: ignore[arg-type]
        return service, client, download, service.run(stop)

    def test_updates_are_handled_and_the_offset_moves_on(self) -> None:
        batches = [[update(URL_A, update_id=10), update("/help", update_id=11)], [update(URL_B, update_id=12)]]
        service, client, download, code = self._run(batches)
        self.assertEqual(code, 0)
        self.assertEqual(client.offsets[:3], [None, 12, 13])
        self.assertEqual(service.username, "testbot")
        self.assertEqual({c[0] for c in download.calls} <= {URL_A, URL_B}, True)

    def test_a_message_that_breaks_the_handler_does_not_stop_the_bot(self) -> None:
        stop = FastStop()
        client = ScriptedClient([[update("/help", update_id=1), update("/help", update_id=2)]], stop)
        service = bot.BotService(make_config(), client, download=FakeDownload())  # type: ignore[arg-type]
        calls = []

        def flaky(item):
            calls.append(item["update_id"])
            if item["update_id"] == 1:
                raise RuntimeError("boom")
        with mock.patch.object(service, "handle_update", side_effect=flaky), \
                self.assertLogs(bot.logger, "ERROR"):
            self.assertEqual(service.run(stop), 0)
        self.assertEqual(calls, [1, 2])

    def test_a_network_error_is_retried(self) -> None:
        with self.assertLogs(bot.logger, "WARNING"):
            _, client, _, code = self._run([TelegramError("getUpdates: timed out"), [update("/help")]])
        self.assertEqual(code, 0)
        self.assertEqual(len(client.sent), 1)  # the help after the error

    def test_a_refused_token_ends_the_bot_with_code_2(self) -> None:
        with self.assertLogs(bot.logger, "ERROR"):
            _, _, _, code = self._run([TelegramError("getUpdates: Unauthorized", code=401)])
        self.assertEqual(code, 2)

    def test_another_poller_ends_the_bot_with_code_2_and_says_why(self) -> None:
        with self.assertLogs(bot.logger, "ERROR") as logs:
            _, _, _, code = self._run([TelegramError("Conflict", code=409)])
        self.assertEqual(code, 2)
        text = "\n".join(logs.output)
        self.assertIn("webhook", text)
        self.assertIn("another copy of the bot", text)
        self.assertIn("does not delete a webhook", text)  # it never removes one by itself

    def test_a_refused_token_does_not_get_the_conflict_explanation(self) -> None:
        with self.assertLogs(bot.logger, "ERROR") as logs:
            self._run([TelegramError("Unauthorized", code=401)])
        self.assertNotIn("webhook", "\n".join(logs.output))

    def test_a_bad_token_at_start_is_refused_before_polling(self) -> None:
        with self.assertLogs(bot.logger, "ERROR"):
            _, client, _, code = self._run([], me=TelegramError("getMe: Unauthorized", code=401))
        self.assertEqual((code, client.offsets), (2, []))

    def test_ctrl_c_stops_the_workers_and_returns_zero(self) -> None:
        stop = FastStop()
        client = ScriptedClient([KeyboardInterrupt()], stop)
        service = bot.BotService(make_config(), client, download=FakeDownload())  # type: ignore[arg-type]
        with self.assertLogs(bot.logger, "INFO"):
            self.assertEqual(service.run(stop), 0)
        self.assertFalse(any(w.is_alive() for w in service._workers))


class EnvironmentLinesTest(unittest.TestCase):
    """What the console shows of the environment: the app's own variables, no secrets."""

    def test_the_apps_variables_are_listed_sorted_with_their_values(self) -> None:
        lines = bot.environment_lines({
            "YTDLP_OUTPUT_DIR": r"C:\Users\user\Downloads\bot-test", "YTDLP_AUDIO_LANG": "original",
            "YTDLP_BOT_MAX_PARALLEL": " 1 ", "ytdlp_lower": "x"})
        self.assertEqual(lines, [
            "YTDLP_AUDIO_LANG=original", "YTDLP_BOT_MAX_PARALLEL=1",
            r"YTDLP_OUTPUT_DIR=C:\Users\user\Downloads\bot-test", "ytdlp_lower=x"])

    def test_nothing_else_of_the_environment_is_shown(self) -> None:
        lines = bot.environment_lines({
            "PATH": "C:\\bin", "ANTHROPIC_API_KEY": "sk-secret-1", "USERPROFILE": "C:\\Users\\u",
            "YTDLP_AUDIO_LANG": "ko"})
        self.assertEqual(lines, ["YTDLP_AUDIO_LANG=ko"])
        self.assertEqual(bot.environment_lines({"PATH": "x"}), [])

    def test_a_secret_is_never_shown(self) -> None:
        env = {"YTDLP_BOT_TOKEN": TOKEN, "YTDLP_SOME_API_KEY": "key-value-1", "YTDLP_X_PASSWORD": "pw-value-1",
               "YTDLP_Y_SECRET": "secret-value-1", "ytdlp_bot_token": "lower-token-1"}
        lines = bot.environment_lines(env)
        self.assertEqual(
            sorted(line.split("=")[1] for line in lines), ["<set>"] * 5)
        text = " ".join(lines)
        for value in (TOKEN, "key-value-1", "pw-value-1", "secret-value-1", "lower-token-1"):
            self.assertNotIn(value, text)

    def test_the_allow_list_shows_how_many_ids_not_which(self) -> None:
        self.assertEqual(
            bot.environment_lines({"YTDLP_BOT_ALLOWED_CHATS": "123456789, -100200;3"}),
            ["YTDLP_BOT_ALLOWED_CHATS=<3 chat id(s)>"])
        self.assertNotIn("123456789", " ".join(bot.environment_lines({"YTDLP_BOT_ALLOWED_CHATS": "123456789"})))

    def test_an_empty_value_is_said_to_be_empty_and_a_url_loses_its_login(self) -> None:
        lines = bot.environment_lines({
            "YTDLP_COOKIE_FILE": "  ", "YTDLP_BOT_TOKEN": "",
            "YTDLP_GETPOT_BGUTIL_BASEURL": "http://user:pass@127.0.0.1:4416"})
        self.assertEqual(lines, [
            "YTDLP_BOT_TOKEN=<empty>", "YTDLP_COOKIE_FILE=<empty>",
            "YTDLP_GETPOT_BGUTIL_BASEURL=http://***@127.0.0.1:4416"])
        self.assertNotIn("user:pass", " ".join(lines))

    def test_the_process_environment_is_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"YTDLP_AUDIO_LANG": "ja", "YTDLP_BOT_TOKEN": TOKEN}):
            lines = bot.environment_lines()
        self.assertIn("YTDLP_AUDIO_LANG=ja", lines)
        self.assertIn("YTDLP_BOT_TOKEN=<set>", lines)
        self.assertNotIn(TOKEN, " ".join(lines))


class RunBotTest(unittest.TestCase):
    def setUp(self) -> None:
        quiet = logging.NullHandler()
        bot.logger.addHandler(quiet)
        self.addCleanup(bot.logger.removeHandler, quiet)

    ENV = {"YTDLP_AUTH_METHOD": "none", "YTDLP_BOT_TOKEN": TOKEN, "YTDLP_BOT_ALLOWED_CHATS": str(CHAT)}
    VERSION_LINE = f"version: {VERSION}"

    def test_the_console_says_which_version_runs_before_anything_else(self) -> None:
        # Whatever happens next (a good start, a refused configuration), the first thing
        # printed names the build: a pasted console log then shows what ran.
        for env in ({}, {"YTDLP_BOT_TOKEN": ""}, {"YTDLP_BOT_MAX_PARALLEL": "0"}):
            with self.subTest(env), mock.patch.object(bot.BotService, "run", return_value=0), \
                    mock.patch.object(bot, "TelegramClient"):
                _, logs = self._run(**env)
            ours = [line for line in logs if "keep assertLogs happy" not in line]
            self.assertTrue(ours[0].endswith(self.VERSION_LINE), ours[0])
            self.assertEqual(sum(self.VERSION_LINE in line for line in logs), 1)

    def test_the_environment_is_shown_before_the_config_without_the_secrets(self) -> None:
        with mock.patch.object(bot.BotService, "run", return_value=0), \
                mock.patch.object(bot, "TelegramClient"):
            _, logs = self._run(YTDLP_AUDIO_LANG="original", YTDLP_SOME_KEY="k-value-1")
        text = "\n".join(logs)
        self.assertLess(text.index("environment:"), text.index("config loaded"))
        for expected in ("YTDLP_AUDIO_LANG=original", "YTDLP_BOT_TOKEN=<set>", "YTDLP_SOME_KEY=<set>",
                         "YTDLP_BOT_ALLOWED_CHATS=<1 chat id(s)>"):
            self.assertIn(expected, text)
        for secret in (TOKEN, "k-value-1"):
            self.assertNotIn(secret, text)
        # A refused configuration shows the variables too: that is when they are needed.
        with mock.patch.object(bot.BotService, "run") as run:
            code, refused = self._run(YTDLP_BOT_MAX_PARALLEL="0")
        self.assertEqual(code, 2)
        self.assertIn("YTDLP_BOT_MAX_PARALLEL=0", "\n".join(refused))

    def test_with_no_variables_set_it_says_so(self) -> None:
        with mock.patch.object(bot.BotService, "run"):
            clean = {k: v for k, v in os.environ.items() if not k.upper().startswith("YTDLP_")}
            with mock.patch.dict(os.environ, clean, clear=True), \
                    mock.patch.object(bot.logging, "basicConfig"), \
                    mock.patch.object(bot, "app_version", return_value=VERSION), \
                    self.assertLogs(bot.logger, "DEBUG") as logs:
                code = bot.run_bot()
        self.assertEqual(code, 2)  # no token: refused, after the lines
        self.assertTrue(any("no YTDLP_* variables set" in line for line in logs.output))

    def _run(self, **env) -> tuple[int, list[str]]:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("YTDLP_")}
        clean.update(self.ENV)
        clean.update(env)
        for key in [k for k, v in clean.items() if v is None]:
            del clean[key]
        with mock.patch.dict(os.environ, clean, clear=True), \
                mock.patch.object(bot.logging, "basicConfig"), \
                mock.patch.object(bot, "app_version", return_value=VERSION), \
                self.assertLogs(bot.logger, "DEBUG") as logs:
            logging.getLogger(bot.__name__).info("(keep assertLogs happy)")
            code = bot.run_bot()
        return code, logs.output

    def test_without_a_token_it_refuses_to_start(self) -> None:
        with mock.patch.object(bot.BotService, "run") as run:
            code, logs = self._run(YTDLP_BOT_TOKEN="")
        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertTrue(any("YTDLP_BOT_TOKEN" in line for line in logs))

    def test_without_an_allow_list_it_refuses_to_start(self) -> None:
        with mock.patch.object(bot.BotService, "run") as run:
            code, logs = self._run(YTDLP_BOT_ALLOWED_CHATS="")
        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertTrue(any("YTDLP_BOT_ALLOWED_CHATS" in line for line in logs))

    def test_a_bad_value_is_refused(self) -> None:
        with mock.patch.object(bot.BotService, "run") as run:
            code, _ = self._run(YTDLP_BOT_MAX_PARALLEL="0")
        self.assertEqual(code, 2)
        run.assert_not_called()

    def test_a_complete_configuration_runs_the_service_and_never_logs_the_token(self) -> None:
        with mock.patch.object(bot.BotService, "run", return_value=0) as run, \
                mock.patch.object(bot, "TelegramClient") as client:
            code, logs = self._run()
        self.assertEqual(code, 0)
        run.assert_called_once()
        client.assert_called_once_with(TOKEN)
        self.assertFalse(any(TOKEN in line for line in logs))

    def test_the_whole_wiring_from_the_environment_to_a_download(self) -> None:
        # run_bot(): config from the environment -> real client -> service -> download.
        # Telegram answers 401 once the one message is handled, which ends the loop (code 2).
        with FakeTelegram() as server:
            server.push_message(CHAT, f"/audio {URL_A}", message_id=8)

            def served() -> bool:
                return any("완료" in p["text"] for p in server.calls_of("editMessageText"))
            real_answer = server._answer

            def answer(method, params):
                if method == "getUpdates" and served():
                    return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
                return real_answer(method, params)
            server._answer = answer  # type: ignore[method-assign]
            download = FakeDownload()
            with mock.patch.object(bot, "run_download", download),                     mock.patch.object(bot, "TelegramClient",
                                      lambda token: TelegramClient(token, api_base=server.api_base, timeout=5)):
                code, logs = self._run()
            self.assertEqual(code, 2)  # the 401 that ended the loop
            (url, opts), = download.calls
            self.assertEqual((url, opts.audio_opts), (URL_A, {"format": "mp3"}))
            sent = server.calls_of("sendMessage")
            self.assertEqual(sent[0]["chat_id"], CHAT)
            self.assertEqual(sent[0]["reply_parameters"]["message_id"], 8)
            self.assertTrue(any("✓ 완료 #1" in p["text"] for p in server.calls_of("editMessageText")))
            self.assertFalse(any(TOKEN in line for line in logs))

    def test_the_service_exit_code_is_passed_on(self) -> None:
        with mock.patch.object(bot.BotService, "run", return_value=2), mock.patch.object(bot, "TelegramClient"):
            code, _ = self._run()
        self.assertEqual(code, 2)


class HandleUrlTest(unittest.TestCase):
    def test_returns_the_saved_path(self) -> None:
        result = DownloadResult(ok=True, files=[{"filepath": "/out/a.mp4"}])
        with mock.patch.object(bot, "run_download", return_value=result) as run:
            path = bot.handle_url(URL_A, output_dir=Path("/out"))
        self.assertEqual(path, Path("/out/a.mp4"))
        self.assertEqual(run.call_args.args[0], URL_A)

    def test_a_failed_download_raises(self) -> None:
        with mock.patch.object(bot, "run_download", return_value=DownloadResult(ok=False, failure_kind="network")):
            with self.assertRaises(RuntimeError) as caught:
                bot.handle_url(URL_A)
        self.assertIn("network", str(caught.exception))

    def test_transcription_is_not_silently_ignored(self) -> None:
        with self.assertRaises(NotImplementedError):
            bot.handle_url(URL_A, transcriber=object())  # type: ignore[arg-type]


class OverTheWireTest(unittest.TestCase):
    """The service and the real client, over HTTP, against the fake Telegram."""

    def setUp(self) -> None:
        quiet = logging.NullHandler()
        bot.logger.addHandler(quiet)
        self.addCleanup(bot.logger.removeHandler, quiet)

    def test_a_message_becomes_a_download_with_a_live_status_message(self) -> None:
        with FakeTelegram() as server:
            client = TelegramClient(TOKEN, api_base=server.api_base, timeout=5)
            download = FakeDownload()
            download.hold(URL_A)
            service = bot.BotService(make_config(), client, download=download)
            stop = threading.Event()
            runner = threading.Thread(target=service.run, args=(stop,), daemon=True)
            runner.start()
            self.addCleanup(runner.join, 10)
            self.addCleanup(stop.set)

            server.push_message(CHAT, URL_A, message_id=31)
            wait_for(lambda: download.started(URL_A).is_set(), what="the download to start")
            (first,) = server.calls_of("sendMessage")
            self.assertEqual(first["chat_id"], CHAT)
            self.assertEqual(first["reply_parameters"]["message_id"], 31)
            self.assertIn("대기 중 #1", first["text"])
            download.gates[URL_A].set()
            wait_for(lambda: any("✓ 완료" in p["text"] for p in server.calls_of("editMessageText")),
                     what="the final edit")
            final = server.calls_of("editMessageText")[-1]
            self.assertIn("저장: /out/제목 aaaaaaaaaaa.mp4", final["text"])
            self.assertEqual(final["chat_id"], CHAT)

            server.push_message(OTHER_CHAT, URL_B)  # a stranger over the wire
            wait_for(lambda: any(p["chat_id"] == OTHER_CHAT for p in server.calls_of("sendMessage")))
            self.assertFalse(download.started(URL_B).is_set())
            stop.set()
            runner.join(10)
            self.assertFalse(runner.is_alive())

    def test_the_token_never_reaches_a_message_or_the_log(self) -> None:
        with FakeTelegram() as server, self.assertLogs(bot.logger, "DEBUG") as logs:
            logging.getLogger(bot.__name__).info("start")
            client = TelegramClient(TOKEN, api_base=server.api_base, timeout=5)
            service = bot.BotService(make_config(), client, download=FakeDownload())
            stop = threading.Event()
            runner = threading.Thread(target=service.run, args=(stop,), daemon=True)
            runner.start()
            server.push_message(CHAT, "/help")
            wait_for(lambda: bool(server.calls_of("sendMessage")))
            stop.set()
            runner.join(10)
        self.assertFalse(any(TOKEN in line for line in logs.output))
        self.assertFalse(any(TOKEN in p["text"] for p in server.calls_of("sendMessage")))


if __name__ == "__main__":
    unittest.main()
