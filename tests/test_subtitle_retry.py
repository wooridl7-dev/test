"""A subtitle that cannot be downloaded must not cost the whole video.

Found on 2026-10-07 in a real run of the packaged GUI: ``Unable to download video
subtitles for 'ko': HTTP Error 429: Too Many Requests``. yt-dlp stops the video
at a failed subtitle unless it is told to ignore errors, so a transient throttle
on a side file lost a 119-item batch's item. The video is fetched again without
the subtitles, once, and the result says so (``subtitles_skipped``).
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from core import downloader
from core.batch import BatchSummary
from core.downloader import DownloadOptions, DownloadResult
from core.failure import subtitle_download_failed
from core.history import HistoryEntry, HistoryStore
from interfaces import cli
from tests.test_cli_cookie_alert import _CliTest
from tests.test_failure import BOT, HTTP_429, OTHER_ERROR, SUBTITLES_429

URL = "https://youtu.be/aaaaaaaaaaa"
SUBS = {"writesubtitles": True, "writeautomaticsub": True, "subtitleslangs": ["ko", "en"]}
SUBTITLES_404 = "ERROR: Unable to download video subtitles for 'en': HTTP Error 404: Not Found"
RECORD_A = {"title": "a", "filepath": "/o/a.mp4"}
RECORD_B = {"title": "b", "filepath": "/o/b.mp4"}


def output(*errors: str) -> downloader._Output:
    out = downloader._Output()
    out.extend(errors)
    return out


class DetectionTest(unittest.TestCase):
    def test_the_real_log_line_and_other_causes(self) -> None:
        self.assertTrue(subtitle_download_failed([SUBTITLES_429]))
        self.assertTrue(subtitle_download_failed([SUBTITLES_404]))  # whatever the reason

    def test_other_failures_are_not_a_subtitle_failure(self) -> None:
        self.assertFalse(subtitle_download_failed([HTTP_429, OTHER_ERROR, BOT]))
        self.assertFalse(subtitle_download_failed(["There are no subtitles for the requested languages"]))
        self.assertFalse(subtitle_download_failed([]))


class RetryTest(unittest.TestCase):
    def run_once(self, *runs, subtitle_opts=SUBS, cancel=None):
        """run_download over canned _run_once outcomes. Returns (result, log lines,
        the subtitle options each attempt was given)."""
        opts = DownloadOptions(url=URL, format_opt="best", output_template="o",
                               subtitle_opts=dict(subtitle_opts), download_archive="archive.txt")
        outcomes = iter(runs)
        given: list[dict] = []
        shown: list[tuple[str, str]] = []
        self.archives: list = []  # the download archive each attempt was given

        def fake(o, pcb, lcb, cancel_event=None):
            given.append(dict(o.subtitle_opts))
            self.archives.append(o.download_archive)
            return next(outcomes)
        with mock.patch.object(downloader, "_run_once", fake):
            result = downloader.run_download(
                URL, opts, log_callback=lambda line, level: shown.append((line, level)), cancel_event=cancel)
        return result, shown, given

    def test_the_video_is_fetched_again_without_the_subtitles(self) -> None:
        result, shown, given = self.run_once(
            (False, output(SUBTITLES_429), [], []), (True, [], [RECORD_A], []))
        self.assertTrue(result.ok)
        self.assertTrue(result.subtitles_skipped)
        self.assertEqual(result.files, [RECORD_A])
        self.assertEqual(given, [SUBS, {}])  # the second try asked for no subtitles at all
        text = " ".join(line for line, _ in shown)
        self.assertIn("자막 없이 영상만 다시 받습니다", text)
        self.assertIn("자막 없이 영상만 받았습니다", text)
        self.assertTrue(all(level == "warning" for line, level in shown if "자막" in line))
        self.assertIn("subtitles skipped", Path(result.log_path).read_text(encoding="utf-8"))

    def test_the_retry_leaves_the_video_out_of_the_download_archive(self) -> None:
        # An archived video is skipped whole next time ("has already been recorded in the
        # archive", before any subtitle: checked with yt-dlp), so recording it would leave
        # its subtitles unfetched for good. Left out, the next run writes them first.
        self.run_once((False, output(SUBTITLES_429), [], []), (True, [], [RECORD_A], []))
        self.assertEqual(self.archives, ["archive.txt", None])

    def test_a_download_that_needs_no_retry_keeps_its_archive(self) -> None:
        self.run_once((True, [], [RECORD_A], []))
        self.assertEqual(self.archives, ["archive.txt"])

    def test_any_reason_for_the_subtitle_failure_counts(self) -> None:
        result, _, given = self.run_once(
            (False, output(SUBTITLES_404), [], []), (True, [], [RECORD_A], []))
        self.assertTrue(result.subtitles_skipped)
        self.assertEqual(len(given), 2)

    def test_it_is_tried_only_once(self) -> None:
        # The retry asks for no subtitles, but suppose its output still blames one:
        # no third attempt, no loop.
        result, _, given = self.run_once(
            (False, output(SUBTITLES_429), [], []), (False, output(SUBTITLES_429), [], []))
        self.assertFalse(result.ok)
        self.assertEqual(len(given), 2)

    def test_a_failure_that_is_not_about_subtitles_is_not_retried(self) -> None:
        result, _, given = self.run_once((False, output(HTTP_429), [], []))
        self.assertEqual((result.ok, result.failure_kind, len(given)), (False, "rate_limited", 1))
        self.assertFalse(result.subtitles_skipped)

    def test_nothing_is_retried_when_no_subtitles_were_asked_for(self) -> None:
        result, _, given = self.run_once((False, output(SUBTITLES_429), [], []), subtitle_opts={})
        self.assertEqual((result.ok, len(given)), (False, 1))

    def test_a_download_that_worked_the_first_time_is_not_marked(self) -> None:
        result, _, given = self.run_once((True, [], [RECORD_A], []))
        self.assertEqual((result.ok, result.subtitles_skipped, len(given)), (True, False, 1))
        self.assertEqual(result.subtitle_summary, "")

    def test_a_retry_that_fails_too_is_reported_as_what_it_failed_with(self) -> None:
        result, shown, _ = self.run_once(
            (False, output(SUBTITLES_429), [], []), (False, output(BOT), [], []))
        self.assertEqual((result.ok, result.failure_kind), (False, "bot_blocked"))
        self.assertFalse(result.subtitles_skipped)
        self.assertTrue(any("자막 없이 다시 받았지만 실패" in line for line, _ in shown))

    def test_a_retry_with_no_cause_keeps_the_first_one_and_does_not_blame_the_subtitles(self) -> None:
        result, shown, _ = self.run_once(
            (False, output(SUBTITLES_429), [], []), (False, output(OTHER_ERROR), [], []))
        self.assertEqual(result.failure_kind, "rate_limited")  # the first run's, since the second said nothing known
        self.assertFalse(any("자막을 끄고" in line for line, _ in shown))  # it ran without subtitles

    def test_cancelling_during_the_retry_ends_it_cancelled(self) -> None:
        cancel = threading.Event()
        outcomes = iter([(False, output(SUBTITLES_429), [], []), (False, output(), [], [])])
        opts = DownloadOptions(url=URL, format_opt="best", output_template="o", subtitle_opts=dict(SUBS))

        def fake(o, pcb, lcb, cancel_event=None):
            outcome = next(outcomes)
            if not o.subtitle_opts:
                cancel.set()  # the user cancels while the retry runs
            return outcome
        with mock.patch.object(downloader, "_run_once", fake):
            result = downloader.run_download(URL, opts, cancel_event=cancel)
        self.assertEqual((result.cancelled, result.ok, result.subtitles_skipped), (True, False, False))

    def test_videos_the_first_attempt_finished_are_kept_without_repeats(self) -> None:
        # A playlist: the first attempt finished a. The retry may report only b ...
        result, _, _ = self.run_once(
            (False, output(SUBTITLES_429), [RECORD_A], []), (True, [], [RECORD_B], []))
        self.assertEqual([r["filepath"] for r in result.files], ["/o/a.mp4", "/o/b.mp4"])
        # ... or a again as well ("already downloaded"): then a is not listed twice.
        result, _, _ = self.run_once(
            (False, output(SUBTITLES_429), [RECORD_A], []), (True, [], [RECORD_A, RECORD_B], []))
        self.assertEqual([r["filepath"] for r in result.files], ["/o/a.mp4", "/o/b.mp4"])


class BatchSummaryTest(unittest.TestCase):
    ITEMS = ["a", "b", "c"]

    def summary(self, *flags: bool) -> BatchSummary:
        results = [DownloadResult(ok=True, subtitles_skipped=flag) for flag in flags]
        return BatchSummary(items=self.ITEMS[: len(flags)], states=["ok"] * len(flags), results=results)

    def test_the_items_without_subtitles_are_counted_and_named_in_the_line(self) -> None:
        summary = self.summary(False, True, True)
        self.assertEqual(summary.subtitles_skipped, 2)
        self.assertEqual(summary.describe(), "완료 3 (자막 없음 2)")

    def test_nothing_changes_when_every_item_has_its_subtitles(self) -> None:
        summary = self.summary(False, False)
        self.assertEqual((summary.subtitles_skipped, summary.describe()), (0, "완료 2"))

    def test_items_that_never_ran_are_not_counted(self) -> None:
        summary = BatchSummary(items=["a", "b"], states=["ok", "pending"],
                               results=[DownloadResult(ok=True, subtitles_skipped=True), None])
        self.assertEqual(summary.subtitles_skipped, 1)

    def test_they_are_finished_items_not_something_left_to_download(self) -> None:
        self.assertTrue(self.summary(True).ok)  # the video is done: nothing in the "remaining" list


class SummaryAndHistoryTest(unittest.TestCase):
    def test_the_summary_says_the_subtitles_are_missing(self) -> None:
        self.assertEqual(DownloadResult(ok=True, subtitles_skipped=True).subtitle_summary, "⚠ 자막 없이 받음")
        self.assertEqual(DownloadResult(ok=True).subtitle_summary, "")

    def test_history_marks_it_for_a_second_look(self) -> None:
        for flagged, status in ((True, "warning"), (False, "ok")):
            entry = HistoryEntry.from_result(URL, DownloadResult(ok=True, subtitles_skipped=flagged))
            self.assertEqual(entry.status, status)

    def test_a_failure_is_a_failure_whatever_the_flag(self) -> None:
        self.assertEqual(HistoryEntry.from_result(URL, DownloadResult(ok=False)).status, "failed")


class GuiSubtitlesTest(unittest.TestCase):
    def setUp(self) -> None:
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
        self.addCleanup(self.root.update)
        from interfaces import gui
        self.gui = gui
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.source = self.directory / "목록.txt"
        self.source.write_text("# 분야\nhttps://youtu.be/aaaaaaaaaaa\n", encoding="utf-8")
        from core.cookie_health import CookieHealthStore
        self.app = gui.DownloaderGUI(
            self.root, history=HistoryStore(self.directory / "history.json"),
            cookie_health=CookieHealthStore(self.directory / "cookie_health.json"))
        self.app.output_path_var.set(str(self.directory))
        thread = mock.patch.object(
            gui.threading, "Thread",
            side_effect=lambda *, target, **kw: mock.Mock(start=target, is_alive=lambda: False))
        thread.start()
        self.addCleanup(thread.stop)
        dialogs = mock.patch.object(gui, "messagebox")
        dialogs.start()
        self.addCleanup(dialogs.stop)
        self.root.update()

    RESULT = DownloadResult(ok=True, title="영상", subtitles_skipped=True, log_path="/logs/x.log")

    def test_the_row_of_a_single_download_says_the_subtitles_are_missing(self) -> None:
        self.app.url_var.set(URL)
        with mock.patch.object(self.gui, "run_download", return_value=self.RESULT):
            self.app.start_download()
        self.root.update()
        row = self.app.jobs_tree.get_children()[0]
        status = self.app.jobs_tree.item(row, "values")[2]
        self.assertIn("자막 없이 받음", status)
        self.assertIn("완료", status)  # still a success
        self.assertEqual(self.app.history.entries()[0].status, "warning")

    def test_a_batch_says_so_in_its_rows_and_its_summary(self) -> None:
        self.app.load_batch(str(self.source))

        def batch(items, opts, **kwargs):
            kwargs["item_callback"](0, items[0], "running", None)
            kwargs["item_callback"](0, items[0], "ok", self.RESULT)
            return BatchSummary(items=items, states=["ok"], results=[self.RESULT])
        with mock.patch.object(self.gui, "run_batch", side_effect=batch):
            self.app.start_batch()
        self.root.update()
        self.assertIn("자막 없이 받음", self.app.batch_tree.set("0", "state"))
        job = self.app._batch_job
        self.assertIn("자막 없이 받은 영상 있음", job.note)
        status_text = self.app.status_text.get("1.0", "end")
        self.assertIn("실행 로그: /logs/x.log", status_text)  # shown because the video is not complete
        self.assertIn("자막 없이 받은 영상 1개", status_text)
        self.assertIn("같은 목록을 다시 실행하면", status_text)
        self.assertIn("자막 없이 받은 1개는 같은 목록을 다시 실행하면 자막을 채웁니다",
                      self.app.batch_summary_var.get())


class CliSubtitlesTest(_CliTest):
    def test_a_single_run_points_at_the_log(self) -> None:
        out = io.StringIO()
        with mock.patch.object(cli, "get_url", return_value=URL), \
                mock.patch.object(cli, "run_download", return_value=GuiSubtitlesTest.RESULT), \
                redirect_stdout(out):
            self.assertEqual(cli.run_cli(), 0)  # still a success
        self.assertIn("실행 로그: /logs/x.log", out.getvalue())

    def test_a_batch_names_the_video_that_came_without_subtitles(self) -> None:
        listing = self.dir / "목록.txt"
        listing.write_text("# 요리\nhttps://youtu.be/aaaaaaaaaaa\n", encoding="utf-8")

        def fake_run_batch(items, base_opts, *, item_callback, **_kw):
            item_callback(0, items[0], "ok", GuiSubtitlesTest.RESULT)
            return BatchSummary(items=list(items), states=["ok"], results=[GuiSubtitlesTest.RESULT])
        out = io.StringIO()
        with mock.patch.object(cli, "run_batch", side_effect=fake_run_batch), \
                mock.patch("builtins.input", return_value=""), redirect_stdout(out):
            self.assertEqual(cli.run_batch_cli(str(listing)), 0)
        text = out.getvalue()
        self.assertIn("영상 — ⚠ 자막 없이 받음", text)
        self.assertIn("실행 로그: /logs/x.log", text)
        self.assertIn("결과: 완료 1 (자막 없음 1)", text)
        self.assertIn("같은 목록을 다시 실행하면 그 영상은 건너뛰고 자막만 받습니다", text)

    def test_a_batch_with_all_its_subtitles_says_nothing_of_the_kind(self) -> None:
        listing = self.dir / "목록2.txt"
        listing.write_text("# 요리\nhttps://youtu.be/aaaaaaaaaaa\n", encoding="utf-8")
        plain = DownloadResult(ok=True, title="영상")

        def fake_run_batch(items, base_opts, *, item_callback, **_kw):
            item_callback(0, items[0], "ok", plain)
            return BatchSummary(items=list(items), states=["ok"], results=[plain])
        out = io.StringIO()
        with mock.patch.object(cli, "run_batch", side_effect=fake_run_batch),                 mock.patch("builtins.input", return_value=""), redirect_stdout(out):
            cli.run_batch_cli(str(listing))
        self.assertNotIn("자막", out.getvalue())


if __name__ == "__main__":
    unittest.main()
