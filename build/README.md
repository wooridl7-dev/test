# Build System — yt-dlp-downloader Windows Distributable

This directory contains everything needed to produce a standalone
Windows executable that end-users can run without installing Python or
any conda environment.

---

## Quick Start

### Option A — PowerShell (recommended)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build\build.ps1
```

### Option B — Double-click (CMD)

Double-click `build\build.bat`.  It simply invokes the PowerShell
script above.

### Available Parameters

| Parameter    | Effect |
|-------------|--------|
| `-SkipVenv`  | Reuse an existing `build/.buildvenv` without recreating it |
| `-Clean`     | Delete `build/build/` and `build/dist/` before starting |
| `-NoZip`     | Skip the final zip step |
| `-Onefile`   | Single-exe mode (slow startup, no support folder) |

Example with parameters:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build\build.ps1 -Clean -NoZip
```

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Windows 10 x64 or Windows 11 | Build machine and target machine |
| Python 3.11+ | Must be on `PATH`, or set `$env:PYTHON_EXE` to the full path |
| uv | Install once: `irm https://astral.sh/uv/install.ps1 \| iex` |
| Internet access | Required for `uv pip install` of PyInstaller and yt-dlp |

PyInstaller is installed automatically into the build venv — you do
**not** need it pre-installed.

### Custom Python path

If Python is installed somewhere unusual:

```powershell
$env:PYTHON_EXE = "C:\Python311\python.exe"
powershell -NoProfile -ExecutionPolicy Bypass -File build\build.ps1
```

---

## Output

After a successful build you will find:

```
build/
  dist/
    yt-dlp-downloader/          <- unzipped, ready-to-run folder
      yt-dlp-gui.exe            <- double-click to open the GUI
      yt-dlp-cli.exe            <- run from cmd for CLI mode
      _internal/                <- Python runtime + bundled packages
      README.md
      QUICKSTART.txt
      cookies.txt               <- replace with your YouTube cookies
    yt-dlp-downloader-0.1.0-win-x64.zip   <- distributable archive
```

### What the end-user gets in the zip

| File / Folder | Purpose |
|---------------|---------|
| `yt-dlp-gui.exe` | Launches the Tkinter GUI (no console window) |
| `yt-dlp-cli.exe` | Interactive CLI (console window) |
| `_internal/` | Python runtime, yt-dlp, all bundled dependencies |
| `README.md` | Project documentation |
| `QUICKSTART.txt` | Quick-start instructions |
| `cookies.txt` | Template — replace with your browser cookie export |

---

## Troubleshooting

### Anti-virus flags the exe

This is a known PyInstaller issue.  Every PyInstaller-built executable
triggers some AV vendors because the bootloader pattern resembles
packers.

Mitigations (in order of preference):

1. Submit a false-positive report to your AV vendor.
2. Add an exclusion for the `build/dist/` folder.
3. Rebuild from source (the exe you built yourself is trusted by
   Windows Defender after the first run on your own machine).
4. UPX is already **disabled** (`upx=False` in the spec files), which
   eliminates the most common AV trigger.

### "Missing DLL" or crash on startup on another PC

Install the **Microsoft Visual C++ Redistributable 2015–2022 (x64)**:

```
https://aka.ms/vs/17/release/vc_redist.x64.exe
```

Windows 10/11 ships with this in most configurations, but a freshly
installed or lightly provisioned machine may not have it.

### Korean / non-ASCII text garbled in CLI mode

The CLI output uses UTF-8.  To display it correctly:

```cmd
chcp 65001
```

Or run in Windows Terminal (which defaults to UTF-8) instead of the
legacy `cmd.exe` console.

### PyInstaller fails with "module not found" for a yt-dlp extractor

The spec files use `collect_all('yt_dlp')` and
`collect_submodules('yt_dlp.extractor')` to bundle every extractor.
If a very new yt-dlp version adds a plugin mechanism outside those
paths, you may need to add:

```python
# In the spec file's hiddenimports list:
'yt_dlp.extractor.NEW_EXTRACTOR_MODULE',
```

Or add a `--collect-submodules yt_dlp.extractor` flag to the
PyInstaller invocation in `build.ps1`.

### Cannot build on Linux / macOS

PyInstaller does **not** support cross-compilation.  You must run this
build system on a Windows 10/11 x64 machine.  Wine is not supported
and known to produce broken exes.

---

## Version Bump

1. Edit the `VERSION` file in the repo root:

   ```
   0.2.0
   ```

2. Run `build.ps1`.  The zip will be named
   `yt-dlp-downloader-0.2.0-win-x64.zip` automatically.

If the repo has git tags, the version is read from
`git describe --tags --always` first; the `VERSION` file is the
fallback.

---

## Architecture Notes

| File | Role |
|------|------|
| `build.ps1` | Orchestrates venv creation (uv), package installs (uv pip), PyInstaller, zipping |
| `build.bat` | One-line CMD wrapper for users who prefer double-click |
| `gui_entry.py` | PyInstaller entry: injects `--gui` into `sys.argv` then calls `main.main()` |
| `cli_entry.py` | PyInstaller entry: injects `--cli` into `sys.argv` then calls `main.main()` |
| `yt-dlp-gui.spec` | PyInstaller spec: GUI build (`console=False`) |
| `yt-dlp-cli.spec` | PyInstaller spec: CLI build (`console=True`) |

The two entry wrapper files exist because PyInstaller freezes
`sys.argv[0]` to the exe name.  Without the wrappers a user who
double-clicks `yt-dlp-gui.exe` without any arguments would need
`main.py`'s default to always be `--gui`, which is fragile.  The
wrappers make each exe self-routing.
