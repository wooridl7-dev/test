"""Exercise batch loading and warning rows with a withdrawn Tk window."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core.batch import BatchSummary, live_path_for
from core.downloader import DownloadResult
from core.history import HistoryStore
from interfaces.gui_jobs import job_row


class GuiBatchTest(unittest.TestCase):
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
        from interfaces import gui
        self.gui = gui
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.source = self.directory / "목록.txt"
        self.source.write_text("# 분야\nhttps://youtu.be/aaaaaaaaaaa\n", encoding="utf-8")
        self.app = gui.DownloaderGUI(self.root, history=HistoryStore(self.directory / "history.json"))
        self.app.output_path_var.set(str(self.directory))
        # Drain callbacks deterministically on the Tk thread. No downloads or
        # background threads are started; the real worker code still runs.
        thread = mock.patch.object(gui.threading, "Thread", side_effect=self.inline_thread)
        thread.start()
        self.addCleanup(thread.stop)
        dialogs = mock.patch.object(gui, "messagebox")
        self.dialogs = dialogs.start()
        self.dialogs.askyesno.return_value = True
        self.addCleanup(dialogs.stop)
        self.root.update()

    @staticmethod
    def inline_thread(*, target, **kwargs):
        return mock.Mock(start=target, is_alive=lambda: False)

    def test_preload_selects_batch_without_starting_a_job(self):
        self.app.load_batch(str(self.source))
        self.assertEqual(self.app.notebook.select(), str(self.app.batch_tab))
        self.assertEqual(len(self.app.batch_tree.get_children()), 1)
        self.assertEqual(self.app._jobs.running_count(), 0)
        self.assertIsNone(self.app._batch_job)

    def test_missing_file_cannot_start_the_previous_plan(self):
        self.app.load_batch(str(self.source))
        self.source.unlink()
        with mock.patch.object(self.gui, "run_batch") as run:
            self.app.start_batch()
        run.assert_not_called()
        self.assertIsNone(self.app._batch_plan)
        self.assertFalse(self.app.batch_tree.get_children())

    def test_open_live_list_preloads_its_rows(self):
        live = live_path_for(self.source)
        live.write_text("# 다음\nhttps://youtu.be/bbbbbbbbbbb\n", encoding="utf-8")
        self.app.load_batch(str(self.source))
        self.app._batch_open_live()
        self.assertEqual(self.app.batch_path_var.get(), str(live))
        self.assertEqual(self.app._batch_plan.items[0].category, "다음")
        self.assertIsNone(self.app._batch_job)

    def test_warning_is_visible_in_batch_and_main_rows(self):
        self.app.load_batch(str(self.source))
        result = DownloadResult(ok=True, title="녹화", warnings=["refresh failed"])

        def batch(items, opts, **kwargs):
            self.assertEqual(kwargs["live_path"], live_path_for(self.source))
            kwargs["item_callback"](0, items[0], "running", None)
            kwargs["item_callback"](0, items[0], "ok", result)
            return BatchSummary(items=items, states=["ok"], results=[result])

        with mock.patch.object(self.gui, "run_batch", side_effect=batch):
            self.app.start_batch()
        self.root.update()
        self.assertIn("조기 종료", self.app.batch_tree.set("0", "state"))
        self.assertIn("조기 종료", job_row(self.app._batch_job, time.monotonic()).values[2])
        self.assertEqual(self.app.history.entries()[0].status, "warning")

    def test_warning_is_visible_for_single_download(self):
        self.app.url_var.set("https://youtu.be/aaaaaaaaaaa")
        with mock.patch.object(self.gui, "run_download", return_value=DownloadResult(
                ok=True, title="녹화", warnings=["refresh failed"])):
            self.app.start_download()
        self.root.update()
        row = self.app.jobs_tree.get_children()[0]
        self.assertIn("조기 종료", self.app.jobs_tree.item(row, "values")[2])


if __name__ == "__main__":
    unittest.main()
