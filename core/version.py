"""Application version string for display, e.g. ``"0.1.0 (723dc02)"``.

* Packaged app: build.ps1 writes the string into ``_app_version.txt``
  and yt-dlp.spec bundles it at the ``sys._MEIPASS`` root.
* Source checkout: the ``VERSION`` file plus ``git describe --always
  --dirty`` when git is available (``-dirty`` marks uncommitted edits).
"""

from __future__ import annotations

import functools
import subprocess
import sys
from pathlib import Path

from core.cookies import no_window_kwargs

__all__ = ["app_version"]

BUNDLED_VERSION_FILE = "_app_version.txt"
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _git_describe() -> str:
    try:
        proc = subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=3,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


@functools.lru_cache(maxsize=None)
def app_version() -> str:
    """Return the version string; ``"0.0.0"`` when nothing is known."""
    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and meipass:
        bundled = _read(Path(meipass) / BUNDLED_VERSION_FILE)
        if bundled:
            return bundled

    base = _read(_REPO_ROOT / "VERSION") or "0.0.0"
    commit = _git_describe()
    return f"{base} ({commit})" if commit else base
