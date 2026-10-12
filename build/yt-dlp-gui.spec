# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec file: yt-dlp-gui (Tkinter GUI, no console window)
#
# Build with:
#   pyinstaller build/yt-dlp-gui.spec
#
# Or let build/build.ps1 do it automatically.
#
# NOTE: This spec uses one-folder (onedir) mode by default so that
# startup is fast and per-file UPX pressure is minimised.  Pass
# -Onefile to build.ps1 to switch to onefile mode.

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules
from PyInstaller.utils.hooks import collect_all as _pyi_collect_all

# ---------------------------------------------------------------------------
# Collect yt_dlp (has dynamic extractor loading - needs full collection)
# ---------------------------------------------------------------------------
yt_dlp_datas, yt_dlp_binaries, yt_dlp_hiddenimports = collect_all('yt_dlp')

# Collect submodules explicitly to ensure all extractors are bundled.
yt_dlp_extractor_imports = collect_submodules('yt_dlp.extractor')

# ---------------------------------------------------------------------------
# Collect Playwright Python package (its driver/ folder is critical: it
# contains node.exe + the JS driver scripts that talk to the browser).
# If playwright is not installed (e.g. user passed -NoPlaywright to
# build.ps1), collect_all() returns empty tuples and the subsequent
# concatenation still works.
# ---------------------------------------------------------------------------
try:
    pw_datas, pw_binaries, pw_hidden = _pyi_collect_all('playwright')
except Exception as _pw_exc:
    print(f'WARNING: collect_all(playwright) failed: {_pw_exc}')
    pw_datas, pw_binaries, pw_hidden = [], [], []

# ---------------------------------------------------------------------------
# Bundle the standalone yt-dlp.exe from the build venv.
# PyInstaller's frozen exe has sys.executable == the app itself, so the
# "-m yt_dlp" fallback does not work. Shipping yt-dlp.exe next to the app
# lets core.cookies.yt_dlp_command() spawn the real binary at runtime.
# ---------------------------------------------------------------------------
_bundled_yt_dlp = []
# Prefer the official standalone yt-dlp.exe (downloaded by build.ps1 into
# .yt-dlp-cache/). The pip setuptools launcher from .buildvenv/Scripts/
# depends on the venv's python.exe and CANNOT be bundled in isolation.
_ytdlp_standalone = (Path('.yt-dlp-cache') / 'yt-dlp.exe').resolve()
if _ytdlp_standalone.is_file():
    _bundled_yt_dlp.append((str(_ytdlp_standalone), '.'))
else:
    print('WARNING: standalone yt-dlp.exe not found in .yt-dlp-cache; '
          'the packaged app will fail to spawn yt-dlp at runtime. '
          'Run build.ps1 without -SkipDownload to fetch it.')

# ---------------------------------------------------------------------------
# Bundle deno runtime (downloaded to build/.deno-cache/ by build.ps1).
# yt-dlp's --remote-components ejs:github uses deno to run JS challenges.
# Without deno on PATH, downloads silently fail with no output.
# ---------------------------------------------------------------------------
_bundled_deno = []
_deno_path = (Path('.deno-cache') / 'deno.exe').resolve()
if _deno_path.is_file():
    _bundled_deno.append((str(_deno_path), '.'))
else:
    print('INFO: deno.exe not found in .deno-cache; skipping deno bundling. '
          'Target PC will need deno on PATH.')

# ---------------------------------------------------------------------------
# Bundle ffmpeg + ffprobe (downloaded to build/.ffmpeg-cache/ by build.ps1).
# Required by Phase F live-stream support to remux .ts -> .mp4 after a
# broadcast ends. Without these, --remux-video mp4 silently no-ops and
# the user is left with a .ts container.
# ---------------------------------------------------------------------------
_bundled_ffmpeg = []
for _exe in ('ffmpeg.exe', 'ffprobe.exe'):
    _path = (Path('.ffmpeg-cache') / _exe).resolve()
    if _path.is_file():
        _bundled_ffmpeg.append((str(_path), '.'))
    else:
        print(f'INFO: {_exe} not found in .ffmpeg-cache; live TS->MP4 remux disabled.')

# ---------------------------------------------------------------------------
# Bundle the Playwright-downloaded browser binaries.
# build.ps1 runs ``playwright install chromium`` with
# PLAYWRIGHT_BROWSERS_PATH=build/.playwright-cache, which populates one or
# more ``chromium*`` subfolders (e.g. chromium-1134, chromium_headless_shell
# on newer Playwright releases). We mirror that whole tree into
# ``<_MEIPASS>/playwright-browsers/`` so core.refresher._configure_playwright_env()
# can point PLAYWRIGHT_BROWSERS_PATH at it at runtime.
# ---------------------------------------------------------------------------
_bundled_browsers: list[tuple[str, str]] = []
_pw_cache = Path('.playwright-cache').resolve()
if _pw_cache.is_dir():
    for child in _pw_cache.iterdir():
        # Chromium dir name is like chromium-1134, chromium_headless_shell-...
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
# Hidden imports
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
    'interfaces',
    'interfaces.gui',
    'interfaces.cli',
    'interfaces.bot',
    # Playwright (cookie auto-refresh). Safety net on top of collect_all().
    'playwright',
    'playwright.async_api',
    'playwright._impl',
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

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    # Entry point: gui_entry.py injects --gui into argv before calling main
    ['gui_entry.py'],
    # The repo root must be in pathex so that core/ and interfaces/ are found
    pathex=['..'],
    binaries=yt_dlp_binaries + _bundled_yt_dlp + _bundled_deno + _bundled_ffmpeg + pw_binaries,
    datas=yt_dlp_datas + pw_datas + _bundled_browsers,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy optional packages not used at runtime
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'PIL',
        'cv2',
        'pytest',
        'setuptools',
        'distutils',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir mode: binaries stay in _MEIPASS folder
    name='yt-dlp-gui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX disabled: known to trigger AV false-positives
    console=False,           # no flashing cmd window for GUI users
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon omitted: no .ico file exists in the repository
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='yt-dlp-gui',
)
