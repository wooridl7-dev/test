"""A hover tooltip for the cells a ttk.Treeview cuts off.

Titles, batch summaries and the sizes of a two-stream download can be longer
than any sensible column. When the mouse rests on a cell whose text does not
fit, the whole text appears next to the pointer; a cell that fits shows
nothing. The Treeview itself is left alone: the tooltip only binds events.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Optional

__all__ = ["TreeCellTooltip"]


class TreeCellTooltip:
    """Show the full text of a Treeview cell that is cut off.

    Args:
        tree: A ``show="headings"`` Treeview (its columns are ``#1``, ``#2`` ...).
        delay_ms: How long the pointer rests on a cell before the tip appears.
        max_width: Widest the tip gets, in pixels; longer text wraps.
    """

    # What a cell's own margins take from its width, besides the text.
    CELL_PADDING = 14

    def __init__(self, tree: ttk.Treeview, *, delay_ms: int = 450, max_width: int = 560) -> None:
        self.tree = tree
        self.delay_ms = delay_ms
        self.max_width = max_width
        self._font = tkfont.Font(root=tree, font=ttk.Style(tree).lookup("Treeview", "font") or "TkDefaultFont")
        self._cell: Optional[tuple[str, str]] = None
        self._after_id: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None
        tree.bind("<Motion>", self._on_motion, add="+")
        for sequence in ("<Leave>", "<ButtonPress>", "<MouseWheel>", "<KeyPress>", "<Destroy>"):
            tree.bind(sequence, self._hide, add="+")

    # -- what to show

    def cell_at(self, x: int, y: int) -> Optional[tuple[str, str]]:
        """``(row id, column id)`` of the data cell at widget coordinates, else ``None``."""
        if self.tree.identify_region(x, y) != "cell":
            return None
        row = self.tree.identify_row(y)
        spec = self.tree.identify_column(x)  # "#3"
        columns = self.tree.cget("columns")
        if isinstance(columns, str):
            columns = (columns,)
        if not row or not spec.startswith("#") or not spec[1:].isdigit():
            return None
        index = int(spec[1:]) - 1
        if not 0 <= index < len(columns):
            return None
        return row, str(columns[index])

    def cut_off_text(self, row: str, column: str) -> Optional[str]:
        """The cell's text when it is wider than its column, else ``None``."""
        if not self.tree.exists(row):
            return None
        text = str(self.tree.set(row, column))
        if not text.strip():
            return None
        # The width on screen: a column's configured width is not what it is
        # drawn with once the table has been stretched or squeezed.
        box = self.tree.bbox(row, column)
        if not box:  # scrolled out of sight
            return None
        return text if self._font.measure(text) + self.CELL_PADDING > box[2] else None

    # -- events

    def _on_motion(self, event: tk.Event) -> None:
        cell = self.cell_at(event.x, event.y)
        if cell == self._cell:
            return
        self._hide()
        self._cell = cell
        if cell is not None:
            self._after_id = self.tree.after(
                self.delay_ms, lambda: self._show(cell, event.x_root, event.y_root)
            )

    def _show(self, cell: tuple[str, str], x_root: int, y_root: int) -> None:
        self._after_id = None
        text = self.cut_off_text(*cell)
        if text is None:
            return
        tip = tk.Toplevel(self.tree)
        tip.wm_overrideredirect(True)
        tip.wm_attributes("-topmost", True)  # above a window that is itself topmost
        tk.Label(
            tip,
            text=text,
            font=self._font,
            justify="left",
            wraplength=self.max_width,
            background="#ffffe1",
            foreground="#000000",
            relief="solid",
            borderwidth=1,
            padx=4,
            pady=2,
        ).pack()
        tip.update_idletasks()
        x = x_root + 12
        y = y_root + 18
        # Keep it on screen.
        x = max(0, min(x, tip.winfo_screenwidth() - tip.winfo_reqwidth() - 4))
        y = max(0, min(y, tip.winfo_screenheight() - tip.winfo_reqheight() - 4))
        tip.wm_geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide(self, _event: Optional[tk.Event] = None) -> None:
        if self._after_id is not None:
            try:
                self.tree.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None
        if _event is not None and _event.type != tk.EventType.Motion:
            self._cell = None
