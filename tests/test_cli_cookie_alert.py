"""The CLI shows when the login cookies expire (DownloadResult.cookie_event).

Both entry points (``run_cli`` and ``run_batch_cli``) run with their menus,
history and downloader replaced; the real printing code runs.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from core import downloader
from core.auth import CookieFileAuth, NoAuth
from core.batch import BatchSummary
from core.cookie_health import CookieHealthStore
from core.downloader import DownloadResult
from core.live import LiveOptions
from interfaces import cli
from tests.test_cookie_health import write_cookies
from tests.test_failure import BOT, COOKIES_ROTATED

URL = "https://youtu.be/aaaaaaaaaaa"
NOTICE = "쿠키의 로그인이 만료된 것으로 확인되었습니다."


class _CliTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.output = os.path.join(str(self.dir), "%(title)s.%(ext)s")
        self.auth = NoAuth()
        for name, value in [
            ("_check_js_runtime_interactive", None),
            ("get_download_options", ("1", "best")),
            ("get_subtitle_options", {}),
            ("get_audio_track_option", ("원본", None)),
            ("get_output_options", self.output),
        ]:
            patcher = mock.patch.object(cli, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(cli, "get_cookie_options", side_effect=lambda: self.auth)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.history = mock.Mock()
        patcher = mock.patch.object(cli, "HistoryStore", return_value=self.history)
        patcher.start()
        self.addCleanup(patcher.stop)
        # A real store would be the user's file; every run here gets a temp one.
        patcher = mock.patch.object(
            cli, "CookieHealthStore", return_value=CookieHealthStore(self.dir / "cookie_health.json")
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class RunCliTest(_CliTest):
    def _run(self, result: DownloadResult) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(cli, "get_url", return_value=URL), \
                mock.patch.object(cli, "run_download", return_value=result), redirect_stdout(out):
            code = cli.run_cli()
        return code, out.getvalue()

    def test_an_expiry_is_announced_with_what_to_do(self) -> None:
        code, out = self._run(DownloadResult(ok=False, failure_kind="auth_expired", cookie_event="invalid"))
        self.assertEqual(code, 1)
        self.assertIn(NOTICE, out)
        self.assertIn("7번(쿠키 자동 갱신)", out)
        self.assertIn("1~3번", out)

    def test_a_successful_run_that_found_the_expiry_is_announced_too(self) -> None:
        # A public video downloads without the login, but yt-dlp still said it expired.
        code, out = self._run(DownloadResult(ok=True, title="공개 영상", cookie_event="invalid"))
        self.assertEqual(code, 0)  # the exit code is unchanged
        self.assertIn(NOTICE, out)

    def test_other_runs_say_nothing_about_cookies(self) -> None:
        for name, result in {
            "ok": DownloadResult(ok=True, title="영상"),
            "replaced login": DownloadResult(ok=True, title="영상", cookie_event="recovered"),
            "bot block": DownloadResult(ok=False, failure_kind="bot_blocked"),
        }.items():
            with self.subTest(name):
                _, out = self._run(result)
                self.assertNotIn(NOTICE, out)

    def test_a_real_run_that_loses_the_login_reaches_the_screen(self) -> None:
        # Real run_download and cookie health store; only yt-dlp itself is canned.
        cookies = write_cookies(self.dir / "cookies.txt")
        self.auth = CookieFileAuth(path=str(cookies))

        def lost_login(*_args, **_kwargs):
            out = downloader._Output()
            out.extend([BOT])
            out.add_warning(COOKIES_ROTATED)
            return (False, out, [], [])

        out = io.StringIO()
        with mock.patch.object(cli, "get_url", return_value=URL), \
                mock.patch.object(downloader, "_run_once", side_effect=lost_login), \
                mock.patch("core.live.detect_live_status", side_effect=AssertionError("probe")), \
                mock.patch.object(cli, "LiveOptions", lambda **kw: LiveOptions(enabled=False, **kw)), \
                redirect_stdout(out):
            self.assertEqual(cli.run_cli(), 1)
            first = out.getvalue()
            self.assertEqual(first.count(NOTICE), 1)
            # The same expiry is already recorded: a second run does not announce it again.
            cli.run_cli()
        self.assertEqual(out.getvalue().count(NOTICE), 1)


class RunBatchCliTest(_CliTest):
    def setUp(self) -> None:
        super().setUp()
        self.list = self.dir / "목록.txt"
        self.list.write_text(
            "# 요리\nhttps://youtu.be/aaaaaaaaaaa\n# 투자\nhttps://youtu.be/bbbbbbbbbbb\n",
            encoding="utf-8",
        )

    def _run(self, events: tuple[str, ...]) -> str:
        """Run a two-item batch whose items end with the given cookie events."""
        def fake_run_batch(items, base_opts, *, item_callback, **_kw):
            results = []
            for index, (item, event) in enumerate(zip(items, events)):
                result = DownloadResult(ok=False, title=f"영상{index}", cookie_event=event)
                item_callback(index, item, "failed", result)
                results.append(result)
            return BatchSummary(items=list(items), states=["failed"] * len(items), results=results)

        out = io.StringIO()
        with mock.patch.object(cli, "run_batch", side_effect=fake_run_batch), \
                mock.patch("builtins.input", return_value=""), redirect_stdout(out):
            cli.run_batch_cli(str(self.list))
        return out.getvalue()

    def test_an_expiry_is_announced_when_it_happens_and_again_at_the_end(self) -> None:
        out = self._run(("", "invalid"))
        self.assertEqual(out.count(NOTICE), 1)  # the full notice, at the item that found it
        self.assertLess(out.index("[2/2]"), out.index(NOTICE))
        self.assertIn("이번 실행 중 쿠키 만료가 확인되었습니다 (2/2 번째)", out)
        self.assertLess(out.index("결과:"), out.index("이번 실행 중 쿠키 만료"))

    def test_no_expiry_no_notice(self) -> None:
        out = self._run(("", "recovered"))
        self.assertNotIn(NOTICE, out)
        self.assertNotIn("이번 실행 중 쿠키 만료", out)


if __name__ == "__main__":
    unittest.main()
