"""Tkinter-based graphical interface.

Moved verbatim-ish from the original single-file ``downloader.py``.
All substantive work is delegated to :mod:`core.downloader` and
:mod:`core.cookies`; this module only owns the tkinter widgets and the
threading glue that keeps the UI responsive during downloads and
cookie refreshes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Callable
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from core.auth import (
    Authenticator,
    BgutilPoTokenAuth,
    BrowserCookieAuth,
    CookieFileAuth,
    NoAuth,
)
from core import presets
from core.batch import (
    DEFAULT_PAUSE,
    MAX_PAUSE_SECONDS,
    BatchPlan,
    live_path_for,
    pause_label,
    read_batch_file,
    remaining_path_for,
    run_batch,
    safe_folder_name,
)
from core.cookie_health import CookieHealthStore
from core.cookies import CookieUpdateResult, update_cookies
from core.downloader import DownloadOptions, DownloadResult, run_download
from core.history import HistoryEntry, HistoryStore
from core.live import DEFAULT_FRAGMENT_RETRIES, DEFAULT_STALL_MINUTES, LiveOptions
from core.partial_merge import MERGE_TIMEOUT
from core.version import app_version
from interfaces.gui_jobs import (
    DEFAULT_MAX_PARALLEL,
    DOWNLOAD,
    MAX_PARALLEL_LIMIT,
    ROW_COLUMNS,
    DownloadJob,
    JobRejected,
    JobTable,
    fit_widths,
    job_row,
)
from interfaces.tk_tooltip import TreeCellTooltip

__all__ = ["DownloaderGUI", "run_gui"]

# Upper end of the fragment-retries field ("무한" is the checkbox next to it).
_MAX_RETRIES_SPIN = 1000
# Upper end of the stall-minutes field (the command line accepts up to a day).
_MAX_STALL_SPIN = 240
# Upper end of the batch pause fields, in seconds.
_MAX_PAUSE_SPIN = int(MAX_PAUSE_SECONDS)

# 일괄 tab "상태" column, by core.batch item state.
_BATCH_STATE_LABELS = {
    "pending": "대기",
    "running": "받는 중…",
    "ok": "✓ 완료",
    "failed": "✗ 실패",
    "cancelled": "취소",
    "live": "라이브라 건너뜀",
    "archived": "이미 받음",
}

# Shown once when a run confirms the login cookies expired (DownloadResult.cookie_event).
_COOKIE_EXPIRED_MESSAGE = (
    "쿠키의 로그인이 만료된 것으로 확인되었습니다.\n\n"
    "로그인이 필요한 영상(멤버 전용 등)은 받지 못합니다. 공개 영상은 쿠키 없이도 받아질 수 있습니다.\n\n"
    "기본 탭의 [쿠키 자동 갱신] 또는 [쿠키 업데이트] 로 쿠키를 다시 받으세요. "
    "브라우저 쿠키를 쓰는 경우에는 그 브라우저에서 YouTube 에 다시 로그인하세요."
)

# 기록 tab "결과" column.
_HISTORY_STATUS_LABELS = {"ok": "✓", "warning": "⚠ 확인 필요", "failed": "✗", "cancelled": "취소", "skipped": "건너뜀"}

# 기본 tab "다운로드 목록" columns, in gui_jobs.ROW_COLUMNS order:
# (heading, width, stretch, anchor).
# (heading, width, stretch, anchor). The widths fit what the rows really
# say, measured in the Treeview font (맑은 고딕 9): a live recording's
# "영상 182.1MB 받음 · 오디오 34.1MB 받음" (226 px), its two speeds
# (111 px), a batch outcome like "취소 (완료 0 · 라이브라 건너뜀 1 · 취소 1)"
# (227 px), each plus the cell's margins. What is still longer (titles, a
# five-part batch outcome, a two-stream VOD's ETAs) shows in full as a
# tooltip. The stretching columns share the table's width (_fit_jobs_columns)
# and may shrink to _JOB_MIN_STRETCH when the window is narrow.
_JOB_COLUMNS = (
    ("#", 28, False, "center"),
    ("대상", 160, True, "w"),
    ("상태", 245, True, "w"),
    ("받은 양 / 진행", 245, True, "w"),
    ("속도", 128, False, "w"),
    ("경과", 62, False, "center"),
    ("마지막 수신", 82, False, "w"),
)
_JOB_MIN_STRETCH = 90


def _open_folder(path: str) -> None:
    """Show *path* in the platform's file manager."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class DownloaderGUI:
    def __init__(
        self,
        root: tk.Tk,
        history: HistoryStore | None = None,
        cookie_health: CookieHealthStore | None = None,
    ) -> None:
        self.root = root
        # Download history (기록 tab + URL dropdown). Injectable for tests.
        self.history = history if history is not None else HistoryStore()
        # Whether the login cookies still work (core.cookie_health).
        self.cookie_health = cookie_health if cookie_health is not None else CookieHealthStore()
        self.history_search_var = tk.StringVar()
        # Last read of the history file; the search box filters this.
        self._history_entries: list[HistoryEntry] = []
        self._history_by_id: dict[str, HistoryEntry] = {}
        # URLs behind the URL dropdown's "title | url" labels, same order.
        self._url_choices: list[str] = []
        self.root.title(f"yt-dlp 다운로더 v{app_version()}")
        self.root.geometry("1000x720")
        self.root.resizable(True, True)

        # Tk variables
        self.url_var = tk.StringVar()
        self.cookie_file_var = tk.StringVar(value="cookies.txt")
        self.output_path_var = tk.StringVar()
        self.quality_var = tk.StringVar(value="1")
        self.subtitle_var = tk.StringVar(value="1")
        # "1" = Korean audio track when the video has one (presets.audio_lang_for).
        self.audio_var = tk.StringVar(value="1")
        self.filename_var = tk.StringVar(value="1")
        self.browser_var = tk.StringVar(value="chrome")
        # Auth-method selector. Default to "cookie-browser" to preserve
        # the pre-Phase-D GUI behaviour where Chrome cookies + existing
        # cookies.txt were used by default.
        self.auth_method_var = tk.StringVar(value="cookie-browser")
        self.bgutil_url_var = tk.StringVar(value="http://127.0.0.1:4416")

        # Phase F: live-stream tuning (tab "고급 / 라이브"). Defaults
        # mirror :class:`core.live.LiveOptions`.
        self.live_enabled_var = tk.BooleanVar(value=True)
        self.live_concurrent_var = tk.IntVar(value=1)
        # Fragment retries: a count (0 = none) or, separately, "infinite".
        self.live_retries_var = tk.IntVar(value=DEFAULT_FRAGMENT_RETRIES)
        self.live_retries_infinite_var = tk.BooleanVar(value=False)
        self.live_retries_note_var = tk.StringVar(value="")
        # Minutes without new data before a recording is stopped (0 = never).
        self.live_stall_var = tk.IntVar(value=DEFAULT_STALL_MINUTES)
        self.live_wait_minutes_var = tk.IntVar(value=60)
        self.live_remux_var = tk.BooleanVar(value=True)
        self.live_force_var = tk.BooleanVar(value=False)
        # Concurrent downloads: each start adds a job to this table. Its
        # cancel event makes core.downloader kill that job's yt-dlp tree.
        self.max_parallel_var = tk.IntVar(value=DEFAULT_MAX_PARALLEL)
        self._jobs = JobTable()
        # Pending ``after`` id of the once-a-second list refresh.
        self._tick_id: str | None = None
        # 일괄 tab: the batch's row in the 다운로드 목록 (one batch at a time).
        self._batch_job: DownloadJob | None = None
        # Set once 종료 is confirmed while downloading (see _on_close).
        self._closing = False
        # The 쿠키 만료 dialog is open (a second expiry must not stack another).
        self._cookie_alert_open = False
        # 일괄 tab: the list file and what was read from it.
        self.batch_path_var = tk.StringVar()
        # Seconds to rest between two downloads of a batch: shortest and longest.
        self.batch_pause_min_var = tk.IntVar(value=int(DEFAULT_PAUSE[0]))
        self.batch_pause_max_var = tk.IntVar(value=int(DEFAULT_PAUSE[1]))
        self.batch_summary_var = tk.StringVar(value="목록 파일(.txt)을 선택하세요.")
        self._batch_plan: BatchPlan | None = None

        default_path = str(Path.home() / "Downloads")
        self.output_path_var.set(default_path)

        self.setup_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI

    def setup_ui(self) -> None:
        """Build the tkinter widget tree using a ``ttk.Notebook`` with
        four tabs: "기본", "고급 / 라이브", "정보", "기록". All pre-existing
        controls are preserved; they are simply moved onto the
        appropriate tab frame. This keeps every button/radio functional
        while giving the live-stream options a dedicated space.
        """
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky="ewns")

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(main_frame)
        self.notebook.grid(row=0, column=0, sticky="ewns")

        # --- Build the tabs --------------------------------------------
        self.basic_tab = ttk.Frame(self.notebook, padding="6")
        self.advanced_tab = ttk.Frame(self.notebook, padding="6")
        self.info_tab = ttk.Frame(self.notebook, padding="6")
        self.batch_tab = ttk.Frame(self.notebook, padding="6")
        self.history_tab = ttk.Frame(self.notebook, padding="6")

        self.notebook.add(self.basic_tab, text="기본")
        self.notebook.add(self.advanced_tab, text="고급 / 라이브")
        self.notebook.add(self.info_tab, text="정보")
        self.notebook.add(self.batch_tab, text="일괄")
        self.notebook.add(self.history_tab, text="기록")

        self._build_basic_tab(self.basic_tab)
        self._build_advanced_tab(self.advanced_tab)
        self._build_info_tab(self.info_tab)
        self._build_batch_tab(self.batch_tab)
        self._build_history_tab(self.history_tab)
        self._refresh_history_view()
        # Re-read on opening the tab: the CLI may have added entries.
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Apply initial visibility based on default auth method.
        self._on_auth_method_change()
        # Changing the cap should enable/disable 다운로드 시작 right away.
        self.max_parallel_var.trace_add(
            "write", lambda *_args: self._on_jobs_changed()
        )
        # The "무한" warning names the auto-stop time. Tcl runs the newest trace
        # first, i.e. before the field itself has the new text: look once idle.
        self.live_stall_var.trace_add(
            "write", lambda *_args: self.root.after_idle(self._sync_retries_widgets)
        )

        self.add_status("=" * 60 + "\n")
        self.add_status(f"yt-dlp 다운로더 v{app_version()} 가 준비되었습니다.\n\n")
        self.add_status("사용 방법:\n")
        self.add_status("  1. '기본' 탭에서 URL 입력 / 인증 / 화질 설정\n")
        self.add_status("  2. '고급 / 라이브' 탭에서 라이브 스트림 옵션 조정 (자동)\n")
        self.add_status("  3. '기본' 탭 아래쪽 '다운로드 시작' (URL 입력란에서 Enter 도 됩니다)\n")
        self.add_status("  4. '기록' 탭에서 이전 다운로드 조회 · URL 다시 쓰기 · 삭제\n")
        self.add_status("     (URL 입력란의 ▼ 에도 최근 기록이 나옵니다)\n")
        self.add_status("  (라이브 URL 은 자동 감지되어 캐치업 다운로드됩니다)\n")
        self.add_status("  (진행 상태는 '기본' 탭 위쪽 '다운로드 목록'에서 보이며,\n")
        self.add_status("   URL 을 바꿔 '다운로드 시작'을 다시 누르면 동시에 받습니다.\n")
        self.add_status("   이 '정보' 탭은 상세 로그를 보는 곳입니다)\n")
        self.add_status("=" * 60 + "\n")

    # ---- Tab 1: 기본 ---------------------------------------------------

    def _build_basic_tab(self, parent: ttk.Frame) -> None:
        """Populate the "기본" tab with all non-advanced controls.

        Internally mirrors the pre-Phase-F flat layout so column weights
        and widget flow match the original GUI as closely as possible.
        """
        parent.columnconfigure(1, weight=1)

        self._build_jobs_panel(parent)

        # URL
        ttk.Label(parent, text="URL:", font=("Arial", 10, "bold")).grid(
            row=1, column=0, sticky="w", pady=5
        )
        url_frame = ttk.Frame(parent)
        url_frame.grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)
        url_frame.columnconfigure(0, weight=1)

        # Editable combobox: type a URL, or pick a recent download from ▼.
        self.url_entry = ttk.Combobox(
            url_frame,
            textvariable=self.url_var,
            width=60,
            postcommand=self._fill_url_dropdown,
        )
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.url_entry.bind("<<ComboboxSelected>>", self._on_url_dropdown_selected)
        # ttk scrolls a combobox's value with the mouse wheel, which here
        # would silently swap a typed URL for a history entry.
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.url_entry.bind(sequence, lambda _event: "break")

        ttk.Button(url_frame, text="열기", command=self.open_url).grid(
            row=0, column=1, padx=(0, 5)
        )
        ttk.Button(url_frame, text="붙여넣기", command=self.paste_url).grid(
            row=0, column=2
        )

        # ------------------------------------------------------- auth
        ttk.Separator(parent, orient="horizontal").grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=10
        )
        ttk.Label(parent, text="인증 방식:", font=("Arial", 10, "bold")).grid(
            row=3, column=0, sticky="w", pady=5
        )

        auth_method_frame = ttk.Frame(parent)
        auth_method_frame.grid(row=3, column=1, columnspan=2, sticky="ew", pady=5)

        auth_methods = [
            ("쿠키파일", "cookie-file"),
            ("브라우저쿠키", "cookie-browser"),
            ("PO토큰(bgutil)", "po-token-bgutil"),
            ("미인증", "none"),
        ]
        for i, (text, value) in enumerate(auth_methods):
            ttk.Radiobutton(
                auth_method_frame,
                text=text,
                variable=self.auth_method_var,
                value=value,
                command=self._on_auth_method_change,
            ).grid(row=0, column=i, padx=5)

        # Cookie-file / Browser sub-sections.
        ttk.Label(parent, text="쿠키 관리:", font=("Arial", 10, "bold")).grid(
            row=4, column=0, sticky="w", pady=5
        )

        cookie_frame = ttk.Frame(parent)
        cookie_frame.grid(row=4, column=1, columnspan=2, sticky="ew", pady=5)
        cookie_frame.columnconfigure(1, weight=1)
        self.cookie_frame = cookie_frame

        ttk.Label(cookie_frame, text="파일:").grid(row=0, column=0, sticky="w")
        self.cookie_entry = ttk.Entry(
            cookie_frame, textvariable=self.cookie_file_var, width=40
        )
        self.cookie_entry.grid(row=0, column=1, sticky="ew", padx=5)

        ttk.Button(cookie_frame, text="찾기", command=self.browse_cookie_file).grid(
            row=0, column=2, padx=(0, 5)
        )
        ttk.Button(
            cookie_frame, text="쿠키 업데이트", command=self.update_cookies
        ).grid(row=0, column=3, padx=(0, 5))
        # Phase C-4: Playwright-backed auto-refresh.
        ttk.Button(
            cookie_frame, text="쿠키 자동 갱신", command=self.auto_refresh_cookies
        ).grid(row=0, column=4)

        # Browser selection (shown when auth-method == cookie-browser)
        browser_frame = ttk.Frame(parent)
        browser_frame.grid(row=5, column=1, columnspan=2, sticky="ew", pady=5)
        self.browser_frame = browser_frame

        ttk.Label(browser_frame, text="브라우저:").grid(row=0, column=0, sticky="w")
        browsers = [("Chrome", "chrome"), ("Firefox", "firefox"), ("Edge", "edge")]
        for i, (text, value) in enumerate(browsers):
            ttk.Radiobutton(
                browser_frame, text=text, variable=self.browser_var, value=value
            ).grid(row=0, column=i + 1, padx=5)

        # bgutil PO Token controls (shown when auth-method == po-token-bgutil)
        bgutil_frame = ttk.Frame(parent)
        bgutil_frame.grid(row=6, column=1, columnspan=2, sticky="ew", pady=5)
        bgutil_frame.columnconfigure(1, weight=1)
        self.bgutil_frame = bgutil_frame

        ttk.Label(bgutil_frame, text="Provider URL:").grid(row=0, column=0, sticky="w")
        self.bgutil_entry = ttk.Entry(
            bgutil_frame, textvariable=self.bgutil_url_var, width=40
        )
        self.bgutil_entry.grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(
            bgutil_frame,
            text="PO Token 연결 테스트",
            command=self.test_bgutil_connection,
        ).grid(row=0, column=2, padx=(5, 0))

        # Download options
        ttk.Separator(parent, orient="horizontal").grid(
            row=7, column=0, columnspan=3, sticky="ew", pady=10
        )
        ttk.Label(
            parent, text="다운로드 옵션:", font=("Arial", 10, "bold")
        ).grid(row=8, column=0, sticky="w", pady=5)

        quality_frame = ttk.Frame(parent)
        quality_frame.grid(row=8, column=1, columnspan=2, sticky="ew", pady=5)

        qualities = [
            ("최고 화질", "1"),
            ("1080p", "2"),
            ("720p", "3"),
            ("480p", "4"),
            ("MP3", "5"),
            ("M4A", "6"),
        ]
        for i, (text, value) in enumerate(qualities):
            ttk.Radiobutton(
                quality_frame, text=text, variable=self.quality_var, value=value
            ).grid(row=0, column=i, padx=2)

        # Subtitles
        ttk.Label(parent, text="자막:").grid(row=9, column=0, sticky="w", pady=5)
        subtitle_frame = ttk.Frame(parent)
        subtitle_frame.grid(row=9, column=1, columnspan=2, sticky="ew", pady=5)

        subtitles = [
            ("없음", "1"),
            ("한국어", "2"),
            ("영어", "3"),
            ("모두", "4"),
            ("자동+수동", "5"),
        ]
        for i, (text, value) in enumerate(subtitles):
            ttk.Radiobutton(
                subtitle_frame, text=text, variable=self.subtitle_var, value=value
            ).grid(row=0, column=i, padx=2)

        # Audio track (videos with dubbed audio). "우선" falls back to the
        # original track when the video has no such language.
        ttk.Label(parent, text="오디오:").grid(row=10, column=0, sticky="w", pady=5)
        audio_frame = ttk.Frame(parent)
        audio_frame.grid(row=10, column=1, columnspan=2, sticky="ew", pady=5)

        audio_tracks = [
            ("한국어 우선", "1"),
            ("원본", "2"),
            ("영어 우선", "3"),
            ("일본어 우선", "4"),
        ]
        for i, (text, value) in enumerate(audio_tracks):
            ttk.Radiobutton(
                audio_frame, text=text, variable=self.audio_var, value=value
            ).grid(row=0, column=i, padx=2)

        # Output
        ttk.Separator(parent, orient="horizontal").grid(
            row=11, column=0, columnspan=3, sticky="ew", pady=10
        )
        ttk.Label(
            parent, text="출력 설정:", font=("Arial", 10, "bold")
        ).grid(row=12, column=0, sticky="w", pady=5)

        path_frame = ttk.Frame(parent)
        path_frame.grid(row=12, column=1, columnspan=2, sticky="ew", pady=5)
        path_frame.columnconfigure(0, weight=1)

        self.path_entry = ttk.Entry(
            path_frame, textvariable=self.output_path_var, width=50
        )
        self.path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(path_frame, text="찾기", command=self.browse_output_path).grid(
            row=0, column=1
        )

        ttk.Label(parent, text="파일명:").grid(row=13, column=0, sticky="w", pady=5)
        filename_frame = ttk.Frame(parent)
        filename_frame.grid(row=13, column=1, columnspan=2, sticky="ew", pady=5)

        filenames = [
            ("제목", "1"),
            ("채널-제목", "2"),
            ("날짜-제목", "3"),
            ("제목(ID)", "4"),
        ]
        for i, (text, value) in enumerate(filenames):
            ttk.Radiobutton(
                filename_frame, text=text, variable=self.filename_var, value=value
            ).grid(row=0, column=i, padx=2)

        # Start: under the settings it acts on. The 정보 tab keeps the log.
        ttk.Style(self.root).configure(
            "Start.TButton", font=("Arial", 11, "bold"), padding=(28, 6)
        )
        start_frame = ttk.Frame(parent)
        start_frame.grid(row=14, column=0, columnspan=3, pady=(10, 0))
        self.start_button = ttk.Button(
            start_frame,
            text="다운로드 시작",
            style="Start.TButton",
            command=self.start_download,
        )
        self.start_button.pack()
        # Paste a URL, press Enter. (Over the cap or on an empty box
        # start_download answers with a message instead of starting.)
        self.url_entry.bind("<Return>", lambda _event: self.start_download())

    def _build_jobs_panel(self, parent: ttk.Frame) -> None:
        """The "다운로드 목록" at the top of the 기본 tab: one row per
        download, refreshed once a second while any is running."""
        frame = ttk.LabelFrame(parent, text="다운로드 목록", padding="4")
        frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        frame.columnconfigure(0, weight=1)

        self.jobs_tree = ttk.Treeview(
            frame,
            columns=ROW_COLUMNS,
            show="headings",
            selectmode="extended",
            height=4,
        )
        for col, (heading, width, stretch, anchor) in zip(ROW_COLUMNS, _JOB_COLUMNS):
            self.jobs_tree.heading(col, text=heading)
            self.jobs_tree.column(
                col, width=width, minwidth=_JOB_MIN_STRETCH if stretch else width,
                stretch=stretch, anchor=anchor,
            )
        self._jobs_tooltip = TreeCellTooltip(self.jobs_tree)
        self.jobs_tree.bind("<Configure>", self._fit_jobs_columns)
        # No bytes for a while: see gui_jobs.STALE_SECONDS.
        self.jobs_tree.tag_configure("stale", foreground="#cc6600")
        self.jobs_tree.grid(row=0, column=0, sticky="ew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.jobs_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.jobs_tree.configure(yscrollcommand=scrollbar.set)

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=2, sticky="e", pady=(4, 0))
        self.jobs_cancel_selected_button = ttk.Button(
            buttons, text="선택 취소", command=self.cancel_selected_jobs, state="disabled"
        )
        self.jobs_cancel_selected_button.pack(side=tk.LEFT, padx=3)
        self.jobs_cancel_all_button = ttk.Button(
            buttons, text="전체 취소", command=self.cancel_download, state="disabled"
        )
        self.jobs_cancel_all_button.pack(side=tk.LEFT, padx=3)
        ttk.Button(
            buttons, text="완료 항목 지우기", command=self.clear_finished_jobs
        ).pack(side=tk.LEFT, padx=3)

    # ---- Tab 2: 고급 / 라이브 ------------------------------------------

    def _build_advanced_tab(self, parent: ttk.Frame) -> None:
        """Populate the "고급 / 라이브" tab with live-stream controls."""
        parent.columnconfigure(0, weight=1)

        live_frame = ttk.LabelFrame(parent, text="라이브 스트림", padding="8")
        live_frame.grid(row=0, column=0, sticky="ew", pady=5)
        live_frame.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            live_frame,
            text="라이브 자동 감지 및 최적화",
            variable=self.live_enabled_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=3)

        ttk.Label(live_frame, text="병렬 세그먼트 수:").grid(
            row=1, column=0, sticky="w", pady=3
        )
        ttk.Spinbox(
            live_frame,
            from_=1,
            to=64,
            textvariable=self.live_concurrent_var,
            width=6,
        ).grid(row=1, column=1, sticky="w", pady=3)

        ttk.Label(live_frame, text="예정 라이브 최대 대기(분):").grid(
            row=2, column=0, sticky="w", pady=3
        )
        ttk.Spinbox(
            live_frame,
            from_=0,
            to=240,
            textvariable=self.live_wait_minutes_var,
            width=6,
        ).grid(row=2, column=1, sticky="w", pady=3)

        ttk.Label(live_frame, text="끊김 시 조각 재시도(회):").grid(
            row=3, column=0, sticky="w", pady=3
        )
        retries_frame = ttk.Frame(live_frame)
        retries_frame.grid(row=3, column=1, sticky="w", pady=3)
        self.live_retries_spin = ttk.Spinbox(
            retries_frame,
            from_=0,
            to=_MAX_RETRIES_SPIN,
            textvariable=self.live_retries_var,
            width=6,
        )
        self.live_retries_spin.grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            retries_frame,
            text="무한",
            variable=self.live_retries_infinite_var,
            command=self._sync_retries_widgets,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(
            retries_frame,
            text="0 = 재시도 안 함",
            foreground="#555555",
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Label(live_frame, text="진척 없음 자동 정지(분):").grid(
            row=4, column=0, sticky="w", pady=3
        )
        stall_frame = ttk.Frame(live_frame)
        stall_frame.grid(row=4, column=1, sticky="w", pady=3)
        self.live_stall_spin = ttk.Spinbox(
            stall_frame,
            from_=0,
            to=_MAX_STALL_SPIN,
            textvariable=self.live_stall_var,
            width=6,
        )
        self.live_stall_spin.grid(row=0, column=0, sticky="w")
        ttk.Label(
            stall_frame,
            text="0 = 끔 (받은 용량이 이 시간 동안 안 늘면 정지)",
            foreground="#555555",
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(
            live_frame,
            textvariable=self.live_retries_note_var,
            foreground="#b00020",
            justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w")

        ttk.Checkbutton(
            live_frame,
            text="라이브 종료 후 MP4 재포장",
            variable=self.live_remux_var,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=3)

        ttk.Checkbutton(
            live_frame,
            text="수동 강제 적용 (디버그)",
            variable=self.live_force_var,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=3)

        help_text = (
            "YouTube 라이브 URL 을 감지하면 자동으로\n"
            "  --live-from-start  (DVR 윈도우 내 첫 부분부터)\n"
            "  --concurrent-fragments N (병렬 캐치업)\n"
            "  --hls-use-mpegts --fragment-retries N (기본 10)\n"
            "  --remux-video mp4 (종료 후 MP4 재포장)\n"
            "을 적용하여 방송 중간에 시작해도 끝까지 받습니다.\n"
            "병렬 1(기본): 받은 조각을 바로 .part 파일에 이어 붙입니다.\n"
            "병렬 2 이상: 더 빨리 따라잡지만 녹화가 끝날 때까지 조각 파일로만\n"
            "  쌓이고, 중간에 멈추면 .part 파일이 비어 있습니다.\n"
            "받은 용량이 '진척 없음 자동 정지' 시간(기본 5분) 동안 늘지 않으면\n"
            "  방송 종료로 보고 자동 정지합니다.\n"
            "DVR 윈도우(보통 12시간) 바깥 부분은 복구 불가."
        )
        ttk.Label(
            live_frame,
            text=help_text,
            foreground="#555555",
            justify="left",
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))

        parallel_frame = ttk.LabelFrame(parent, text="동시 다운로드", padding="8")
        parallel_frame.grid(row=1, column=0, sticky="ew", pady=5)
        ttk.Label(parallel_frame, text="동시에 받을 최대 개수:").grid(
            row=0, column=0, sticky="w", pady=3
        )
        ttk.Spinbox(
            parallel_frame,
            from_=1,
            to=MAX_PARALLEL_LIMIT,
            textvariable=self.max_parallel_var,
            width=6,
        ).grid(row=0, column=1, sticky="w", padx=(6, 0), pady=3)
        ttk.Label(
            parallel_frame,
            text=(
                "URL 을 바꿔 '다운로드 시작'을 다시 누르면 동시에 받고, 이 개수를 넘으면\n"
                "대기열에 넣었다가 자리가 나는 대로 차례로 시작합니다.\n"
                "같은 영상은 한 번에 하나만 받습니다. 한꺼번에 많이 받으면\n"
                "YouTube 가 봇 감지/속도 제한을 걸 수 있어 기본은 3개입니다.\n"
                "제목이 같은 다른 영상을 함께 받으면 나중 것의 파일명에 ' (ID)'가 붙습니다."
            ),
            foreground="#555555",
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Placeholder for future advanced options (Phase G/H). Kept as
        # an empty LabelFrame so subsequent additions slot in without
        # repositioning the live block.
        future_frame = ttk.LabelFrame(parent, text="기타 (예약)", padding="8")
        future_frame.grid(row=2, column=0, sticky="ew", pady=5)
        ttk.Label(
            future_frame,
            text="향후 추가될 고급 옵션 자리입니다.",
            foreground="#888888",
        ).grid(row=0, column=0, sticky="w")

    # ---- Tab 3: 정보 ---------------------------------------------------

    def _build_info_tab(self, parent: ttk.Frame) -> None:
        """Populate the "정보" tab: the status log (starting and cancelling
        downloads is on the 기본 tab)."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        text_frame = ttk.Frame(parent)
        text_frame.grid(row=0, column=0, sticky="ewns", pady=(0, 6))
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)

        self.status_text = tk.Text(text_frame, height=16, width=80, wrap=tk.WORD)
        self.status_text.grid(row=0, column=0, sticky="ewns")

        scrollbar = ttk.Scrollbar(
            text_frame, orient="vertical", command=self.status_text.yview
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.status_text.configure(yscrollcommand=scrollbar.set)

        button_frame = ttk.Frame(parent)
        button_frame.grid(row=1, column=0, pady=5)

        ttk.Button(
            button_frame, text="상태 지우기", command=self.clear_status
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="종료", command=self._on_close).pack(
            side=tk.LEFT, padx=5
        )

    # ---- Tab 4: 기록 ---------------------------------------------------

    def _build_batch_tab(self, parent: ttk.Frame) -> None:
        """Populate the "일괄" tab: download a URL list into category folders."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        file_frame = ttk.Frame(parent)
        file_frame.grid(row=0, column=0, sticky="ew")
        file_frame.columnconfigure(1, weight=1)
        ttk.Button(file_frame, text="목록 파일 선택…", command=self._batch_choose_file).grid(
            row=0, column=0, padx=(0, 5)
        )
        ttk.Entry(file_frame, textvariable=self.batch_path_var, state="readonly").grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Button(file_frame, text="다시 읽기", command=self._batch_load).grid(
            row=0, column=2, padx=(5, 0)
        )

        ttk.Label(parent, textvariable=self.batch_summary_var).grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )

        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=2, column=0, sticky="ewns", pady=(6, 0))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        columns = (
            # (id, heading, width, stretch, anchor)
            ("no", "#", 40, False, "e"),
            ("category", "분야", 100, False, "w"),
            ("title", "제목 / URL", 300, True, "w"),
            ("state", "상태", 190, False, "center"),
        )
        self.batch_tree = ttk.Treeview(
            tree_frame, columns=[c[0] for c in columns], show="headings", selectmode="browse"
        )
        for col, heading, width, stretch, anchor in columns:
            self.batch_tree.heading(col, text=heading)
            self.batch_tree.column(col, width=width, stretch=stretch, anchor=anchor)
        self._batch_tooltip = TreeCellTooltip(self.batch_tree)
        self.batch_tree.grid(row=0, column=0, sticky="ewns")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.batch_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.batch_tree.configure(yscrollcommand=scrollbar.set)

        ttk.Label(
            parent,
            text=(
                "목록 형식: '# 분야' 줄 아래의 URL 은 <저장 폴더>/<분야>/ 에 저장됩니다.\n"
                "화질·자막·오디오·저장 폴더·파일명·인증은 기본 / 고급 탭 설정을 씁니다. "
                "방송 중인 라이브는 건너뛰고, 이미 받은 영상은 다시 받지 않습니다."
            ),
            foreground="gray",
            justify=tk.LEFT,
        ).grid(row=3, column=0, sticky="w", pady=(6, 0))

        pause_frame = ttk.Frame(parent)
        pause_frame.grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Label(pause_frame, text="영상 사이 휴식(초):").grid(row=0, column=0, sticky="w")
        self.batch_pause_min_spin = ttk.Spinbox(
            pause_frame, from_=0, to=_MAX_PAUSE_SPIN, textvariable=self.batch_pause_min_var,
            width=5,
        )
        self.batch_pause_min_spin.grid(row=0, column=1, padx=(6, 0))
        ttk.Label(pause_frame, text="~").grid(row=0, column=2, padx=4)
        self.batch_pause_max_spin = ttk.Spinbox(
            pause_frame, from_=0, to=_MAX_PAUSE_SPIN, textvariable=self.batch_pause_max_var,
            width=5,
        )
        self.batch_pause_max_spin.grid(row=0, column=3)
        ttk.Label(
            pause_frame,
            text="최소~최대 사이 무작위. 0~0 이면 쉬지 않지만 봇 감지가 더 잘 걸립니다.",
            foreground="gray",
        ).grid(row=0, column=4, padx=(8, 0))

        button_frame = ttk.Frame(parent)
        button_frame.grid(row=5, column=0, pady=(6, 0))
        self.batch_start_button = ttk.Button(
            button_frame, text="일괄 다운로드 시작", command=self.start_batch
        )
        self.batch_start_button.pack(side=tk.LEFT, padx=3)
        self.batch_cancel_button = ttk.Button(
            button_frame, text="취소", command=self.cancel_batch, state="disabled"
        )
        self.batch_cancel_button.pack(side=tk.LEFT, padx=3)
        ttk.Button(button_frame, text="폴더 열기", command=self._batch_open_folder).pack(
            side=tk.LEFT, padx=3
        )
        ttk.Button(button_frame, text="라이브 대기 목록 열기", command=self._batch_open_live).pack(
            side=tk.LEFT, padx=3
        )

    def load_batch(self, path: str) -> None:
        """Preload a list for review; never start a download implicitly."""
        if self._batch_active():
            return
        self.batch_path_var.set(path)
        self._batch_load()
        self.notebook.select(self.batch_tab)

    def _batch_open_live(self) -> None:
        if self._batch_active():
            messagebox.showinfo("라이브 대기 목록", "일괄 다운로드가 끝난 뒤 열 수 있습니다.")
            return
        source = self.batch_path_var.get()
        if not source or not live_path_for(source).is_file():
            messagebox.showinfo("라이브 대기 목록", "저장된 라이브 대기 목록이 없습니다.")
            return
        self.load_batch(str(live_path_for(source)))

    def _batch_choose_file(self) -> None:
        path = filedialog.askopenfilename(
            title="일괄 다운로드 목록",
            filetypes=[("텍스트 파일", "*.txt"), ("모든 파일", "*.*")],
        )
        if path:
            self.load_batch(path)

    def _batch_load(self) -> None:
        """(Re)read the list file into the 일괄 tab."""
        if self._batch_active():
            return  # the rows belong to the running batch
        path = self.batch_path_var.get()
        self._batch_plan = None
        self.batch_tree.delete(*self.batch_tree.get_children())
        if not path:
            return
        try:
            plan = read_batch_file(path)
        except OSError as exc:
            self.batch_summary_var.set("목록 파일을 읽지 못했습니다. 파일을 다시 선택하세요.")
            messagebox.showerror("오류", f"목록 파일을 읽지 못했습니다:\n{exc}")
            return
        self._batch_plan = plan
        summary = plan.describe()
        if plan.invalid:
            shown = ", ".join(str(line_no) for line_no, _ in plan.invalid[:5])
            summary += f" (URL 이 아닌 줄: {shown}{' …' if len(plan.invalid) > 5 else ''})"
        self.batch_summary_var.set(summary)
        for index, item in enumerate(plan.items):
            self.batch_tree.insert(
                "", tk.END, iid=str(index),
                values=(index + 1, item.category, item.url, _BATCH_STATE_LABELS["pending"]),
            )

    def _batch_open_folder(self) -> None:
        """The selected row's category folder, else the output folder."""
        folder = Path(self.output_path_var.get())
        selection = self.batch_tree.selection()
        if selection and self._batch_plan is not None:
            item = self._batch_plan.items[int(selection[0])]
            category_folder = folder / safe_folder_name(item.category)
            if category_folder.is_dir():
                folder = category_folder
        if not folder.is_dir():
            messagebox.showwarning("폴더 열기", "저장 폴더를 찾을 수 없습니다.")
            return
        try:
            _open_folder(str(folder))
        except OSError as exc:
            messagebox.showerror("폴더 열기", f"폴더를 열 수 없습니다: {exc}")

    def _batch_set_row(self, index: int, state: str, title: str = "", warning: str = "") -> None:
        """Update one 일괄 row from the worker thread."""
        def _apply() -> None:
            iid = str(index)
            if not self.batch_tree.exists(iid):
                return
            if title:
                self.batch_tree.set(iid, "title", title)
            label = _BATCH_STATE_LABELS.get(state, state)
            self.batch_tree.set(iid, "state", f"{label} · {warning}" if warning else label)
            if state == "running":
                self.batch_tree.see(iid)
        self._on_ui(_apply)

    def _batch_active(self) -> bool:
        """A batch is running or waiting for a slot."""
        job = self._batch_job
        return job is not None and job.active

    def _batch_pause(self) -> tuple[float, float]:
        """The 일괄 tab's rest between two downloads as ``(shortest, longest)``
        seconds. A field that is empty or not a number (half-edited) is its
        default, one outside the range is clamped, and the longest is never
        shorter than the shortest. The fields' own text is read (the IntVar
        takes "1.5" as 1)."""

        def seconds(spin: ttk.Spinbox, default: float) -> int:
            try:
                return max(0, min(int(spin.get().strip()), _MAX_PAUSE_SPIN))
            except ValueError:
                return int(default)

        low = seconds(self.batch_pause_min_spin, DEFAULT_PAUSE[0])
        high = seconds(self.batch_pause_max_spin, DEFAULT_PAUSE[1])
        return float(low), float(max(low, high))

    def start_batch(self) -> None:
        if self._closing or self._batch_active():
            return
        self._batch_load()  # validate current contents before confirming the count
        plan = self._batch_plan
        if plan is None or not plan.items:
            messagebox.showwarning("경고", "받을 URL 이 있는 목록 파일을 먼저 선택하세요.")
            return
        output_dir = self.output_path_var.get()
        if not messagebox.askyesno(
            "일괄 다운로드",
            f"{len(plan.items)}개를 차례로 받아 분야별로 저장합니다.\n"
            f"저장 폴더: {output_dir}\n\n시작할까요?",
        ):
            return
        base_opts = self._collect_options("")
        pause = self._batch_pause()
        remaining_path = remaining_path_for(self.batch_path_var.get())
        live_path = live_path_for(self.batch_path_var.get())
        total = len(plan.items)

        # The whole batch is one row of the 다운로드 목록 and one download
        # towards the concurrent cap; each item's progress shows in that row.
        self._jobs.max_parallel = self._max_parallel()
        try:
            job = self._jobs.add_batch(
                f"일괄: {Path(self.batch_path_var.get()).name} ({total}개)"
            )
        except JobRejected as exc:
            messagebox.showinfo("일괄 다운로드", str(exc))
            return
        self._batch_job = job
        on_progress, on_log = self._download_callbacks(job)

        def on_item(index, item, state, result) -> None:
            self._batch_set_row(
                index, state, result.title if result is not None else "",
                " · ".join(filter(None, (result.warning_summary, result.cookie_summary, result.subtitle_summary)))
                if result is not None else "",
            )
            if state == "running":
                job.start_item(f"\U0001f4da 일괄 {index + 1}/{total}", item.url)
            if result is not None:
                job.end_item()
                self._on_cookie_event(result.cookie_event)
                if result.log_path and (not result.ok or result.warnings or result.subtitles_skipped) and not result.skipped:
                    self._say(job, f"[{index + 1}/{total}] 실행 로그: {result.log_path}\n")
                folder = os.path.join(output_dir, safe_folder_name(item.category))
                self._record_history(item.url, result, folder)

        def worker() -> None:
            final, note = "failed", ""
            try:
                self._say(
                    job, f"일괄 다운로드 시작: {total}개 (영상 사이 휴식: {pause_label(pause)})\n"
                )
                summary = run_batch(
                    plan.items,
                    base_opts,
                    progress_callback=on_progress,
                    log_callback=on_log,
                    item_callback=on_item,
                    cancel_event=job.cancel_event,
                    pause=pause,
                    remaining_path=remaining_path,
                    live_path=live_path,
                )
                note = summary.describe()
                if any(r and r.warnings for r in summary.results):
                    note = "⚠ 라이브 조기 종료 가능 · " + note
                if any(r and r.subtitles_skipped for r in summary.results):
                    note = "⚠ 자막 없이 받은 영상 있음 · " + note
                if any(r and r.cookie_event == "invalid" for r in summary.results):
                    note = "⚠ 쿠키 만료 · " + note
                final = (
                    "cancelled" if job.cancel_event.is_set()
                    else "failed" if summary.counts["failed"] else "done"
                )
                text = f"끝: {note}"
                if summary.remaining_file:
                    text += f" — 남은 목록: {Path(summary.remaining_file).name}"
                if summary.live_file:
                    text += f" — 라이브 대기: {Path(summary.live_file).name}"
                if summary.subtitles_skipped:
                    text += f" — 자막 없이 받은 {summary.subtitles_skipped}개는 같은 목록을 다시 실행하면 자막을 채웁니다"
                    self._say(
                        job,
                        f"⚠ 자막 없이 받은 영상 {summary.subtitles_skipped}개: 같은 목록을 다시 실행하면 "
                        "그 영상은 건너뛰고 자막만 받습니다.\n",
                    )
                self._on_ui(lambda: self.batch_summary_var.set(text))
            except Exception as exc:
                self._say(job, f"✗ 일괄 다운로드 중 오류: {exc}\n")
                note = "오류"
            finally:
                job.finish(final, time.monotonic(), note=note)
                self._on_ui(self._on_jobs_changed)

        def launch() -> None:
            job.thread = threading.Thread(target=worker, daemon=True)
            try:
                job.thread.start()
            except RuntimeError as exc:  # "can't start new thread"
                job.finish("failed", time.monotonic(), note="시작 실패")
                messagebox.showerror("일괄 다운로드", f"일괄 다운로드를 시작하지 못했습니다: {exc}")

        self._launch_or_queue(job, launch, "일괄 다운로드")

    def cancel_batch(self) -> None:
        """Stop the running batch only (전체 취소 stops every download)."""
        job = self._batch_job
        if job is not None and self._jobs.cancel(job.id):
            self._say(job, "일괄 다운로드 취소 중...\n")
            self._on_jobs_changed()

    def _build_history_tab(self, parent: ttk.Frame) -> None:
        """Populate the "기록" tab: searchable list of past downloads."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        search_frame = ttk.Frame(parent)
        search_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        search_frame.columnconfigure(1, weight=1)
        ttk.Label(search_frame, text="검색:").grid(row=0, column=0, sticky="w")
        ttk.Entry(search_frame, textvariable=self.history_search_var).grid(
            row=0, column=1, sticky="ew", padx=5
        )
        self.history_count_label = ttk.Label(search_frame, text="")
        self.history_count_label.grid(row=0, column=2, sticky="e")

        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=1, column=0, sticky="ewns")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        columns = (
            # (id, heading, width, stretch, anchor)
            ("time", "일시", 90, False, "w"),
            ("status", "결과", 45, False, "center"),
            ("title", "제목", 260, True, "w"),
            ("audio", "오디오", 60, False, "center"),
            ("url", "URL", 220, True, "w"),
        )
        self.history_tree = ttk.Treeview(
            tree_frame,
            columns=[c[0] for c in columns],
            show="headings",
            selectmode="extended",
        )
        for col, heading, width, stretch, anchor in columns:
            self.history_tree.heading(col, text=heading)
            self.history_tree.column(col, width=width, stretch=stretch, anchor=anchor)
        self.history_tree.grid(row=0, column=0, sticky="ewns")
        scrollbar = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.history_tree.yview
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.history_tree.configure(yscrollcommand=scrollbar.set)
        self.history_tree.bind("<Double-1>", lambda _event: self._history_use_url())
        self.history_search_var.trace_add(
            "write", lambda *_args: self._refresh_history_view(reload=False)
        )

        button_frame = ttk.Frame(parent)
        button_frame.grid(row=2, column=0, pady=(6, 0))
        for text, command in (
            ("URL 입력란에 넣기", self._history_use_url),
            ("브라우저로 열기", self._history_open_browser),
            ("폴더 열기", self._history_open_folder),
            ("로그 열기", self._history_open_log),
            ("선택 삭제", self._history_delete_selected),
            ("전체 삭제", self._history_clear),
        ):
            ttk.Button(button_frame, text=text, command=command).pack(
                side=tk.LEFT, padx=3
            )

    def _refresh_history_view(self, reload: bool = True) -> None:
        """Redraw the 기록 tab, re-reading the history file if *reload*."""
        if reload:
            self._history_entries = self.history.entries()
        entries = self._history_entries
        query = self.history_search_var.get().strip().lower()
        if query:
            entries = [
                e for e in entries if query in e.title.lower() or query in e.url.lower()
            ]

        tree = self.history_tree
        tree.delete(*tree.get_children())
        self._history_by_id = {}
        for entry in entries:
            if entry.id in self._history_by_id:
                continue  # duplicate id in a hand-edited file
            self._history_by_id[entry.id] = entry
            tree.insert(
                "",
                tk.END,
                iid=entry.id,
                values=(
                    entry.time[5:16],  # "MM-DD HH:MM"
                    _HISTORY_STATUS_LABELS.get(entry.status, entry.status),
                    entry.title or "(제목 없음)",
                    entry.audio_language or "-",
                    entry.url,
                ),
            )
        self.history_count_label.configure(text=f"{len(self._history_by_id)}건")

    def _on_tab_changed(self, _event: object = None) -> None:
        if self.notebook.select() == str(self.history_tab):
            self._refresh_history_view()

    def _selected_history(self) -> list[HistoryEntry]:
        """Selected 기록 rows; shows a hint when nothing is selected."""
        selected = [
            self._history_by_id[iid]
            for iid in self.history_tree.selection()
            if iid in self._history_by_id
        ]
        if not selected:
            messagebox.showinfo("기록", "목록에서 항목을 선택하세요.")
        return selected

    def _history_use_url(self) -> None:
        selected = self._selected_history()
        if not selected:
            return
        self.url_var.set(selected[0].url)
        self.notebook.select(self.basic_tab)
        self.add_status(f"기록에서 URL 선택: {selected[0].url}\n")

    def _history_open_browser(self) -> None:
        selected = self._selected_history()
        if selected:
            webbrowser.open(selected[0].url)

    def _history_open_folder(self) -> None:
        selected = self._selected_history()
        if not selected:
            return
        entry = selected[0]
        # The folder of a file that still exists, else the saved-to folder.
        folder = next(
            (str(Path(p).parent) for p in entry.files if Path(p).exists()),
            entry.output_dir,
        )
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("폴더 열기", "저장 폴더를 찾을 수 없습니다.")
            return
        try:
            _open_folder(folder)
        except OSError as exc:
            messagebox.showerror("폴더 열기", f"폴더를 열 수 없습니다: {exc}")

    def _history_open_log(self) -> None:
        """Open the selected download's run log (core.run_log)."""
        selected = self._selected_history()
        if not selected:
            return
        log_file = selected[0].log_file
        if not log_file or not Path(log_file).is_file():
            messagebox.showinfo(
                "로그 열기",
                "이 항목의 실행 로그가 없습니다.\n"
                "(로그 저장 기능 이전의 기록이거나, 오래되어 삭제된 로그입니다)",
            )
            return
        try:
            _open_folder(log_file)  # opens a file with its default app too
        except OSError as exc:
            messagebox.showerror("로그 열기", f"로그를 열 수 없습니다: {exc}")

    def _history_delete_selected(self) -> None:
        selected = self._selected_history()
        if not selected:
            return
        try:
            self.history.remove(entry.id for entry in selected)
        except OSError as exc:
            messagebox.showerror("기록", f"기록을 저장하지 못했습니다: {exc}")
        self._refresh_history_view()

    def _history_clear(self) -> None:
        if not messagebox.askyesno(
            "기록 전체 삭제",
            "다운로드 기록을 모두 지울까요?\n(받은 파일은 지워지지 않습니다)",
        ):
            return
        try:
            self.history.clear()
        except OSError as exc:
            messagebox.showerror("기록", f"기록을 저장하지 못했습니다: {exc}")
        self._refresh_history_view()

    def _fill_url_dropdown(self) -> None:
        """Load recent downloads into the URL combobox as it opens."""
        recent = self.history.recent(20)
        self._url_choices = [entry.url for entry in recent]
        labels = []
        for entry in recent:
            title = entry.title if len(entry.title) <= 40 else entry.title[:39] + "…"
            labels.append(f"{title} | {entry.url}" if title else entry.url)
        self.url_entry.configure(values=labels)

    def _on_url_dropdown_selected(self, _event: object = None) -> None:
        """Replace the picked "title | url" label with the bare URL."""
        index = self.url_entry.current()
        if 0 <= index < len(self._url_choices):
            self.url_var.set(self._url_choices[index])

    def _record_history(self, url: str, result: DownloadResult, output_dir: str) -> None:
        """Save one history entry (worker thread) and refresh the 기록 tab."""
        try:
            self.history.add(HistoryEntry.from_result(url, result, output_dir))
        except Exception as exc:
            self.add_status(f"⚠️ 다운로드 기록 저장 실패: {exc}\n")
            return
        try:
            self.root.after(0, self._refresh_history_view)
        except Exception:
            pass  # window already closed

    # ---------------------------------------------------- auth toggles

    def _on_auth_method_change(self) -> None:
        """Show/hide auth sub-widgets based on the selected method."""
        method = self.auth_method_var.get()

        # Cookie-file section: visible for cookie-file and cookie-browser
        # (the browser path also writes to cookies.txt).
        show_cookie_block = method in {"cookie-file", "cookie-browser"}
        if show_cookie_block:
            self.cookie_frame.grid()
        else:
            self.cookie_frame.grid_remove()

        # Browser radios only visible for cookie-browser.
        if method == "cookie-browser":
            self.browser_frame.grid()
        else:
            self.browser_frame.grid_remove()

        # bgutil URL entry + test button only for PO Token.
        if method == "po-token-bgutil":
            self.bgutil_frame.grid()
        else:
            self.bgutil_frame.grid_remove()

    def test_bgutil_connection(self) -> None:
        """Run BgutilPoTokenAuth.validate() on a background thread."""
        def worker() -> None:
            try:
                url = self.bgutil_url_var.get().strip() or "http://127.0.0.1:4416"
                self.add_status(f"bgutil provider 연결 테스트 시작: {url}\n")
                auth = BgutilPoTokenAuth(provider_url=url)
                result = auth.validate()
                if result.success:
                    self.add_status(f"\u2713 {result.message}\n")
                else:
                    self.add_status(
                        f"\u2717 bgutil 검증 실패 ({result.error_kind})\n"
                    )
                    self.add_status(result.message + "\n")
            except Exception as exc:
                self.add_status(f"\u2717 bgutil 테스트 중 오류: {exc}\n")

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    # -------------------------------------------------------------- status

    def add_status(self, message: str) -> None:
        """Append a timestamped status line. Thread-safe via ``after``."""
        def _append() -> None:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.status_text.insert(tk.END, f"[{timestamp}] {message}")
            self.status_text.see(tk.END)

        # Route through the Tk event loop so background threads are safe.
        try:
            self.root.after(0, _append)
        except RuntimeError:
            # Root destroyed; swallow.
            pass

    def clear_status(self) -> None:
        self.status_text.delete(1.0, tk.END)

    # ------------------------------------------------------- URL helpers

    def open_url(self) -> None:
        url = self.url_var.get().strip()
        if url:
            self.add_status(f"브라우저에서 URL 열기: {url}\n")
            webbrowser.open(url)
        else:
            messagebox.showwarning("경고", "URL을 입력해주세요.")

    def paste_url(self) -> None:
        try:
            url = self.root.clipboard_get()
            self.url_var.set(url.strip())
            self.add_status(f"URL 붙여넣기 완료: {url.strip()}\n")
        except Exception:
            messagebox.showwarning("경고", "클립보드에서 URL을 가져올 수 없습니다.")

    def browse_cookie_file(self) -> None:
        filename = filedialog.askopenfilename(
            title="쿠키 파일 선택",
            filetypes=[("텍스트 파일", "*.txt"), ("모든 파일", "*.*")],
        )
        if filename:
            self.cookie_file_var.set(filename)
            self.add_status(f"쿠키 파일 선택: {filename}\n")

    def browse_output_path(self) -> None:
        directory = filedialog.askdirectory(title="저장 경로 선택")
        if directory:
            self.output_path_var.set(directory)
            self.add_status(f"저장 경로 선택: {directory}\n")

    # ---------------------------------------------------- cookie update

    def update_cookies(self) -> None:
        """Trigger cookie extraction on a background thread."""
        def worker() -> None:
            try:
                self.add_status("쿠키 업데이트 시작...\n")
                browser = self.browser_var.get()
                cookie_file = self.cookie_file_var.get() or "cookies.txt"
                result: CookieUpdateResult = update_cookies(browser, cookie_file)
                if result.success:
                    self.add_status(f"\u2713 {result.message}\n")
                else:
                    self.add_status(f"\u2717 쿠키 업데이트 실패 ({result.error_kind})\n")
                    self.add_status(result.message + "\n")
            except Exception as exc:
                self.add_status(f"\u2717 쿠키 업데이트 중 오류: {exc}\n")

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    # -------------------------------------------- Playwright cookie refresh

    def auto_refresh_cookies(self) -> None:
        """Open a bundled Chromium window to refresh cookies via Playwright.

        Uses the :mod:`core.refresher` wrapper so the same logic backs
        scripts/refresh_cookies.py and this button. Always runs headful
        (``auto=False``) so first-time users can log in manually; the
        persistent profile at ``~/.ytdlp-cookie-refresher/`` keeps the
        session alive between runs.
        """
        # Import lazily: avoids paying Playwright's import cost on GUI startup.
        from core.refresher import playwright_available, refresh_cookies

        if not playwright_available():
            messagebox.showerror(
                "쿠키 자동 갱신 불가",
                "Playwright 모듈이 포함되어 있지 않습니다. "
                "빌드 시 -NoPlaywright 옵션이 사용되었을 가능성이 있습니다.",
            )
            return

        def worker() -> None:
            try:
                self.add_status("쿠키 자동 갱신 시작 (Chromium 창이 열립니다)...\n")

                cookie_file = self.cookie_file_var.get().strip()
                if cookie_file:
                    output = Path(cookie_file).expanduser()
                else:
                    output = Path.home() / "cookies.txt"

                profile_dir = (
                    Path.home() / ".ytdlp-cookie-refresher" / "chromium-profile"
                )

                def _log(msg: str) -> None:
                    self.add_status(msg + "\n")

                result = refresh_cookies(
                    output=output,
                    profile_dir=profile_dir,
                    browser="chromium",
                    auto=False,
                    log_callback=_log,
                )

                if result.success:
                    self.add_status(
                        f"\u2713 쿠키 자동 갱신 완료: {result.output_path} "
                        f"({result.cookies_written} cookies)\n"
                    )
                    # Point the cookie-file entry at the freshly written file.
                    self.cookie_file_var.set(str(result.output_path))
                else:
                    self.add_status(
                        f"\u2717 쿠키 자동 갱신 실패 (exit {result.exit_code}): "
                        f"{result.message}\n"
                    )
            except Exception as exc:
                self.add_status(f"\u2717 쿠키 자동 갱신 중 오류: {exc}\n")

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    # ---------------------------------------------------- download flow

    def _collect_options(self, url: str) -> DownloadOptions:
        quality_choice = self.quality_var.get()
        format_opt = presets.format_for(quality_choice)
        audio_opts = presets.audio_opts_for(quality_choice)
        subtitle_opts = presets.subtitle_opts_for(self.subtitle_var.get())
        full_path = presets.output_template_for(
            self.output_path_var.get(), self.filename_var.get()
        )

        auth: Authenticator = self._build_authenticator()

        # Phase F: assemble live-stream options from the advanced tab.
        # Guard against stale Spinbox values (empty strings when the
        # user deletes the field while editing).
        try:
            concurrent = int(self.live_concurrent_var.get())
        except (TypeError, tk.TclError, ValueError):
            concurrent = LiveOptions().concurrent_fragments
        try:
            wait_minutes = int(self.live_wait_minutes_var.get())
        except (TypeError, tk.TclError, ValueError):
            wait_minutes = 60
        live_opts = LiveOptions(
            enabled=bool(self.live_enabled_var.get()),
            concurrent_fragments=max(1, min(concurrent, 64)),
            wait_minutes=max(0, min(wait_minutes, 240)),
            remux_to_mp4=bool(self.live_remux_var.get()),
            force=bool(self.live_force_var.get()),
            stall_minutes=self._stall_minutes(),
            fragment_retries=self._fragment_retries(),
        )

        return DownloadOptions(
            url=url,
            format_opt=format_opt,
            output_template=full_path,
            auth=auth,
            subtitle_opts=subtitle_opts,
            audio_opts=audio_opts,
            live_opts=live_opts,
            audio_lang=presets.audio_lang_for(self.audio_var.get()),
            cookie_health=self.cookie_health,
        )

    def _sync_retries_widgets(self) -> None:
        """"무한" turns the count off, and says what that risks."""
        infinite = bool(self.live_retries_infinite_var.get())
        self.live_retries_spin.configure(state="disabled" if infinite else "normal")
        stall = self._stall_minutes()
        if not infinite:
            note = ""
        elif stall > 0:
            note = (
                "무한: 방송이 끝난 뒤에도 없는 조각을 계속 재시도합니다.\n"
                f"진척 없음 자동 정지({stall}분)가 멈춰 줍니다."
            )
        else:
            note = (
                "무한 + 자동 정지 꺼짐: 방송이 끝난 뒤에도 멈추지 않을 수 있습니다\n"
                "(2026-04-26 사고). 재시도 횟수를 정하거나 자동 정지(분)를 켜세요."
            )
        self.live_retries_note_var.set(note)

    def _stall_minutes(self) -> int:
        """The "진척 없음 자동 정지(분)" field: 0 means off. A field that is
        empty or not a number (half-edited) is the default; one outside the
        range is clamped. The field's own text is read (the IntVar takes "1.5"
        as 1)."""
        try:
            return max(0, min(int(self.live_stall_spin.get().strip()), _MAX_STALL_SPIN))
        except ValueError:
            return DEFAULT_STALL_MINUTES

    def _fragment_retries(self) -> int | None:
        """The retry count from the advanced tab; ``None`` for "무한". A count
        that is empty or not a number (a half-edited field) is the default, one
        outside the field's range is clamped, and 0 stays 0, never infinite."""
        if self.live_retries_infinite_var.get():
            return None
        # The field's own text, not the IntVar: that one reads "1.5" as 1.
        try:
            return max(0, min(int(self.live_retries_spin.get().strip()), _MAX_RETRIES_SPIN))
        except ValueError:
            return DEFAULT_FRAGMENT_RETRIES

    def _build_authenticator(self) -> Authenticator:
        """Construct an :class:`Authenticator` from the current UI state."""
        method = self.auth_method_var.get()

        if method == "none":
            return NoAuth()

        if method == "po-token-bgutil":
            url = self.bgutil_url_var.get().strip() or "http://127.0.0.1:4416"
            return BgutilPoTokenAuth(provider_url=url)

        if method == "cookie-file":
            cookie_file = self.cookie_file_var.get() or "cookies.txt"
            return CookieFileAuth(path=cookie_file)

        # Default / "cookie-browser": preserve pre-refactor behaviour —
        # use cookies.txt if it exists on disk, otherwise fall back to
        # live browser extraction.
        cookie_file = self.cookie_file_var.get()
        if cookie_file and Path(cookie_file).exists():
            return CookieFileAuth(path=cookie_file)
        browser = self.browser_var.get() or "chrome"
        return BrowserCookieAuth(browser=browser)

    def _max_parallel(self) -> int:
        """The 고급 tab's concurrent-download cap; stale spinbox text
        (empty while editing) falls back to the default."""
        try:
            value = int(self.max_parallel_var.get())
        except (TypeError, tk.TclError, ValueError):
            value = DEFAULT_MAX_PARALLEL
        return max(1, min(value, MAX_PARALLEL_LIMIT))

    def _say(self, job: DownloadJob, message: str) -> None:
        """A status line, tagged ``[#N]`` while several downloads run."""
        self.add_status(self._jobs.tag_for(job) + message)

    def _download_callbacks(
        self, job: DownloadJob
    ) -> tuple[Callable[[dict], None], Callable[[str, str], None]]:
        """(progress, log) callbacks for :func:`run_download` on behalf of
        *job*: lines on the 정보 tab and progress for its row in the
        다운로드 목록. A batch uses one pair for all of its items."""
        # Per-stream throttle state: yt-dlp may download video and audio
        # concurrently (live), each with its own percentage.
        # * Known percentage: one status line per 10% bucket crossing
        #   (0=0–9%, 1=10–19%, …, 10=100%).
        # * Unknown (live): one line every 30 s showing bytes received.
        last_bucket: dict[int, int] = {}
        last_emit: dict[int, float] = {}

        def on_progress(info: dict) -> None:
            # Every line feeds the 다운로드 목록; only the log is throttled.
            job.record_progress(info, time.monotonic())
            stream = info.get("stream", 0)
            label = f"[스트림 {stream + 1}] " if stream else ""
            if info.get("percent_known", True):
                bucket = int(info["percentage"]) // 10
                if last_bucket.get(stream) == bucket:
                    return
                last_bucket[stream] = bucket
                self._say(
                    job,
                    f"진행: {label}{info['bar']} {info['total_size']} "
                    f"{info['speed']} ETA {info['eta']}\n",
                )
                return
            now = time.monotonic()
            if now - last_emit.get(stream, float("-inf")) < 30:
                return
            last_emit[stream] = now
            self._say(
                job,
                f"진행: {label}{info['downloaded']} 받음, {info['speed']} "
                "(라이브 - 전체 크기 미상)\n",
            )

        def on_log(line: str, level: str) -> None:
            # Hook into the live-detection info lines emitted by
            # core.downloader to show what kind of URL this job is. (A
            # batch's row shows its own position instead.)
            if job.kind == DOWNLOAD:
                if "LIVE 감지" in line:
                    job.badge = "\U0001f534 LIVE 캐치업"
                elif "예정 라이브" in line:
                    job.badge = "⏰ 예정 대기"
                elif "아카이브 라이브" in line:
                    job.badge = "\U0001f4fc 아카이브"
                elif "종료된 라이브" in line:
                    job.badge = "⏳ 종료된 라이브"
                elif "일반 영상" in line:
                    job.badge = "\U0001f4fa 일반"
            prefix = {"error": "✗", "warning": "⚠️"}.get(level, "")
            self._say(job, f"{prefix} {line}\n" if prefix else f"{line}\n")

        return on_progress, on_log

    def start_download(self) -> None:
        if self._closing:
            return  # 종료 confirmed: nothing new may start
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("경고", "URL을 입력해주세요.")
            return

        opts = self._collect_options(url)
        output_dir = self.output_path_var.get()
        self._jobs.max_parallel = self._max_parallel()
        try:
            job = self._jobs.add(url)
        except JobRejected as exc:
            messagebox.showinfo("다운로드", str(exc))
            return

        on_progress, on_log = self._download_callbacks(job)

        def say(message: str) -> None:
            self._say(job, message)

        def worker() -> None:
            result = DownloadResult(ok=False)
            note = ""
            try:
                say("다운로드 시작...\n")
                result = run_download(
                    url,
                    opts,
                    progress_callback=on_progress,
                    log_callback=on_log,
                    cancel_event=job.cancel_event,
                )
                if result.cancelled:
                    merged = [f for f in result.files if f.get("partial")]
                    note = "부분 저장" if merged else ""
                    say(
                        "✗ 사용자에 의해 다운로드 취소됨. "
                        + (
                            f"받은 부분은 '{Path(merged[0]['filepath']).name}' 로 저장했습니다.\n"
                            if merged
                            else "받던 파일(.part/.ts)은 저장 폴더에 남아 있습니다.\n"
                        )
                    )
                elif result.skipped == "busy":
                    note = "이미 받는 중"
                    say("✗ 같은 영상(또는 같은 파일명)을 이미 받는 중이라 시작하지 않았습니다.\n")
                else:
                    say("✓ 다운로드 완료!\n" if result.ok else "✗ 다운로드 실패\n")
                if result.log_path:
                    say(f"실행 로그: {result.log_path}\n")
            except Exception as exc:
                say(f"✗ 다운로드 중 오류: {exc}\n")
                log_path = getattr(exc, "log_path", "")  # set by run_download
                if log_path:
                    result.log_path = log_path
                    say(f"실행 로그: {log_path}\n")
            finally:
                self._record_history(url, result, output_dir)
                note = " · ".join(
                    filter(None, (note, result.warning_summary, result.cookie_summary, result.subtitle_summary))
                )
                state = (
                    "cancelled" if result.cancelled else ("done" if result.ok else "failed")
                )
                job.finish(state, time.monotonic(), title=result.title, note=note)
                self._on_ui(self._on_jobs_changed)
                self._on_cookie_event(result.cookie_event)

        def launch() -> None:
            job.thread = threading.Thread(target=worker, daemon=True)
            try:
                job.thread.start()
            except RuntimeError as exc:  # "can't start new thread"
                # Without this the job would hold a slot and its URL forever.
                job.finish("failed", time.monotonic(), note="시작 실패")
                messagebox.showerror("다운로드", f"다운로드를 시작하지 못했습니다: {exc}")

        self._launch_or_queue(job, launch, "다운로드")

    def _on_cookie_event(self, event: str) -> None:
        """React to ``DownloadResult.cookie_event`` from a worker thread. The
        event is set once per expiry, so the dialog does not repeat; "recovered"
        needs none (the row says so and the 정보 tab has the log line)."""
        if event == "invalid":
            self._on_ui(self._alert_cookie_expired)

    def _alert_cookie_expired(self) -> None:
        """Tell the user the login cookies stopped working (Tk thread)."""
        if self._closing or self._cookie_alert_open:
            return
        self._cookie_alert_open = True
        try:
            messagebox.showwarning("쿠키 만료", _COOKIE_EXPIRED_MESSAGE)
        finally:
            self._cookie_alert_open = False

    # ------------------------------------------------- 다운로드 목록 / jobs

    def _on_ui(self, callback: Callable[[], None]) -> None:
        """Run *callback* on the Tk thread; safe from workers and after
        the window is gone."""
        try:
            self.root.after(0, callback)
        except (RuntimeError, tk.TclError):
            pass

    def _launch_or_queue(self, job: DownloadJob, launch: Callable[[], None], what: str) -> None:
        """Start *job*'s worker now, or - when it got no slot - keep *launch*
        for :meth:`_start_queued` and say so on the 정보 tab."""
        if job.queued:
            job.launch = launch
            self.add_status(
                f"{job.tag} {what}을(를) 대기열에 넣었습니다 "
                f"(동시 {self._jobs.max_parallel}개 사용 중). 자리가 나면 자동으로 시작합니다.\n"
            )
        else:
            launch()
        self._on_jobs_changed()

    def _start_queued(self) -> None:
        """Give free slots to waiting downloads (Tk thread). Nothing starts
        while the window is closing."""
        if self._closing:
            return
        while True:
            promoted = self._jobs.promote()
            if not promoted:
                return
            for job in promoted:
                launch, job.launch = job.launch, None
                if launch is None:  # cannot happen; do not hold the slot
                    job.finish("failed", time.monotonic(), note="시작 실패")
                    continue
                self.add_status(f"{job.tag} 대기 끝 — 다운로드를 시작합니다.\n")
                launch()

    def _on_jobs_changed(self) -> None:
        """Redraw the 다운로드 목록 and the buttons (Tk thread). While any
        download runs, a once-a-second timer keeps elapsed time and the
        "마지막 수신" column moving."""
        try:
            self._jobs.max_parallel = self._max_parallel()
            self._start_queued()
            self._refresh_jobs_view()
            self._update_job_controls()
            if self._tick_id is None and self._jobs.running_count() > 0:
                self._tick_id = self.root.after(1000, self._tick_jobs)
        except tk.TclError:
            pass  # window destroyed

    def _tick_jobs(self) -> None:
        self._tick_id = None
        self._on_jobs_changed()

    def _fit_jobs_columns(self, event: tk.Event) -> None:
        """Share the table's width out among its columns. ttk does not shrink the
        stretching columns when the window is narrower than their total, which
        pushed the right-hand columns out of view."""
        if event.width < 200:  # not laid out yet
            return
        specs = [(w, _JOB_MIN_STRETCH if s else w, s) for _h, w, s, _a in _JOB_COLUMNS]
        for col, width in zip(ROW_COLUMNS, fit_widths(event.width - 4, specs)):
            if int(self.jobs_tree.column(col, "width")) != width:
                self.jobs_tree.column(col, width=width)

    def _refresh_jobs_view(self) -> None:
        tree = self.jobs_tree
        now = time.monotonic()
        jobs = self._jobs.jobs
        present = set(tree.get_children())
        positions = self._jobs.queue_positions()
        for iid in present - {str(job.id) for job in jobs}:
            tree.delete(iid)
        for job in jobs:
            iid = str(job.id)
            row = job_row(job, now, positions.get(job.id, 0))
            tags = ("stale",) if row.stale else ()
            if iid in present:
                tree.item(iid, values=row.values, tags=tags)
            else:
                tree.insert("", tk.END, iid=iid, values=row.values, tags=tags)
                tree.see(iid)

    def _update_job_controls(self) -> None:
        self._jobs.max_parallel = self._max_parallel()
        # Past the cap a start waits in the queue instead of being refused.
        self.start_button.configure(state="normal")
        batch_active = self._batch_active()
        self.batch_start_button.configure(state="disabled" if batch_active else "normal")
        self.batch_cancel_button.configure(state="normal" if batch_active else "disabled")
        cancel_state = "normal" if self._jobs.active_count() else "disabled"
        for button in (
            self.jobs_cancel_selected_button,
            self.jobs_cancel_all_button,
        ):
            button.configure(state=cancel_state)

    def cancel_selected_jobs(self) -> None:
        """Cancel the 다운로드 목록 rows that are selected."""
        selected = [int(iid) for iid in self.jobs_tree.selection()]
        if not selected:
            messagebox.showinfo("다운로드", "취소할 항목을 목록에서 선택하세요.")
            return
        for job_id in selected:
            if self._jobs.cancel(job_id):
                self.add_status(f"[#{job_id}] 다운로드 취소 중...\n")
        self._on_jobs_changed()

    def cancel_download(self) -> None:
        """Cancel every running download (전체 취소).

        Sets each job's event passed to :func:`core.downloader.run_download`,
        which kills that yt-dlp process tree within about half a second.
        The worker thread then reports "취소됨" and merges what it got.
        """
        count = self._jobs.cancel_all()
        if count:
            self.add_status(
                "다운로드 취소 중...\n" if count == 1 else f"다운로드 {count}개 취소 중...\n"
            )
            self._on_jobs_changed()

    def clear_finished_jobs(self) -> None:
        self._jobs.clear_finished()
        self._on_jobs_changed()

    def _on_close(self) -> None:
        """Window close / 종료: never leave yt-dlp running orphaned.

        A stopped live recording is merged from its .part files inside
        the download worker, so wait for the worker before closing
        rather than cutting the merge off.
        """
        if self._closing:
            return  # already waiting for the downloads to stop
        if self._jobs.any_alive():
            running = self._jobs.running_count()
            if not messagebox.askyesno(
                "종료 확인",
                (
                    "다운로드가 진행 중입니다."
                    if running <= 1
                    else f"다운로드 {running}개가 진행 중입니다."
                )
                + " 중단하고 종료할까요?\n"
                "(라이브 녹화는 지금까지 받은 부분을 합쳐 저장한 뒤 종료합니다)",
            ):
                return
            self._closing = True
            self._jobs.cancel_all()
            self.root.title("(정리 중…) " + self.root.title())
            self.notebook.select(self.info_tab)
            self.add_status("다운로드를 멈추고 정리하는 중입니다. 끝나면 창이 닫힙니다.\n")
            # The kill takes about a second; merging a long recording can
            # take minutes. Each merge gives up after MERGE_TIMEOUT, and a
            # run merges at most two (first try + cookie retry), so this cap
            # only matters if something else hangs: closing mid-merge would
            # orphan ffmpeg and leave its temp copies behind.
            deadline = time.monotonic() + 2 * MERGE_TIMEOUT + 60

            def _wait_then_destroy() -> None:
                if self._jobs.any_alive() and time.monotonic() < deadline:
                    self.root.after(200, _wait_then_destroy)
                else:
                    self.root.destroy()

            _wait_then_destroy()
            return
        self.root.destroy()


def run_gui(*, batch_path: str | None = None) -> int:
    """Launch the GUI event loop. Returns a shell exit code."""
    print("=" * 60)
    print("       yt-dlp 다운로더 (GUI 모드)")
    print("=" * 60)
    print("\nGUI 창을 여는 중...\n")

    root = tk.Tk()
    app = DownloaderGUI(root)
    if batch_path:
        app.load_batch(batch_path)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(run_gui())
