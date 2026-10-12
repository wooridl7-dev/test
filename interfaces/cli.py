"""Interactive command-line interface.

This module owns all the ``input()`` / ``print()`` calls that used to
live in the single-file ``downloader.py``. It delegates the actual
work to :mod:`core.downloader` and :mod:`core.cookies`.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

from core import presets
from core.batch import (
    DEFAULT_PAUSE,
    live_path_for,
    pause_label,
    read_batch_file,
    remaining_path_for,
    run_batch,
    safe_folder_name,
)
from core.auth import (
    Authenticator,
    AuthValidationResult,
    BgutilPoTokenAuth,
    CookieFileAuth,
    NoAuth,
)
from core.cookie_health import CookieHealthStore
from core.cookies import (
    CookieUpdateResult,
    is_admin_on_windows,
    update_cookies,
)
from core.downloader import DownloadOptions, ProgressDisplay, check_js_runtime, run_download
from core.history import HistoryEntry, HistoryStore
from core.live import DEFAULT_FRAGMENT_RETRIES, DEFAULT_STALL_MINUTES, LiveOptions

__all__ = ["run_batch_cli", "run_cli"]


# --- Option prompts ---------------------------------------------------------


_HISTORY_STATUS_LABELS = {"ok": "✓", "warning": "⚠ 확인 필요", "failed": "✗", "cancelled": "취소", "skipped": "건너뜀"}


def _pick_from_history(history: HistoryStore) -> str:
    """최근 기록 10개를 보여 주고 고른 URL 을 반환합니다. ("" = 고르지 않음)"""
    recent = history.recent(10)
    if not recent:
        print("다운로드 기록이 없습니다.")
        return ""
    print("\n=== 최근 다운로드 기록 ===")
    for number, entry in enumerate(recent, 1):
        status = _HISTORY_STATUS_LABELS.get(entry.status, entry.status)
        title = entry.title or "(제목 없음)"
        print(f"{number:2}. [{entry.time[5:16]}] {status} {title} — {entry.url}")
    choice = input("\n번호 선택 (Enter: URL 직접 입력): ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(recent):
        return recent[int(choice) - 1].url
    return ""


def get_url(history: HistoryStore) -> str:
    """URL 을 입력받습니다. 'h' 를 입력하면 최근 기록에서 고릅니다."""
    while True:
        url = input("\nURL 입력 (최근 기록: h): ").strip()
        if url.lower() != "h":
            return url
        picked = _pick_from_history(history)
        if picked:
            print(f"URL: {picked}")
            return picked


def get_download_options() -> tuple[str, str]:
    """사용자로부터 다운로드 옵션을 입력받습니다."""
    print("\n=== 다운로드 옵션 ===")
    print("1. 최고 화질 (영상+오디오)")
    print("2. 1080p")
    print("3. 720p")
    print("4. 480p")
    print("5. 오디오만 (MP3)")
    print("6. 오디오만 (M4A)")

    choice = input("\n옵션 선택 (기본값: 1): ").strip() or "1"
    return choice, presets.format_for(choice)


def get_subtitle_options() -> dict:
    """자막 옵션을 입력받습니다."""
    print("\n=== 자막 옵션 ===")
    print("1. 자막 없음")
    print("2. 한국어 자막")
    print("3. 영어 자막")
    print("4. 모든 자막")
    print("5. 자동 생성 자막 포함")

    choice = input("\n자막 선택 (기본값: 1): ").strip() or "1"
    return presets.subtitle_opts_for(choice)


_AUDIO_TRACK_LABELS = {
    "1": "한국어 우선",
    "2": "원본",
    "3": "영어 우선",
    "4": "일본어 우선",
}


def get_audio_track_option() -> tuple[str, str | None]:
    """오디오 트랙 옵션을 입력받습니다. (메뉴 이름, 언어 코드)를 반환."""
    print("\n=== 오디오 트랙 (더빙 영상) ===")
    print("1. 한국어 우선 (없으면 원본)")
    print("2. 원본")
    print("3. 영어 우선 (없으면 원본)")
    print("4. 일본어 우선 (없으면 원본)")

    choice = input("\n오디오 선택 (기본값: 1): ").strip() or "1"
    if choice not in _AUDIO_TRACK_LABELS:
        choice = "1"
    return _AUDIO_TRACK_LABELS[choice], presets.audio_lang_for(choice)


def get_output_options() -> str:
    """출력 경로 및 파일명 옵션을 입력받습니다."""
    print("\n=== 출력 설정 ===")
    default_path = str(Path.home() / "Downloads")
    output_path = input(f"저장 경로 (기본값: {default_path}): ").strip() or default_path

    print("\n파일명 형식:")
    print("1. 제목만")
    print("2. 채널명 - 제목")
    print("3. 업로드 날짜 - 제목")
    print("4. 제목 (ID)")

    name_choice = input("\n파일명 형식 선택 (기본값: 1): ").strip() or "1"
    return presets.output_template_for(output_path, name_choice)


def _describe_error(result: CookieUpdateResult) -> str:
    """Translate a failure result into a user-facing explanation."""
    if result.error_kind == "admin_context":
        return (
            "관리자 권한에서는 Chrome/Edge 쿠키를 복호화할 수 없습니다. "
            "관리자가 아닌 일반 창에서 다시 실행하세요."
        )
    if result.error_kind == "browser_locked":
        return "브라우저가 실행 중입니다. 해당 브라우저를 모두 종료한 뒤 다시 시도하세요."
    if result.error_kind == "browser_not_found":
        return "브라우저 프로필을 찾지 못했습니다. 브라우저가 설치되어 있는지 확인하세요."
    if result.error_kind == "yt_dlp_missing":
        return "yt-dlp가 설치되어 있지 않습니다. 'pip install yt-dlp'을 실행하세요."
    return result.message


def _describe_auth_error(result: AuthValidationResult) -> str:
    """Human-facing explanation for an :class:`AuthValidationResult`."""
    kind = result.error_kind
    if kind == "bgutil_plugin_missing":
        return (
            "bgutil-ytdlp-pot-provider 플러그인이 설치되어 있지 않습니다. "
            "'pip install bgutil-ytdlp-pot-provider' 를 실행해주세요."
        )
    if kind == "bgutil_unreachable":
        return (
            "bgutil provider 사이드카가 응답하지 않습니다. "
            "Docker 컨테이너가 기동 중인지 확인해주세요."
        )
    if kind == "yt_dlp_too_old":
        return (
            "yt-dlp 버전이 너무 낮습니다. 'pip install -U yt-dlp' 로 업그레이드해주세요."
        )
    if kind == "cookie_file_missing":
        return "쿠키 파일을 찾을 수 없습니다."
    if kind == "cookie_file_empty":
        return "쿠키 파일이 비어 있습니다."
    return result.message


def _run_cookie_update(browser: str, cookie_file: str) -> bool:
    """쿠키를 업데이트하고 결과를 화면에 출력합니다."""
    print(f"\n{browser.title()} 브라우저에서 쿠키를 추출합니다...")
    print(f"출력 파일: {cookie_file}")
    result = update_cookies(browser, cookie_file)
    if result.success:
        print(f"\u2713 {result.message}")
        return True
    print(f"\u2717 쿠키 업데이트 실패: {_describe_error(result)}")
    if result.message and result.error_kind not in {"admin_context"}:
        # Include the raw stderr tail for debugging, but not for the
        # admin hint (which is already self-explanatory).
        print(result.message)
    return False


def get_cookie_options() -> Authenticator:
    """인증 방식을 선택합니다 (YouTube 봇 감지 우회용).

    Returns an :class:`Authenticator` describing the chosen strategy.
    Legacy callers that still expect a dict can wrap this in
    :func:`core.auth.authenticator_from_legacy_dict` inverse — but new
    code paths should prefer the Authenticator directly.
    """
    print("\n=== 인증 설정 (YouTube 봇 감지 우회) ===")
    print("\u26a0\ufe0f  YouTube가 봇 감지를 강화하여 인증이 필요합니다!")

    if is_admin_on_windows():
        print(
            "\n\u26a0\ufe0f  관리자 권한으로 실행 중입니다. "
            "Chrome/Edge 자동 추출은 실패할 수 있으니 일반 창에서 실행하세요."
        )

    print("\n1. Chrome에서 자동으로 쿠키 업데이트하고 사용 [권장]")
    print("2. Firefox에서 자동으로 쿠키 업데이트하고 사용")
    print("3. Edge에서 자동으로 쿠키 업데이트하고 사용")
    print("4. 기존 쿠키 파일 사용")
    print("5. 인증 안함 (실패할 가능성 높음)")
    print("6. PO Token (bgutil 사이드카 자동) [헤드리스 권장]")
    print("7. 쿠키 자동 갱신 (Playwright, 브라우저 창이 열립니다)")

    choice = input("\n인증 옵션 선택 (기본값: 1): ").strip() or "1"

    if choice in {"1", "2", "3"}:
        browser_map = {"1": "chrome", "2": "firefox", "3": "edge"}
        browser = browser_map[choice]
        cookie_file = "cookies.txt"
        if _run_cookie_update(browser, cookie_file):
            print(f"\n\u2713 {browser.title()} 쿠키 업데이트 완료!")
            return CookieFileAuth(path=cookie_file)
        print("\n\u2717 쿠키 업데이트 실패. 다른 옵션을 선택해주세요.")
        return get_cookie_options()

    if choice == "4":
        print("\n쿠키 파일 사용 방법:")
        print("  1. Chrome 확장 프로그램 'Get cookies.txt LOCALLY' 설치")
        print("     https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc")
        print("  2. YouTube에 로그인한 상태에서 확장 프로그램 클릭")
        print("  3. 'Export' 버튼으로 cookies.txt 저장")

        cookie_file = input("\n쿠키 파일 경로 (기본값: cookies.txt): ").strip() or "cookies.txt"
        if not Path(cookie_file).exists():
            print(f"\n\u26a0\ufe0f  '{cookie_file}' 파일이 없습니다.")
            print("자동 업데이트 옵션(1-3)을 선택하시는 것을 권장합니다.")
            proceed = input("그래도 계속하시겠습니까? (y/N): ").strip().lower()
            if proceed != "y":
                return get_cookie_options()
        return CookieFileAuth(path=cookie_file)

    if choice == "5":
        print("\n\u26a0\ufe0f  경고: 인증 없이 시도하면 실패할 가능성이 높습니다!")
        return NoAuth()

    if choice == "6":
        default_url = "http://127.0.0.1:4416"
        print("\nbgutil PO Token provider 설정")
        print(f"  기본 URL: {default_url}")
        print("  (Docker 로 사이드카 기동: docker run -d -p 4416:4416 "
              "brainicism/bgutil-ytdlp-pot-provider:latest)")
        url = input(f"\nProvider URL (기본값: {default_url}): ").strip() or default_url
        auth = BgutilPoTokenAuth(provider_url=url)
        print(f"\nbgutil provider 연결 테스트: {url}")
        result = auth.validate()
        if result.success:
            print(f"\u2713 {result.message}")
            return auth
        print(f"\u2717 bgutil 검증 실패: {_describe_auth_error(result)}")
        print(result.message)
        print("\n권장 대체 수단:")
        print("  - 옵션 1-3: 브라우저 쿠키 자동 추출")
        print("  - 옵션 4: 기존 cookies.txt 사용")
        proceed = input("\n그래도 이 provider 로 진행하시겠습니까? (y/N): ").strip().lower()
        if proceed == "y":
            return auth
        return get_cookie_options()

    if choice == "7":
        # Lazy import: avoids asyncio/Playwright cost when users don't pick this.
        from core.refresher import playwright_available, refresh_cookies

        if not playwright_available():
            print(
                "\n\u2717 Playwright 모듈이 포함되어 있지 않습니다. "
                "빌드 시 -NoPlaywright 옵션이 사용되었을 가능성이 있습니다."
            )
            return get_cookie_options()

        default_cookie = "cookies.txt"
        cookie_file = (
            input(f"\n쿠키 파일 경로 (기본값: {default_cookie}): ").strip()
            or default_cookie
        )
        profile_dir = Path.home() / ".ytdlp-cookie-refresher" / "chromium-profile"
        print("\nChromium 창이 열립니다. YouTube 에 로그인한 뒤 창을 닫아주세요.")
        result = refresh_cookies(
            output=Path(cookie_file),
            profile_dir=profile_dir,
            browser="chromium",
            auto=False,
            log_callback=lambda m: print(m),
        )
        if result.success:
            print(
                f"\n\u2713 쿠키 자동 갱신 완료: {result.output_path} "
                f"({result.cookies_written} cookies)"
            )
            return CookieFileAuth(path=str(result.output_path))
        print(f"\n\u2717 쿠키 자동 갱신 실패 (exit {result.exit_code}): {result.message}")
        return get_cookie_options()

    # Unknown choice: re-prompt.
    return get_cookie_options()


# --- Callbacks used during the actual download ------------------------------


class _CliProgressPrinter:
    """Adapter that turns progress dicts into overwritable CLI lines."""

    def __init__(self) -> None:
        self._formatter = ProgressDisplay()
        self._last_len = 0

    def on_progress(self, info: dict) -> None:
        line = self._formatter.format_line(info)
        pad = max(0, self._last_len - len(line))
        sys.stdout.write("\r" + line + (" " * pad))
        sys.stdout.flush()
        self._last_len = len(line)

    def on_log(self, line: str, level: str) -> None:
        # Make sure log output starts on a fresh line so it does not
        # collide with the in-place progress line.
        if self._last_len:
            sys.stdout.write("\r" + " " * self._last_len + "\r")
            self._last_len = 0
        prefix = {"error": "\u2717", "warning": "\u26a0\ufe0f", "info": " "}.get(level, " ")
        print(f"{prefix} {line}")


# Shown when a run confirms the login cookies expired (DownloadResult.cookie_event).
# The core already logs a warning line; this block is what a long run does not bury.
_COOKIE_EXPIRED_LINES = (
    "쿠키의 로그인이 만료된 것으로 확인되었습니다.",
    "로그인이 필요한 영상(멤버 전용 등)은 받지 못합니다. 공개 영상은 쿠키 없이도 받아질 수 있습니다.",
    "다음 실행의 인증 설정에서 7번(쿠키 자동 갱신) 또는 1~3번(브라우저에서 쿠키 업데이트)으로 쿠키를 다시 받으세요.",
    "브라우저 쿠키를 쓰는 경우에는 그 브라우저에서 YouTube 에 다시 로그인하세요.",
)


def _print_cookie_expired(printer: _CliProgressPrinter) -> None:
    for line in _COOKIE_EXPIRED_LINES:
        printer.on_log(line, "warning")


# --- JS runtime preflight ---------------------------------------------------


def _check_js_runtime_interactive() -> None:
    """deno 설치 확인 및 안내."""
    if check_js_runtime():
        return

    print("\n\u26a0\ufe0f  JavaScript 런타임(deno)이 설치되어 있지 않습니다.")
    print("   YouTube 다운로드를 위해 설치를 권장합니다.\n")
    print("설치 방법:")
    print("  Linux/macOS: curl -fsSL https://deno.land/install.sh | sh")
    print("  Windows: irm https://deno.land/install.ps1 | iex")
    print("  또는 공식 사이트: https://deno.land/\n")
    proceed = input("설치 없이 계속하시겠습니까? (y/N): ").strip().lower()
    if proceed != "y":
        print("\n프로그램을 종료합니다. deno를 설치한 후 다시 실행해주세요.")
        sys.exit(0)
    print()


# --- Entry point ------------------------------------------------------------


def run_cli(
    fragment_retries: int | None = DEFAULT_FRAGMENT_RETRIES,
    stall_minutes: int = DEFAULT_STALL_MINUTES,
) -> int:
    """Run the interactive CLI. Returns a shell exit code.

    *fragment_retries* is ``--fragment-retries`` for a live recording:
    ``0`` means no retries, ``None`` infinite. *stall_minutes* stops a
    recording that receives nothing for that long (``0`` = never).
    """
    print("=" * 60)
    print("       yt-dlp 다운로더 (CLI 모드)")
    print("=" * 60)

    _check_js_runtime_interactive()

    history = HistoryStore()
    url = get_url(history)
    if not url:
        print("URL을 입력해주세요.")
        return 0

    quality_choice, format_opt = get_download_options()
    subtitle_opts = get_subtitle_options()
    audio_label, audio_lang = get_audio_track_option()
    output_path = get_output_options()
    auth = get_cookie_options()

    audio_opts = presets.audio_opts_for(quality_choice)

    # Phase F: live-stream auto-detection is enabled by default from the
    # CLI. No new menu items are added; the downloader logs the detected
    # status via log_callback so the user sees it inline.
    opts = DownloadOptions(
        url=url,
        format_opt=format_opt,
        output_template=output_path,
        auth=auth,
        subtitle_opts=subtitle_opts,
        audio_opts=audio_opts,
        live_opts=LiveOptions(fragment_retries=fragment_retries, stall_minutes=stall_minutes),
        audio_lang=audio_lang,
        cookie_health=CookieHealthStore(),
    )

    printer = _CliProgressPrinter()
    print("\n다운로드 시작...")
    print(f"URL: {url}")
    print(f"저장 경로: {output_path}")
    print(f"오디오: {audio_label}")
    print(f"인증 방식: {auth.describe()}")

    result = run_download(
        url,
        opts,
        progress_callback=printer.on_progress,
        log_callback=printer.on_log,
    )
    print()  # final newline to flush any progress line
    if result.cookie_event == "invalid":
        _print_cookie_expired(printer)
    if result.log_path and (not result.ok or result.warnings or result.subtitles_skipped):
        print(f"실행 로그: {result.log_path}")
    try:
        history.add(HistoryEntry.from_result(url, result, os.path.dirname(output_path)))
    except OSError as exc:
        print(f"⚠️ 다운로드 기록 저장 실패: {exc}")
    return 0 if result.ok else 1


# --- Batch mode (main.py --batch FILE) --------------------------------------


_BATCH_STATE_LABELS = {
    "ok": ("\u2713", "info"),
    "archived": ("이미 받음", "info"),
    "live": ("라이브라 건너뜀", "warning"),
    "failed": ("\u2717 실패", "error"),
    "cancelled": ("취소", "warning"),
}


def run_batch_cli(
    list_path: str,
    fragment_retries: int | None = DEFAULT_FRAGMENT_RETRIES,
    stall_minutes: int = DEFAULT_STALL_MINUTES,
    pause: tuple[float, float] = DEFAULT_PAUSE,
) -> int:
    """목록 파일의 URL 을 차례로 받아 분야별 폴더에 저장합니다.

    화질·자막·오디오·저장 경로·인증은 처음에 한 번만 묻습니다. Ctrl+C 는
    지금 받는 영상을 멈추고 남은 URL 을 ``<목록>.남은목록.txt`` 에
    저장합니다(한 번 더 누르면 바로 종료). 모두 받았거나 건너뛰었으면 0,
    남은 것이 있으면 1 을 반환합니다. *pause* 는 영상 사이에 쉬는 시간(초)의
    ``(최소, 최대)`` 입니다.
    """
    print("=" * 60)
    print("       yt-dlp 다운로더 (일괄 모드)")
    print("=" * 60)

    try:
        plan = read_batch_file(list_path)
    except OSError as exc:
        print(f"\u2717 목록 파일을 읽지 못했습니다: {exc}")
        return 1
    print(f"\n목록: {list_path}")
    print(plan.describe())
    for category in plan.categories:
        count = sum(1 for item in plan.items if item.category == category)
        print(f"  - {category}: {count}개")
    for line_no, text in plan.invalid:
        print(f"  \u26a0\ufe0f {line_no}번째 줄은 URL 이 아니라 건너뜁니다: {text}")
    if not plan.items:
        print("받을 URL 이 없습니다.")
        return 1

    _check_js_runtime_interactive()
    quality_choice, format_opt = get_download_options()
    subtitle_opts = get_subtitle_options()
    audio_label, audio_lang = get_audio_track_option()
    output_path = get_output_options()
    auth = get_cookie_options()

    base_opts = DownloadOptions(
        url="",
        format_opt=format_opt,
        output_template=output_path,
        auth=auth,
        subtitle_opts=subtitle_opts,
        audio_opts=presets.audio_opts_for(quality_choice),
        live_opts=LiveOptions(fragment_retries=fragment_retries, stall_minutes=stall_minutes),
        audio_lang=audio_lang,
        cookie_health=CookieHealthStore(),
    )
    output_dir = os.path.dirname(output_path)
    print(f"\n저장 경로: {output_dir}/<분야>/")
    print(f"오디오: {audio_label}")
    print(f"인증 방식: {auth.describe()}")
    print(f"영상 사이 휴식: {pause_label(pause)}")
    if input(f"\n{len(plan.items)}개를 받습니다. 시작할까요? (Y/n): ").strip().lower() == "n":
        return 0

    history = HistoryStore()
    printer = _CliProgressPrinter()
    total = len(plan.items)
    cookie_expired: list[int] = []  # items whose run confirmed the expiry (said once per expiry)

    def on_item(index, item, state, result) -> None:
        if state == "running":
            return
        label, level = _BATCH_STATE_LABELS.get(state, (state, "info"))
        title = (result.title if result is not None else "") or item.url
        if result is not None and result.subtitles_skipped:
            title = f"{title} — {result.subtitle_summary}"
        printer.on_log(f"[{index + 1}/{total}] {item.category} {label} {title}", level)
        if result is None:
            return  # skipped before running: nothing new for the history
        if result.cookie_event == "invalid":
            cookie_expired.append(index)
            _print_cookie_expired(printer)
        if result.log_path and (not result.ok or result.warnings or result.subtitles_skipped) and not result.skipped:
            print(f"    실행 로그: {result.log_path}")
        folder = os.path.join(output_dir, safe_folder_name(item.category))
        try:
            history.add(HistoryEntry.from_result(item.url, result, folder))
        except OSError as exc:
            print(f"\u26a0\ufe0f 다운로드 기록 저장 실패: {exc}")

    cancel_event = threading.Event()

    def on_interrupt(_signum, _frame) -> None:
        if cancel_event.is_set():
            raise KeyboardInterrupt
        cancel_event.set()
        printer.on_log("취소 중... 남은 목록을 저장합니다. (한 번 더 누르면 바로 종료)", "warning")

    previous = signal.signal(signal.SIGINT, on_interrupt)
    try:
        summary = run_batch(
            plan.items,
            base_opts,
            progress_callback=printer.on_progress,
            log_callback=printer.on_log,
            item_callback=on_item,
            cancel_event=cancel_event,
            pause=pause,
            remaining_path=remaining_path_for(list_path),
            live_path=live_path_for(list_path),
        )
    except KeyboardInterrupt:  # second Ctrl+C
        print("\n중단했습니다.")
        return 130
    finally:
        signal.signal(signal.SIGINT, previous)

    print()
    print(f"결과: {summary.describe()}")
    if summary.subtitles_skipped:
        print(
            f"⚠️ 자막 없이 받은 영상 {summary.subtitles_skipped}개: 같은 목록을 다시 실행하면 "
            "그 영상은 건너뛰고 자막만 받습니다."
        )
    if cookie_expired:
        # A long batch scrolls the notice away: remind once more at the end.
        print(
            f"⚠️ 이번 실행 중 쿠키 만료가 확인되었습니다 ({cookie_expired[0] + 1}/{total} 번째). "
            "쿠키를 다시 받은 뒤 로그인이 필요했던 항목을 다시 받으세요."
        )
    if summary.remaining_file:
        print(f"다시 받기: python main.py --batch \"{summary.remaining_file}\"")
    if summary.live_file:
        print(f"방송 종료 후: python main.py --batch \"{summary.live_file}\"")
    return 0 if summary.ok else 1


if __name__ == "__main__":
    sys.exit(run_cli())
