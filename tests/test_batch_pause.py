"""Tests for the rest between two downloads of a batch: how a person states it
(``--batch-pause``, the 일괄 tab) and that run_batch keeps it.

A typo must not quietly remove the pause, which is what keeps a long list from
looking like a bot to YouTube.

Run from the repo root with::

    python -m unittest

Standard library only.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
from core import batch
from core.batch import (
    DEFAULT_PAUSE,
    MAX_PAUSE_SECONDS,
    BatchItem,
    check_pause,
    parse_pause,
    pause_label,
    run_batch,
)
from core.downloader import DownloadOptions, DownloadResult


class ParseTest(unittest.TestCase):
    def test_a_range_or_one_number(self) -> None:
        for text, expected in (("5-15", (5.0, 15.0)), ("5~15", (5.0, 15.0)),
                               (" 5 - 15 ", (5.0, 15.0)), ("2.5-7.5", (2.5, 7.5)),
                               ("10", (10.0, 10.0)), ("0", (0.0, 0.0)), ("0-0", (0.0, 0.0)),
                               ("0-30", (0.0, 30.0)),
                               (f"{MAX_PAUSE_SECONDS:g}", (MAX_PAUSE_SECONDS, MAX_PAUSE_SECONDS))):
            with self.subTest(text):
                self.assertEqual(parse_pause(text), expected)

    def test_everything_else_is_refused(self) -> None:
        for text in ("-1", "15-5", "601", "5-601", "abc", "", "  ", "5-", "-5", "5--15",
                     "1e2", "５", "5,15", "5 15", "5초", "nan", "inf"):
            with self.subTest(text):
                with self.assertRaises(ValueError):
                    parse_pause(text)

    def test_the_error_says_what_is_accepted(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_pause("15-5")
        self.assertIn("5-15", str(caught.exception))
        self.assertIn("'15-5'", str(caught.exception))


class CheckTest(unittest.TestCase):
    def test_valid_pairs(self) -> None:
        for pair in ((0, 0), (5, 15), (5.0, 5.0), (0.5, 600), DEFAULT_PAUSE):
            with self.subTest(pair):
                self.assertEqual(check_pause(pair), (float(pair[0]), float(pair[1])))

    def test_invalid_pairs(self) -> None:
        for pair in ((-1, 5), (10, 5), (0, 601), (True, 5), (0, True), (1, 2, 3), (5,), 5, None,
                     ("1", "2"), (float("nan"), 5), (0, float("inf"))):
            with self.subTest(pair):
                with self.assertRaises(ValueError):
                    check_pause(pair)  # type: ignore[arg-type]


class LabelTest(unittest.TestCase):
    def test_labels(self) -> None:
        self.assertEqual(pause_label((5.0, 15.0)), "5~15초")
        self.assertEqual(pause_label((10.0, 10.0)), "10초")
        self.assertEqual(pause_label((0.0, 0.0)), "쉬지 않음")
        self.assertEqual(pause_label((0.0, 30.0)), "0~30초")
        self.assertEqual(pause_label((2.5, 7.5)), "2.5~7.5초")


class RunBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.items = [BatchItem("분야", f"https://youtu.be/{c * 11}", c * 11, n)
                      for n, c in enumerate("abc", 1)]
        self.base = DownloadOptions(
            url="", format_opt="best", output_template=str(self.dir / "%(title)s.%(ext)s"))
        self.logs: list[str] = []

    def _run(self, pause):
        with mock.patch.object(batch, "run_download", return_value=DownloadResult(ok=True)) as run, \
                mock.patch.object(batch, "_wait", return_value=False) as wait:
            summary = run_batch(self.items, self.base, pause=pause,
                                log_callback=lambda line, _lv: self.logs.append(line))
        return summary, run, wait

    def test_it_rests_between_downloads_for_a_time_inside_the_range(self) -> None:
        _, run, wait = self._run((3.0, 9.0))
        self.assertEqual(run.call_count, 3)
        self.assertEqual(wait.call_count, 2)  # between three downloads, not before the first
        for call in wait.call_args_list:
            self.assertTrue(3.0 <= call.args[1] <= 9.0, call)
        self.assertEqual(sum("쉽니다" in line for line in self.logs), 2)

    def test_a_fixed_pause(self) -> None:
        _, _, wait = self._run((7.0, 7.0))
        self.assertEqual([call.args[1] for call in wait.call_args_list], [7.0, 7.0])

    def test_zero_means_no_pause_at_all(self) -> None:
        summary, run, wait = self._run((0.0, 0.0))
        self.assertEqual(run.call_count, 3)
        wait.assert_not_called()
        self.assertFalse(any("쉽니다" in line for line in self.logs))
        self.assertTrue(summary.ok)

    def test_the_default_is_the_documented_five_to_fifteen(self) -> None:
        self.assertEqual(DEFAULT_PAUSE, (5.0, 15.0))
        with mock.patch.object(batch, "run_download", return_value=DownloadResult(ok=True)), \
                mock.patch.object(batch, "_wait", return_value=False) as wait:
            run_batch(self.items[:2], self.base)
        self.assertTrue(5.0 <= wait.call_args.args[1] <= 15.0)

    def test_a_bad_pair_stops_before_anything_is_downloaded(self) -> None:
        for pause in ((10, 5), (-1, 5), (0, 9999), (1, 2, 3), None):
            with self.subTest(pause), mock.patch.object(batch, "run_download") as run:
                with self.assertRaises(ValueError):
                    run_batch(self.items, self.base, pause=pause)  # type: ignore[arg-type]
                run.assert_not_called()


class CommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(main, "yt_dlp_missing_hint", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _refused(self, argv: list[str]) -> None:
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            main.main(argv)
        self.assertEqual(caught.exception.code, 2)

    def test_batch_gets_the_pause(self) -> None:
        for text, expected in (("5-15", (5.0, 15.0)), ("0", (0.0, 0.0)), ("10", (10.0, 10.0))):
            with self.subTest(text), mock.patch("interfaces.cli.run_batch_cli", return_value=0) as run:
                main.main(["--batch", "list.txt", "--batch-pause", text])
                run.assert_called_once_with("list.txt", pause=expected)

    def test_it_can_be_combined_with_the_live_flags_and_cli(self) -> None:
        with mock.patch("interfaces.cli.run_batch_cli", return_value=0) as run:
            main.main(["--cli", "--batch", "list.txt", "--batch-pause=2-4",
                       "--fragment-retries", "0", "--stall-minutes", "9"])
        run.assert_called_once_with("list.txt", fragment_retries=0, stall_minutes=9,
                                    pause=(2.0, 4.0))

    def test_without_the_flag_nothing_extra_is_passed(self) -> None:
        with mock.patch("interfaces.cli.run_batch_cli", return_value=0) as run:
            main.main(["--batch", "list.txt"])
        run.assert_called_once_with("list.txt")

    def test_bad_values_are_refused_before_anything_runs(self) -> None:
        for argv in (["--batch", "l.txt", "--batch-pause", "-1"],
                     ["--batch", "l.txt", "--batch-pause", "15-5"],
                     ["--batch", "l.txt", "--batch-pause", "abc"],
                     ["--batch", "l.txt", "--batch-pause", ""],
                     ["--batch", "l.txt", "--batch-pause", "9999"],
                     ["--batch", "l.txt", "--batch-pause"]):
            with self.subTest(argv), mock.patch("interfaces.cli.run_batch_cli") as run:
                self._refused(argv)
                run.assert_not_called()

    def test_it_needs_a_command_line_batch(self) -> None:
        for argv in (["--batch-pause", "3"],
                     ["--cli", "--batch-pause", "3"],
                     ["--gui", "--batch", "list.txt", "--batch-pause", "3"],
                     ["--bot", "--batch-pause", "3"]):
            with self.subTest(argv):
                self._refused(argv)

    def test_the_help_mentions_it(self) -> None:
        self.assertIn("--batch-pause", main.build_parser().format_help())


class GuiFieldTest(unittest.TestCase):
    """The 일괄 tab's two fields, in a withdrawn Tk window."""

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
        from core.history import HistoryStore
        from interfaces import gui
        self.gui = gui
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.app = gui.DownloaderGUI(
            self.root, history=HistoryStore(self.directory / "history.json"))
        self.app.output_path_var.set(str(self.directory))
        self.root.update()

    def _pause(self, low, high) -> tuple[float, float]:
        for spin, value in ((self.app.batch_pause_min_spin, low),
                            (self.app.batch_pause_max_spin, high)):
            if value is not None:
                spin.delete(0, "end")
                spin.insert(0, str(value))
        return self.app._batch_pause()

    def test_the_default_is_five_to_fifteen(self) -> None:
        self.assertEqual(self.app._batch_pause(), (5.0, 15.0))

    def test_values_reach_the_pair_and_zero_stays_zero(self) -> None:
        self.assertEqual(self._pause(2, 30), (2.0, 30.0))
        self.assertEqual(self._pause(0, 0), (0.0, 0.0))

    def test_a_longest_below_the_shortest_is_raised_to_it(self) -> None:
        self.assertEqual(self._pause(20, 8), (20.0, 20.0))

    def test_half_edited_fields_fall_back_to_their_defaults(self) -> None:
        for text in ("", "abc", "1.5", "-"):
            with self.subTest(text):
                self.assertEqual(self._pause(text, text), (5.0, 15.0))

    def test_numbers_outside_the_range_are_clamped(self) -> None:
        self.assertEqual(self._pause("-4", "99999"), (0.0, float(self.gui._MAX_PAUSE_SPIN)))

    def test_start_batch_hands_the_pair_to_run_batch(self) -> None:
        from core.batch import BatchSummary
        source = self.directory / "목록.txt"
        source.write_text("# 분야\nhttps://youtu.be/aaaaaaaaaaa\n", encoding="utf-8")
        self.app.load_batch(str(source))
        self._pause(3, 4)
        seen: dict = {}

        def fake_batch(items, opts, **kwargs):
            seen.update(kwargs)
            return BatchSummary(items=items, states=["ok"], results=[DownloadResult(ok=True)])

        threads = mock.patch.object(
            self.gui.threading, "Thread",
            side_effect=lambda *, target, **kw: mock.Mock(start=target, is_alive=lambda: False))
        dialogs = mock.patch.object(self.gui, "messagebox")
        with threads, dialogs as box, mock.patch.object(self.gui, "run_batch", side_effect=fake_batch):
            box.askyesno.return_value = True
            self.app.start_batch()
        self.root.update()
        self.assertEqual(seen["pause"], (3.0, 4.0))
        self.assertIn("영상 사이 휴식: 3~4초", self.app.status_text.get("1.0", "end"))


if __name__ == "__main__":
    unittest.main()
