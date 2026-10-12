"""Unified entry point for the yt-dlp downloader.

Dispatches to one of the three interface layers based on command-line
flags. Keeping this file small and import-light means ``python main.py
--help`` works even on a box where tkinter is missing, because we only
import the GUI module when the user actually asks for it.

Usage::

    python main.py                 # GUI (default)
    python main.py --gui           # GUI, explicit
    python main.py --cli           # interactive CLI
    python main.py --bot           # Telegram bot service (see README, "텔레그램 봇")
    python main.py --batch 목록.txt  # download a URL list into category folders
    python main.py --cli --fragment-retries 0   # live: do not retry missing fragments
"""

from __future__ import annotations

import argparse
import sys


def ensure_utf8_stdio() -> None:
    """Force stdout/stderr to UTF-8 on Windows console.

    The default Windows console codepage (cp949 on Korean Windows, cp1252
    on Western) cannot encode the warning emoji / check mark glyphs that
    our Korean UI strings use. ``reconfigure`` is a no-op if the streams
    are already UTF-8 (e.g. on Linux, or in a UTF-8 code-paged Windows
    terminal), so it is safe to call unconditionally.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            # Older Python, or stream is not a TextIOWrapper (e.g. tests).
            pass


def yt_dlp_missing_hint() -> str | None:
    """Return a how-to-fix message if yt-dlp cannot be run, else ``None``.

    From a source checkout, ``core.cookies.yt_dlp_command`` falls back to
    ``<this python> -m yt_dlp``. Launching with a system Python that lacks
    the package (e.g. ``python3 main.py`` instead of the project venv)
    otherwise only surfaces as "No module named yt_dlp" once a download
    starts, so check up front and point at the venv interpreter.
    """
    import importlib.util
    from pathlib import Path

    from core.cookies import yt_dlp_command

    if getattr(sys, "frozen", False):
        # Frozen bundle: yt_dlp_command() raises FileNotFoundError when no
        # yt-dlp.exe is found; the download paths report that themselves.
        return None
    if yt_dlp_command() != [sys.executable, "-m", "yt_dlp"]:
        return None  # standalone yt-dlp on PATH
    if importlib.util.find_spec("yt_dlp") is not None:
        return None

    root = Path(__file__).resolve().parent
    if sys.platform == "win32":
        venv_python = root / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = root / ".venv" / "bin" / "python"
    lines = [f"✗ 현재 Python({sys.executable})에 yt-dlp가 설치되어 있지 않습니다."]
    if venv_python.is_file():
        lines.append(f"  프로젝트 가상환경으로 실행하세요: {venv_python} {root / 'main.py'}")
    else:
        lines.append(f"  설치하세요: {sys.executable} -m pip install -U \"yt-dlp[default]\"")
    return "\n".join(lines)


def _fragment_retries(text: str) -> int | None:
    """argparse type for --fragment-retries: a count, or None for infinite."""
    from core.live import parse_fragment_retries

    try:
        return parse_fragment_retries(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _batch_pause(text: str) -> tuple[float, float]:
    """argparse type for --batch-pause: ``MIN-MAX`` or one number, in seconds."""
    from core.batch import parse_pause

    try:
        return parse_pause(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _stall_minutes(text: str) -> int:
    """argparse type for --stall-minutes: minutes, 0 = watchdog off."""
    from core.live import parse_stall_minutes

    try:
        return parse_stall_minutes(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yt-dlp-downloader",
        description="YouTube 다운로더 (GUI / CLI / bot)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--gui",
        action="store_const",
        dest="mode",
        const="gui",
        help="Launch the Tk GUI (default).",
    )
    mode.add_argument(
        "--cli",
        action="store_const",
        dest="mode",
        const="cli",
        help="Run the interactive CLI.",
    )
    mode.add_argument(
        "--bot",
        action="store_const",
        dest="mode",
        const="bot",
        help="Run the Telegram bot service (configured by YTDLP_BOT_* environment variables).",
    )
    parser.add_argument(
        "--batch",
        metavar="FILE",
        help="Download every URL in FILE, one after another, into a folder "
        "per '# category' heading (CLI), or preload the list with --gui.",
    )
    parser.add_argument(
        "--batch-pause",
        metavar="MIN-MAX",
        type=_batch_pause,
        default=argparse.SUPPRESS,
        help="With --batch: seconds to wait, at random between MIN and MAX, between "
        "two downloads (default 5-15; one number such as 10 fixes it; 0 = no pause, "
        "which makes bot detection more likely). The GUI has fields on its batch tab.",
    )
    parser.add_argument(
        "--fragment-retries",
        metavar="N|infinite",
        type=_fragment_retries,
        default=argparse.SUPPRESS,  # absent unless given: None would mean infinite
        help="Live recording: how often to retry a fragment that does not arrive "
        "(default 10; 0 = no retries; 'infinite' relies on the stall watchdog to "
        "end the run). For --cli and --batch; the GUI has a field on its "
        "advanced/live tab and the bot reads YTDLP_FRAGMENT_RETRIES.",
    )
    parser.add_argument(
        "--stall-minutes",
        metavar="N",
        type=_stall_minutes,
        default=argparse.SUPPRESS,
        help="Live recording: stop it when no new data arrives for N minutes "
        "(default 5; 0 = never stop it this way). For --cli and --batch; the GUI "
        "has a field on its advanced/live tab and the bot reads YTDLP_STALL_MINUTES.",
    )
    parser.set_defaults(mode=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.batch and args.mode == "bot":
        parser.error("--batch cannot be used with --bot")
    cli_batch = bool(args.batch) and args.mode != "gui"
    # Only what was given: an absent option must stay different from "infinite" (None).
    extra = {
        name: getattr(args, name)
        for name in ("fragment_retries", "stall_minutes")
        if hasattr(args, name)
    }
    if extra and not (args.mode == "cli" or cli_batch):
        flags = " and ".join("--" + name.replace("_", "-") for name in extra)
        parser.error(
            f"{flags} apply to --cli and --batch; the GUI has fields on its "
            "advanced/live tab, the bot reads YTDLP_FRAGMENT_RETRIES and YTDLP_STALL_MINUTES"
        )

    pause = {"pause": args.batch_pause} if hasattr(args, "batch_pause") else {}
    if pause and not cli_batch:
        parser.error(
            "--batch-pause applies to --batch (not with --gui, where the batch tab "
            "has the fields)"
        )
    extra.update(pause)

    hint = yt_dlp_missing_hint()
    if hint:
        print(hint, file=sys.stderr)
        return 1

    if cli_batch:
        from interfaces.cli import run_batch_cli

        return run_batch_cli(args.batch, **extra)
    if args.mode == "cli":
        from interfaces.cli import run_cli

        return run_cli(**extra)
    if args.mode == "bot":
        from interfaces.bot import run_bot

        return run_bot()

    # Default: GUI. Import lazily so a headless box still gets --help.
    from interfaces.gui import run_gui

    return run_gui(batch_path=args.batch) if args.batch else run_gui()


if __name__ == "__main__":
    sys.exit(main())
