# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec file: yt-dlp-downloader (unified GUI + CLI build)
#
# Replaces the previous yt-dlp-gui.spec / yt-dlp-cli.spec pair. Using
# PyInstaller's MERGE() lets both entry points share a single _internal/
# directory, eliminating ~1.4GB of duplication and roughly halving build
# time. The output layout matches what build.ps1 used to assemble manually:
#
#   dist/yt-dlp-downloader/
#       yt-dlp-gui.exe
#       yt-dlp-cli.exe
#       _internal/...
#
# Build with:  pyinstaller build/yt-dlp.spec
# Or let build/build.ps1 do it.

from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules
from PyInstaller.utils.hooks import collect_all as _pyi_collect_all

# ---------------------------------------------------------------------------
# Shared collection (runs once, reused by both Analyses)
# ---------------------------------------------------------------------------

# yt_dlp has dynamic extractor loading - needs full collection
yt_dlp_datas, yt_dlp_binaries, yt_dlp_hiddenimports = collect_all('yt_dlp')
yt_dlp_extractor_imports = collect_submodules('yt_dlp.extractor')

# Playwright Python package: its driver/ folder contains node.exe + JS
# driver scripts that talk to the bundled browser. If playwright isn't
# installed (e.g. -NoPlaywright passed to build.ps1), collect_all returns
# empty tuples and downstream concatenation still works.
try:
    pw_datas, pw_binaries, pw_hidden = _pyi_collect_all('playwright')
except Exception as _pw_exc:
    print(f'WARNING: collect_all(playwright) failed: {_pw_exc}')
    pw_datas, pw_binaries, pw_hidden = [], [], []

# bgutil-ytdlp-pot-provider plugin lives under the *separate* yt_dlp_plugins
# namespace package - collect_all('yt_dlp') does NOT pick it up. Without
# this, the wrapper's BgutilPoTokenAuth.validate() probe (importlib.find_spec
# on yt_dlp_plugins.extractor.getpot_bgutil*) returns "bgutil_plugin_missing"
# even when the build venv had the package installed.
# (Standalone yt-dlp.exe needs a separate copy step - see build.ps1.)
try:
    bg_datas, bg_binaries, bg_hidden = _pyi_collect_all('yt_dlp_plugins')
except Exception as _bg_exc:
    print(f'WARNING: collect_all(yt_dlp_plugins) failed: {_bg_exc}')
    bg_datas, bg_binaries, bg_hidden = [], [], []

# Display version for the GUI title (core/version.py), written by
# build.ps1. Missing when running pyinstaller by hand -> "0.0.0".
_app_version = []
_app_version_file = Path('_app_version.txt').resolve()
if _app_version_file.is_file():
    _app_version.append((str(_app_version_file), '.'))

# Standalone yt-dlp.exe: PyInstaller's frozen exe has sys.executable ==
# the app itself, so the "-m yt_dlp" fallback does not work. Shipping
# yt-dlp.exe next to the app lets core.cookies.yt_dlp_command() spawn the
# real binary at runtime. Use the official standalone exe (downloaded by
# build.ps1 into .yt-dlp-cache/), NOT the pip setuptools launcher from
# .buildvenv/Scripts which depends on the venv's python.exe.
_bundled_yt_dlp = []
_ytdlp_standalone = (Path('.yt-dlp-cache') / 'yt-dlp.exe').resolve()
if _ytdlp_standalone.is_file():
    _bundled_yt_dlp.append((str(_ytdlp_standalone), '.'))
else:
    print('WARNING: standalone yt-dlp.exe not found in .yt-dlp-cache; '
          'the packaged app will fail to spawn yt-dlp at runtime. '
          'Run build.ps1 without -SkipDownload to fetch it.')

# deno runtime: yt-dlp's --remote-components ejs:github uses deno to run
# JS challenges. Without deno on PATH, downloads silently fail.
_bundled_deno = []
_deno_path = (Path('.deno-cache') / 'deno.exe').resolve()
if _deno_path.is_file():
    _bundled_deno.append((str(_deno_path), '.'))
else:
    print('INFO: deno.exe not found in .deno-cache; skipping deno bundling. '
          'Target PC will need deno on PATH.')

# ffmpeg + ffprobe: required by Phase F live-stream support to remux
# .ts -> .mp4 after a broadcast ends. Without these, --remux-video mp4
# silently no-ops and the user is left with a .ts container.
_bundled_ffmpeg = []
for _exe in ('ffmpeg.exe', 'ffprobe.exe'):
    _path = (Path('.ffmpeg-cache') / _exe).resolve()
    if _path.is_file():
        _bundled_ffmpeg.append((str(_path), '.'))
    else:
        print(f'INFO: {_exe} not found in .ffmpeg-cache; live TS->MP4 remux disabled.')

