"""Tests for the 다운로드 목록 column fitting and the tooltip of cut-off cells.

The window is shown but fully transparent (and far from the pointer), because
a Treeview only knows the width of a cell once it is on screen. Skipped when
tkinter or a display is missing.

Run from the repo root with::

    python -m unittest

Standard library only.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.cookie_health import CookieHealthStore
from core.history import HistoryStore
from interfaces.gui_jobs import fit_widths


class FitWidthsTest(unittest.TestCase):
    COLUMNS = [(28, 28, False), (160, 90, True), (245, 90, True), (245, 90, True),
               (128, 128, False), (62, 62, False), (82, 82, False)]

    def test_exact_room_keeps_the_base_widths(self) -> None:
        self.assertEqual(fit_widths(950, self.COLUMNS), [base for base, _, _ in self.COLUMNS])

    def test_extra_room_goes_to_the_stretching_columns_in_proportion(self) -> None:
        widths = fit_widths(1300, self.COLUMNS)
        self.assertEqual(sum(widths), 1300)
        self.assertEqual([widths[i] for i in (0, 4, 5, 6)], [28, 128, 62, 82])  # fixed ones stay
        self.assertEqual(widths[2], widths[3])
        self.assertGreater(widths[2], widths[1])  # proportional: 245 : 160

    def test_missing_room_is_taken_from_the_stretching_columns(self) -> None:
        widths = fit_widths(800, self.COLUMNS)
        self.assertEqual(sum(widths), 800)
        self.assertEqual([widths[i] for i in (0, 4, 5, 6)], [28, 128, 62, 82])
        self.assertTrue(all(widths[i] >= 90 for i in (1, 2, 3)))

    def test_a_column_that_would_go_below_its_minimum_stays_at_it(self) -> None:
        columns = [(100, 90, True), (400, 90, True)]
        widths = fit_widths(300, columns)  # proportionally 60 / 240: the first is pinned at 90
        self.assertEqual(widths, [90, 210])

    def test_when_even_the_minimums_do_not_fit_they_win(self) -> None:
        widths = fit_widths(100, self.COLUMNS)
        self.assertEqual([widths[i] for i in (1, 2, 3)], [90, 90, 90])
        self.assertEqual(widths[0] + widths[4] + widths[5] + widths[6], 300)

    def test_no_stretching_column_means_nothing_changes(self) -> None:
        self.assertEqual(fit_widths(900, [(50, 50, False), (70, 70, False)]), [50, 70])

    def test_rounding_never_loses_a_pixel(self) -> None:
        for available in range(560, 1400, 7):
            with self.subTest(available):
                widths = fit_widths(available, self.COLUMNS)
                self.assertEqual(sum(widths), max(available, 570))

    def test_widths_are_whole_numbers(self) -> None:
        self.assertTrue(all(isinstance(w, int) for w in fit_widths(913, self.COLUMNS)))


class _TkTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import tkinter as tk
        except ImportError:
            self.skipTest("tkinter is unavailable")
        self.tk = tk
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("Tk display is unavailable")
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.pump)  # run what is pending (the GUI queues status lines) before destroy
        try:
            self.root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass

    def pump(self, rounds: int = 5) -> None:
        for _ in range(rounds):
            self.root.update()


class TooltipTest(_TkTest):
    def setUp(self) -> None:
        super().setUp()
        from tkinter import ttk

        from interfaces.tk_tooltip import TreeCellTooltip
        self.tree = ttk.Treeview(self.root, columns=("a", "b"), show="headings", height=4)
        for column, width in (("a", 90), ("b", 300)):
            self.tree.heading(column, text=column)
            self.tree.column(column, width=width, stretch=False)
        self.tree.insert("", "end", iid="r1", values=("short", "x" * 120))
        self.tree.insert("", "end", iid="r2", values=("a rather long title for a narrow column", ""))
        self.tree.pack()
        self.root.geometry("+4000+4000")
        self.root.deiconify()
        self.pump()
        self.tip = TreeCellTooltip(self.tree, delay_ms=0)

    def _point(self, row: str, column: str) -> tuple[int, int]:
        x, y, width, height = self.tree.bbox(row, column)
        return x + 5, y + height // 2

    def _hover(self, row: str, column: str) -> None:
        x, y = self._point(row, column)
        self.tree.event_generate("<Motion>", x=x, y=y,
                                 rootx=self.tree.winfo_rootx() + x, rooty=self.tree.winfo_rooty() + y)
        self.pump()

    def test_cell_at_names_the_cell_under_the_pointer(self) -> None:
        self.assertEqual(self.tip.cell_at(*self._point("r1", "b")), ("r1", "b"))
        self.assertEqual(self.tip.cell_at(*self._point("r2", "a")), ("r2", "a"))

    def test_nothing_is_under_a_heading_or_empty_space(self) -> None:
        self.assertIsNone(self.tip.cell_at(10, 3))  # the heading
        self.assertIsNone(self.tip.cell_at(10, self.tree.winfo_height() - 3))  # below the rows

    def test_only_a_cell_that_does_not_fit_has_text_to_show(self) -> None:
        self.assertEqual(self.tip.cut_off_text("r1", "b"), "x" * 120)
        self.assertEqual(self.tip.cut_off_text("r2", "a"), "a rather long title for a narrow column")
        self.assertIsNone(self.tip.cut_off_text("r1", "a"))  # "short" fits
        self.assertIsNone(self.tip.cut_off_text("r2", "b"))  # empty
        self.assertIsNone(self.tip.cut_off_text("nope", "a"))

    def test_the_width_on_screen_decides_not_the_configured_one(self) -> None:
        self.assertIsNone(self.tip.cut_off_text("r1", "a"))
        self.tree.column("a", width=20)  # squeezed, as when the window gets narrow
        self.pump()
        self.assertEqual(self.tip.cut_off_text("r1", "a"), "short")

    def test_resting_on_a_cut_off_cell_shows_its_whole_text(self) -> None:
        self._hover("r2", "a")
        tip = self.tip._tip
        self.assertIsNotNone(tip)
        label = tip.winfo_children()[0]
        self.assertEqual(label.cget("text"), "a rather long title for a narrow column")

    def test_a_cell_that_fits_shows_nothing(self) -> None:
        self._hover("r1", "a")
        self.assertIsNone(self.tip._tip)

    def test_leaving_or_clicking_hides_it(self) -> None:
        self._hover("r2", "a")
        self.assertIsNotNone(self.tip._tip)
        self.tree.event_generate("<Leave>")
        self.pump()
        self.assertIsNone(self.tip._tip)
        self._hover("r2", "a")  # coming back shows it again
        self.assertIsNotNone(self.tip._tip)
        self.tree.event_generate("<ButtonPress-1>", x=5, y=5)
        self.pump()
        self.assertIsNone(self.tip._tip)

    def test_moving_to_another_cell_replaces_the_tip(self) -> None:
        self._hover("r2", "a")
        first = self.tip._tip
        self._hover("r1", "b")
        self.assertIsNotNone(self.tip._tip)
        self.assertIsNot(self.tip._tip, first)
        self.assertEqual(self.tip._tip.winfo_children()[0].cget("text"), "x" * 120)

    def test_the_tip_goes_when_the_row_does(self) -> None:
        self.tip.delay_ms = 50  # long enough to remove the row first
        x, y = self._point("r2", "a")
        self.tree.event_generate("<Motion>", x=x, y=y,
                                 rootx=self.tree.winfo_rootx() + x, rooty=self.tree.winfo_rooty() + y)
        self.tree.delete("r2")
        self.root.after(120, self.root.quit)
        self.root.mainloop()
        self.assertIsNone(self.tip._tip)


class JobsColumnsTest(_TkTest):
    """The real 기본 tab list follows the window's width."""

    def setUp(self) -> None:
        super().setUp()
        from interfaces import gui
        self.gui = gui
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        self.app = gui.DownloaderGUI(
            self.root, history=HistoryStore(directory / "history.json"),
            cookie_health=CookieHealthStore(directory / "health.json"))
        self.root.geometry("+4000+4000")
        self.root.deiconify()
        self.pump(10)

    def _widths(self, window_width: int) -> tuple[list[int], int]:
        self.root.geometry(f"{window_width}x720")
        self.pump(10)
        tree = self.app.jobs_tree
        return [int(tree.column(c, "width")) for c in tree["columns"]], tree.winfo_width()

    def test_the_default_window_fits_a_live_recording_row(self) -> None:
        self.assertEqual(self.root.geometry().split("+")[0], "1000x720")
        widths, tree_width = self._widths(1000)
        self.assertLessEqual(sum(widths), tree_width)
        # the row of the Phase J run: both streams, both speeds, a batch outcome
        from tkinter import font as tkfont
        font = tkfont.Font(root=self.root, font="TkDefaultFont")
        for column, text in ((3, "영상 182.1MB 받음 · 오디오 34.1MB 받음"),
                             (4, "793.4KB/s · 82.2KB/s"),
                             (2, "취소 (완료 0 · 라이브라 건너뜀 1 · 취소 1)")):
            with self.subTest(text):
                self.assertGreater(widths[column], font.measure(text) * 0.9)

    def test_a_narrow_window_still_shows_every_column(self) -> None:
        widths, tree_width = self._widths(800)
        self.assertLessEqual(sum(widths), tree_width)  # nothing pushed out of view
        self.assertTrue(all(w >= self.gui._JOB_MIN_STRETCH for w in widths[1:4]))

    def test_a_wide_window_gives_the_extra_to_the_text_columns(self) -> None:
        narrow, _ = self._widths(900)
        wide, _ = self._widths(1400)
        self.assertGreater(wide[1], narrow[1])
        self.assertGreater(wide[3], narrow[3])
        self.assertEqual([wide[i] for i in (0, 4, 5, 6)], [narrow[i] for i in (0, 4, 5, 6)])


if __name__ == "__main__":
    unittest.main()
