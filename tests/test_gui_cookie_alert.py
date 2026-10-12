"""The GUI shows when the login cookies expire (DownloadResult.cookie_event).

Drives the real GUI with a withdrawn Tk window (skipped without a display), like
test_gui_batch: no downloads or background threads run, the real worker code does.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import downloader
from core.batch import BatchSummary
from core.cookie_health import CookieHealthStore
from core.downloader import DownloadResult
from core.history import HistoryStore
from interfaces.gui_jobs import job_row
from tests.test_cookie_health import write_cookies
from tests.test_failure import BOT, COOKIES_ROTATED


class CookieSummaryTest(unittest.TestCase):
    def test_an_expiry_says_the_cookies_must_be_extracted_again(self):
        summary = DownloadResult(ok=False, cookie_event="invalid").cookie_summary
        self.assertIn("쿠키 만료", summary)
        self.assertIn("다시 추출", summary)

    def test_a_replaced_login_says_the_new_cookies_were_seen(self):
        self.assertIn("새 쿠키", DownloadResult(ok=True, cookie_event="recovered").cookie_summary)

    def test_a_run_that_changed_nothing_says_nothing(self):
        self.assertEqual(DownloadResult(ok=True).cookie_summary, "")
        self.assertEqual(DownloadResult(ok=False, failure_kind="bot_blocked").cookie_summary, "")


class GuiCookieAlertTest(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
        except ImportError:
            self.skipTest("tkinter is unavailable")
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("Tk display is unavailable")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.root.update)  # run pending log appends before the window goes
        from interfaces import gui
        self.gui = gui
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.source = self.directory / "목록.txt"
        self.source.write_text("# 분야\nhttps://youtu.be/aaaaaaaaaaa\n", encoding="utf-8")
        self.health = CookieHealthStore(self.directory / "cookie_health.json")
        self.app = gui.DownloaderGUI(
            self.root, history=HistoryStore(self.directory / "history.json"),
            cookie_health=self.health,
        )
        self.app.output_path_var.set(str(self.directory))
        thread = mock.patch.object(
            gui.threading, "Thread",
            side_effect=lambda *, target, **kw: mock.Mock(start=target, is_alive=lambda: False),
        )
        thread.start()
        self.addCleanup(thread.stop)
        dialogs = mock.patch.object(gui, "messagebox")
        self.dialogs = dialogs.start()
        self.dialogs.askyesno.return_value = True
        self.addCleanup(dialogs.stop)
        self.root.update()

    def _download(self, result: DownloadResult) -> str:
        """Run one single download that ends with *result*; its row's status."""
        self.app.url_var.set("https://youtu.be/aaaaaaaaaaa")
        with mock.patch.object(self.gui, "run_download", return_value=result):
            self.app.start_download()
        self.root.update()
        row = self.app.jobs_tree.get_children()[-1]
        return self.app.jobs_tree.item(row, "values")[2]

    def _batch(self, result: DownloadResult) -> None:
        self.app.load_batch(str(self.source))

        def batch(items, opts, **kwargs):
            kwargs["item_callback"](0, items[0], "running", None)
            kwargs["item_callback"](0, items[0], "failed", result)
            return BatchSummary(items=items, states=["failed"], results=[result])

        with mock.patch.object(self.gui, "run_batch", side_effect=batch):
            self.app.start_batch()
        self.root.update()

    def test_an_expiry_is_shown_in_the_row_and_in_one_dialog(self):
        status = self._download(
            DownloadResult(ok=False, failure_kind="auth_expired", cookie_event="invalid")
        )
        self.assertIn("쿠키 만료", status)
        self.dialogs.showwarning.assert_called_once()
        title, text = self.dialogs.showwarning.call_args.args
        self.assertEqual(title, "쿠키 만료")
        self.assertIn("쿠키 자동 갱신", text)

    def test_a_successful_run_that_found_the_expiry_is_flagged_too(self):
        # A public video downloads without the login but yt-dlp still says it expired.
        status = self._download(DownloadResult(ok=True, title="공개 영상", cookie_event="invalid"))
        self.assertIn("쿠키 만료", status)
        self.dialogs.showwarning.assert_called_once()

    def test_a_replaced_login_is_noted_in_the_row_without_a_dialog(self):
        status = self._download(DownloadResult(ok=True, title="영상", cookie_event="recovered"))
        self.assertIn("새 쿠키 확인됨", status)
        self.dialogs.showwarning.assert_not_called()

    def test_a_run_without_a_cookie_event_shows_nothing(self):
        status = self._download(DownloadResult(ok=False, failure_kind="bot_blocked"))
        self.assertNotIn("쿠키", status)
        self.dialogs.showwarning.assert_not_called()

    def test_an_expiry_in_a_batch_is_shown_in_both_rows_and_one_dialog(self):
        self._batch(DownloadResult(ok=False, failure_kind="auth_expired", cookie_event="invalid"))
        self.assertIn("쿠키 만료", self.app.batch_tree.set("0", "state"))
        self.assertIn("쿠키 만료", job_row(self.app._batch_job, time.monotonic()).values[2])
        self.dialogs.showwarning.assert_called_once()

    def test_a_second_expiry_does_not_stack_another_dialog(self):
        # While the first dialog is open the Tk loop still runs, so another run's
        # event can arrive; it must not open a second dialog.
        self.dialogs.showwarning.side_effect = lambda *a: self.app._alert_cookie_expired()
        self.app._alert_cookie_expired()
        self.assertEqual(self.dialogs.showwarning.call_count, 1)
        self.assertFalse(self.app._cookie_alert_open)
        self.app._alert_cookie_expired()  # once closed, a later expiry is shown again
        self.assertEqual(self.dialogs.showwarning.call_count, 2)

    def test_no_dialog_while_the_window_is_closing(self):
        self.app._closing = True
        self.app._alert_cookie_expired()
        self.dialogs.showwarning.assert_not_called()

    def test_a_real_run_that_loses_the_login_reaches_the_screen_once(self):
        # Real run_download and cookie health store; only yt-dlp itself is canned.
        cookies = write_cookies(self.directory / "cookies.txt")
        self.app.auth_method_var.set("cookie-file")
        self.app.cookie_file_var.set(str(cookies))
        self.app.live_enabled_var.set(False)  # no live probe (it would start yt-dlp)

        def lost_login():
            out = downloader._Output()
            out.extend([BOT])
            out.add_warning(COOKIES_ROTATED)
            return (False, out, [], [])

        with mock.patch.object(downloader, "_run_once", side_effect=lambda *a, **k: lost_login()):
            first = self._download_url("https://youtu.be/aaaaaaaaaaa")
            self.assertIn("쿠키 만료", first)
            self.dialogs.showwarning.assert_called_once()
            self.assertFalse(self.health.is_healthy())
            # The same expiry is not announced again (it is already recorded).
            self._download_url("https://youtu.be/bbbbbbbbbbb")
        self.dialogs.showwarning.assert_called_once()

    def _download_url(self, url: str) -> str:
        self.app.url_var.set(url)
        self.app.start_download()
        self.root.update()
        row = self.app.jobs_tree.get_children()[-1]
        return self.app.jobs_tree.item(row, "values")[2]


if __name__ == "__main__":
    unittest.main()
