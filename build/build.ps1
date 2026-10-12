#Requires -Version 5.1
<#
.SYNOPSIS
    Build standalone Windows distributable for yt-dlp-downloader using PyInstaller.

.DESCRIPTION
    - Creates an isolated build venv (build/.buildvenv) -- never touches the
      user's conda/system Python.
    - Installs yt-dlp (required) and bgutil-ytdlp-pot-provider (optional).
    - Downloads the standalone yt-dlp.exe into build/.yt-dlp-cache and
      re-downloads it whenever it is older than the venv's yt-dlp.
    - Runs PyInstaller once with the unified yt-dlp.spec (MERGE-based);
      both yt-dlp-gui.exe and yt-dlp-cli.exe land in one folder sharing
      a single _internal/ directory.
    - Copies README.md, QUICKSTART.txt, and a cookies.txt template into dist.
    - Zips the output to build/dist/yt-dlp-downloader-<version>-win-x64.zip.

.PARAMETER SkipVenv
    Reuse an existing build/.buildvenv without recreating it.

.PARAMETER Clean
    Delete build/build/ and build/dist/ before starting.

.PARAMETER NoZip
    Skip the final zip step.

.PARAMETER Onefile
    Switch PyInstaller to --onefile mode (single .exe, slower startup).

.PARAMETER NoDeno
    Skip downloading and bundling deno.exe (JS challenge resolver).
    Resulting dist will require deno to be installed on the target PC.

.PARAMETER NoPlaywright
    Skip installing Playwright and downloading the Chromium browser.
    Resulting dist will NOT support the GUI "쿠키 자동 갱신" button;
    users will have to rely on the older browser-cookie extraction path.

.PARAMETER NoFfmpeg
    Skip downloading and bundling ffmpeg.exe / ffprobe.exe.
    Resulting dist will NOT be able to remux live-stream MPEG-TS
    recordings into MP4 (the Phase F "라이브 종료 후 MP4 재포장"
    feature); downloads still work but stay in .ts container.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File build\build.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File build\build.ps1 -Clean -NoZip
#>

[CmdletBinding()]
param(
    [switch]$SkipVenv,
    [switch]$Clean,
    [switch]$NoZip,
    [switch]$Onefile,
    [switch]$NoDeno,
    [switch]$NoPlaywright,
    [switch]$NoFfmpeg
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

function Write-Step {
    param([string]$Message)
    Write-Host "[STEP] $Message" -ForegroundColor Cyan
}

function Write-OK {
    param([string]$Message)
    Write-Host "[ OK ] $Message" -ForegroundColor Green
}

function Write-Fail {
    param([string]$Message)
    Write-Host "[FAIL] $Message" -ForegroundColor Red
}

function Invoke-Checked {
    <#
    .SYNOPSIS
        Run a command; throw a descriptive error if it fails.
    #>
    param(
        [string]$Description,
        [scriptblock]$Command
    )
    Write-Step $Description
    & $Command
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "$Description -- exit code $LASTEXITCODE"
        throw "Command failed: $Description"
    }
    Write-OK $Description
}

# ---------------------------------------------------------------------------
# Resolve paths relative to THIS script (works from any cwd)
# ---------------------------------------------------------------------------
$BuildDir  = $PSScriptRoot                                    # .../build
$RepoRoot  = Split-Path -Parent $BuildDir                     # .../yt-dlp
$VenvDir   = Join-Path $BuildDir ".buildvenv"
$VenvPy    = Join-Path $VenvDir "Scripts\python.exe"
$DistBase  = Join-Path $BuildDir "dist"
$DistDir   = Join-Path $DistBase "yt-dlp-downloader"
$PyiBuild  = Join-Path $BuildDir "build"   # PyInstaller's intermediate output
$DenoCache = Join-Path $BuildDir ".deno-cache"
$DenoExe   = Join-Path $DenoCache "deno.exe"
$YtDlpCache = Join-Path $BuildDir ".yt-dlp-cache"
$YtDlpExe  = Join-Path $YtDlpCache "yt-dlp.exe"
$PwCache   = Join-Path $BuildDir ".playwright-cache"
$FfmpegCache = Join-Path $BuildDir ".ffmpeg-cache"
$FfmpegExe   = Join-Path $FfmpegCache "ffmpeg.exe"
$FfprobeExe  = Join-Path $FfmpegCache "ffprobe.exe"

