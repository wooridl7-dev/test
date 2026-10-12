# yt-dlp 다운로더

YouTube 동영상을 다운로드하는 GUI/CLI 프로그램입니다. 모듈화된 구조로
재구성되어 향후 메신저 봇 서비스(Linux/Docker)와 Whisper 전사 기능을 쉽게
추가할 수 있습니다.

## 최근 주요 변경사항

자세한 히스토리는 [CHANGELOG.md](CHANGELOG.md) 참조.

- **Phase B** — 모듈화 리팩터 (`core/` + `interfaces/` 분리)
- **Phase D** — `Authenticator` 추상화 + bgutil PO Token (쿠키/PO Token/브라우저/미인증 4 방식)
- **Phase Build** — Windows 10+ PyInstaller 배포 (yt-dlp / deno / Playwright + Chromium 전부 번들)
- **Phase C-4** — GUI "쿠키 자동 갱신" 버튼 (Playwright 내장, Python 불필요)
- **Phase F** — 라이브 스트림 자동 감지 + 캐치업 다운로드 (DVR 윈도우 내 처음부터 병렬, MP4 재포장)
- **Phase I** — 일괄 다운로드: URL 목록 파일을 차례로 받아 `# 분야` 별 폴더에 저장 (GUI '일괄' 탭, `--batch`)
- **Phase J** — 기본 탭 다운로드 목록 + 동시 다운로드, 다운로드마다 실행 로그 저장

## 프로젝트 구조

```
yt-dlp/
├── main.py                 # 통합 진입점 (--gui / --cli / --bot / --batch)
├── downloader.py           # 이전 버전 호환 셸
├── diagnose.py             # 진단 스크립트
├── VERSION                 # 배포 버전
│
├── core/                   # 순수 로직 (tkinter/input/print 없음)
│   ├── auth.py             # Authenticator 추상화 + 4 구현
│   ├── batch.py            # 일괄 다운로드 (목록 파싱, 분야별 폴더, 이어받기)
│   ├── cookie_health.py    # 로그인 쿠키가 아직 되는지 기록 (cookie_health.json)
│   ├── cookies.py          # yt-dlp 쿠키 추출·검증, frozen 런타임 헬퍼
│   ├── downloader.py       # 다운로드 + ProgressDisplay + 환경변수 주입
│   ├── failure.py          # 실패 원인 분류 (인증 만료 / 봇 차단 / PO Token / 접근 권한 / 네트워크)
│   ├── history.py          # 다운로드 기록 (GUI '기록' 탭, CLI `h`)
│   ├── live.py             # 라이브 감지 + 라이브 옵션
│   ├── output_claims.py    # 동시 다운로드끼리 같은 출력 파일명을 쓰지 않도록 조율
│   ├── partial_merge.py    # 멈춘 라이브 녹화를 재생 가능한 파일로 병합
│   ├── presets.py          # GUI/CLI 가 같이 쓰는 다운로드 프리셋
│   ├── refresher.py        # Playwright 기반 쿠키 자동 갱신 엔진
│   ├── run_log.py          # 다운로드마다 실행 로그 파일 저장
│   ├── telegram.py         # 텔레그램 Bot API 클라이언트 (표준 라이브러리만)
│   └── transcriber.py      # Whisper 전사 백엔드 (스텁)
│
├── interfaces/             # 사용자 대면 레이어
│   ├── cli.py              # 대화형 CLI (7택 인증 메뉴)
│   ├── gui.py              # tkinter GUI ("쿠키 자동 갱신" 버튼 포함)
│   ├── gui_jobs.py         # GUI 동시 다운로드 작업 표 (Tk 없음)
│   ├── tk_tooltip.py       # 잘린 목록 칸의 전체 글을 보여 주는 툴팁
│   └── bot.py              # 텔레그램 봇 (env 기반 config, 대기열·진행률·취소)
│
├── scripts/                # 개발자용 독립 도구
│   ├── refresh_cookies.py  # core.refresher CLI 래퍼 (cron/스케줄러용)
│   ├── collect_log_phrases.py  # 실행 로그의 실패·경고 문구를 형태별로 집계 (읽기 전용)
│   └── README.md
│
├── build/                  # Windows 배포 빌드 시스템
│   ├── build.ps1           # PowerShell 빌드 스크립트
│   ├── build.bat           # 더블클릭용 래퍼
│   ├── yt-dlp-gui.spec     # PyInstaller (GUI, console=False)
│   ├── yt-dlp-cli.spec     # PyInstaller (CLI, console=True)
│   ├── gui_entry.py        # --gui argv 주입 엔트리
│   ├── cli_entry.py        # --cli argv 주입 엔트리
│   └── README.md
│
├── docker/                 # bgutil PO Token 사이드카 배포
│   ├── docker-compose.yml
│   ├── Dockerfile.downloader
│   └── README.md
│
├── CHANGELOG.md            # 변경 이력
├── TODO.md                 # 진행 중/예정 작업
├── README.md               # 이 파일
└── QUICKSTART.txt          # 빠른 시작 가이드
```

`core/`는 **플랫폼 독립적**이고 **상호작용이 없는** 순수 로직만 포함합니다.
모든 `input()` / `print()` / `tkinter`는 `interfaces/`에만 존재합니다.
덕분에 Linux/Docker에서 메신저 봇을 돌릴 때 `core.downloader.download(...)`와
`core.refresher.refresh_cookies(...)` 를 그대로 임포트하여 사용할 수 있습니다.

## 실행 방식 선택

### A. 배포본 사용자 (권장)
빌드 산출물 `yt-dlp-downloader-<version>-win-x64.zip` 을 받아 압축 해제 후
`yt-dlp-gui.exe` 또는 `yt-dlp-cli.exe` 를 실행합니다. **Python/conda/pip 불필요**.
yt-dlp, deno, Playwright + Chromium 모두 번들에 포함되어 있습니다.

### B. 소스에서 실행 (개발자)
Python 3.12+ 가 있는 **임의의** 환경에서 동작합니다. 본 프로젝트는 `uv`로
의존성을 관리합니다 (`.python-version`에 `3.12` 고정).

