"""PyInstaller entry point that forces GUI mode.

When the user double-clicks yt-dlp-gui.exe this script ensures
``--gui`` is injected into sys.argv before handing off to main.main(),
so the application always opens the Tkinter GUI regardless of what
argv[0] happens to look like inside the frozen bundle.
"""

from __future__ import annotations

import sys


def _inject_gui_flag() -> None:
    """Insert --gui as the first real argument if not already present."""
    flags = ("--gui", "--cli", "--bot")
    if not any(f in sys.argv for f in flags):
        # argv[0] is the executable path; keep it, inject after it.
        sys.argv = [sys.argv[0], "--gui"] + sys.argv[1:]


_inject_gui_flag()

import main  # noqa: E402  (must come after argv mutation)

if __name__ == "__main__":
    sys.exit(main.main())
