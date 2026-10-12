"""Downloads past the concurrent cap wait in a queue instead of being refused.

Drives the real GUI with a withdrawn Tk window (skipped without a display).
Worker threads are not run: each "thread" just records its target, so the
test decides when a download finishes.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.batch import BatchSummary
from core.downloader import DownloadResult
from core.history import HistoryStore


class GuiQueueTest(unittest.TestCase):
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
        self.app = gui.DownloaderGUI(self.root, history=HistoryStore(self.directory / "history.json"))
        self.app.output_path_var.set(str(self.directory))
        self.app.max_parallel_var.set(1)

        # A started "thread" only records its worker; run_worker() plays it.
        self.workers: list = []
        thread = mock.patch.object(
            gui.threading, "Thread",
            side_effect=lambda *, target, **kw: mock.Mock(
                start=lambda: self.workers.append(target), is_alive=lambda: False),
        )
        thread.start()
        self.addCleanup(thread.stop)
        dialogs = mock.patch.object(gui, "messagebox")
        self.dialogs = dialogs.start()
        self.dialogs.askyesno.return_value = True
        self.addCleanup(dialogs.stop)
        self.downloads: list[str] = []
        download = mock.patch.object(gui, "run_download", side_effect=self._fake_download)
        download.start()
        self.addCleanup(download.stop)
        self.root.update()

    def _fake_download(self, url, opts, **kwargs):
        self.downloads.append(url)
        return DownloadResult(ok=True, title=url.rsplit("/", 1)[-1])

    def _start(self, url: str):
        self.app.url_var.set(url)
        self.app.start_download()
        return self.app._jobs.jobs[-1]

    def _run_next_worker(self) -> None:
        self.workers.pop(0)()  # the download ends: job.finish + a UI refresh
        self.root.update()

    def _states(self) -> list[str]:
        return [job.state for job in self.app._jobs.jobs]

    def _log(self) -> str:
        self.root.update()  # the log is appended on the Tk thread's event loop
        return self.app.status_text.get("1.0", "end")

    def test_a_second_download_waits_and_starts_when_the_first_ends(self):
        first = self._start("https://example.com/a")
        second = self._start("https://example.com/b")
        self.assertEqual(self._states(), ["running", "queued"])
        self.assertEqual(len(self.workers), 1)  # only the first has a worker
        self.dialogs.showinfo.assert_not_called()  # not refused
        self.assertIn("대기열에 넣었습니다", self._log())
        row = self.app.jobs_tree.item(str(second.id), "values")
        self.assertEqual(row[2], "대기 중 (1번째)")

        self._run_next_worker()  # the first finishes
        self.assertEqual(self._states(), ["done", "running"])
        self.assertEqual(len(self.workers), 1)  # the second's worker was started
        self.assertIn("대기 끝", self._log())
        self.assertIsNone(second.launch)
        self._run_next_worker()
        self.assertEqual(self._states(), ["done", "done"])
        self.assertEqual(self.downloads, ["https://example.com/a", "https://example.com/b"])
        self.assertTrue(first.title and second.title)

    def test_start_button_stays_usable_at_the_cap(self):
        self._start("https://example.com/a")
        self.assertEqual(str(self.app.start_button.cget("state")), "normal")
        self.assertEqual(str(self.app.jobs_cancel_all_button.cget("state")), "normal")

    def test_queue_order_is_first_in_first_out(self):
        self._start("https://example.com/a")
        self._start("https://example.com/b")
        self._start("https://example.com/c")
        self._run_next_worker()
        self._run_next_worker()
        self._run_next_worker()
        self.assertEqual([u.rsplit("/", 1)[-1] for u in self.downloads], ["a", "b", "c"])

    def test_a_raised_cap_starts_waiting_downloads(self):
        self._start("https://example.com/a")
        self._start("https://example.com/b")
        self._start("https://example.com/c")
        self.app.max_parallel_var.set(3)
        self.app._on_jobs_changed()
        self.assertEqual(self._states(), ["running"] * 3)
        self.assertEqual(len(self.workers), 3)

    def test_cancelling_a_waiting_download_removes_it_without_running_it(self):
        self._start("https://example.com/a")
        waiting = self._start("https://example.com/b")
        self.app.jobs_tree.selection_set(str(waiting.id))
        self.app.cancel_selected_jobs()
        self.assertEqual(self._states(), ["running", "cancelled"])
        self._run_next_worker()
        self.assertEqual(self.workers, [])  # nothing else was started
        self.assertEqual(self.downloads, ["https://example.com/a"])

    def test_cancel_all_cancels_waiting_downloads_too(self):
        self._start("https://example.com/a")
        self._start("https://example.com/b")
        self.app.cancel_download()
        self.assertEqual(self._states(), ["running", "cancelled"])
        self.assertTrue(self.app._jobs.jobs[0].cancel_event.is_set())

    def test_nothing_starts_while_the_window_is_closing(self):
        self._start("https://example.com/a")
        self._start("https://example.com/b")
        self.app._closing = True
        self._run_next_worker()
        self.assertEqual(self._states(), ["done", "queued"])
        self.assertEqual(self.workers, [])

    def test_the_same_waiting_url_is_refused_with_a_message(self):
        self._start("https://example.com/a")
        self._start("https://example.com/b")
        self.app.url_var.set("https://example.com/b")
        self.app.start_download()
        self.dialogs.showinfo.assert_called_once()
        self.assertIn("대기열", self.dialogs.showinfo.call_args.args[1])
        self.assertEqual(len(self.app._jobs.jobs), 2)

    def test_a_batch_waits_for_a_slot_too(self):
        source = self.directory / "목록.txt"
        source.write_text("# 분야\nhttps://youtu.be/aaaaaaaaaaa\n", encoding="utf-8")
        self._start("https://example.com/a")
        self.app.load_batch(str(source))
        ran = []

        def batch(items, opts, **kwargs):
            ran.append(items[0].url)
            return BatchSummary(items=items, states=["ok"], results=[DownloadResult(ok=True)])

        with mock.patch.object(self.gui, "run_batch", side_effect=batch):
            self.app.start_batch()
            batch_job = self.app._batch_job
            self.assertTrue(batch_job.queued)
            self.assertEqual(ran, [])
            self.assertEqual(str(self.app.batch_start_button.cget("state")), "disabled")
            self.assertEqual(str(self.app.batch_cancel_button.cget("state")), "normal")
            self.app.start_batch()  # pressing again while it waits does nothing
            self.assertEqual(len(self.app._jobs.jobs), 2)
            self._run_next_worker()  # the single download ends
            self.assertTrue(batch_job.running)
            self._run_next_worker()  # the batch runs
        self.assertEqual(ran, ["https://youtu.be/aaaaaaaaaaa"])
        self.assertEqual(batch_job.state, "done")

    def test_a_waiting_batch_can_be_cancelled_and_its_list_stays_loaded(self):
        source = self.directory / "목록.txt"
        source.write_text("# 분야\nhttps://youtu.be/aaaaaaaaaaa\n", encoding="utf-8")
        self._start("https://example.com/a")
        self.app.load_batch(str(source))
        with mock.patch.object(self.gui, "run_batch") as run:
            self.app.start_batch()
            self.app.cancel_batch()
            self.assertEqual(self.app._batch_job.state, "cancelled")
            self._run_next_worker()
        run.assert_not_called()
        self.assertEqual(str(self.app.batch_start_button.cget("state")), "normal")


if __name__ == "__main__":
    unittest.main()