# ---------------------------------------------------------------------------
# Determine version string
# ---------------------------------------------------------------------------
Write-Step "Determining version"
$VersionFile = Join-Path $RepoRoot "VERSION"
$Version = "0.0.0"

# Try git first (produces tags like v0.1.0-3-gabcdef or just v0.1.0)
$gitOutput = ""
try {
    $gitOutput = & git -C "$RepoRoot" describe --tags --always 2>&1
    if ($LASTEXITCODE -eq 0 -and $gitOutput -ne "") {
        # Strip leading 'v' if present
        $Version = ($gitOutput.Trim() -replace '^v', '')
        Write-OK "Version from git: $Version"
    }
} catch {
    # git not available -- fall through to VERSION file
}

if ($Version -eq "0.0.0") {
    if (Test-Path $VersionFile) {
        $Version = (Get-Content $VersionFile -Raw).Trim()
        Write-OK "Version from VERSION file: $Version"
    } else {
        Write-Host "[WARN] No VERSION file found and git unavailable; using 0.0.0" -ForegroundColor Yellow
        $Version = "0.0.0"
    }
}

# Display version baked into the app (GUI title bar), read at runtime by
# core/version.py: "<VERSION file> (<git describe --always --dirty>)".
# Same format as a source checkout shows. yt-dlp.spec bundles the file.
$AppVersion = if (Test-Path $VersionFile) { (Get-Content $VersionFile -Raw).Trim() } else { "0.0.0" }
try {
    $gitCommit = & git -C "$RepoRoot" describe --always --dirty 2>$null
    if ($LASTEXITCODE -eq 0 -and $gitCommit) { $AppVersion = "$AppVersion ($("$gitCommit".Trim()))" }
} catch {
    # git not available -- VERSION file only
}
$AppVersionFile = Join-Path $BuildDir "_app_version.txt"
[IO.File]::WriteAllText($AppVersionFile, $AppVersion)
Write-OK "App version: $AppVersion"

# ---------------------------------------------------------------------------
# Clean if requested
# ---------------------------------------------------------------------------
if ($Clean) {
    Write-Step "Cleaning previous build artifacts"
    if (Test-Path $PyiBuild) {
        Remove-Item -Recurse -Force "$PyiBuild"
        Write-OK "Removed $PyiBuild"
    }
    if (Test-Path $DistBase) {
        Remove-Item -Recurse -Force "$DistBase"
        Write-OK "Removed $DistBase"
    }
}

# ---------------------------------------------------------------------------
# Check Python prerequisite
# ---------------------------------------------------------------------------
Write-Step "Checking Python prerequisite"

# Allow override via environment variable
$PythonExe = $env:PYTHON_EXE
if (-not $PythonExe) {
    $PythonExe = "python"
}

try {
    $pyVer = & $PythonExe --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "python returned non-zero"
    }
} catch {
    Write-Fail "Python not found. Install Python 3.11+ and ensure it is on PATH, or set `$env:PYTHON_EXE."
    exit 1
}

# Parse major.minor
$verMatch = [regex]::Match($pyVer, 'Python (\d+)\.(\d+)')
if (-not $verMatch.Success) {
    Write-Fail "Could not parse Python version from: $pyVer"
    exit 1
}
$pyMajor = [int]$verMatch.Groups[1].Value
$pyMinor = [int]$verMatch.Groups[2].Value

