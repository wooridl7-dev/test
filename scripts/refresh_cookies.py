"""
refresh_cookies.py — Playwright 기반 YouTube 쿠키 추출 CLI 래퍼
======================================================================
순수 로직은 :mod:`core.refresher` 에 있으며, 이 스크립트는 얇은 CLI
인터페이스(args 파싱 + 표준 출력)만 담당합니다.

사용법:
    python scripts/refresh_cookies.py [--output PATH] [--profile DIR]
                                      [--auto] [--browser chromium|firefox]

CLI 인터페이스는 리팩터링 전과 100% 동일하므로 기존 cron / Windows
작업 스케줄러 설정을 그대로 사용할 수 있습니다. ``--help`` 는
Playwright 가 설치되지 않은 환경에서도 정상 동작합니다 (core.refresher
가 playwright 임포트를 지연시키기 때문).

관련 작업: TODO.md Phase C-4 "로컬 PC 측 쿠키 갱신 스크립트"
"""

import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so ``from core.refresher import ...`` works
# when the script is executed directly via ``python scripts/refresh_cookies.py``.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    default_output = str(Path("~/cookies.txt"))
    # Keep description ASCII-safe for Windows cp949 terminals.
    parser = argparse.ArgumentParser(
        prog="refresh_cookies.py",
        description=(
            "Extract YouTube cookies via Playwright and save as Netscape cookies.txt.\n"
            "First run: omit --auto to open a browser window and log in manually.\n"
            "Subsequent runs: use --auto for headless scheduled execution."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=default_output,
        help=f"Output path for cookies.txt (default: {default_output})",
    )
    parser.add_argument(
        "--profile",
        metavar="DIR",
        default="~/.ytdlp-cookie-refresher/chromium-profile",
        help=(
            "Persistent browser profile directory "
            "(default: ~/.ytdlp-cookie-refresher/<browser>-profile/)"
        ),
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Headless mode. Exit 4 if login page detected (for schedulers/cron).",
    )
    parser.add_argument(
        "--browser",
        choices=["chromium", "firefox"],
        default="chromium",
        help="Browser to use (default: chromium)",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # --profile default incorporates the browser name unless user overrode it.
    if args.profile == "~/.ytdlp-cookie-refresher/chromium-profile":
        args.profile = f"~/.ytdlp-cookie-refresher/{args.browser}-profile"

    # Import lazily AFTER argparse so ``--help`` does not need core.refresher.
    from core.refresher import refresh_cookies

    def _log(msg: str) -> None:
        # Preserve legacy stderr routing for CLI consumers (cron log redirection).
        print(msg, file=sys.stderr)

    try:
        result = refresh_cookies(
            output=Path(args.output),
            profile_dir=Path(args.profile),
            browser=args.browser,
            auto=args.auto,
            log_callback=_log,
        )
    except KeyboardInterrupt:
        print("\n중단되었습니다.", file=sys.stderr)
        sys.exit(1)

    if result.success:
        # Keep the legacy stdout line so existing log parsers keep working.
        print(f"✓ Saved {result.cookies_written} cookies to {result.output_path}")

    sys.exit(result.exit_code)


if __name__ == "__main__":
    main()
