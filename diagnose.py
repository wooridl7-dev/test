"""yt-dlp 다운로더 진단 스크립트.

Standalone: run directly with ``python diagnose.py``. Reuses the
``core.cookies`` helpers so the invocation is consistent with the rest
of the codebase.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Ensure the project root is importable when diagnose.py is run via an
# absolute path (e.g. from a scheduled task) rather than from cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.cookies import load_cookies, platform_label, yt_dlp_command  # noqa: E402
from main import ensure_utf8_stdio  # noqa: E402

# Korean UI + emoji glyphs must survive cp949 Windows consoles.
ensure_utf8_stdio()

print("=" * 60)
print("yt-dlp 다운로더 진단")
print("=" * 60)

# 1. Python / Platform
print(f"\n1. Python 버전: {sys.version.split()[0]}")
print(f"   플랫폼: {platform_label()}")
print(f"   실행파일: {sys.executable}")

# 2. Working directory
print(f"\n2. 현재 디렉토리: {os.getcwd()}")

# 3. yt-dlp availability
print("\n3. yt-dlp 설치 확인:")
yt_dlp_bin = shutil.which("yt-dlp")
if yt_dlp_bin:
    print(f"   \u2713 yt-dlp 발견: {yt_dlp_bin}")
else:
    print("   \u2717 yt-dlp CLI를 찾을 수 없음. python -m yt_dlp 사용")

try:
    version_cmd = yt_dlp_command() + ["--version"]
    result = subprocess.run(
        version_cmd,
        capture_output=True,
        text=True,
        timeout=10,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode == 0:
        print(f"   \u2713 yt-dlp 버전: {result.stdout.strip()}")
    else:
        print("   \u2717 yt-dlp 모듈을 찾을 수 없음")
        print("   설치: uv pip install yt-dlp")
except Exception as exc:  # pragma: no cover
    print(f"   \u2717 에러: {exc}")

# 4. cookies.txt
print("\n4. cookies.txt 파일 확인:")
cookie_candidates = [
    Path("cookies.txt"),
    _PROJECT_ROOT / "cookies.txt",
]
seen: set = set()
found_cookie_path = None
for candidate in cookie_candidates:
    resolved = candidate.resolve()
    if resolved in seen:
        continue
    seen.add(resolved)
    try:
        validated = load_cookies(candidate)
    except FileNotFoundError:
        continue
    except ValueError as exc:
        print(f"   \u26a0\ufe0f  {candidate}: {exc}")
        continue

    print(f"   \u2713 파일 발견: {validated}")
    print(f"   - 크기: {validated.stat().st_size} bytes")
    try:
        with open(validated, "r", encoding="utf-8") as fh:
            lines = [fh.readline().strip() for _ in range(3)]
        print("   - 처음 3줄:")
        for i, line in enumerate(lines, 1):
            if line:
                print(f"     {i}. {line[:60]}...")
    except Exception as exc:
        print(f"   - 읽기 에러: {exc}")
    found_cookie_path = validated
    break

if found_cookie_path is None:
    print("   \u2717 cookies.txt 파일을 찾을 수 없음")
    print("   'python downloader.py --cli'로 쿠키를 자동 생성하거나")
    print("   Chrome 확장 프로그램으로 cookies.txt를 생성하세요.")

# 5. JS runtime
print("\n5. JavaScript 런타임 확인:")
deno_cmd = shutil.which("deno")
if deno_cmd:
    print(f"   \u2713 deno 발견: {deno_cmd}")
else:
    print("   \u26a0\ufe0f  deno 없음 (YouTube 다운로드에 권장)")
    if sys.platform == "win32":
        print("   설치: PowerShell에서")
        print("     irm https://deno.land/install.ps1 | iex")
    else:
        print("   설치: curl -fsSL https://deno.land/install.sh | sh")

# 6. Connectivity
print("\n6. YouTube 연결 테스트:")
try:
    import urllib.request

    response = urllib.request.urlopen("https://www.youtube.com", timeout=5)
    print(f"   \u2713 YouTube 연결 성공 (상태 코드: {response.status})")
except Exception as exc:
    print(f"   \u2717 YouTube 연결 실패: {exc}")

# 7. Dry-run yt-dlp
print("\n7. 테스트 다운로드 (정보만 가져오기):")
test_url = "https://youtu.be/vENN6-d_3AQ"
print(f"   URL: {test_url}")

cmd = yt_dlp_command()
if found_cookie_path is not None:
    cmd.extend(["--cookies", str(found_cookie_path)])
cmd.extend(
    [
        "--skip-download",
        "--print", "title",
        "--print", "uploader",
        test_url,
    ]
)

print(f"   명령: {' '.join(cmd)}")
print("\n   실행 중...\n")

try:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode == 0:
        print("   \u2713 성공!")
        print(f"\n출력:\n{result.stdout}")
    else:
        print(f"   \u2717 실패 (종료 코드: {result.returncode})")
        print(f"\n표준 출력:\n{result.stdout}")
        print(f"\n에러 출력:\n{result.stderr}")

        error_text = (result.stderr or "") + (result.stdout or "")

        if "Sign in to confirm you're not a bot" in error_text:
            print("\n>> 문제: 봇 감지")
            print("   해결: cookies.txt가 올바르게 생성되었는지 확인하세요.")
            print("   1. YouTube에 로그인한 상태에서 쿠키 추출")
            print("   2. cookies.txt를 프로그램과 같은 폴더에 저장")

        if (
            "No such file or directory" in error_text
            or "cannot find" in error_text
        ):
            print("\n>> 문제: 파일을 찾을 수 없음")
            print("   해결: cookies.txt 파일 경로를 확인하세요.")

        if "No module named 'yt_dlp'" in error_text:
            print("\n>> 문제: yt-dlp가 설치되지 않음")
            print("   해결: uv pip install yt-dlp")

except subprocess.TimeoutExpired:
    print("   \u2717 타임아웃 (30초 초과)")
except Exception as exc:
    print(f"   \u2717 예외 발생: {exc}")

print("\n" + "=" * 60)
print("진단 완료")
print("=" * 60)
