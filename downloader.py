"""Backwards-compatibility shim.

The original project shipped a single ``downloader.py`` that contained
both the CLI and the GUI. The code has since been split across the
``core/`` and ``interfaces/`` packages and the canonical entry point is
:mod:`main`. This file is kept so that the long-standing commands
continue to work:

* ``python downloader.py``        -> GUI
* ``python downloader.py --cli``  -> interactive CLI

Any new tooling should prefer ``python main.py --gui|--cli|--bot``.
"""

from __future__ import annotations

import sys

from main import ensure_utf8_stdio


def main() -> int:
    ensure_utf8_stdio()

    # Preserve the original argv contract (no argparse): any occurrence of
    # ``--cli`` in argv forces CLI mode, everything else is GUI.
    if "--cli" in sys.argv[1:]:
        from interfaces.cli import run_cli

        return run_cli()

    from interfaces.gui import run_gui

    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
