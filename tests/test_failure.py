"""Tests for core.failure and how run_download uses it.

Where each sample line comes from (few failure wordings have been seen in a real
run log, 2026-10-07: the HTTP 403, the ``No module named 'yt_dlp'`` traceback end,
and a network timeout only as a mid-run retry line. Replace the others when a log
has them):

* ``[log]``    a real run log (~/.ytdlp-downloader/logs, 2026-10-05 to 10-07)
* ``[source]`` yt-dlp's source (youtube/_base.py, _video.py, common.py)
* ``[youtube]`` YouTube's own text that yt-dlp passes on, from memory -
  neither in a log nor in the source. Least certain.
* ``[stdlib]`` Python / OS error texts (getaddrinfo, connection reset, ...)
  from memory, as yt-dlp wraps them. Equally uncertain.

Run from the repo root with::

    python -m unittest

Standard library only.
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest import mock

from core import downloader
from core.auth import BgutilPoTokenAuth, BrowserCookieAuth, NoAuth
from core.downloader import DownloadOptions
from core.failure import (
    FailureKind,
    classify_failure,
    counts_against_cookie_health,
    failure_hint,
    sidecar_unreachable,
)
from tests.test_downloader import _FakeProcess
from tests.test_run_log import REFRESH_GIVE_UP

HOWTO = (
    "Use --cookies-from-browser or --cookies for the authentication. See  "
    "https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  "
    "for how to manually pass cookies. Also see  "
    "https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies  "
    "for tips on effectively exporting YouTube cookies"
)  # [source] _youtube_login_hint, appended to every "sign in" reason

# [log] real, from two runs on 2026-10-05
SIDECAR_DOWN = (
    "WARNING: [youtube] [pot:bgutil:http] Error reaching GET http://127.0.0.1:4416/ping "
    "(caused by TransportError). Please make sure that the server is reachable at "
    "http://127.0.0.1:4416."
)
# [source] BaseInfoExtractor._request_webpage
COOKIES_ROTATED = (
    "WARNING: [youtube] The provided YouTube account cookies are no longer valid. "
    "They have likely been rotated in the browser as a security measure. For tips on how "
    "to effectively export YouTube cookies, refer to  "
    "https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies ."
)
# [source] _check_login_required
LOGIN_NEEDED = f"ERROR: [youtube:tab] Login details are needed to download this content. {HOWTO}"
# [youtube] the reason, with [source] the hint after it; YouTube writes a curly apostrophe
BOT = f"ERROR: [youtube] abc: Sign in to confirm you’re not a bot. {HOWTO}"
BOT_STRAIGHT = "ERROR: Sign in to confirm you're not a bot"  # the old test's spelling
# [youtube]
AGE_GATE = f"ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users. {HOWTO}"
PRIVATE = f"ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video. {HOWTO}"
PAYMENT = "ERROR: [youtube] abc: This video requires payment to watch"
MEMBERS = (
    "ERROR: [youtube] abc: Join this channel to get access to members-only content "
    "like this video, and other exclusive perks."
)
# [source] _video.py: subreason.startswith(...) -> raise_geo_restricted
REGION = "ERROR: [youtube] abc: The uploader has not made this video available in your country"
# [source] _video.py
RATE_LIMITED = (
    "ERROR: [youtube] abc: This content isn't available, try again later. Your account has "
    "been rate-limited by YouTube for up to an hour."
)
# [log] real, 2026-10-07: the GUI (packaged build) downloading a video with Korean + English subtitles
# asked for; yt-dlp stops the whole video when a subtitle fails (YoutubeDL._write_subtitles, unless
# ignoreerrors is True). The same video worked five minutes later: a transient throttle.
SUBTITLES_429 = "ERROR: Unable to download video subtitles for 'ko': HTTP Error 429: Too Many Requests"
# [stdlib] the same throttle on the video itself (the form is yt-dlp's, from memory)
HTTP_429 = "ERROR: unable to download video data: HTTP Error 429: Too Many Requests"
# [source] _video.py (the PO Token warning)
PO_TOKEN_MISSING = (
    "WARNING: [youtube] abc: web client https formats require a GVS PO Token which was not "
    "provided. They will be skipped as they may yield HTTP Error 403."
)
# [log] real, 2026-10-07: the Telegram bot's first /audio of a video with 21 audio tracks. It was a
# transient failure: the same link worked on the next try, and re-running yt-dlp's exact command
# four times (one alone, three at once) did too.
HTTP_403 = "ERROR: unable to download video data: HTTP Error 403: Forbidden"
OTHER_ERROR = "ERROR: [youtube] abc: Requested format is not available"

# Connection errors. yt-dlp prints "[download] Got error: <error>. Retrying (n/m)..." while it
# retries (plain output, not an ERROR line) and "ERROR: \r[download] Got error: <error>. Giving up
# after n retries" when it stops (downloader/common.py report_retry -> RetryManager.report_retry).
_NET_TEXT = "HTTPSConnectionPool(host='rr12---sn-3u-bh2zy.googlevideo.com', port=443): Read timed out."
# [log] real, 2026-10-05, 233 times in one run: the mid-run line (the only form real logs have)
NETWORK_RETRYING = f"[download] Got error: {_NET_TEXT}. Retrying (1/30)..."
# [log] the error text + [source] the give-up wrapper
NETWORK_TIMEOUT = f"ERROR: \r[download] Got error: {_NET_TEXT}. Giving up after 29 retries"
# [source] networking/exceptions.py IncompleteRead: "N bytes read, M more expected" (the real log
# shows "N bytes read. Retrying", masked)
NETWORK_SHORT_READ = (
    "ERROR: \r[download] Got error: 1048576 bytes read, 524288 more expected. Giving up after 9 retries")
# [stdlib] Python / OS error texts from memory, as yt-dlp wraps them. Least certain.
NETWORK_DNS = (
    "ERROR: [youtube] abc: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo "
    "failed> (caused by URLError(gaierror(11001, 'getaddrinfo failed')))")
NETWORK_NAME = (
    "ERROR: [youtube] abc: Unable to download webpage: <urlopen error [Errno -3] Temporary "
    "failure in name resolution>")
NETWORK_ABORTED = (
    "ERROR: unable to download video data: ('Connection aborted.', ConnectionResetError(10054, "
    "'An existing connection was forcibly closed by the remote host', None, 10054, None))")
NETWORK_RESET = "ERROR: [youtube] abc: Unable to download webpage: [Errno 104] Connection reset by peer"
NETWORK_CLOSED = (
    "ERROR: [youtube] abc: Unable to download webpage: Remote end closed connection without response")
# An extractor retry that is still going on (WARNING, from yt-dlp's source).
NETWORK_RETRY_WARNING = (
    "WARNING: [youtube] abc: Unable to download webpage: The read operation timed out. Retrying (1/3)...")

# yt-dlp could not even start. [log] real, 2026-10-07: a source-mode run whose PATH held a stale
# yt-dlp.exe launcher; the traceback's last line is not an "ERROR:" line.
YT_DLP_MODULE_MISSING = "ModuleNotFoundError: No module named 'yt_dlp'"
YT_DLP_TRACEBACK = [
    "Traceback (most recent call last):",
    '  File "<frozen runpy>", line 198, in _run_module_as_main',
    '  File "C:\\Users\\user\\AppData\\Roaming\\Python\\Python39\\Scripts\\yt-dlp.EXE\\__main__.py", line 2, in <module>',
    "    from yt_dlp import main",
    YT_DLP_MODULE_MISSING,
]
# [stdlib] distlib's launcher message, from memory.
LAUNCHER_BROKEN = (
    "Fatal error in launcher: Unable to create process using '\"C:\\Python39\\python.exe\" "
    "\"C:\\Python39\\Scripts\\yt-dlp.exe\"'")


class ClassifyFailureTest(unittest.TestCase):
    def kind(self, *lines: str) -> FailureKind:
        return classify_failure(lines)

    def test_each_cause(self) -> None:
        cases = {
            FailureKind.AUTH_EXPIRED: [COOKIES_ROTATED, LOGIN_NEEDED],
            FailureKind.BOT_BLOCKED: [BOT, BOT_STRAIGHT],
            FailureKind.CONTENT_ACCESS: [AGE_GATE, PRIVATE, PAYMENT, MEMBERS, REGION],
            FailureKind.RATE_LIMITED: [RATE_LIMITED, SUBTITLES_429, HTTP_429],
            FailureKind.PO_TOKEN: [HTTP_403, PO_TOKEN_MISSING],
            FailureKind.NETWORK: [
                NETWORK_TIMEOUT, NETWORK_SHORT_READ, NETWORK_DNS, NETWORK_NAME,
                NETWORK_ABORTED, NETWORK_RESET, NETWORK_CLOSED,
            ],
            FailureKind.YT_DLP_MISSING: [
                YT_DLP_MODULE_MISSING, 'ModuleNotFoundError: No module named "yt_dlp"', LAUNCHER_BROKEN,
            ],
            FailureKind.OTHER: [OTHER_ERROR, "ERROR: Did not get any data blocks", ""],
        }
        for expected, lines in cases.items():
            for line in lines:
                with self.subTest(expected=expected.value, line=line[:50]):
                    self.assertIs(self.kind(line), expected)

    def test_no_lines_is_other(self) -> None:
        self.assertIs(classify_failure([]), FailureKind.OTHER)

    def test_signing_in_is_not_always_a_bot_block_or_an_expired_login(self) -> None:
        # Every "sign in" reason carries the cookies hint.
        for line in (AGE_GATE, PRIVATE):
            self.assertIs(self.kind(line), FailureKind.CONTENT_ACCESS)

    def test_a_403_alone_is_a_po_token_matter_not_an_expired_cookie(self) -> None:
        kind = self.kind(HTTP_403)
        self.assertIs(kind, FailureKind.PO_TOKEN)
        self.assertFalse(counts_against_cookie_health(kind, uses_cookies=True))

    def test_stronger_statements_win(self) -> None:
        # yt-dlp's own "cookies are no longer valid" beats the bot block that follows it.
        self.assertIs(self.kind(BOT, COOKIES_ROTATED), FailureKind.AUTH_EXPIRED)
        # A bot block beats a 403, and a restriction beats a throttle.
        self.assertIs(self.kind(HTTP_403, BOT), FailureKind.BOT_BLOCKED)
        self.assertIs(self.kind(RATE_LIMITED, PRIVATE), FailureKind.CONTENT_ACCESS)

    def test_a_connection_error_that_is_still_being_retried_is_not_a_failure_cause(self) -> None:
        # What real logs show 233 times: yt-dlp is retrying, the attempt may yet work.
        self.assertIs(self.kind(NETWORK_RETRYING), FailureKind.OTHER)
        self.assertIs(self.kind(NETWORK_RETRY_WARNING), FailureKind.OTHER)
        # A transient timeout that recovered must not explain a run that failed for another reason.
        self.assertIs(self.kind(OTHER_ERROR, NETWORK_RETRY_WARNING), FailureKind.OTHER)

    def test_giving_up_alone_is_not_a_network_error(self) -> None:
        # The live URL refresh warning ends "Giving up after 3 retries" too.
        self.assertIs(self.kind(REFRESH_GIVE_UP), FailureKind.OTHER)
        self.assertIs(self.kind("ERROR: Giving up after 3 retries"), FailureKind.OTHER)

    def test_the_unreachable_sidecar_is_not_the_network(self) -> None:
        self.assertIs(self.kind(SIDECAR_DOWN), FailureKind.OTHER)
        # Even if its message mentioned a timeout: the sidecar has its own hint.
        self.assertIs(
            self.kind("WARNING: [youtube] [pot:bgutil:http] Error reaching GET http://127.0.0.1:4416/ping "
                      "(caused by TransportError('timed out'))"),
            FailureKind.OTHER)

    def test_a_network_error_is_the_weakest_statement(self) -> None:
        self.assertIs(self.kind(NETWORK_TIMEOUT, HTTP_403), FailureKind.PO_TOKEN)
        self.assertIs(self.kind(NETWORK_TIMEOUT, BOT), FailureKind.BOT_BLOCKED)
        self.assertIs(self.kind(NETWORK_TIMEOUT, PRIVATE), FailureKind.CONTENT_ACCESS)
        self.assertIs(self.kind(NETWORK_TIMEOUT, RATE_LIMITED), FailureKind.RATE_LIMITED)
        self.assertIs(self.kind(NETWORK_TIMEOUT, COOKIES_ROTATED), FailureKind.AUTH_EXPIRED)

    def test_yt_dlp_that_did_not_start_beats_every_other_statement(self) -> None:
        for other in (BOT, COOKIES_ROTATED, HTTP_403, PRIVATE, NETWORK_TIMEOUT, RATE_LIMITED):
            with self.subTest(other[:40]):
                self.assertIs(self.kind(other, YT_DLP_MODULE_MISSING), FailureKind.YT_DLP_MISSING)

    def test_a_429_is_a_throttle_not_an_unknown_failure(self) -> None:
        # It ended as "other", with no advice at all, in the real run of 2026-10-07.
        self.assertIs(self.kind(SUBTITLES_429), FailureKind.RATE_LIMITED)
        self.assertFalse(counts_against_cookie_health(self.kind(SUBTITLES_429), uses_cookies=True))

    def test_a_throttle_loses_to_a_restriction_and_beats_a_bare_403(self) -> None:
        self.assertIs(self.kind(SUBTITLES_429, PRIVATE), FailureKind.CONTENT_ACCESS)
        self.assertIs(self.kind(HTTP_403, SUBTITLES_429), FailureKind.RATE_LIMITED)

    def test_the_word_429_inside_something_else_is_not_a_throttle(self) -> None:
        self.assertIs(self.kind("ERROR: [youtube] abc: Requested format 14294 is not available"),
                      FailureKind.OTHER)
        self.assertIs(self.kind("ERROR: fragment 4290 of 5000 failed"), FailureKind.OTHER)

    def test_another_missing_module_is_not_a_missing_yt_dlp(self) -> None:
        # yt-dlp started and one of its optional pieces is missing: not this verdict.
        self.assertIs(self.kind("ModuleNotFoundError: No module named 'brotli'"), FailureKind.OTHER)
        self.assertIs(self.kind("ERROR: No module named 'curl_cffi'"), FailureKind.OTHER)

    def test_the_word_bot_inside_another_word_is_not_a_bot_block(self) -> None:
        # The old check matched "bot" in "both" next to "detected".
        line = "ERROR: both audio and video detected, but neither could be merged"
        self.assertIs(self.kind(line), FailureKind.OTHER)
        self.assertIs(self.kind("ERROR: bot detection triggered, please confirm"),
                      FailureKind.BOT_BLOCKED)

    def test_case_does_not_matter(self) -> None:
        self.assertIs(self.kind(BOT.upper()), FailureKind.BOT_BLOCKED)


class CookieHealthPolicyTest(unittest.TestCase):
    def test_only_a_lost_login_with_cookies_counts(self) -> None:
        for kind in FailureKind:
            for uses_cookies in (True, False):
                with self.subTest(kind=kind.value, uses_cookies=uses_cookies):
                    expected = kind is FailureKind.AUTH_EXPIRED and uses_cookies
                    self.assertEqual(
                        counts_against_cookie_health(kind, uses_cookies=uses_cookies), expected)


class SidecarTest(unittest.TestCase):
    def test_the_real_log_line_is_recognised(self) -> None:
        self.assertTrue(sidecar_unreachable([SIDECAR_DOWN]))

    def test_other_lines_are_not(self) -> None:
        self.assertFalse(sidecar_unreachable([PO_TOKEN_MISSING, HTTP_403, OTHER_ERROR]))
        self.assertFalse(sidecar_unreachable([]))

    def test_an_unreachable_sidecar_alone_does_not_decide_the_cause(self) -> None:
        # Seen on runs that finished fine, so it is not evidence by itself.
        self.assertIs(classify_failure([SIDECAR_DOWN, OTHER_ERROR]), FailureKind.OTHER)


class HintTest(unittest.TestCase):
    def test_every_known_cause_has_a_hint_and_an_unknown_one_has_none(self) -> None:
        for kind in FailureKind:
            hint = failure_hint(kind)
            self.assertEqual(bool(hint), kind is not FailureKind.OTHER, kind)

    def test_the_hint_does_not_blame_the_cookie_for_a_403_or_a_bot_block(self) -> None:
        self.assertIn("PO Token", failure_hint(FailureKind.PO_TOKEN))
        self.assertNotIn("다시 추출", failure_hint(FailureKind.PO_TOKEN))

    def test_a_403_hint_says_it_may_pass_before_sending_anyone_to_the_po_token(self) -> None:
        # The only real 403 so far was transient, and the user runs no PO Token sidecar: the
        # advice to check one came first and sent them looking in the wrong place.
        hint = failure_hint(FailureKind.PO_TOKEN)
        self.assertIn("일시적일 수 있", hint)
        self.assertIn("다시 시도", hint)
        self.assertLess(hint.index("다시 시도"), hint.index("PO Token(bgutil"))
        self.assertIn("단정할 수는 없", failure_hint(FailureKind.BOT_BLOCKED, uses_cookies=True))

    def test_the_hint_depends_on_whether_cookies_were_in_use(self) -> None:
        self.assertIn("다시 추출", failure_hint(FailureKind.AUTH_EXPIRED, uses_cookies=True))
        self.assertIn("설정", failure_hint(FailureKind.AUTH_EXPIRED, uses_cookies=False))

    def test_an_unreachable_sidecar_is_mentioned(self) -> None:
        self.assertIn("사이드카", failure_hint(FailureKind.PO_TOKEN, sidecar_down=True))
        self.assertNotIn("Error reaching", failure_hint(FailureKind.PO_TOKEN))

    def test_a_missing_yt_dlp_says_how_to_fix_it(self) -> None:
        hint = failure_hint(FailureKind.YT_DLP_MISSING)
        self.assertIn("pip install", hint)
        self.assertIn("where yt-dlp", hint)
        self.assertNotIn("다시 추출", hint)

    def test_a_network_failure_points_at_the_connection_not_the_cookie(self) -> None:
        for uses_cookies in (True, False):
            hint = failure_hint(FailureKind.NETWORK, uses_cookies=uses_cookies)
            self.assertIn("인터넷 연결", hint)
            self.assertNotIn("다시 추출", hint)


class RunOnceWarningsTest(unittest.TestCase):
    """_run_once keeps WARNING lines apart from the output."""

    OPTS = DownloadOptions(url="https://youtu.be/x", format_opt="best", output_template="o")

    def _run(self, lines: list[str], returncode: int = 1):
        with mock.patch.object(downloader, "yt_dlp_command", return_value=["yt-dlp"]), \
                mock.patch.object(downloader.subprocess, "Popen", return_value=_FakeProcess(
                    [(line + "\n").encode("utf-8") for line in lines], returncode)):
            return downloader._run_once(self.OPTS, lambda _i: None, lambda _l, _lv: None)

    def test_warnings_are_collected_once_and_stay_out_of_the_output(self) -> None:
        ok, captured, _, _ = self._run([COOKIES_ROTATED, COOKIES_ROTATED, SIDECAR_DOWN, BOT])
        self.assertFalse(ok)
        self.assertEqual(captured.warnings, [COOKIES_ROTATED, SIDECAR_DOWN])
        self.assertEqual(captured, [BOT])
        self.assertEqual(downloader._failure_lines(captured), [BOT, COOKIES_ROTATED, SIDECAR_DOWN])

    def test_a_plain_list_is_still_accepted(self) -> None:
        self.assertEqual(downloader._failure_lines([BOT, "[download] 5%"]), [BOT])

    def test_the_lines_saying_yt_dlp_did_not_start_are_failure_lines_too(self) -> None:
        # The traceback's last line is not an "ERROR:" line, so it used to be dropped.
        lines = [*YT_DLP_TRACEBACK, "ERROR: later", LAUNCHER_BROKEN, "ImportError: x", "[download] 5%"]
        self.assertEqual(downloader._failure_lines(lines),
                         [YT_DLP_MODULE_MISSING, "ERROR: later", LAUNCHER_BROKEN, "ImportError: x"])

    def test_the_number_of_remembered_warnings_is_capped(self) -> None:
        lines = [f"WARNING: [youtube] problem {n}" for n in range(downloader._Output.MAX_WARNINGS + 20)]
        _, captured, _, _ = self._run(lines)
        self.assertEqual(len(captured.warnings), downloader._Output.MAX_WARNINGS)


class RunDownloadFailureKindTest(unittest.TestCase):
    URL = "https://youtu.be/x"

    @staticmethod
    def _output(errors: list[str], warnings: list[str] = ()):
        out = downloader._Output()
        out.extend(errors)
        for line in warnings:
            out.add_warning(line)
        return out

    def _run(self, auth, *runs):
        opts = DownloadOptions(url=self.URL, format_opt="best", output_template="o", auth=auth)
        results = iter(runs)
        shown: list[tuple[str, str]] = []

        def fake(opts, pcb, lcb, cancel_event=None):
            return next(results)
        with mock.patch.object(downloader, "_run_once", fake):
            result = downloader.run_download(
                self.URL, opts, log_callback=lambda line, level: shown.append((line, level)))
        return result, shown

    def test_a_lost_login_is_reported_with_cookies_advice(self) -> None:
        cookies = BrowserCookieAuth(browser="chrome")
        result, shown = self._run(cookies, (False, self._output([BOT], [COOKIES_ROTATED]), [], []))
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "auth_expired")
        self.assertTrue(any("다시 추출" in line for line, _ in shown))
        self.assertIn("failure kind: auth_expired",
                      Path(result.log_path).read_text(encoding="utf-8"))

    def test_a_403_points_to_the_po_token_not_the_cookie(self) -> None:
        for auth in (BrowserCookieAuth(browser="chrome"), BgutilPoTokenAuth()):
            # The 403 is tried once more by itself (HTTP_403_RETRY_DELAY): it fails the same way.
            with mock.patch.object(downloader, "HTTP_403_RETRY_DELAY", 0.0):
                result, shown = self._run(
                    auth, (False, self._output([HTTP_403], [SIDECAR_DOWN]), [], []),
                    (False, self._output([HTTP_403], [SIDECAR_DOWN]), [], []))
            self.assertEqual(result.failure_kind, "po_token")
            hints = [line for line, _ in shown if "PO Token" in line]
            self.assertTrue(hints)
            self.assertTrue(any("사이드카 응답 실패" in line for line in hints))
            self.assertFalse(any("다시 추출" in line for line, _ in shown))

    def test_a_429_on_the_video_itself_gets_the_plain_advice(self) -> None:
        result, shown = self._run(NoAuth(), (False, self._output([HTTP_429]), [], []))
        self.assertEqual(result.failure_kind, "rate_limited")
        self.assertTrue(any("잠시 뒤 다시 시도" in line for line, _ in shown))
        self.assertFalse(any("자막" in line for line, _ in shown))

    def test_a_dropped_connection_is_named_and_not_blamed_on_the_cookie(self) -> None:
        # Real runs print the retry line many times and then give up.
        for auth in (BrowserCookieAuth(browser="chrome"), NoAuth()):
            result, shown = self._run(
                auth, (False, self._output([NETWORK_TIMEOUT], [SIDECAR_DOWN]), [], []))
            self.assertEqual(result.failure_kind, "network")
            self.assertTrue(any("인터넷 연결" in line for line, _ in shown))
            self.assertFalse(any("다시 추출" in line for line, _ in shown))
            self.assertFalse(any("자동 재시도" in line for line, _ in shown))  # no Chrome retry

    def test_the_real_run_whose_yt_dlp_did_not_start_is_named_and_explained(self) -> None:
        # The 2026-10-07 run end to end: the process prints the traceback and exits 1.
        lines = [(line + "\n").encode("utf-8") for line in YT_DLP_TRACEBACK]
        shown: list[tuple[str, str]] = []
        opts = DownloadOptions(url=self.URL, format_opt="best", output_template="o")
        # Popen is patched for the whole process, and app_version() runs `git describe` through it
        # (once; it is cached): pin the version so the test does not depend on what ran before it.
        with mock.patch.object(downloader, "app_version", return_value="test"), \
                mock.patch.object(downloader, "yt_dlp_command", return_value=["yt-dlp"]), \
                mock.patch.object(downloader.subprocess, "Popen", return_value=_FakeProcess(lines, 1)):
            result = downloader.run_download(
                self.URL, opts, log_callback=lambda line, level: shown.append((line, level)))
        self.assertEqual(result.failure_kind, "yt_dlp_missing")
        self.assertTrue(any("pip install" in line for line, _ in shown))
        self.assertIn("failure kind: yt_dlp_missing", Path(result.log_path).read_text(encoding="utf-8"))

    def test_an_unknown_failure_is_other_and_gets_no_hint(self) -> None:
        result, shown = self._run(NoAuth(), (False, self._output([OTHER_ERROR]), [], []))
        self.assertEqual(result.failure_kind, "other")
        self.assertEqual([line for line, _ in shown if "다운로드 실패" not in line
                          and not line.startswith("yt-dlp")], [])

    def test_success_and_cancel_have_no_failure_kind(self) -> None:
        result, _ = self._run(NoAuth(), (True, [], [], []))
        self.assertEqual(result.failure_kind, "")
        event = threading.Event()
        event.set()
        with mock.patch.object(downloader, "_run_once") as run_once:
            result = downloader.run_download(self.URL, DownloadOptions(
                url=self.URL, format_opt="best", output_template="o"), cancel_event=event)
        run_once.assert_not_called()
        self.assertEqual(result.failure_kind, "")

    def test_a_bot_block_without_auth_is_still_retried_with_chrome_cookies(self) -> None:
        first = {"title": "a", "filepath": "/o/a.mp4"}
        result, shown = self._run(
            NoAuth(),
            (False, self._output([BOT]), [], []),
            (True, [], [first], []),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.failure_kind, "")
        self.assertTrue(any("Chrome 쿠키로 자동 재시도" in line for line, _ in shown))

    def test_a_bot_block_with_cookies_is_not_retried_and_not_called_an_expired_cookie(self) -> None:
        result, shown = self._run(
            BrowserCookieAuth(browser="chrome"), (False, self._output([BOT]), [], []))
        self.assertEqual(result.failure_kind, "bot_blocked")
        self.assertFalse(any("자동 재시도" in line for line, _ in shown))
        self.assertTrue(any("단정할 수는 없" in line for line, _ in shown))

    def test_a_failed_retry_keeps_the_first_cause_unless_the_retry_says_more(self) -> None:
        locked = "ERROR: Could not copy Chrome cookie database."
        result, _ = self._run(
            NoAuth(), (False, self._output([BOT]), [], []), (False, self._output([locked]), [], []))
        self.assertEqual(result.failure_kind, "bot_blocked")
        result, _ = self._run(
            NoAuth(), (False, self._output([BOT]), [], []),
            (False, self._output([], [COOKIES_ROTATED]), [], []))
        self.assertEqual(result.failure_kind, "auth_expired")

    def test_a_both_and_detected_in_the_output_no_longer_triggers_the_chrome_retry(self) -> None:
        line = "ERROR: both audio and video detected, but neither could be merged"
        result, shown = self._run(NoAuth(), (False, self._output([line]), [], []))
        self.assertEqual(result.failure_kind, "other")
        self.assertFalse(any("자동 재시도" in text for text, _ in shown))


if __name__ == "__main__":
    unittest.main()