```powershell
# 1. uv 설치 (한 번만)
irm https://astral.sh/uv/install.ps1 | iex

# 2. 기존 Python(예: conda env)을 빌려 격리된 .venv 생성
#    (작성자 환경 예시 — 본인의 Python 3.12 경로로 교체)
uv venv --python C:\tools\miniconda3\envs\ryzen-ai-1.6.1\python.exe

# 3. 활성화 + 의존성 설치
.venv\Scripts\Activate.ps1
uv pip install yt-dlp

# 4. 실행
python main.py
```

> 참고: 작성자는 `ryzen-ai-1.6.1` conda env (NPU 툴체인용 개인 환경)의 Python
> 3.12.11 인터프리터를 `--python` 으로 빌려 사용합니다. **conda env 자체는
> 건드리지 않고**, uv가 그 인터프리터를 기반으로 별도 `.venv`를 만듭니다.
> 본인의 Python 3.12 설치라면 어떤 경로든(다른 conda env / pyenv / system
> Python) 사용 가능합니다.

#### (선택) JavaScript 런타임
소스 모드에서는 yt-dlp 의 JS challenge 해결에 deno 가 필요할 수 있습니다
(배포본은 이미 번들됨).

**Windows PowerShell:**
```powershell
irm https://deno.land/install.ps1 | iex
```

**Linux/macOS:**
```bash
curl -fsSL https://deno.land/install.sh | sh
```

## 실행 방법

### GUI 모드 (기본)
```powershell
python main.py            # 권장
python main.py --gui      # 명시적 GUI
python downloader.py      # 이전 버전 호환
```

### CLI 모드
```powershell
python main.py --cli
python downloader.py --cli   # 이전 버전 호환
python main.py --cli --fragment-retries 0   # 라이브: 끊긴 조각을 다시 받지 않음
```
`--fragment-retries N|infinite` 는 라이브 녹화에서 받지 못한 조각을 몇 번 다시 시도할지 정합니다
(기본 10, `0` 은 재시도 안 함, `infinite` 는 끝없이 — 이때는 진척 없음 자동 정지(기본 5분)만이 멈춰 줍니다).
`--batch` 와 함께도 쓸 수 있고, GUI 는 고급/라이브 탭, 봇은 환경변수 `YTDLP_FRAGMENT_RETRIES` 로 정합니다.

`--stall-minutes N` 은 라이브 녹화가 N분 동안 새 데이터를 받지 못하면 멈추는 시간입니다(기본 5, `0` 은 끔, 최대 1440).
방송이 끝났는데 yt-dlp 가 빠져나오지 못하는 경우를 잡는 안전장치라 `0` 으로 끄면 `--fragment-retries infinite` 와
함께 쓸 때 멈추지 않을 수 있습니다(시작할 때 경고). 같은 방식으로 `--cli`·`--batch` 에서만 받고, GUI 는 고급/라이브 탭의
"진척 없음 자동 정지(분)", 봇은 환경변수 `YTDLP_STALL_MINUTES` 입니다. 값이 잘못되면 시작하지 않고 알립니다.