if ($pyMajor -lt 3 -or ($pyMajor -eq 3 -and $pyMinor -lt 11)) {
    Write-Fail "Python 3.11+ required, found $pyVer"
    exit 1
}
Write-OK "Found $pyVer"

# ---------------------------------------------------------------------------
# Check uv prerequisite (uv replaces pip for venv creation and package install)
# ---------------------------------------------------------------------------
Write-Step "Checking uv prerequisite"
try {
    $uvVer = & uv --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "uv returned non-zero"
    }
} catch {
    Write-Fail "uv not found. Install with: irm https://astral.sh/uv/install.ps1 | iex"
    exit 1
}
Write-OK "Found $uvVer"

# ---------------------------------------------------------------------------
# Create or reuse build venv
# ---------------------------------------------------------------------------
if ($SkipVenv -and (Test-Path $VenvPy)) {
    Write-Step "Reusing existing build venv at $VenvDir"
} else {
    if (Test-Path $VenvDir) {
        Write-Step "Removing existing build venv before re-creating"
        Remove-Item -Recurse -Force "$VenvDir"
    }
    Invoke-Checked "Create isolated build venv (uv, borrowing $PythonExe)" {
        & uv venv --python "$PythonExe" "$VenvDir"
    }
}

Write-OK "Build venv ready: $VenvDir"

# ---------------------------------------------------------------------------
# Install PyInstaller
# ---------------------------------------------------------------------------
Invoke-Checked "Install PyInstaller" {
    & uv pip install --python "$VenvPy" --quiet pyinstaller
}

# ---------------------------------------------------------------------------
# Install required runtime dependency: yt-dlp
# ---------------------------------------------------------------------------
Invoke-Checked "Install yt-dlp (required)" {
    & uv pip install --python "$VenvPy" --quiet "yt-dlp"
}

# ---------------------------------------------------------------------------
# Download the OFFICIAL standalone yt-dlp.exe from GitHub releases.
# The pip-installed yt-dlp.exe is just a setuptools console_scripts launcher
# that depends on the venv's python.exe, so it breaks when bundled alone.
# The GitHub release is a self-contained PyInstaller build (~17MB) that
# works standalone.
#
# The cache is refreshed whenever it is older than the yt-dlp just
# pip-installed into the venv (always the latest release): the bundled
# exe is what actually talks to YouTube, and an outdated copy is the
# most common cause of sudden download failures. Versions are dotted
# dates ("2026.08.19", nightlies add a 4th part), so [version] orders
# them correctly.
# ---------------------------------------------------------------------------
function Get-YtDlpVersion {
    param([scriptblock]$Command)
    try {
        # Collect everything first: piping into Select-Object -First 1
        # stops the native process early and leaves $LASTEXITCODE = -1.
        $out = @(& $Command)
        if ($LASTEXITCODE -eq 0 -and $out.Count -gt 0) { return [version]"$($out[0])".Trim() }
    } catch { }
    return $null
}

if (-not (Test-Path $YtDlpCache)) {
    New-Item -ItemType Directory -Path "$YtDlpCache" | Out-Null
}
$wantYtDlp = Get-YtDlpVersion { & $VenvPy -c "import yt_dlp.version as v; print(v.__version__)" }
if (Test-Path $YtDlpExe) {
    $haveYtDlp = Get-YtDlpVersion { & $YtDlpExe --version }
    if ($null -eq $haveYtDlp) {
        Write-Step "Cached yt-dlp.exe does not report a version -- re-downloading"
        Remove-Item -Force "$YtDlpExe"
    } elseif ($null -eq $wantYtDlp) {
        Write-OK "yt-dlp.exe (standalone) $haveYtDlp cached (venv version unknown, keeping cache)"
    } elseif ($haveYtDlp -lt $wantYtDlp) {
        Write-Step "Cached yt-dlp.exe $haveYtDlp is older than venv yt-dlp $wantYtDlp -- re-downloading"
        Remove-Item -Force "$YtDlpExe"
    } else {
        Write-OK "yt-dlp.exe (standalone) $haveYtDlp already cached at $YtDlpExe"
    }
}
if (-not (Test-Path $YtDlpExe)) {
    Write-Step "Downloading standalone yt-dlp.exe from GitHub releases"
    $ytdlpUrl = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $oldProgress = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"
    try {
        Invoke-WebRequest -Uri $ytdlpUrl -OutFile $YtDlpExe -UseBasicParsing
    } finally {
        $ProgressPreference = $oldProgress
    }
    if (-not (Test-Path $YtDlpExe)) {
        Write-Fail "yt-dlp.exe standalone download failed."
        exit 1
    }
    Write-OK "yt-dlp.exe (standalone) $(Get-YtDlpVersion { & $YtDlpExe --version }) downloaded: $YtDlpExe"
}

