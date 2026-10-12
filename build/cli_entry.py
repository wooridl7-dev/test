"""PyInstaller entry point that forces CLI mode.

When the user runs yt-dlp-cli.exe (or double-clicks it) this script
ensures ``--cli`` is injected into sys.argv before handing off to
main.main(), so the application always enters the interactive CLI
regardless of what argv[0] looks like inside the frozen bundle.
"""

from __future__ import annotations

import sys


def _inject_cli_flag() -> None:
    """Insert --cli as the first real argument if not already present."""
    flags = ("--gui", "--cli", "--bot")
    if not any(f in sys.argv for f in flags):
        # argv[0] is the executable path; keep it, inject after it.
        sys.argv = [sys.argv[0], "--cli"] + sys.argv[1:]


_inject_cli_flag()

import main  # noqa: E402  (must come after argv mutation)

if __name__ == "__main__":
    sys.exit(main.main())