# Playwright Chromium: build.ps1 runs ``playwright install chromium`` with
# PLAYWRIGHT_BROWSERS_PATH=build/.playwright-cache, populating one or more
# ``chromium*`` subfolders. Mirror the whole tree into
# ``<_MEIPASS>/playwright-browsers/`` so core.refresher._configure_playwright_env()
# can point PLAYWRIGHT_BROWSERS_PATH at it at runtime.
_bundled_browsers: list[tuple[str, str]] = []
_pw_cache = Path('.playwright-cache').resolve()
if _pw_cache.is_dir():
    for child in _pw_cache.iterdir():
        if child.is_dir() and child.name.startswith('chromium'):
            for file_path in child.rglob('*'):
                if file_path.is_file():
                    rel = file_path.relative_to(_pw_cache)
                    dest_dir = f"playwright-browsers/{rel.parent.as_posix()}"
                    _bundled_browsers.append((str(file_path), dest_dir))
else:
    print('WARNING: .playwright-cache not found; cookie auto-refresh will require '
          'Playwright installed on target.')

# ---------------------------------------------------------------------------
# Hidden imports (shared between GUI and CLI builds)
# ---------------------------------------------------------------------------
hidden_imports = [
    # Standard library modules that PyInstaller may miss
    'tkinter',
    'tkinter.ttk',
    'tkinter.filedialog',
    'tkinter.messagebox',
    'tkinter.scrolledtext',
    # Our own packages
    'core',
    'core.cookies',
    'core.downloader',
    'core.auth',
    'core.transcriber',
    'core.refresher',
    'core.live',
    'core.presets',
    'core.version',
    'interfaces',
    'interfaces.gui',
    'interfaces.cli',
    'interfaces.bot',
    # Playwright (cookie auto-refresh). Safety net on top of collect_all().
    'playwright',
    'playwright.async_api',
    'playwright._impl',
    # bgutil PO Token plugin (separate yt_dlp_plugins namespace package).
    # Safety net on top of collect_all('yt_dlp_plugins') above.
    'yt_dlp_plugins',
    'yt_dlp_plugins.extractor',
    'yt_dlp_plugins.extractor.getpot_bgutil',
    'yt_dlp_plugins.extractor.getpot_bgutil_http',
    'yt_dlp_plugins.extractor.getpot_bgutil_script',
    # yt-dlp dynamic imports
    'yt_dlp',
    'yt_dlp.extractor',
    'yt_dlp.postprocessor',
    'yt_dlp.networking',
    # Common dependencies yt-dlp may pull in
    'certifi',
    'urllib3',
    'http.cookiejar',
    'json',
    'html.parser',
    'xml.etree.ElementTree',
    'websockets',
    'mutagen',
]
hidden_imports += yt_dlp_hiddenimports
hidden_imports += yt_dlp_extractor_imports
hidden_imports += pw_hidden
hidden_imports += bg_hidden

# Common Analysis kwargs to keep GUI/CLI configurations in lockstep.
_analysis_common = dict(
    pathex=['..'],  # repo root: makes core/, interfaces/ importable
    binaries=yt_dlp_binaries + _bundled_yt_dlp + _bundled_deno + _bundled_ffmpeg + pw_binaries + bg_binaries,
    datas=yt_dlp_datas + pw_datas + _bundled_browsers + bg_datas + _app_version,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'numpy', 'pandas', 'scipy',
        'PIL', 'cv2', 'pytest', 'setuptools', 'distutils',
    ],
    noarchive=False,
    optimize=0,
)

# ---------------------------------------------------------------------------
# Two Analyses: one per entry point. MERGE() then deduplicates the binary
# / data lists so subsequent COLLECT inputs only carry the unique pieces.
# ---------------------------------------------------------------------------
gui_a = Analysis(['gui_entry.py'], **_analysis_common)
cli_a = Analysis(['cli_entry.py'], **_analysis_common)

# After MERGE: gui_a owns shared binaries/datas; cli_a is pruned of
# anything already in gui_a. The tuple form is (analysis, name, exe_name).
MERGE(
    (gui_a, 'yt-dlp-gui', 'yt-dlp-gui'),
    (cli_a, 'yt-dlp-cli', 'yt-dlp-cli'),
)

gui_pyz = PYZ(gui_a.pure)
cli_pyz = PYZ(cli_a.pure)

# Common EXE flags. Per-target overrides live in each EXE() call below.
_exe_common = dict(
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX disabled: known to trigger AV false-positives
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    exclude_binaries=True,  # onedir mode: binaries stay in shared _internal/
)

gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    [],
    name='yt-dlp-gui',
    console=False,        # no flashing cmd window for GUI users
    **_exe_common,
)

cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    [],
    name='yt-dlp-cli',
    console=True,         # keep console window open for CLI interaction
    **_exe_common,
)

# Single COLLECT: produces dist/yt-dlp-downloader/ with both .exe files
# sharing one _internal/ subfolder. Replaces the previous build.ps1 logic
# of running two PyInstaller passes and Copy-Item -Recurse'ing them
# together.
coll = COLLECT(
    gui_exe, cli_exe,
    gui_a.binaries, gui_a.zipfiles, gui_a.datas,
    cli_a.binaries, cli_a.zipfiles, cli_a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='yt-dlp-downloader',
)