### 일괄 다운로드
```powershell
python main.py --batch 내youtube.txt
python main.py --gui --batch 내youtube.txt  # 일괄 탭에 미리 열기 (자동 시작 안 함)
yt-dlp-cli.exe --batch 내youtube.txt   # 배포본
```
자세한 내용은 아래 [일괄 다운로드](#일괄-다운로드-1) 참조.

### 텔레그램 봇
```powershell
$env:YTDLP_BOT_TOKEN = "123456:ABC..."        # @BotFather 가 준 토큰
$env:YTDLP_BOT_ALLOWED_CHATS = "123456789"    # 봇이 상대할 채팅 id (쉼표로 여러 개)
python main.py --bot
```
텔레그램으로 YouTube 링크를 보내면 이 PC(또는 서버)가 받아 둡니다. 자세한 내용은 아래
[텔레그램 봇](#텔레그램-봇-1) 참조.

## 인증 방식

YouTube 는 봇 감지를 강화하여 인증이 사실상 필수입니다. 본 프로젝트는
`core/auth.py::Authenticator` 추상화 위에 네 가지 방식을 제공합니다.

| 방식 | 헤드리스 OK | 유지보수 | 권장 시나리오 |
|---|---|---|---|
| **PO Token (bgutil 사이드카)** | ✅ | 낮음 | Docker 메신저 봇, 서버 배포 |
| **쿠키 파일** | ✅ (부트스트랩 필요) | 중간 (만료 갱신) | 로컬/서버 모두. 프리미엄·멤버십 콘텐츠. |
| **브라우저 쿠키 자동 추출** | ❌ (브라우저 필요) | 낮음 | 개인 데스크톱 사용 |
| **미인증** | ✅ | — | 공개 저퀄리티 테스트용 |

### 1. PO Token via bgutil (권장, 헤드리스 친화)
2026년 현재 YouTube 다운로드에 사실상 필수. `bgutil-ytdlp-pot-provider`
플러그인이 토큰을 자동 생성·주입합니다.

**로컬 개발**:
```powershell
docker run -d -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider:latest
uv pip install bgutil-ytdlp-pot-provider
python main.py --cli      # 메뉴에서 "6. PO Token (bgutil)" 선택
```

**서버 배포** (Docker compose):
```bash
cd docker && docker compose up -d
```
자세한 내용은 [`docker/README.md`](docker/README.md) 참조.

### 2. 자동 쿠키 업데이트
1. Chrome/Firefox/Edge 에서 **YouTube 에 로그인**
2. 프로그램 실행 (관리자 권한 아님)
3. GUI: "쿠키 업데이트" 버튼 / CLI: 옵션 1-3

### 3. 수동 쿠키 파일
1. Chrome 확장 프로그램 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) 설치
2. YouTube 로그인 상태에서 확장 프로그램 클릭 → "Export"
3. 프로젝트 폴더에 `cookies.txt` 배치

### 4. 쿠키 자동 갱신 (내장)

메인 배포본(PyInstaller zip)에는 **Playwright 와 Chromium 브라우저**가 함께
번들되어 있어, Python 이나 venv 없이 GUI 에서 바로 YouTube 쿠키를 갱신할 수
있습니다.

1. 프로그램 실행 (`yt-dlp-gui.exe`).
2. "쿠키 관리" 영역의 **"쿠키 자동 갱신"** 버튼 클릭.
3. 자동으로 Chromium 창이 열립니다. 첫 실행에서는 YouTube 에 **수동 로그인** 후
   브라우저 창을 닫으세요.
4. 프로그램이 쿠키를 수집해 Netscape 형식 `cookies.txt` 로 저장하고, 쿠키파일
   입력란에 자동으로 경로를 반영합니다.
5. 두 번째 실행부터는 로그인 상태가 프로필에 남아 있어 창을 닫기만 하면 갱신이
   자동으로 완료됩니다.

프로필 저장 위치:

- Windows: `%USERPROFILE%\.ytdlp-cookie-refresher\chromium-profile\`
- Linux/macOS: `~/.ytdlp-cookie-refresher/chromium-profile/`

개발자용 분리 스크립트(`scripts/refresh_cookies.py`)도 여전히 사용 가능합니다.
해당 경로는 Playwright 를 pip 로 직접 설치한 환경을 요구하며, cron 이나
Windows 작업 스케줄러에 `--auto` 모드로 등록해 서버용 무인 갱신에 활용할 수
있습니다.

> CLI 모드에서는 인증 메뉴 **"7. 쿠키 자동 갱신"** 항목이 동일한 흐름을
> 제공합니다.

### OAuth 는 지원하지 않습니다
yt-dlp 가 2024.11 에 공식적으로 OAuth2 지원을 제거했습니다 ([커밋 52c0ffe](https://github.com/yt-dlp/yt-dlp/commit/52c0ffe40ad6e8404d93296f575007b05b04c686)).
YouTube 가 TV device code flow 를 차단했기 때문이며, 복구 계획은 없습니다.
`--username` / `--password` 도 YouTube 추출기에서 구현되지 않았습니다.

## 라이브 스트림 다운로드

YouTube 라이브 URL 을 입력하면 자동으로 감지·최적화됩니다:

| 상태 | 자동 동작 |
|---|---|
| 🔴 `is_live` | 방송 시작 시점부터 캐치업 (DVR 윈도우 내), 받은 조각을 바로 이어 붙임 |
| ⏰ `is_upcoming` | 최대 60분 대기 후 시작 |
| 📼 `was_live` | 아카이브 일반 다운로드 |
| 📺 `not_live` | 일반 영상 처리 |

녹화는 `<제목>.f<번호>.mp4.part` 파일(영상·오디오 각각)에 쌓이고, 끝나면 하나의
MP4 로 합쳐집니다. 쓰는 중에는 Windows 탐색기가 이 파일을 **0 KB 로 표시할 수 있지만**
실제로는 커지고 있습니다.

**중간에 멈추면 자동으로 합칩니다.** 취소·종료 버튼, CLI 의 Ctrl+C, 진척 없음
워치독으로 녹화가 멈추면 그때까지 받은 부분(아직 이어 붙이지 못한 조각 포함)을
`<제목> (부분 녹화).mp4` 로 저장합니다. 종료 버튼은 합치기가 끝난 뒤 창을 닫습니다.
원본 `.part` 파일은 같은 주소를 다시 받을 때 이어받기에 쓰이므로 남겨 두며, 필요 없으면
지워도 됩니다. 직접 합치려면:
`ffmpeg -i 영상.part -i 오디오.part -map 0:v -map 1:a -c copy 결과.mp4`

방송 도중 멤버십 전용으로 바뀌는 등 방송 정보를 다시 받을 수 없게 되면, yt-dlp 는
방송이 끝난 것으로 보고 그 시점까지 받은 부분만 녹화합니다.

GUI "고급 / 라이브" 탭에서:
- 병렬 세그먼트 수 조정 (기본 1). 2 이상이면 더 빨리 따라잡지만, 녹화가 끝날 때까지
  조각이 `-FragN` 파일로만 쌓이고 `.part` 파일은 비어 있습니다 (중간에 멈추면 직접 이어 붙여야 함)
- 예정 라이브 최대 대기 시간
- MP4 재포장 on/off
- 자동 감지 on/off

**DVR 제약**: YouTube 라이브의 과거 세그먼트 보존은 보통 12시간입니다. 방송이
이보다 오래 진행됐다면 가장 이른 가용 세그먼트부터 시작됩니다 ("진짜 처음" 은
복구 불가).

## 배포본 빌드 (개발자용)

Windows 10+ 용 독립 실행 zip 을 생성합니다.

```powershell
cd build
.\build.ps1 -Clean
```

산출물: `build\dist\yt-dlp-downloader-<version>-win-x64.zip` (~260MB).
내부에 Python 런타임, yt-dlp, deno, Playwright + Chromium 이 모두 포함되어
있어 타겟 PC 에 추가 설치 없이 동작합니다.

파라미터:
- `-Clean` — 이전 산출물 제거
- `-SkipVenv` — 기존 `.buildvenv` 재사용 (빠른 재빌드)
- `-NoZip` — zip 생성 생략
- `-NoDeno` — deno.exe 번들 생략 (배포본 용량 ~100MB ↓)
- `-NoPlaywright` — Playwright + Chromium 번들 생략 (용량 ~140MB ↓, "쿠키 자동 갱신" 비활성)
- `-NoFfmpeg` — ffmpeg/ffprobe 번들 생략 (용량 ~35MB ↓, 라이브 TS→MP4 재포장 비활성)

자세한 내용은 [`build/README.md`](build/README.md) 참조.

## 쿠키 업데이트 오류 분류

`core.cookies.update_cookies()`는 실패 시 `error_kind`를 반환하여 UI가 적절한
안내를 할 수 있도록 합니다:

| `error_kind`          | 의미 / 해결                                                  |
| --------------------- | ------------------------------------------------------------ |
| `admin_context`       | Windows 관리자 권한에서는 Chrome/Edge 쿠키 복호화 불가. 일반 창에서 실행. |
| `browser_locked`      | 브라우저가 실행 중이라 쿠키 DB 접근 불가. 브라우저 종료 후 재시도. |
| `browser_not_found`   | 지정한 브라우저/프로필을 찾을 수 없음. 설치/프로필명 확인.    |
| `yt_dlp_missing`      | yt-dlp 미설치. `uv pip install yt-dlp`.                       |
| `other`               | 위 분류에 속하지 않는 기타 오류. stderr 끝 5줄 동봉.          |

## 크로스 플랫폼 주의사항

* 경로는 `pathlib.Path` 또는 `os.path.join`만 사용합니다. 하드코딩된 `\\`
  혹은 `/`는 없습니다.
* 브라우저 프로필 경로를 직접 탐색하지 않습니다. yt-dlp의
  `--cookies-from-browser`가 플랫폼별 기본 위치를 알아서 처리합니다.
* 관리자 권한 감지는 `sys.platform == "win32"`에서만 수행하며, 다른
  플랫폼에서는 항상 `False`를 반환합니다.
* `subprocess.run` 호출은 모두 `encoding="utf-8", errors="replace"`로
  통일하여 Windows 한글 콘솔에서도 안전합니다.

## 다운로드 옵션

### 화질
- 최고 화질 (bestvideo+bestaudio)
- 1080p / 720p / 480p
- MP3 / M4A (오디오 추출)

### 자막
- 없음 / 한국어 / 영어 / 모든 자막 / 자동 생성 자막 포함

### 오디오 트랙
여러 언어 오디오(더빙)가 있는 영상에서 받을 음성을 고릅니다.
- **한국어 우선 (기본)** / 원본 / 영어 우선 / 일본어 우선
- "우선"은 그 언어 트랙이 있으면 그것을, 없으면 원본 트랙을 받습니다.
  yt-dlp 기본값은 원본 트랙이라, 예전에는 한국어 더빙이 있어도 영어 원본을 받았습니다.
- YouTube 자동(AI) 더빙 트랙도 그 언어 트랙으로 취급됩니다. 원래 목소리가 필요하면 '원본'을 고르세요.
  화면해설(음성 설명) 트랙은 고르지 않습니다.
- 라이브 자동 감지가 켜져 있으면 로그에 `🔊 오디오 트랙 24개 — 한국어(ko) 선택`
  처럼 어떤 트랙을 받는지 표시됩니다.

### 파일명 형식
- 제목
- 채널명 - 제목
- 업로드 날짜 - 제목
- 제목 (ID)

동시에 받는 서로 다른 영상의 파일명이 같아지면(제목이 같은 경우 등) 나중에 시작한 쪽의 파일명에 ` (ID)` 를
붙여 구분하고 정보 탭에 알립니다. 같은 영상이 이미 받는 중이면(링크 모양이 달라도) 시작하지 않습니다.
영상 정보를 읽지 못했거나 라이브 자동 감지를 끈 경우에는 파일명을 미리 알 수 없어 확인하지 못하니,
제목이 같을 수 있는 영상을 함께 받을 때는 '제목 (ID)' 를 고르세요.

**긴 제목 (Linux · macOS)**: 파일 이름 하나는 255바이트(암호화된 홈 폴더는 143바이트)까지입니다. 한글은 글자당 3바이트라
100자에 가까운 한글 제목은 받는 중에 생기는 `<제목>.f299.mp4.part` 가 한도를 넘어
`ERROR: unable to open for writing: [Errno 36] File name too long` 으로 실패했습니다. 이제 저장 폴더의 한도를 읽어
이름이 넘칠 것 같으면 제목을 바이트 단위로 잘라 쓰고(`제목이 길어 파일명이 … 한도를 넘습니다` 로 정보 탭에 알림) 짧은 제목은
그대로 둡니다. Windows 는 글자 수로 세므로 바꾸지 않습니다. 영상 정보를 읽지 못한 경우에는 제목을 항상 한도 안으로 묶지만
`" | : ? /` 처럼 이름에서 3바이트로 늘어나는 글자가 많은 제목까지는 막지 못할 수 있습니다.

## 일괄 다운로드

URL 을 모아 둔 텍스트 파일을 넣으면 한 개씩 차례로 받아 분야별 폴더에 저장합니다.
GUI 는 **'일괄' 탭**(목록 파일 선택 → 일괄 다운로드 시작), CLI 는
`python main.py --batch <목록.txt>` 입니다.

목록 형식 — `#` 로 시작하는 줄이 분야 이름이고, 그 아래 URL 이 그 분야 폴더로 갑니다:

```
# 요리
https://youtu.be/-OnM0ABAYzM
https://youtu.be/-mqV2nO4XS4   URL 뒤에 적은 메모는 무시됩니다

# 투자
https://youtu.be/09BcoSNaxdk
https://www.youtube.com/watch?v=0QsCi1vFcuM
```

결과: `<저장 폴더>/요리/…`, `<저장 폴더>/투자/…`.

- 첫 `#` 줄보다 앞에 있는 URL 은 `미분류` 폴더에 저장됩니다.
- 화질·자막·오디오·저장 폴더·파일명·인증은 한 번 정한 설정을 모든 영상에 씁니다.
  GUI 는 기본 / 고급 탭 설정, CLI 는 처음에 한 번 묻습니다.
- 같은 영상(`youtu.be/ID`, `watch?v=ID`, `/live/ID`, `/shorts/ID` 가 같은 ID)이 여러 번 있으면
  한 번만 받습니다. URL 이 아닌 줄은 건너뛰고 줄 번호를 알려 줍니다.
- 봇 감지를 피하려고 영상 사이에 기본 5~15초(무작위) 쉽니다. GUI 는 '일괄' 탭의 "영상 사이 휴식(초)" 최소·최대,
  CLI 는 `--batch-pause MIN-MAX`(`5-15`, 하나의 수 `10`, `0` 은 쉬지 않음 — 봇 감지가 더 잘 걸립니다)로 정하며,
  600초를 넘거나 최소가 최대보다 크거나 숫자가 아니면 시작하지 않고 알립니다.
- GUI 에서는 일괄 전체가 '기본' 탭의 **다운로드 목록에 한 행**(`📚 일괄 3/12` 와 지금 받는 영상의 진행)으로
  보이고, 동시 다운로드 한도에 1건으로 셉니다(한도가 차 있으면 일괄도 대기열에서 차례를 기다립니다). 일괄을 하는 동안에도 기본 탭에서 다른 URL 을 동시에
  받을 수 있습니다(일괄이 지금 받는 영상과 같은 주소는 거부. 반대로 기본 탭에서 이미 받는 중인 영상이
  목록에 있어도 일괄은 건너뛰지 않습니다 — 일괄은 분야 폴더에 저장하므로 보통 파일이 겹치지 않습니다).
  일괄 탭의 [취소] 는 일괄만,
  목록의 [전체 취소] 는 모든 다운로드를 멈춥니다. 일괄은 한 번에 하나만 돌립니다.
- **방송 중이거나 예정된 라이브는 건너뜁니다**(녹화하면 몇 시간 동안 다음 영상이 막히므로).
  끝난 라이브는 일반 영상처럼 받습니다. 라이브를 녹화하려면 기본 탭에서 따로 받으세요.
- 건너뛴 라이브는 목록 옆 `<목록>.라이브대기.txt` 에 분야별로 모아 둡니다.
  방송 종료 후 GUI [라이브 대기 목록 열기] 또는 `python main.py --batch "<목록>.라이브대기.txt"`
  로 다시 받으세요. 성공하거나 이미 받은 항목은 대기 목록에서 빠지고, 취소·실패·미처리 항목은 유지됩니다.
  일부 목록만 다시 실행해도 기존 대기 항목은 유지하며, 모두 받으면 대기 파일은 지워집니다.
- **이어받기**: 받은 영상은 저장 폴더의 `.batch-archive.txt` 에 기록되어, 같은 목록을 다시
  실행하면 이미 받은 영상은 바로 건너뜁니다('이미 받음').
- 실패하거나 취소된 URL 은 목록 옆 `<목록>.남은목록.txt` 에 같은 형식으로 저장됩니다.
  그 파일로 다시 실행하면 남은 것만 받고, 다 받으면 파일은 지워집니다.
- 취소는 GUI 취소 버튼 또는 CLI Ctrl+C(두 번 누르면 바로 종료). 영상마다 '기록' 탭에도 남습니다.

## 다운로드 기록

다운로드할 때마다 (성공 · 실패 · 취소 · 건너뜀 모두) 일시, 제목, URL, 오디오 언어,
저장 위치가 기록됩니다. 최근 500건까지 남습니다.

- **GUI URL 입력란 ▼**: 최근 20개가 `제목 | URL` 로 나오고, 고르면 URL 이 입력됩니다.
- **GUI '기록' 탭**: 제목/URL 검색, 더블클릭 또는 [URL 입력란에 넣기],
  [브라우저로 열기], [폴더 열기], [로그 열기], [선택 삭제], [전체 삭제]. 기록을 지워도 받은 파일은 그대로입니다.
- **CLI**: URL 입력에서 `h` 를 치면 최근 10개 중에서 번호로 고릅니다.
- 저장 위치: `~/.ytdlp-downloader/history.json` (Windows 는
  `C:\Users\<사용자>\.ytdlp-downloader\history.json`). 다른 곳에 두려면 환경변수
  `YTDLP_HISTORY_FILE` 에 파일 경로를 지정하세요.

## 실행 로그

다운로드 한 번마다 로그 파일이 하나씩 저장됩니다. yt-dlp 가 출력한 내용 전부, 앱 메시지,
스트림별 진행 상황(30초마다 한 줄), 종료 코드와 결과가 들어 있어서, 창을 닫은 뒤에도
"왜 실패로 끝났는지" 를 확인할 수 있습니다. 쿠키 파일은 경로·크기·수정 시각만 적고 내용은 적지 않습니다.
일반 yt-dlp 경고(`WARNING:`)는 로그 파일에만 남깁니다. 라이브 녹화 중
`Cannot find refreshed url for format` 경고가 나오면 **⚠ 라이브 조기 종료 가능**을
정보 탭·CLI와 작업 목록에 표시합니다(일괄은 해당 영상 행에도 표시).
종료 코드가 0이어도 녹화 길이를 확인하세요. 기록에는 `⚠ 확인 필요`로 남으며,
다른 종류의 경고까지 조기 종료로 판정하지는 않습니다.

- 저장 위치: `~/.ytdlp-downloader/logs/<날짜-시각>_<영상ID>.log` (Windows 는
  `C:\Users\<사용자>\.ytdlp-downloader\logs`). 다른 곳에 두려면 환경변수 `YTDLP_LOG_DIR`.
- 최근 300개만 남기고 오래된 것은 지웁니다(일괄은 영상마다 하나씩 만듭니다)(이 앱이 만든 `날짜-시각_이름.log` 파일만 지웁니다).
  한 파일은 20MB 까지만 기록하고, 오류·종료 코드·결과 줄은 그 뒤에도 남깁니다.
- 정보 탭에는 끝날 때 로그 경로가 표시되고, 기록 탭의 [로그 열기] 로 열 수 있습니다.
  CLI 는 실패하거나 라이브 조기 종료 경고가 있을 때 로그 경로를 보여 줍니다.

### 실패 원인과 쿠키 상태

다운로드가 실패하면 yt-dlp 가 한 말로 원인을 가려 한 줄 안내합니다. 로그인 만료 / 봇 차단 /
HTTP 403·PO Token / 결제·멤버·비공개·연령·지역 제한 / 요청 제한 / 네트워크 끊김 / yt-dlp 실행 불가
(아래 [문제 해결](#no-module-named-yt_dlp-소스-모드-전용)). 쿠키를 다시 받으라고 권하는 것은
**yt-dlp 가 직접 로그인이 풀렸다고 한 경우뿐**입니다. 봇 차단과 403 은 쿠키 탓이 아닐 수 있어
PO Token 점검을 먼저 안내합니다. 원인은 실행 로그의 `failure kind:` 줄에 남습니다.

영상 받기를 시작한 직후(진행 줄이 1MiB 미만을 보고하는 동안) `HTTP Error 403` 으로 실패하면 10초 뒤 **한 번** 자동으로 다시 시도합니다.
받던 부분은 yt-dlp 가 이어받습니다. 이런 403 은 실제로 몇 분 뒤 다시 하면 받아지는 일시적인 것이 많았습니다 (5건 모두 3~48분 뒤 성공).
결과는 실행 로그에 `재시도 성공!` 또는 `재시도도 실패했습니다` 로 남습니다. 그 이상 받은 뒤의 403, 요청 제한·봇 차단이 함께 있는 경우,
취소한 경우에는 다시 시도하지 않습니다.

네트워크 끊김(`network`)은 yt-dlp 가 **재시도를 포기하고 실패한** 연결 오류(시간 초과, 연결 끊김, 덜 받음, 이름 조회 실패)만
셉니다. `[download] Got error: ... Retrying (3/30)...` 처럼 아직 재시도하는 줄은 실패가 아니라서 원인으로 보지 않고,
`Giving up after N retries` 만으로는 판단하지 않습니다 (라이브 주소 갱신 실패 경고도 그렇게 끝납니다). 다른 원인이 함께 보이면 그쪽이 우선입니다.
쿠키 건강도에는 세지 않습니다.

`HTTP Error 429: Too Many Requests` 도 YouTube 의 요청 제한으로 보고 "잠시 뒤 다시 시도" 를 안내합니다.

**자막을 받다가 실패하면 영상은 자막 없이라도 받습니다.** yt-dlp 는 자막 하나를 못 받으면 (429 가 흔한 원인이고 몇 분 뒤엔 되기도 합니다)
영상 전체를 중단합니다 (yt-dlp 의 설계: 자막 오류는 `ignoreerrors` 가 아니면 치명적). 앱은 이때 **자막 없이 영상만 한 번 다시 받고**
결과에 `⚠ 자막 없이 받음` 을 붙입니다 — 다운로드 목록·일괄 행과 끝 요약, 기록 탭의 `⚠ 확인 필요`, CLI 의 줄, 실행 로그의 `subtitles skipped`.
영상은 완료로 치지만 **자막 파일은 없습니다.** 자막이 필요하면 그 영상을 다시 받으세요 — 영상은 이미 있어 건너뛰고 자막만 받습니다.
**일괄에서는 이 영상을 보관 기록(`--download-archive`)에 올리지 않으므로** 같은 목록을 다시 실행하면 이 영상만 자막을 다시 받아 봅니다
(보관 기록에 올라간 영상은 자막 없이도 통째로 건너뛰기 때문입니다). 자막이 또 막히면 또 자막 없이 두고, 채워질 때까지 목록을 돌릴 때마다 시도합니다.
일괄의 끝 줄과 요약에 `자막 없이 받은 N개` 가 나옵니다. 자막 말고 영상 쪽 실패는 이 재시도를 하지 않습니다.
(재생목록 항목은 이 재시도가 보관 기록을 쓰지 않아, 보관 기록에 있지만 지워 버린 영상이 다시 받아질 수 있습니다.)

로그인 쿠키(`cookies.txt` 에 `LOGIN_INFO` 와 `SAPISID` 계열이 있는 것)를 쓰는 경우 로그인 만료를
`~/.ytdlp-downloader/cookie_health.json` 에 기록합니다 (`YTDLP_COOKIE_HEALTH_FILE` 로 변경, 서버에서는
`$XDG_STATE_HOME/ytdlp-bot/`). 로그인이 없는 쿠키 파일은 만료될 수 없으므로 기록하지 않습니다.
새로 로그인한 쿠키로 바꾸면 기록이 새로 시작됩니다.

만료를 처음 확인한 실행에서는 **GUI** 가 다운로드 목록 행에 `⚠ 쿠키 만료 — 다시 추출 필요` 를 붙이고 `쿠키 만료`
대화상자를 한 번 띄웁니다. **CLI**(`--cli`, `--batch`)는 안내 글을 냅니다 (일괄은 끝에서 한 번 더).
같은 만료는 한 번만 알리고 종료 코드는 바꾸지 않습니다. 메신저 알림은 아직 없습니다.

## 프로그래밍 API

다른 프로그램에서 import 해서 사용할 수 있습니다:

```python
from core.downloader import download, DownloadOptions
from core.auth import BgutilPoTokenAuth, CookieFileAuth

# PO Token 방식 (권장, 헤드리스)
auth = BgutilPoTokenAuth(provider_url="http://127.0.0.1:4416")
v = auth.validate()
if not v.success:
    raise RuntimeError(v.message)

opts = DownloadOptions(
    url="https://youtu.be/...",
    format_opt="bestvideo+bestaudio/best",
    output_template="downloads/%(title)s.%(ext)s",
    auth=auth,
    audio_lang="ko",  # 한국어 오디오 트랙 우선 (None = 원본 트랙)
)
ok = download(opts.url, opts)

# 쿠키 파일 방식
opts = DownloadOptions(
    url="https://youtu.be/...",
    format_opt="bestvideo+bestaudio/best",
    output_template="downloads/%(title)s.%(ext)s",
    auth=CookieFileAuth("cookies.txt"),
)
```

기존 `cookie_opts={"cookiefile": ...}` dict 형식도 내부에서 자동 변환되어
하위호환됩니다.

받은 파일의 제목과 최종 경로가 필요하면 `download()` 대신 `run_download()` 를 씁니다:

```python
from core.downloader import run_download

result = run_download(opts.url, opts)
if result.ok:
    for item in result.files:  # 영상별 id, title, filepath, language
        print(item["title"], item["filepath"], item.get("language"))
```

콜백을 지정하면 진행상황과 로그를 임의의 UI로 라우팅할 수 있습니다:

```python
download(url, opts,
         progress_callback=lambda info: print(info["bar"]),
         log_callback=lambda line, lvl: print(lvl, line))
```

## 텔레그램 봇

`python main.py --bot` 은 텔레그램 봇을 띄웁니다. 링크를 보내면 받고, 진행 상황을 한 메시지에서 계속 고쳐
보여 주고, 끝나면 저장 경로(또는 실패 이유)로 마무리합니다.

### 시작하기
1. 텔레그램에서 **@BotFather** 에게 `/newbot` 으로 봇을 만들고 토큰을 받습니다.
2. 토큰은 **환경변수 `YTDLP_BOT_TOKEN`** 에만 둡니다 (명령줄 인자나 파일에 적지 말고, 채팅·git 에 올리지 마세요).
   Docker 는 `docker/.env` (git 이 무시합니다).
3. `YTDLP_BOT_ALLOWED_CHATS` 는 비어 있으면 봇이 **시작하지 않습니다** (아무나 받게 하는 문을 열어 두지 않기 위해서).
   내 chat id 를 아직 모르면 우선 `0` 을 넣고 시작한 뒤 봇에게 아무 말이나 하세요. 목록에 없는 채팅에는
   `허용되지 않은 채팅입니다 (chat id: 123456789)` 라고 한 번 답하니, 그 id 로 바꿔 다시 시작하면 됩니다.
   그룹 채팅의 id 는 음수이고, 그룹을 허용하면 그 그룹의 모든 사람이 쓸 수 있습니다 (그룹에서 쓰는 법은 아래 [그룹에서 쓰려면](#그룹에서-쓰려면)).

### 쓰는 법
| 보내는 말 | 하는 일 |
|---|---|
| YouTube 링크 | 영상(최고 화질)을 받습니다. 한 메시지에 5개까지 |
| `/video <링크>` | 위와 같습니다. **그룹에서는 이 형태를 쓰세요** ([그룹에서 쓰려면](#그룹에서-쓰려면)) |
| `/audio <링크>` | 오디오만 MP3 로 받습니다 |
| `/status` | 받는 중·대기 중인 작업 |
| `/cancel [#번호]` | 작업 취소. 번호가 없으면 이 채팅의 전부. 받던 라이브는 지금까지 받은 부분을 합쳐 저장합니다 |
| `/help` | 사용법 |

한 번에 `YTDLP_BOT_MAX_PARALLEL`(기본 2)개만 받고 나머지는 대기열(`YTDLP_BOT_MAX_QUEUED`, 기본 20)에서 기다립니다.
같은 링크가 이미 받는 중·대기 중이면 다시 받지 않습니다. 파일명은 `제목 (영상ID).확장자` 라서 제목이 같은 영상이
서로를 "이미 받음" 으로 건너뛰지 않습니다. 저장 위치는 `YTDLP_OUTPUT_DIR` (기본 `downloads`).
쿠키 로그인이 만료된 것이 확인되면 허용된 모든 채팅에 **한 번** 알립니다.

**방송 중인 라이브 링크를 보내면 방송이 끝날 때까지(또는 `/cancel` 할 때까지) 자리 하나를 차지합니다.** 기본 2자리면
라이브 두 개로 모든 자리가 찰 수 있습니다. 데이터가 들어오지 않는 녹화는 `YTDLP_STALL_MINUTES`(기본 5분)가 멈춰 줍니다.

### 그룹에서 쓰려면
텔레그램의 **privacy mode** 때문에, 그룹에 들어간 봇은 기본으로 일반 메시지(링크만 보낸 글)를 받지 못하고 명령어와 봇에게 단
답장 같은 것만 받습니다. 그래서 그룹에서는:
* `/video <링크>` 또는 `/audio <링크>` 로 요청하세요. 그룹에 봇이 여럿이면 `/video@봇이름 <링크>` 처럼 이름을 붙이면 이 봇만 응답합니다
  (다른 봇을 가리킨 명령은 무시합니다).
* 링크만 보내도 받게 하려면 @BotFather 에서 `/setprivacy` → **Disable** 한 뒤 **봇을 그룹에서 내보냈다가 다시 추가**해야 적용됩니다
  (텔레그램 공식 문서). 이 경우 봇은 그룹의 모든 메시지를 보게 됩니다.
* 그룹 id 는 음수이고, 그 id 를 `YTDLP_BOT_ALLOWED_CHATS` 에 넣으면 **그룹의 모든 사람**이 쓸 수 있습니다. 개인 채팅은 이 제한이 없습니다.

### 안전 장치
* **허용 목록이 없으면 시작하지 않습니다.** 목록에 없는 채팅은 무시하고, 처음 한 번만 chat id 를 알려 줍니다.
* **YouTube 링크만 받습니다** (`youtube.com`, `youtu.be`, `youtube-nocookie.com`). 다른 사이트는
  `YTDLP_BOT_ALLOWED_HOSTS` 로 열 수 있습니다 (`*` 는 전부). 서버 안쪽 주소를 건드리는 링크를 막기 위한 기본값입니다.
* 토큰은 로그·오류 메시지·`repr` 어디에도 나오지 않습니다.
* 텔레그램 쪽 문제(수정 실패, 요청 제한)로 다운로드가 실패하지는 않습니다.
* **다운로드가 어떻게 끝났는지(완료·실패·취소)와 쿠키 만료 알림은 연결이 불안정해 못 보내도 버리지 않고** 5→10→20→40→60초 간격으로
  계속 다시 보냅니다 (연결 끊김·요청 제한·텔레그램 서버 오류만. 봇 차단·채팅 없음 같은 거부는 다시 보내지 않고, 최대 1시간, 봇을 끌 때 마지막으로 한 번 더).
  진행률 갱신과 `/status` 같은 답은 다시 보내지 않습니다. 보내다 시간 초과된 메시지가 실제로는 도착했다면 같은 메시지가 두 번 보일 수 있습니다.

### 환경변수
`YTDLP_BOT_TOKEN`(필수), `YTDLP_BOT_ALLOWED_CHATS`(필수), `YTDLP_BOT_ALLOWED_HOSTS`, `YTDLP_BOT_MAX_PARALLEL`(1–5),
`YTDLP_BOT_MAX_QUEUED`(1–100), `YTDLP_AUDIO_LANG`(기본 원본 트랙, `ko` 처럼 언어 코드를 주면 그 언어 트랙을 먼저 고름 — 외국어 영상이면 YouTube 의 자동 더빙을 받는다) 와, 기존의 `YTDLP_AUTH_METHOD`,
`YTDLP_COOKIE_FILE`, `YTDLP_GETPOT_BGUTIL_BASEURL`, `YTDLP_OUTPUT_DIR`, `YTDLP_FRAGMENT_RETRIES`, `YTDLP_STALL_MINUTES`.
값이 잘못되면 어느 변수가 문제인지 알리고 시작하지 않습니다 (종료 코드 2).

시작하면 콘솔에 **실행 중인 버전**(`version: 0.1.0 (커밋)`, 소스 실행에서 고친 파일이 있으면 `-dirty`)과 **`YTDLP_*` 환경변수**를 먼저 찍습니다.
토큰·비밀 값(이름에 `TOKEN`·`SECRET`·`KEY`·`PASSWORD` 가 든 것)은 `<set>`, 허용 채팅은 개수(`<2 chat id(s)>`)만 보이고,
다른 환경변수(PATH 등)는 찍지 않습니다. 설정이 잘못돼 시작하지 못할 때도 이 줄들이 먼저 나옵니다. 이름을 잘못 쓴 변수도 그대로 보여 오타를 알아볼 수 있습니다.

### 한계
* **받은 파일을 텔레그램으로 돌려주지는 않습니다.** 답장에는 봇이 도는 컴퓨터의 저장 경로가 나옵니다.
* **전사(Whisper)는 아직 없습니다.**
* **연결이 1시간 넘게 끊기거나 봇을 껐을 때 아직 못 보낸 끝 메시지는 사라집니다** (받은 파일은 저장돼 있습니다).
* **재시작하면 대기열이 사라집니다.** 받던 것은 취소되고 (라이브는 받은 부분을 합쳐 저장), 기다리던 것은 사라집니다.
* 봇이 꺼져 있는 동안 보낸 메시지는 켜질 때 처리됩니다. 텔레그램은 받지 못한 메시지를 **24시간**까지만 보관하고(공식 문서),
  그보다 오래된 것은 사라집니다. 24시간 안에 다시 켜면 그동안 보낸 요청이 한꺼번에 시작됩니다.
* 봇의 메시지를 받으려면 한 곳에서만 폴링해야 합니다. 같은 봇을 다른 곳에서 돌리고 있거나 웹훅이 설정돼 있으면 시작할 때
  텔레그램이 거절하고(`409`), 봇은 이유를 알리고 종료 코드 2 로 끝납니다. 웹훅을 **지우지는 않습니다** (남의 것일 수 있어서).
* **실제 텔레그램에 연결해 시험한 적은 없습니다.** 테스트는 로컬 가짜 서버로 했습니다.

## 진단 도구

문제가 발생하면 진단 스크립트를 실행하세요:

```powershell
python diagnose.py
```

Python/플랫폼, yt-dlp 설치, cookies.txt 존재, deno, YouTube 연결,
테스트 다운로드를 순서대로 점검합니다.

## 일반적인 문제 해결

### "관리자 권한에서 실행 중" 경고
Chromium 계열 브라우저는 DPAPI 보안 컨텍스트 때문에 관리자 프로세스에서
쿠키를 복호화할 수 없습니다. 관리자 권한이 아닌 일반 PowerShell/CMD에서
실행하세요.

### "봇 감지" 에러
1. 브라우저에서 YouTube에 로그인되어 있는지 확인
2. "쿠키 업데이트" 실행
3. 다른 브라우저 시도 (Chrome → Firefox → Edge)

`core.downloader.download()`는 첫 시도에서 봇 감지가 나고 쿠키가 설정되지
않은 경우 자동으로 Chrome 쿠키로 한 번 재시도합니다.

### "No module named 'yt_dlp'" (소스 모드 전용)
```powershell
uv pip install yt-dlp
```
배포본(.exe) 은 yt-dlp 를 내장하므로 이 오류가 뜨지 않습니다.

설치했는데도 이 오류가 나면, 소스로 실행할 때 앱이 **PATH 에서 `yt-dlp` 를 먼저 찾는다**는 점을 의심하세요 (`where yt-dlp`).
예전 파이썬이 남긴 고장난 `yt-dlp.exe` 가 앞에 있으면 (2026-10-07 에 `Python39\Scripts\yt-dlp.exe` 로 실제로 겪었습니다) 같은
오류가 납니다. 그 파일을 지우거나 옮기거나, 이번 실행에만 PATH 를 바꾸세요. 앱은 이 경우를 `yt_dlp_missing` 으로 분류해
같은 안내를 직접 보여 줍니다 (GUI·CLI 의 정보 줄, 텔레그램 봇의 실패 답).

## 향후 계획

* **Phase C**: 메신저 알림 훅 (쿠키 만료 감지와 GUI·CLI 표시는 구현됨, `TODO.md` 참조)
* **Phase D'**: `core/transcriber.py` 에 Whisper 백엔드 구현
  (faster-whisper / whisper.cpp / onnx-ryzen)
* **Phase E**: 텔레그램 봇은 구현됨 (받은 파일 돌려주기·전사 연동은 아직, `TODO.md` 참조). 다른 메신저(Discord / KakaoTalk)는 미정
* 최종 목표: 메신저로 받은 URL 을 Docker 컨테이너에서 다운로드 → 전사 → 응답하는
  완전 자동화 파이프라인

## 라이센스

개인 사용 목적으로만 사용하세요.