# ---------------------------------------------------------------------------
# Install optional runtime dependency: bgutil-ytdlp-pot-provider
# ---------------------------------------------------------------------------
Write-Step "Install bgutil-ytdlp-pot-provider (optional -- continuing on failure)"
& uv pip install --python "$VenvPy" --quiet "bgutil-ytdlp-pot-provider" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] bgutil-ytdlp-pot-provider install failed (optional -- skipping)" -ForegroundColor Yellow
} else {
    Write-OK "bgutil-ytdlp-pot-provider installed"
}

# ---------------------------------------------------------------------------
# Download deno (JS challenge resolver used by yt-dlp --remote-components)
# Cached under build/.deno-cache; reuse across builds.
# ---------------------------------------------------------------------------
if (-not $NoDeno) {
    if (-not (Test-Path $DenoCache)) {
        New-Item -ItemType Directory -Path "$DenoCache" | Out-Null
    }
    if (Test-Path $DenoExe) {
        Write-OK "deno.exe already cached at $DenoExe"
    } else {
        Write-Step "Downloading deno runtime (x86_64-pc-windows-msvc)"
        $denoUrl = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"
        $denoZip = Join-Path $DenoCache "deno.zip"
        # PowerShell 5.1 uses TLS 1.0 by default -- GitHub requires TLS 1.2.
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        # ProgressPreference=SilentlyContinue dramatically speeds up IWR on PS5.1
        $oldProgress = $ProgressPreference
        $ProgressPreference = "SilentlyContinue"
        try {
            Invoke-WebRequest -Uri $denoUrl -OutFile $denoZip -UseBasicParsing
        } finally {
            $ProgressPreference = $oldProgress
        }
        if (-not (Test-Path $denoZip)) {
            Write-Fail "deno download failed."
            exit 1
        }
        Write-Step "Extracting deno.zip"
        Expand-Archive -Path $denoZip -DestinationPath $DenoCache -Force
        Remove-Item $denoZip -Force
        if (-not (Test-Path $DenoExe)) {
            Write-Fail "deno.exe not found after extraction."
            exit 1
        }
        Write-OK "deno.exe ready: $DenoExe"
    }
} else {
    Write-Host "[INFO] -NoDeno specified: skipping deno bundling" -ForegroundColor Yellow
    Write-Host "       Target PC must have deno on PATH." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Download ffmpeg + ffprobe (BtbN Windows builds) for Phase F live-stream
# MP4 remux. Cached under build/.ffmpeg-cache; reuse across builds.
# ---------------------------------------------------------------------------
if (-not $NoFfmpeg) {
    if (-not (Test-Path $FfmpegCache)) {
        New-Item -ItemType Directory -Path "$FfmpegCache" | Out-Null
    }
    if ((Test-Path $FfmpegExe) -and (Test-Path $FfprobeExe)) {
        Write-OK "ffmpeg already cached at $FfmpegCache"
    } else {
        Write-Step "Downloading ffmpeg (BtbN Windows build)"
        $ffmpegUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
        $ffmpegZip = Join-Path $FfmpegCache "ffmpeg.zip"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $oldProgress = $ProgressPreference
        $ProgressPreference = "SilentlyContinue"
        try {
            Invoke-WebRequest -Uri $ffmpegUrl -OutFile $ffmpegZip -UseBasicParsing
        } finally {
            $ProgressPreference = $oldProgress
        }
        if (-not (Test-Path $ffmpegZip)) {
            Write-Fail "ffmpeg download failed."
            exit 1
        }
        # Extract only bin/ffmpeg.exe and bin/ffprobe.exe from the archive.
        Write-Step "Extracting ffmpeg.exe + ffprobe.exe"
        $tmpExtract = Join-Path $FfmpegCache "_tmp"
        if (Test-Path $tmpExtract) {
            Remove-Item -Recurse -Force "$tmpExtract"
        }
        Expand-Archive -Path $ffmpegZip -DestinationPath $tmpExtract -Force
        # The zip has a top-level ffmpeg-master-... folder; its bin/
        # subdirectory holds ffmpeg.exe / ffprobe.exe / ffplay.exe.
        $topDir = Get-ChildItem -Path $tmpExtract -Directory | Select-Object -First 1
        if ($null -eq $topDir) {
            Write-Fail "Unexpected ffmpeg archive layout: no top-level directory under $tmpExtract"
            exit 1
        }
        $binDir = Join-Path $topDir.FullName "bin"
        $srcFfmpeg  = Join-Path $binDir "ffmpeg.exe"
        $srcFfprobe = Join-Path $binDir "ffprobe.exe"
        if (-not (Test-Path $srcFfmpeg) -or -not (Test-Path $srcFfprobe)) {
            Write-Fail "ffmpeg/ffprobe not found under $binDir after extraction"
            exit 1
        }
        Copy-Item -Path $srcFfmpeg  -Destination $FfmpegExe  -Force
        Copy-Item -Path $srcFfprobe -Destination $FfprobeExe -Force
        Remove-Item -Recurse -Force "$tmpExtract"
        Remove-Item -Force "$ffmpegZip"
        if (-not (Test-Path $FfmpegExe)) {
            Write-Fail "ffmpeg extraction failed"
            exit 1
        }
        Write-OK "ffmpeg ready: $FfmpegExe"
    }
} else {
    Write-Host "[INFO] -NoFfmpeg specified: skipping ffmpeg bundling" -ForegroundColor Yellow
    Write-Host "       라이브 스트림 TS->MP4 재포장이 비활성됩니다." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Install Playwright + download Chromium browser binaries.
# Chromium is bundled under <_MEIPASS>/playwright-browsers/ by the spec files,
# so the final exe can auto-refresh YouTube cookies without a Python/venv on
# the target PC. The cache dir is reused across builds to avoid ~150MB of
# repeated downloads.
# ---------------------------------------------------------------------------
if (-not $NoPlaywright) {
    Invoke-Checked "Install Playwright (uv)" {
        & uv pip install --python "$VenvPy" --quiet "playwright>=1.40"
    }

    if (-not (Test-Path $PwCache)) {
        New-Item -ItemType Directory -Path "$PwCache" | Out-Null
    }

    # Scope PLAYWRIGHT_BROWSERS_PATH to this step only.
    $env:PLAYWRIGHT_BROWSERS_PATH = "$PwCache"
    Write-Step "Downloading Chromium for Playwright (to $PwCache)"
    # --with-deps is Linux-only; on Windows only the binary is needed.
    & "$VenvPy" -m playwright install chromium
    $pwExit = $LASTEXITCODE
    Remove-Item Env:\PLAYWRIGHT_BROWSERS_PATH
    if ($pwExit -ne 0) {
        Write-Fail "playwright install chromium failed (exit $pwExit)"
        exit 1
    }
    Write-OK "Chromium downloaded"
} else {
    Write-Host "[INFO] -NoPlaywright specified: skipping Playwright + Chromium bundling" -ForegroundColor Yellow
    Write-Host "       GUI '쿠키 자동 갱신' 버튼이 동작하지 않습니다." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Run PyInstaller (single unified spec produces both yt-dlp-gui.exe and
# yt-dlp-cli.exe inside one shared dist/yt-dlp-downloader/ folder via
# MERGE()). Replaces the previous two-pass build + Copy-Item -Recurse
# merge that was duplicating ~1.4GB of _internal/ across cli/gui dirs.
# ---------------------------------------------------------------------------
$Spec = Join-Path $BuildDir "yt-dlp.spec"

$pyiCommonArgs = @(
    "--distpath", "$DistBase",
    "--workpath", "$PyiBuild",
    "--noconfirm"
)

if ($Onefile) {
    Write-Host "[INFO] Onefile mode requested -- note: yt-dlp.spec pins onedir;" -ForegroundColor Yellow
    Write-Host "       edit the .spec file if you want true one-file builds." -ForegroundColor Yellow
}

# Defensive cleanup: remove any leftover per-spec output folders from a
# previous (pre-merge) build so they don't shadow the unified output or
# inflate disk usage. The unified spec writes directly to $DistDir.
foreach ($leftover in @("yt-dlp-gui", "yt-dlp-cli")) {
    $stale = Join-Path $DistBase $leftover
    if (Test-Path $stale) {
        Write-Step "Removing stale per-spec output: $stale"
        Remove-Item -Recurse -Force "$stale"
    }
}

# PyInstaller rejects makespec-only options (including --specpath) when a
# .spec file is provided. We must run from the spec's own directory so any
# relative paths inside the spec resolve correctly.
Push-Location $BuildDir
try {
    Invoke-Checked "PyInstaller: build unified yt-dlp-downloader (gui + cli)" {
        & "$VenvPy" -m PyInstaller @pyiCommonArgs "$Spec"
    }
} finally {
    Pop-Location
}

if (-not (Test-Path $DistDir)) {
    throw "PyInstaller did not produce the expected output folder: $DistDir"
}

Write-OK "Executables assembled in $DistDir"

# ---------------------------------------------------------------------------
# Make the bgutil PO Token plugin discoverable by the bundled standalone
# yt-dlp.exe (which is its own frozen binary with embedded Python and
# CANNOT see the wrapper's site-packages). Standalone yt-dlp scans
# ``<exe-dir>/yt-dlp-plugins/<package>/yt_dlp_plugins/`` at startup, so
# we mirror the venv-installed plugin tree there.
#
# The wrapper exes (yt-dlp-gui/cli.exe) get the plugin via PyInstaller's
# collect_all('yt_dlp_plugins') in yt-dlp.spec; this step is purely for
# the standalone yt-dlp.exe at $DistDir/_internal/yt-dlp.exe.
# ---------------------------------------------------------------------------
$BgutilSrc = Join-Path $VenvDir "Lib\site-packages\yt_dlp_plugins"
if (Test-Path $BgutilSrc) {
    Write-Step "Mirroring bgutil plugin for standalone yt-dlp.exe"
    $PluginRoot = Join-Path $DistDir "_internal\yt-dlp-plugins\bgutil-pot-provider\yt_dlp_plugins"
    if (Test-Path $PluginRoot) {
        Remove-Item -Recurse -Force "$PluginRoot"
    }
    New-Item -ItemType Directory -Path "$PluginRoot" -Force | Out-Null
    Copy-Item -Path "$BgutilSrc\*" -Destination "$PluginRoot" -Recurse -Force
    Write-OK "bgutil plugin mirrored to $PluginRoot"
} else {
    Write-Host "[WARN] bgutil plugin not found in venv site-packages -- " `
        "PO Token auth will be unavailable in this build." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Copy user-facing files into dist folder
# ---------------------------------------------------------------------------
Write-Step "Copying README.md and QUICKSTART.txt"

$ReadmeSrc = Join-Path $RepoRoot "README.md"
$QsSrc     = Join-Path $RepoRoot "QUICKSTART.txt"

if (Test-Path $ReadmeSrc) {
    Copy-Item "$ReadmeSrc" "$DistDir\" -Force
    Write-OK "Copied README.md"
} else {
    Write-Host "[WARN] README.md not found at $ReadmeSrc -- skipping" -ForegroundColor Yellow
}

if (Test-Path $QsSrc) {
    Copy-Item "$QsSrc" "$DistDir\" -Force
    Write-OK "Copied QUICKSTART.txt"
} else {
    Write-Host "[WARN] QUICKSTART.txt not found at $QsSrc -- skipping" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Write cookies.txt placeholder (Netscape header format)
# ---------------------------------------------------------------------------
Write-Step "Writing cookies.txt placeholder"
$CookieDest = Join-Path $DistDir "cookies.txt"
# Always overwrite the shipped template. Force ASCII (no BOM) because
# yt-dlp rejects Netscape cookie files that start with the UTF-8 BOM
# (it reports "invalid length 1: '\ufeff# Netscape ...'"). PowerShell 5.1's
# `Set-Content -Encoding UTF8` emits a BOM, so we use raw byte IO instead.
$cookieTemplate = @"
# Netscape HTTP Cookie File
# This file is used by yt-dlp to authenticate with YouTube.
#
# HOW TO EXPORT YOUR COOKIES:
#   1. Install a browser extension such as "Get cookies.txt LOCALLY" (Chrome/Edge)
#      or "cookies.txt" (Firefox).
#   2. Navigate to https://www.youtube.com and sign in.
#   3. Use the extension to export cookies in Netscape format.
#   4. Replace this file with the exported file.
#
# FORMAT: domain  include-subdomains  path  secure  expiry  name  value
# Example (do not copy this line):
# .youtube.com  TRUE  /  TRUE  9999999999  CONSENT  YES+...
#
# Leave this file empty (only these comments) if you do not need authentication.
"@
# Convert LF to CRLF for Windows and write as pure ASCII without BOM.
$cookieBytes = [System.Text.Encoding]::ASCII.GetBytes(
    ($cookieTemplate -replace "`r?`n", "`r`n") + "`r`n"
)
[System.IO.File]::WriteAllBytes($CookieDest, $cookieBytes)
Write-OK "cookies.txt placeholder written (ASCII, no BOM)"

# ---------------------------------------------------------------------------
# Zip the dist folder
# ---------------------------------------------------------------------------
if (-not $NoZip) {
    $ZipName = "yt-dlp-downloader-$Version-win-x64.zip"
    $ZipPath = Join-Path $DistBase $ZipName

    Write-Step "Creating zip: $ZipPath"

    if (Test-Path $ZipPath) {
        Remove-Item "$ZipPath" -Force
    }

    # Compress-Archive is available on PowerShell 5+ (Windows 10 built-in)
    Compress-Archive -Path "$DistDir" -DestinationPath "$ZipPath" -CompressionLevel Optimal

    if (Test-Path $ZipPath) {
        $zipSize = [math]::Round((Get-Item "$ZipPath").Length / 1MB, 1)
        Write-OK "Zip created: $ZipPath ($zipSize MB)"
    } else {
        Write-Fail "Zip was not created at $ZipPath"
        exit 1
    }
} else {
    Write-Host "[INFO] -NoZip specified -- skipping zip step" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-OK "===== BUILD COMPLETE ====="
Write-Host "  Dist folder : $DistDir" -ForegroundColor Green
if (-not $NoZip) {
    $ZipName = "yt-dlp-downloader-$Version-win-x64.zip"
    $ZipPath = Join-Path $DistBase $ZipName
    Write-Host "  Zip archive : $ZipPath" -ForegroundColor Green
}
Write-Host ""

exit 0
