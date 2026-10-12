"""Unit tests for core.batch (downloading a URL list into category folders)."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from core import batch
from core.batch import (
    BatchItem,
    category_template,
    live_path_for,
    parse_batch_text,
    read_batch_file,
    remaining_path_for,
    run_batch,
    safe_folder_name,
    video_id_of,
)
from core.downloader import DownloadOptions, DownloadResult
from core.live import LiveOptions


class ParseTest(unittest.TestCase):
    def test_categories_duplicates_and_invalid_lines(self) -> None:
        plan = parse_batch_text(
            "https://youtu.be/-OnM0ABAYzM\n"
            "\n"
            "# 요리\n"
            "https://youtu.be/-mqV2nO4XS4   백종원 김치찌개\n"
            "youtu.be/09BcoSNaxdk\n"
            "##  투자 / 주식 \n"
            "https://www.youtube.com/watch?v=-mqV2nO4XS4&t=10\n"  # duplicate
            "https://youtube.com/live/Eif05zG8M-k\n"
            "그냥 메모\n"
            "https://example.com/video.mp4\n"
        )
        self.assertEqual(
            [(i.category, i.url, i.video_id) for i in plan.items],
            [
                ("미분류", "https://youtu.be/-OnM0ABAYzM", "-OnM0ABAYzM"),
                ("요리", "https://youtu.be/-mqV2nO4XS4", "-mqV2nO4XS4"),
                ("요리", "https://youtu.be/09BcoSNaxdk", "09BcoSNaxdk"),
                ("투자 _ 주식", "https://youtube.com/live/Eif05zG8M-k", "Eif05zG8M-k"),
                ("투자 _ 주식", "https://example.com/video.mp4", ""),
            ],
        )
        self.assertEqual([(d.line_no, d.video_id) for d in plan.duplicates], [(7, "-mqV2nO4XS4")])
        self.assertEqual(plan.invalid, [(9, "그냥 메모")])
        self.assertEqual(plan.categories, ["미분류", "요리", "투자 _ 주식"])
        self.assertEqual(plan.describe(), "분야 3개 · 5개 · 중복 1 · 잘못된 줄 1")

    def test_video_ids(self) -> None:
        for url, expected in [
            ("https://youtu.be/ERA_YfNnARU?si=abc", "ERA_YfNnARU"),
            ("https://m.youtube.com/watch?v=ERA_YfNnARU", "ERA_YfNnARU"),
            ("https://www.youtube.com/shorts/ERA_YfNnARU", "ERA_YfNnARU"),
            ("https://www.youtube.com/playlist?list=PL123", ""),
            ("https://youtu.be/short", ""),
        ]:
            self.assertEqual(video_id_of(url), expected, url)

    def test_folder_names(self) -> None:
        self.assertEqual(safe_folder_name('a:b*c?"d"'), "a_b_c__d_")
        self.assertEqual(safe_folder_name("  ... "), "미분류")
        self.assertEqual(safe_folder_name("요리."), "요리")
        self.assertEqual(safe_folder_name("con"), "con_")

    def test_file_encodings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = "# 요리\nhttps://youtu.be/-OnM0ABAYzM\n"
            for encoding in ("utf-8-sig", "cp949"):
                path = Path(tmp) / f"{encoding}.txt"
                path.write_bytes(text.encode(encoding))
                plan = read_batch_file(path)
                self.assertEqual([i.category for i in plan.items], ["요리"], encoding)

    def test_remaining_path(self) -> None:
        self.assertEqual(remaining_path_for("/d/내youtube.txt").name, "내youtube.남은목록.txt")
        # Running the remaining list again keeps updating the same file.
        again = Path("/d/내youtube.남은목록.txt")
        self.assertEqual(remaining_path_for(again), again)

    def test_category_template(self) -> None:
        template = os.path.join("out", "%(title)s.%(ext)s")
        self.assertEqual(
            category_template(template, "100% 요리"),
            os.path.join("out", "100%% 요리", "%(title)s.%(ext)s"),
        )

    def test_saved_lists_share_stable_paths(self) -> None:
        for name in ("목록.txt", "목록.남은목록.txt", "목록.라이브대기.txt", "목록.라이브대기.남은목록.txt"):
            self.assertEqual(live_path_for(name).name, "목록.라이브대기.txt")
        self.assertEqual(remaining_path_for("목록.라이브대기.txt").name, "목록.라이브대기.남은목록.txt")
        self.assertEqual(remaining_path_for("목록.라이브대기.남은목록.txt").name,
                         "목록.라이브대기.남은목록.txt")


class RunBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.base = DownloadOptions(
            url="", format_opt="best",
            output_template=str(self.dir / "%(title)s.%(ext)s"),
            live_opts=LiveOptions(),
        )
        self.items = [
            BatchItem("https://youtu.be/aaaaaaaaaaa", "요리", 1, "aaaaaaaaaaa"),
            BatchItem("https://youtu.be/bbbbbbbbbbb", "요리", 2, "bbbbbbbbbbb"),
            BatchItem("https://youtu.be/ccccccccccc", "투자", 4, "ccccccccccc"),
        ]
        self.remaining = self.dir / "list.남은목록.txt"
        self.live = self.dir / "list.라이브대기.txt"

    def _run(self, results, cancel_event=None, pause=(0.0, 0.0)):
        """Run with run_download returning *results* in turn (a callable
        result is called with the options first)."""
        calls: list[DownloadOptions] = []
        events: list[tuple[int, str]] = []
        queue = list(results)

        def fake(url, opts, **kwargs):
            calls.append(opts)
            self.assertTrue(kwargs["skip_live"])
            result = queue.pop(0)
            return result(opts) if callable(result) else result

        with mock.patch.object(batch, "run_download", side_effect=fake):
            summary = run_batch(
                self.items, self.base,
                item_callback=lambda i, _item, state, _r: events.append((i, state)),
                cancel_event=cancel_event, pause=pause, remaining_path=self.remaining,
                live_path=self.live,
            )
        return summary, calls, events

    def test_each_item_goes_to_its_category_folder(self) -> None:
        summary, calls, events = self._run([
            DownloadResult(ok=True, title="a"),
            DownloadResult(ok=False, title="b", skipped="live"),
            DownloadResult(ok=False),
        ])
        self.assertEqual(
            [c.output_template for c in calls],
            [str(self.dir / "요리" / "%(title)s.%(ext)s")] * 2
            + [str(self.dir / "투자" / "%(title)s.%(ext)s")],
        )
        self.assertEqual({c.download_archive for c in calls}, {str(self.dir / ".batch-archive.txt")})
        self.assertEqual(summary.states, ["ok", "live", "failed"])
        self.assertEqual(events, [(0, "running"), (0, "ok"), (1, "running"), (1, "live"),
                                  (2, "running"), (2, "failed")])
        self.assertEqual(summary.describe(), "완료 1 · 라이브라 건너뜀 1 · 실패 1")
        self.assertFalse(summary.ok)
        self.assertEqual(self.remaining.read_text(encoding="utf-8"),
                         "# 투자\nhttps://youtu.be/ccccccccccc\n")

    def test_live_status_does_not_carry_over(self) -> None:
        def detect_live(opts):
            opts.live_opts._detected_status = "is_live"  # as run_download's probe does
            return DownloadResult(ok=False, skipped="live")

        def check_fresh(opts):
            self.assertIsNone(opts.live_opts._detected_status)
            return DownloadResult(ok=True)
        summary, _, _ = self._run([detect_live, check_fresh, check_fresh])
        self.assertEqual(summary.states, ["live", "ok", "ok"])
        self.assertIsNone(self.base.live_opts._detected_status)

    def test_live_skips_are_saved_separately_and_can_be_retried(self) -> None:
        summary, _, _ = self._run([
            DownloadResult(ok=True), DownloadResult(ok=False, skipped="live"),
            DownloadResult(ok=False),
        ])
        self.assertEqual(summary.live_file, str(self.live))
        self.assertEqual(read_batch_file(self.live).items[0].url, self.items[1].url)
        self.assertEqual(read_batch_file(self.live).items[0].category, "요리")
        self.assertEqual(read_batch_file(self.remaining).items[0].url, self.items[2].url)
        self.items = read_batch_file(self.live).items
        summary, _, _ = self._run([DownloadResult(ok=True)])
        self.assertIsNone(summary.live_file)
        self.assertFalse(self.live.exists())

    def test_partial_rerun_preserves_unseen_live_urls_without_duplicates(self) -> None:
        self._run([DownloadResult(ok=False, skipped="live")] * 3)
        self.items = [self.items[1]]
        self._run([DownloadResult(ok=False, skipped="live")])
        self.assertEqual(len(read_batch_file(self.live).items), 3)
        self._run([DownloadResult(ok=True)])
        self.assertEqual([i.video_id for i in read_batch_file(self.live).items],
                         ["aaaaaaaaaaa", "ccccccccccc"])

    def test_cancel_preserves_unprocessed_live_urls(self) -> None:
        self._run([DownloadResult(ok=False, skipped="live")] * 3)
        event = threading.Event()

        def cancel(opts):
            event.set()
            return DownloadResult(ok=False, cancelled=True)

        self._run([DownloadResult(ok=True), cancel], cancel_event=event)
        self.assertEqual([i.video_id for i in read_batch_file(self.live).items],
                         ["bbbbbbbbbbb", "ccccccccccc"])

    def test_failed_atomic_write_preserves_the_old_list(self) -> None:
        self.live.write_text("# 이전\nhttps://youtu.be/aaaaaaaaaaa\n", encoding="utf-8")
        before = self.live.read_bytes()
        with mock.patch.object(batch.os, "replace", side_effect=PermissionError("busy")):
            summary, _, _ = self._run([DownloadResult(ok=False, skipped="live")] * 3)
        self.assertEqual(self.live.read_bytes(), before)
        self.assertIsNone(summary.live_file)
        self.assertFalse(list(self.dir.glob("*.tmp")))

    def test_retrying_live_list_does_not_erase_the_original_failures(self) -> None:
        self._run([DownloadResult(ok=False), DownloadResult(ok=False, skipped="live"),
                   DownloadResult(ok=True)])
        previous_failures = self.remaining.read_bytes()
        with mock.patch.object(batch, "run_download", return_value=DownloadResult(ok=True)):
            run_batch(read_batch_file(self.live).items, self.base, pause=(0, 0),
                      remaining_path=remaining_path_for(self.live), live_path=live_path_for(self.live))
        self.assertEqual(self.remaining.read_bytes(), previous_failures)
        self.assertFalse(self.live.exists())

    def test_archived_items_are_skipped_without_running(self) -> None:
        (self.dir / ".batch-archive.txt").write_text("youtube bbbbbbbbbbb\n", encoding="utf-8")
        summary, calls, _ = self._run([DownloadResult(ok=True), DownloadResult(ok=True)])
        self.assertEqual([c.url for c in calls], [self.items[0].url, self.items[2].url])
        self.assertEqual(summary.states, ["ok", "archived", "ok"])
        self.assertTrue(summary.ok)

    def test_nothing_left_removes_an_old_remaining_list(self) -> None:
        self.remaining.write_text("# 요리\nhttps://youtu.be/aaaaaaaaaaa\n", encoding="utf-8")
        summary, _, _ = self._run([DownloadResult(ok=True)] * 3)
        self.assertIsNone(summary.remaining_file)
        self.assertFalse(self.remaining.exists())

    def test_cancel_marks_the_rest_and_saves_them(self) -> None:
        event = threading.Event()

        def cancel_during(opts):
            event.set()
            return DownloadResult(ok=False, cancelled=True)
        summary, calls, events = self._run([DownloadResult(ok=True), cancel_during],
                                           cancel_event=event)
        self.assertEqual(len(calls), 2)
        self.assertEqual(summary.states, ["ok", "cancelled", "cancelled"])
        self.assertEqual(events[-1], (2, "cancelled"))
        self.assertEqual(
            self.remaining.read_text(encoding="utf-8"),
            "# 요리\nhttps://youtu.be/bbbbbbbbbbb\n\n# 투자\nhttps://youtu.be/ccccccccccc\n",
        )

    def test_archived_items_after_a_cancel_are_not_left_over(self) -> None:
        (self.dir / ".batch-archive.txt").write_text("youtube ccccccccccc\n", encoding="utf-8")
        event = threading.Event()

        def cancel_during(opts):
            event.set()
            return DownloadResult(ok=False, cancelled=True)
        summary, _, _ = self._run([cancel_during], cancel_event=event)
        self.assertEqual(summary.states, ["cancelled", "cancelled", "archived"])
        self.assertEqual([i.video_id for i in summary.remaining], ["aaaaaaaaaaa", "bbbbbbbbbbb"])

    def test_cancel_during_the_pause_stops_at_once(self) -> None:
        event = threading.Event()

        def finish_then_cancel(opts):
            threading.Timer(0.05, event.set).start()
            return DownloadResult(ok=True)
        summary, calls, _ = self._run([finish_then_cancel], cancel_event=event,
                                      pause=(30.0, 30.0))
        self.assertEqual(len(calls), 1)  # did not wait 30 s for the next one
        self.assertEqual(summary.states, ["ok", "cancelled", "cancelled"])

    def test_an_exception_does_not_stop_the_list(self) -> None:
        def boom(opts):
            raise RuntimeError("boom")
        summary, _, _ = self._run([boom, DownloadResult(ok=True), DownloadResult(ok=True)])
        self.assertEqual(summary.states, ["failed", "ok", "ok"])

    def test_a_crash_keeps_the_run_log_path(self) -> None:
        # run_download tags the exception it re-raises with the run's log file.
        def boom(opts):
            exc = RuntimeError("boom")
            exc.log_path = "C:\\logs\\crashed.log"
            raise exc
        summary, _, _ = self._run([boom, DownloadResult(ok=True), DownloadResult(ok=True)])
        self.assertEqual(summary.results[0].log_path, "C:\\logs\\crashed.log")
        self.assertEqual(summary.results[1].log_path, "")


if __name__ == "__main__":
    unittest.main()
