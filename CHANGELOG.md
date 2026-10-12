# 변경 이력

본 프로젝트는 단일 파일 스크립트에서 모듈화된 GUI/CLI/봇 아키텍처로
재구성되었습니다. 아래는 Phase 별 주요 변경사항 요약입니다.

---

## Phase B — 모듈화 리팩터 (완료)

단일 `downloader.py` (839줄, GUI + CLI + 다운로드 로직 혼재) 를 분리.

| 영역 | 결과 |
|---|---|
| 순수 로직 | `core/cookies.py`, `core/downloader.py`, `core/transcriber.py` — tkinter/input/print 의존 없음 |
| 사용자 대면 | `interfaces/gui.py` (tkinter), `interfaces/cli.py` (대화형), `interfaces/bot.py` (메신저 봇 스텁) |
| 진입점 | `main.py` — `--gui` / `--cli` / `--bot` 디스패처 |
| 하위호환 | `downloader.py` — 기존 `python downloader.py [--cli]` 명령 그대로 동작하는 shim |
| 진단 | `diagnose.py` — `core.cookies` 재사용 |

핵심 버그 픽스: 존재하지 않던 `--print-cookies` yt-dlp 옵션 제거, 정식
`--cookies-from-browser` + `--cookies` 조합으로 교체.

---

## Phase D — 인증 추상화 + bgutil PO Token (완료)

YouTube 인증이 OAuth2 (yt-dlp 가 2024.11 제거), 쿠키 (만료/잠금 이슈),
PO Token (2026 현실적 필수) 3가지 상황으로 나뉘는 현실에 맞춰 전략 패턴 도입.

| 변경 | 파일 |
|---|---|
| `Authenticator` 프로토콜 + 4 구현 | `core/auth.py` (신규) |
| → `CookieFileAuth`, `BrowserCookieAuth`, `BgutilPoTokenAuth`, `NoAuth` | |
| `DownloadOptions.auth` 필드 도입 + 기존 `cookie_opts` dict 자동 변환 | `core/downloader.py` |
| GUI 인증 방식 라디오 (4택) + bgutil 연결 테스트 | `interfaces/gui.py` |
| CLI 인증 메뉴 6택 (PO Token 옵션 추가) | `interfaces/cli.py` |
| 봇 환경변수 기반 설정 (`YTDLP_AUTH_METHOD`, `YTDLP_GETPOT_BGUTIL_BASEURL`) | `interfaces/bot.py` |
| Docker compose 사이드카 배포 | `docker/docker-compose.yml`, `docker/Dockerfile.downloader`, `docker/README.md` |

재시도 경로 수정: 사용자가 명시적으로 고른 Authenticator 는 봇 감지 재시도 시에도
덮어쓰지 않음 (기존에는 모든 실패에서 Chrome 쿠키로 강제 재시도).

---

## Phase Build — Windows 10+ 배포 빌드 시스템 (완료)

PyInstaller 기반 one-folder 배포. Python/conda/pip 없는 시스템에서 실행 가능.

| 구성 | 내용 |
|---|---|
| `build/build.ps1` | PowerShell 5.1 호환. 격리된 venv → PyInstaller → zip 패키징 |
| `build/build.bat` | 더블클릭용 CMD 래퍼 |
| `build/yt-dlp-gui.spec` | GUI exe (console=False), yt-dlp extractor 전체 번들 |
| `build/yt-dlp-cli.spec` | CLI exe (console=True), 동일 구성 |
| `build/gui_entry.py` / `build/cli_entry.py` | `sys.argv` 에 `--gui`/`--cli` 자동 주입 |
| `build/README.md` | 빌드 가이드, AV 오탐 대응, PyInstaller 주의사항 |
| `VERSION`, `.gitignore` | 버전 관리 + 빌드 캐시 제외 |

### 번들 포함 바이너리
프레임 과정에서 발견된 "PyInstaller + 파이썬 의존 도구" 호환성 문제를 해결:

| 도구 | 번들 방식 | 이유 |
|---|---|---|
| **yt-dlp.exe** | GitHub 공식 standalone 빌드 (`.yt-dlp-cache/`) | pip setuptools launcher 는 venv python.exe 에 의존하여 단독 번들 불가 |
| **deno.exe** | GitHub 공식 zip (`.deno-cache/`) | yt-dlp `--remote-components ejs:github` JS challenge 해결기 |
| **Playwright + Chromium** | `.playwright-cache/` → `_MEIPASS/playwright-browsers/` | GUI "쿠키 자동 갱신" 버튼 구현 |

런타임 `core.cookies.bundled_runtime_dir()` 이 `sys._MEIPASS` 를 자식 프로세스의
`PATH` 에 prepend, `PYTHONIOENCODING=utf-8` 주입 (Windows 한글 파일명 mojibake 방지).

### 버그 픽스 (빌드 과정에서 발견)
- `--specpath` 옵션은 `.spec` 파일과 함께 쓸 수 없음 → `Push-Location` 방식으로 교체
- `cookies.txt` 템플릿에 UTF-8 BOM 삽입 (`Set-Content -Encoding UTF8` 부작용) → ASCII + `WriteAllBytes` 로 교체
- PyInstaller frozen 모드에서 `sys.executable` == 앱 자체 → `yt_dlp_command()` 가 번들 exe 우선 탐색하도록 수정
- 번들된 yt-dlp 에 출력이 없을 때 명확한 안내 (deno 미설치 가능성 포함)
- GUI 로그 영역이 창 리사이즈 시 따라오지 않던 문제 → `main_frame.rowconfigure(13, weight=1)`

---

## Phase F — 라이브 스트림 감지 + 캐치업 (완료)

YouTube 라이브 URL 을 자동 감지하여 `--live-from-start --concurrent-fragments 16
--hls-use-mpegts --fragment-retries infinite --remux-video mp4` 등을 자동 적용.
진행 중인 방송을 중간에 시작해도 DVR 윈도우 내에서 처음부터 병렬 캐치업.

| 파일 | 역할 |
|---|---|
| `core/live.py` (신규) | `LiveStatusInfo`, `LiveOptions`, `detect_live_status()` |
| `core/downloader.py` | `DownloadOptions.live_opts` + `_build_cmd` 라이브 블록 + 프로브 |
| `interfaces/gui.py` | `ttk.Notebook` 3 탭 (기본/고급·라이브/정보) + 취소 버튼 |
| `build/build.ps1` + specs | ffmpeg.exe + ffprobe.exe 번들 (BtbN Windows build) |

배포본 zip +35MB (약 295MB 총). `-NoFfmpeg` 빌드 플래그로 제외 가능.

---

## Phase C-4 — 쿠키 자동 갱신 (완료)

Chrome 127+ 의 app-bound encryption + DB 잠금 ([yt-dlp #7271](https://github.com/yt-dlp/yt-dlp/issues/7271))
때문에 `--cookies-from-browser chrome` 신뢰성이 낮아진 현실에 대응.

**설계**: Playwright 로 전용 브라우저 프로필을 유지 → `context.cookies()` API 로
직접 추출 → Netscape 포맷으로 저장. 확장 프로그램 UI 자동화나 Chrome DB
복호화가 필요 없음.

| 파일 | 역할 |
|---|---|
| `core/refresher.py` (신규) | `refresh_cookies()` 동기 래퍼, `playwright_available()`, frozen 모드 `PLAYWRIGHT_BROWSERS_PATH` 자동 설정 |
| `scripts/refresh_cookies.py` | 개발자용 CLI 래퍼 (thin) — cron / 작업 스케줄러 등록용 |
| `scripts/requirements.txt`, `scripts/README.md` | 소스 모드 설치 가이드 |
| `interfaces/gui.py` | "쿠키 자동 갱신" 버튼 추가 (in-process Playwright 호출) |
| `interfaces/cli.py` | 인증 메뉴 7번 추가 |

### 배포본 내장
main 빌드에 Playwright + Chromium 이 포함되어 Python 없는 타겟 PC 에서도
GUI 버튼 한 번으로 동작. 최종 zip 크기 ~260MB.

### RPA 대안 검토 결과 (참고)
| 후보 | 결론 |
|---|---|
| Power Automate Desktop | 가능하나 Shadow DOM/좌표 의존으로 취약 |
| AutoHotkey | Windows 전용, JSON→Netscape 변환 별도 |
| UiPath Community | 설치 2GB+, 상용 라이선스 제약 |
| Claude Computer Use API | 주기 호출 시 월 $20~30 비용 비효율 |
| Claude Playwright MCP | Claude Code 세션 밖 실행 불가 |
| **Playwright (채택)** | API 레벨에서 쿠키 저장소 직접 접근, 가장 안정적 |

---

## Phase H — 오디오 트랙 선택 + 다운로드 기록 (완료, 2026-09-25)

### 오디오 트랙 선택
한국어 더빙이 있는데도 영어로 받아지던 문제 대응. yt-dlp 는 오디오 트랙을
원본 → YouTube 기본 트랙 → 나머지 순으로 고르고, 프리셋의 `bestaudio[ext=m4a]`
에는 언어 조건이 없어 영어 원본 + 한국어 더빙 영상은 영어로 받아졌다.

| 변경 | 파일 |
|---|---|
| `prefer_audio_language()` — 오디오 선택자에 `[language^=ko][language!$=-desc]` 를 붙인 대안을 앞에 (더빙이 Opus 뿐이어도 H.264 영상 유지, 화면해설 트랙 제외), 기존 체인은 폴백으로 뒤에 | `core/presets.py` |
| `DownloadOptions.audio_lang` (None = 원본, 기존 동작) → `-f` 에 적용 | `core/downloader.py` |
| 라이브 감지 조회 결과로 `🔊 오디오 트랙 N개 — 한국어(ko) 선택` 로그 (추가 요청 없음) | `core/downloader.py` |
| GUI '오디오:' 행, CLI 오디오 메뉴 — 한국어 우선(기본) / 원본 / 영어 우선 / 일본어 우선 | `interfaces/gui.py`, `interfaces/cli.py` |

확인: 오디오 24개 영상(원본 `en-US`)에서 720p 기존 `136+140-23 | en-US` →
한국어 우선 `136+140-11 | ko`. 한국어 없는 영상은 기존과 동일(`133+140`).

### 다운로드 기록
이전에 받은 영상을 다시 찾고 URL 을 재사용할 수 있게 모든 다운로드(성공·실패·취소)를 기록.

| 변경 | 파일 |
|---|---|
| `--print "after_move:[ytdlp-gui-result] %(.{id,title,filepath,language,playlist_title})j" --no-quiet` 로 영상별 제목·최종 경로·오디오 언어를 stdout 에서 수집 (진행률 출력 유지, ASCII JSON 이라 코드페이지 무관, 임시 파일 없음) | `core/downloader.py` |
| `DownloadResult` + `run_download()`; 기존 `download()` 는 `run_download().ok` 래퍼로 호환 유지 | `core/downloader.py` |
| `HistoryStore` — `~/.ytdlp-downloader/history.json` (`YTDLP_HISTORY_FILE`), 최신 500건, 원자적 저장(고유 임시 파일 + fsync + 교체 재시도), 손상·비 UTF-8 파일은 `.bak` 로 옮기고 새로 시작, 읽기 오류 때는 덮어쓰지 않음 | `core/history.py` (신규) |
| URL 입력란 ▼ 최근 20개 (마우스 휠로 입력한 URL 이 바뀌지 않게 막음), '기록' 탭 (검색 · URL 재사용 · 브라우저 · 폴더 열기 · 선택/전체 삭제) | `interfaces/gui.py` |
| URL 입력에서 `h` → 최근 10개 중 선택, 다운로드 후 기록 저장 | `interfaces/cli.py` |

---

## Phase I — 일괄 다운로드 + 분야별 폴더 (완료, 2026-09-28)

URL 을 모아 둔 텍스트 파일(`내youtube.txt`, 119개)을 한 번에 받아 분야별로 정리하려는 요구.
분야는 목록 안에 `# 분야` 줄로 적는다(자동 분류보다 정확하고 추가 비용 없음).

| 변경 | 파일 |
|---|---|
| 목록 파싱(`# 분야`, 메모, 중복 ID 제거, 잘못된 줄 보고, UTF-8/BOM/cp949), 분야 폴더명 정리(Windows 금지 문자·예약어, `%` 이스케이프), `run_batch()` — 영상별 `run_download`, 5~15초 휴식, 취소, `<목록>.남은목록.txt` | `core/batch.py` (신규) |
| `DownloadOptions.download_archive` → `--download-archive`; 목록은 `<저장 폴더>/.batch-archive.txt` 를 공유해 다시 실행하면 받은 영상을 yt-dlp 호출 없이 건너뜀 | `core/downloader.py`, `core/batch.py` |
| `run_download(skip_live=True)` — 기존 라이브 감지 한 번으로 방송 중·예정 라이브를 건너뜀; `DownloadResult.skipped` (`live` / `archived`), 기록 상태 `skipped`('건너뜀') | `core/downloader.py`, `core/history.py` |
| GUI '일괄' 탭 — 목록 파일 선택, 분야·제목·상태 목록, 시작/취소, 폴더 열기; 단일 다운로드와 같은 취소·종료 처리 | `interfaces/gui.py` |
| `main.py --batch FILE` — 설정을 한 번 묻고 진행, `[3/119] 요리 ✓ 제목` 출력, Ctrl+C 는 남은 목록 저장 후 종료(두 번이면 즉시) | `main.py`, `interfaces/cli.py` |

확인: 실제 YouTube 로 2개 분야·2개 영상 목록 → 분야 폴더에 저장, 다시 실행 시
'이미 받음 2', 다운로드 도중 Ctrl+C → 즉시 멈추고 남은 3개를 `남은목록.txt` 에 저장
(yt-dlp 프로세스 남지 않음).

**2026-10-06 — GUI 일괄을 기본 탭 다운로드 목록에 통합** (Phase J 와 병합): 일괄 전체가 목록의
한 행(`📚 일괄 3/12` + 지금 받는 영상의 진행)으로 보이고 동시 다운로드 한도에 1건으로 센다.
일괄은 한 번에 하나만 돌린다. 일괄 탭 [취소] 는 일괄만, 목록의 [전체 취소] 는 전부 멈춘다.
영상 하나가 끝나면 그 영상의 URL·진행 정보를 바로 비워, 다음 영상을 기다리는 5~15초 동안에도
같은 주소를 기본 탭에서 받을 수 있고 행이 "멈춤" 으로 보이지 않는다. 일괄은 기본 탭에서 이미 받는
영상을 건너뛰지 않는다(분야 폴더에 저장하므로 보통 파일이 겹치지 않음). 중간에 예외가 난 영상도
실행 로그 경로를 남겨 기록 탭 [로그 열기] 로 열 수 있다.

---

## Phase J — 기본 탭 다운로드 목록 + 동시 다운로드 + 실행 로그 (완료, 2026-10-05)

받는 동안 기본 탭에서는 상태를 알 수 없었고(진행 로그는 '정보' 탭에 듬성듬성 찍힘), 라이브가 "실패" 로
끝나도 원인을 확인할 기록이 남지 않았다. 상태를 기본 탭으로 올리고, 다운로드마다 로그를 파일로 남겼다.

| 변경 | 파일 |
|---|---|
| 기본 탭 상단 "다운로드 목록" — 작업별 상태·받은 양·속도·경과·마지막 수신, 1초 갱신. 60초 넘게 받은 양이 안 변하면 ⚠, [선택 취소] / [전체 취소] / [완료 항목 지우기]. 완료 항목은 20개까지 유지 | `interfaces/gui.py` |
| 동시 다운로드(기본 3, 고급 탭에서 1~5). 같은 영상은 링크 모양이 달라도(`youtu.be`, `watch?v=`, `/live/`, `/shorts/`, 추적 파라미터) 한 번만 받음. 창을 닫으면 모든 작업을 취소하고 부분 병합이 끝나길 기다림 | `interfaces/gui.py` |
| 작업 표(`DownloadJob`, `JobTable`)와 행 포맷을 Tk 없이 분리 — 단위 테스트 | `interfaces/gui_jobs.py` (신규), `tests/test_gui_jobs.py` |
| [다운로드 시작] 버튼을 기본 탭으로 이동(URL 입력창에서 Enter 로도 시작). 정보 탭은 로그만. 창 크기 800×720 | `interfaces/gui.py` |
| 다운로드마다 실행 로그 `~/.ytdlp-downloader/logs/<시각>_<영상ID 또는 호스트>.log` (`$YTDLP_LOG_DIR` 로 변경, 최근 300개, 파일당 20MB). yt-dlp 출력 전체 + 종료 코드 + 결과 + 환경 요약(쿠키 파일은 경로·크기·수정 시각만, 내용 없음). `--no-warnings` 를 빼서 경고를 로그 파일에만 남김. 진행 줄은 스트림당 30초에 한 번 | `core/run_log.py` (신규), `core/downloader.py` |
| 기록 항목에 `log_file`, 기록 탭 [로그 열기], CLI 는 실패 시 `실행 로그:` 경로 출력 | `core/history.py`, `interfaces/gui.py`, `interfaces/cli.py` |
| `run_download` 를 얇은 래퍼 + `_run_download` 로 나눔(로그 생성·콜백 감싸기·결과에 `log_path`) | `core/downloader.py` |

### 실행 로그로 밝혀 고친 두 가지 (2026-10-05, 실제 라이브 2건)

- **방송 끝에서 "실패" 로 찍히던 정상 녹화**: 방송이 끝나면 마지막 조각 요청이 비어 yt-dlp 가 `ERROR: Did not get any data blocks` 를 내고 그 조각을 건너뛰지만, 병합·이동까지 마치고도 종료 코드를 1 로 남긴다(`downloader/http.py` 의 `report_error`). 1시간 14분을 받은 녹화가 이렇게 "실패" 가 됐다. 이제 오류가 그것 하나뿐이고, 건너뛴 조각이 있고, 시작한 모든 영상의 최종 파일이 있고, 종료 코드가 1 일 때만 완료로 처리하고 ⚠ 로 건너뛴 조각을 알린다. 다른 오류가 하나라도 있거나 파일이 없으면 그대로 실패. (`core.downloader._skipped_fragments`, `tests/test_end_of_stream.py` 에 실제 로그 줄 사용)
- **방송이 막 끝난(post_live) 영상이 거의 멈춘 듯 받던 문제**: 조각이 초 단위로 잘게 나뉜 이 영상에 `--http-chunk-size` 가 붙으면 서버가 범위 요청을 ~20초씩 붙잡아 조각당 ~40초(68분에 21MB)가 걸렸다. post_live 에서는 이 옵션을 빼고 받는다. 완전히 끝난 뒤(`was_live`)의 영상은 일반 영상 경로로 12MiB/s 로 정상. GUI 는 "⏳ 종료된 라이브 (YouTube 가 영상을 처리 중)" 로 구분해 표시.

### 알려진 제약

- 한도를 넘는 시작은 대기열에 넣지 않고 안내 후 거부한다. → 2026-10-06 에 대기열로 바꿈(아래).
- 동시 다운로드는 YouTube 로 가는 요청 수를 곱하므로 봇 감지에 더 민감하다(기본 상한 3).
- 제목이 같은 서로 다른 영상은 파일명 옵션이 "제목(ID)" 가 아니면 같은 파일명이 될 수 있다(같은 URL 만 거부). → 2026-10-06 에 해결(아래).
- 당시 조기 종료 관련 경고는 로그 파일에만 남았음. 2026-10-06부터 라이브 주소 갱신 실패 경고를 GUI에도 표시한다 (아래 후속 처리 참조).

확인: 전체 테스트 212개 통과(2개 건너뜀), 화면 없이 GUI 를 mainloop 아래에서 구동해 목록·취소·일괄 행 확인, 실제 yt-dlp 로 단일·일괄 E2E, 실제 라이브 로그를 되돌려 재생해 종료 코드 1 처리 확인.

---

## 사소한 개선 (상시)

- 개발자 전용 conda 환경명 (`ryzen-ai-1.6.1`) 을 사용자 대면 메시지에서 제거.
  배포본 GUI/CLI 시작 로그 / 트러블슈팅에서 conda 활성화 강요 문구 삭제.
  README.md / QUICKSTART.txt 는 "작성자 개인 NPU 환경" 으로 주석 처리.
- subprocess 호출 시 Windows 콘솔 플래시 방지 (`CREATE_NO_WINDOW`).
- 쿠키 업데이트 실패 시 stderr / stdout / returncode / 실행 명령을 모두 로그에
  노출하여 "(other)" 미분류 이슈 진단 가능.
- **2026-04-26 라이브 녹화 무한 루프 픽스**: 라이브 catchup 시 `core/downloader.py`
  가 `--fragment-retries infinite` 를 주입하던 것을 `10` 으로 변경. 방송 종료 후
  존재하지 않는 다음 fragment 를 영원히 재시도하며 프로세스가 빠져나오지 못하던
  사고에 대응. 향후 사용자 설정/워치독 고도화는 TODO.md "Phase F-2" 참조.
- **2026-05-01 pip → uv 마이그레이션**: `build.ps1` 의 `python -m venv` / `pip install`
  호출을 `uv venv` / `uv pip install` 로 교체, `.python-version` (3.12) 추가. README /
  QUICKSTART 의 사용자 설치 절차도 `uv` 기준으로 갱신. `pyproject.toml` 도입은 보류
  (현재 의존성이 단순하고 PyInstaller 가 `.python-version` + `uv` 만으로 충분).
- **2026-05-01 PyInstaller 단일 spec 통합 + 빌드 dedup**: 별도였던 `yt-dlp-gui.spec`
  / `yt-dlp-cli.spec` 두 spec 을 `yt-dlp.spec` 한 파일로 통합하고 `MERGE()` 로 두 .exe
  가 하나의 `_internal/` 을 공유하도록 변경. PyInstaller 호출 1회, `Copy-Item` 머지
  로직 제거. **빌드 중간 산출물 디스크 사용량 4.2GB → 1.4GB**, 빌드 시간 약 40~50%
  단축 (분석/수집 1회). 옛 두 spec 은 이제 미사용 — 새 빌드 검증 후 삭제 가능.
- **2026-05-01 bgutil PO Token 플러그인 번들링 픽스**: 빌드된 GUI 에서 "PO Token
  (bgutil)" 선택 시 `bgutil_plugin_missing` 으로 실패하던 문제 해결. 두 레이어로
  나뉜 원인을 모두 처리: (1) `yt-dlp.spec` 에 `collect_all('yt_dlp_plugins')` +
  hidden_imports 추가 — 래퍼(yt-dlp-gui/cli.exe) 의 `importlib.find_spec` probe 가
  통과하도록. (2) `build.ps1` 에 plugin tree 미러링 단계 추가 —
  `<dist>/_internal/yt-dlp-plugins/bgutil-pot-provider/yt_dlp_plugins/` 로 복사하여
  standalone `yt-dlp.exe` 가 런타임에 발견할 수 있도록. `yt_dlp_plugins` 가 별도
  namespace package 라 기존 `collect_all('yt_dlp')` 가 놓치는 함정이었음.
- **2026-05-13 Phase F-2 End-of-stream 워치독 구현**: DASH 라이브 방송 종료 시
  manifest 가 `@type` 를 `dynamic`→`static` 으로 전환하지 않고 끝나 yt-dlp 가
  manifest 재폴링 + HTTPS 타임아웃 (`googlevideo.com Read timed out`) 루프에
  갇히던 문제 대응 (관찰: 1시간 30분간 .part 파일 7.8MB 만 추가, yt-dlp 미종료).
  `core/downloader.py` 에 `StallWatchdog` 데몬 스레드 도입: progress 콜백이
  N분간 호출되지 않으면 `process.terminate()` 송신. N은
  `LiveOptions.stall_minutes` (기본 5분, 0=비활성, `core/live.py`). 활성 조건은
  `opts.live_opts.effective` 이라 VOD 경로 영향 없음. 워치독 발화 시 한 줄
  안내 로그 출력. 기존 `--fragment-retries 10` 은 HLS fragment-단위 재시도에만
  적용되어 DASH 의 manifest-poll 루프를 못 끊었던 한계 보완. GUI/CLI 노출은
  다음 이터레이션 보류 (사용 패턴 관찰 후 결정).
- **2026-05-02 한글 파일명 mojibake 픽스**: standalone yt-dlp.exe 가 `PYTHONIOENCODING=utf-8`
  / `PYTHONUTF8=1` 를 설정해도 자체 Windows-specific stdio 래퍼(yt_dlp.compat) 로
  cp949 / 시스템 MBCS 바이트를 stdout 에 emit. 우리 측에서 `encoding='utf-8',
  errors='replace'` 로 텍스트 디코드하던 탓에 한국어 부분이 모두 U+FFFD 로 치환되어
  `���� ī��` 같이 보이던 문제. `core/downloader.py::_run_once` 의 stdout 캡처를
  바이너리 모드로 변경하고 `_decode_yt_dlp_line()` 헬퍼로 **UTF-8 시도 → 실패 시
  Windows 한정 MBCS 폴백 → 마지막 UTF-8 replace** 의 3-단계 디코드 적용. cp949 입력 +
  UTF-8 입력 + ASCII 모두 정상 처리 검증 완료. 다른 subprocess 호출(live/auth/cookies)
  은 `--dump-json` / `--version` 등 ASCII-escape JSON 또는 ASCII 출력이라 영향 없음.
- **2026-05-02 다운로드 로그 노이즈 정리 + 라이브 옵션 안내**:
  (1) **Progress 라인 이중 출력 제거**: yt-dlp 가 chunk 전환 시 emit 하는
  `[download] X% of YYY at Unknown B/s ETA Unknown` 줄이 strict regex 매칭 실패로
  일반 로그로 빠지던 문제. `ProgressDisplay._PROGRESS_HINT_RE` (loose detector) +
  `is_progress_line()` 메서드 추가, `_run_once` 에서 progress-ish 라인은 silent
  drop. 다운로드 한 번당 ~50% 로그 라인 감소.
  (2) **GUI throttle 버그 수정** ([interfaces/gui.py](interfaces/gui.py)): 기존
  `int(pct) % 10 == 0` 체크가 [10.0, 11.0) / [20.0, 21.0) / ... 구간을 모두
  통과시켜 10% 당 ~10 줄을 출력. 마지막 10%-bucket 추적으로 변경, 10% 교차마다
  정확히 1 줄.
  (3) **라이브 옵션 비적용 알림** ([core/downloader.py](core/downloader.py)):
  사용자가 `concurrent_fragments` 등 라이브 탭 옵션을 디폴트와 다르게 설정했는데
  URL 이 `was_live`/`not_live` 로 분류되어 archive/VOD 경로로 가면 한 줄로
  "(라이브 옵션 [...] 은 archive/VOD 영상에는 적용되지 않습니다)" 안내. 디폴트
  유지 시엔 침묵.
- **2026-05-01 SABR 후반 truncation 대응 (포맷 디폴트 + retries)**: PO Token 으로
  1차 SABR 차단은 풀렸으나 일부 영상에서 f251 (Opus) 후반 ~9% 에서 "X bytes read,
  Y more expected" 로 끊기는 second-stage throttling 관찰. 두 측면으로 보강:
  (1) GUI / CLI 의 화질 디폴트 6 종을 모두 `bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]`
  우선 + 기존 표현으로 폴백하도록 변경. AV1 (399) / Opus (251) 대신 H.264 (137 등) /
  AAC (140) 로 가서 throttling 회피. AAC 오디오는 이 변경으로 풀스트림 통과 확인.
  (2) `core/downloader.py` 공통 인자에 `--http-chunk-size 10M` (yt-dlp 기본값으로
  명시) + `--retries 30` (기본 10 에서 상향). 초기 시도였던 `--http-chunk-size 1M`
  은 단일 다운로드에서 요청 수가 ~10 배로 증가 → anti-bot 트리거가 오히려 빨리
  걸리는 현상이 관찰되어 기본값으로 복원. retries 30 은 일시적 CDN throttling 이
  통상 30~60 초 안에 풀리는 것을 흡수하기 위함.
- **2026-09-24 진행률 실시간 표시 + 워치독 오작동 픽스**: 파이프 출력에서
  yt-dlp 가 진행률을 `\r` 로만 구분해 다운로드가 끝날 때까지 진행률이 전달되지
  않던 문제. `--newline` 추가, progress 템플릿에 추정 크기·받은 용량 필드 추가,
  파서가 `N/A` / `Unknown` / `~추정` / 1시간 이상 ETA / 동시 스트림(`1:`/`2:`)
  을 처리. 워치독은 받은 용량이 늘어날 때만 진척으로 판단하도록 변경 — 기존에는
  라이브 녹화가 정상이어도 `stall_minutes` 후 종료될 수 있었음. GUI 는 크기 미상
  라이브에서 30초마다 받은 용량을 표시.
- **2026-09-24 GUI 취소 버튼 실동작**: 취소 버튼이 항상 비활성이고 눌러도 플래그만
  세우던 문제. `download(cancel_event=...)` 로 yt-dlp 프로세스 트리를
  `taskkill /T` 로 종료 (onefile yt-dlp.exe 자식 + ffmpeg 포함, 워치독·Ctrl+C
  에도 적용). 다운로드 중 시작 버튼 비활성(중복 실행 방지), 창 닫기 시 확인 후
  종료. GUI 도움말의 `--fragment-retries infinite` 문구를 `10` 으로 정정.
- **2026-09-24 Docker 빌드 컨텍스트·compose 정리**: 루트 `.dockerignore` 신설 —
  기존에는 `COPY . /app` 이 `build/`·`ffmpeg/` (약 3.9GB) 와 `cookies.txt`
  (로그인 세션) 까지 이미지에 담았음. 컨텍스트 304KB 로 축소 확인. compose:
  bgutil 이미지에 wget/curl 이 없어 항상 실패하던 healthcheck 를 `node fetch`
  로 교체, `depends_on` 을 `service_healthy` 로, 미구현 봇이 무한 재시작하지
  않도록 downloader `restart: "no"`, 폐기된 `version` 키 제거.
- **2026-09-24 단위 테스트 도입** (`tests/`, 표준 라이브러리 `unittest`):
  `ProgressDisplay.parse_progress` (실제 yt-dlp 출력 샘플 + 설치된 yt-dlp 로
  템플릿 왕복 검증) 와 `_build_cmd` (오디오/자막/인증/라이브 플래그). 실행:
  `python -m unittest`. `pyproject.toml` 은 기존 결정대로 보류.
- **2026-09-24 번들 yt-dlp.exe 자동 갱신**: `build.ps1` 이 `build/.yt-dlp-cache`
  에 파일이 있으면 무조건 재사용해, 배포본이 5개월 전 yt-dlp (2026.03.17) 로
  빌드되던 문제. 캐시된 exe 버전을 방금 venv 에 설치한 최신 yt-dlp 와 비교해
  더 오래됐거나 버전을 읽을 수 없으면 다시 받도록 변경. deno / ffmpeg 캐시는
  YouTube 변경에 덜 민감하여 기존대로 수동 갱신.
- **2026-09-24 중복 코드 정리** (동작 변경 없음, 아래 1건 제외):
  GUI/CLI 가 각자 들고 있던 화질·오디오·자막·파일명 표를 `core/presets.py` 로
  통합. yt-dlp 자식 프로세스 환경(UTF-8, 번들 PATH, 인증 env) 과 콘솔 창 숨김
  플래그를 `core.cookies.yt_dlp_env()` / `no_window_kwargs()` 로 통합해
  downloader / live / cookies / auth 가 공유. DPAPI 관리자 제약 브라우저 목록을
  `CHROMIUM_BROWSERS` 하나로, UTF-8 콘솔 설정을 `main.ensure_utf8_stdio()`
  하나로 통합. `_resolve_auth` 제거, `DownloadOptions` 재조립을
  `dataclasses.replace` 로 대체, auth.py 미사용 import 정리.
  **동작 변경**: `update_cookies()` 만 번들 PATH 주입이 빠져 있던 것을 통일
  (배포본 쿠키 업데이트에서도 번들 deno 사용), bgutil 버전 확인도 같은 환경 +
  콘솔 창 숨김 적용. GUI/CLI 모든 선택지 조합 234건의 결과를 리팩터 전후로
  비교해 위 1건 외 동일함을 확인.
- **2026-09-24 bgutil 주소 전달 버그 픽스** (Ubuntu 테스트에서 발견): 앱은 bgutil
  주소를 `YTDLP_GETPOT_BGUTIL_BASEURL` 환경변수로만 넘겼으나 플러그인 2.0.0 은
  환경변수를 읽지 않고 기본값 `127.0.0.1:4416` 으로 접속. Windows 에서는 기본값이
  우연히 맞아 드러나지 않았고, GUI 의 Provider URL 입력도 사실상 무시되고 있었음.
  `--extractor-args youtubepot-bgutilhttp:base_url=<URL>` 로 전달하도록 변경
  (라이브 감지 프로브에도 적용). 봇/compose 의 `YTDLP_GETPOT_BGUTIL_BASEURL` 은
  봇 설정 입력으로 유지. `localhost:4416` 에서 토큰 발급, `4417` 에서 해당 주소
  접속 실패를 확인해 인자가 실제로 쓰이는 것을 검증.
- **2026-09-24 Docker 이미지에 deno 추가**: `JS runtimes: none` 경고 해소
  (`COPY --from=denoland/deno:bin`). 이미지 빌드 후 `JS runtimes: deno-2.9.7` 확인.
- **2026-09-24 Linux 취소/워치독 프로세스 그룹 종료**: yt-dlp 를 새 세션으로
  시작하고 `os.killpg` (SIGTERM → 5초 후 SIGKILL) 로 ffmpeg 등 자식까지 종료.
  WSL 에서 취소 2.3초 내 자식 종료 확인 (기존 방식은 고아 자식이 파이프를 잡아
  약 2분간 멈춤).
- **2026-09-24 GUI 버전 표시**: 창 제목과 시작 로그에 `v0.1.0 (커밋)` 표시.
  `core/version.py` — 소스 실행 시 `VERSION` + `git describe --always --dirty`,
  배포본은 `build.ps1` 이 쓰고 spec 이 번들한 `_app_version.txt` 를 읽음.
- `.gitignore` 에 `.venv/`, `build/_app_version.txt` 추가. `docker/README.md` 의
  환경변수 설명 정정 (플러그인은 환경변수를 읽지 않음, CLI/GUI 는 직접 입력).
- **2026-09-24 Docker 이미지 의존성 고정** (Ubuntu 패치에서 채택): `yt-dlp[default]`
  로 설치해 `yt-dlp-ejs` (JS 챌린지 풀이 스크립트, yt-dlp 버전에 맞춰 고정) 를
  포함 — 실행 시 GitHub 에서 받아오지 않음. deno 는 `denoland/deno:bin-2.9.7`
  로 고정. 이미지 빌드 후 deno 2.9.7 / yt-dlp-ejs 0.8.0 / 컨테이너 내 추출 성공 확인.
- **2026-09-25 yt-dlp 미설치 Python 조기 감지** (Ubuntu 테스트에서 발견): 시스템
  `python3 main.py` 로 GUI 를 띄우면 다운로드 시점에야 `No module named yt_dlp`
  로 실패. `main.py` 가 시작 시 `<python> -m yt_dlp` 폴백인데 모듈이 없으면
  프로젝트 `.venv` 인터프리터(없으면 pip 설치 명령)를 안내하고 종료. 배포본과
  PATH 의 standalone yt-dlp 는 검사 대상 아님. 배포본은 `sys.frozen` 으로 먼저
  건너뜀 — yt-dlp.exe 가 없을 때 `yt_dlp_command()` 의 `FileNotFoundError` 가
  시작 시점에 터져 창 모드 exe 가 조용히 종료되던 문제 방지.
- **2026-09-25 yt-dlp.exe 누락 시 CLI traceback 픽스**: `_run_once` 의
  `_build_cmd()` 가 `FileNotFoundError` 처리 범위 밖이라, 배포본 CLI 에서
  yt-dlp.exe 가 없으면 traceback 으로 종료. 이제 GUI 와 같이
  `yt-dlp not found: …` 오류 로그 후 다운로드 실패로 끝남.
- **2026-09-28 라이브 캐치업 속도·안전성 픽스** (exe 로 라이브 녹화 중 발견: `.part`
  조각 파일 400여 개가 쌓였다가 한꺼번에 사라지고, 누적 파일이 0 KB 로 보임):
  - 원인 1 — `--http-chunk-size 10M` 이 라이브 조각에도 붙어, 조각마다 서버가 범위
    요청을 ~20초씩 붙잡음. 같은 설정 재현 0.35 조각/초 → 옵션 제거 시 3.2, 병렬 1 이면
    11.9 조각/초(오디오). 이제 VOD 에만 적용.
  - 원인 2 — 병렬 2 이상이면 yt-dlp(2026.08)가 끝없는 라이브 조각 목록을
    `Executor.map` 에 넘겨, 목록이 닫힐 때까지 아무것도 이어 붙이지 않고 `-FragN`
    파일로만 쌓음. 방송이 멤버십 전용으로 바뀌어 목록 갱신이 실패하자 목록이 닫히며
    한꺼번에 이어 붙여짐(조각이 "사라진" 순간). 중간에 멈추면 `.part` 가 빈 채로 남는
    구조. 라이브 기본 병렬을 16 → 1 로 바꿔 받는 즉시 이어 붙이게 함 (1080p 영상+오디오
    60초에 영상 약 5분·오디오 약 22분 분량, 취소 후에도 두 `.part` 를 ffmpeg 로 합칠 수 있음).
  - 누적 파일은 쓰는 중 탐색기에 0 KB 로 보일 수 있음(NTFS 표시 지연) — 라이브 시작 시
    로그로 안내. 병렬 2 이상을 고르면 위 동작을 경고.
- **2026-09-28 멈춘 라이브 녹화 자동 머지** (`core/partial_merge.py`): yt-dlp 는 녹화가
  스스로 끝날 때만 영상·오디오를 합치므로, 취소·종료·Ctrl+C·워치독으로 멈추면
  `.part` 만 남던 문제. 이제 그때까지 받은 부분을 `<제목> (부분 녹화).mp4` 로 ffmpeg
  remux. 아직 이어 붙이지 못한 `-FragN` 조각도 포함(병렬 2 이상에서 `.part` 가 0 바이트여도
  실제 1,246 조각으로 영상 3:56 복구 확인). 최종 파일명과 포맷 ID 는
  `--print "video:… %(.{filename,format_id})j"` 로 받음 — `[download] Destination:` 줄은
  콘솔 코드페이지 때문에 ⧸ ＂ 같은 문자가 깨짐. 포맷 ID 에 맞는 `.part` 만 합치므로 같은
  제목으로 전에 다른 화질로 받다 남은 `.part` 는 섞이지 않음. 원본 `.part` 는 이어받기용으로
  남김. 합치기는 조각 복사까지 20분 안에 끝나지 않으면 포기하고, GUI 종료 버튼은 합치기가
  끝난 뒤 창을 닫음(그 전에 닫혀 ffmpeg·임시 파일이 남지 않도록). 합치는 중 Ctrl+C 로
  멈추면 반쯤 쓴 결과 파일은 지움.

---

## 2026-10-06 — 라이브 후속 처리와 일괄 목록 다시 열기

- **Linux 의 "File name too long" 수정** — 사용자 Ubuntu 로그: `ERROR: unable to open for writing: [Errno 36] File name too long:
  '/home/user/Downloads/이번 주택은 … #풀하우스.f299.mp4.part'`. 제목이 98자(248바이트)인 한글이라 받는 중의 `.f299.mp4.part`
  까지 262바이트가 되어 Linux 의 파일 이름 한도 255바이트를 넘었다 (완성본 `.mp4` 는 252바이트라 들어가서 더 눈에 띄지 않았다).
  yt-dlp 는 기본으로 이름을 줄이지 않고 `--trim-filenames` 는 글자 수 기준이라 한글에 맞지 않는다. 템플릿의 바이트 단위 `B` 변환
  (`%(title).150B`)을 쓰도록 `core/filename_limit.py` 를 추가했다: 저장 폴더의 한도(`PC_NAME_MAX`, 없는 폴더는 가장 가까운 상위
  폴더, 암호화된 홈 143)에서 yt-dlp 가 이름 뒤에 붙이는 것(`.f251-drc.webm.part-Frag12345` 등 최대 약 36바이트)과 부분 녹화 합치기의
  접미사(28)·글자 치환 여유를 뺀 만큼만 제목에 준다. 라이브 감지 조회로 영상 정보를 알면 이름이 실제로 넘칠 때만 줄이고(짧은 제목은
  그대로, 한글 이름이 정보 탭에 `제목이 길어 …` 로 알려짐), 모르면 항상 묶는다. `" * : < > ? | / \` 는 yt-dlp 가 3바이트 전각으로
  바꾸므로 그 크기까지 계산해 잘린 뒤의 이름이 한도 안에 들게 한다. 파일명 충돌 방지(`output_claims`)도 잘린 이름 기준으로 비교하고,
  ` (ID)` 가 붙으면 다시 맞춘다. Windows 는 글자 수·전체 경로 기준이라 바꾸지 않는다.
  검증: 테스트 32개(`tests/test_filename_limit.py`) — 사용자 로그의 제목을 그대로 쓴 경우, 실제 yt-dlp 2026.08.19 의
  `prepare_filename` 으로 4가지 파일명 형식 × 한도 255/143 × 한글·일본어·이모지·특수문자 제목에서 이름 + 접미사가 한도 안인지
  확인 (yt-dlp 가 없으면 건너뜀. 고치지 않으면 307건 실패). **사용자 영상(`D1WknJLGrIc`)의 제목은 클라우드에서 YouTube 접속이 막혀 가져오지
  못했고, 실제 Ubuntu 에서 그 영상을 다시 받아 보는 확인은 아직 못 했다.**
- **실행 로그 문구 집계 스크립트** — `scripts/collect_log_phrases.py`. 로그 폴더를 읽기만 해 실행 결과·실패 원인 분류 개수와
  yt-dlp 의 `ERROR`·`WARNING` 줄(과 방송 종료·재시도 같은 정보 줄)을 ID·주소·숫자를 가린 형태별로 묶은 표(횟수, 성공/실패 로그,
  라이브 여부, 지금 분류기의 해석)와 분류되지 않은 실패를 Markdown/JSON 으로 낸다. TODO 의 C-2 "오류 문구를 실제 로그 샘플로
  확정"과 F-2 "다른 조기 종료 경고"를 사용자 PC 의 실제 로그로 정리하기 위한 도구다. 예시 줄은 주소 쿼리·긴 토큰·사용자 폴더 이름을
  가린다. 표준 라이브러리만 사용. **실제 로그는 클라우드에 없어 `RunLog` 로 만든 로그로만 시험했다 (`tests/test_collect_log_phrases.py`
  33개)** — 항목 자체는 실제 결과를 받아야 닫힌다.
- **일괄의 영상 사이 휴식 시간 조절 (Phase I 후속)** — 5~15초로 고정이던 휴식을 GUI '일괄' 탭의 "영상 사이 휴식(초)" 최소·최대
  입력란과 CLI `--batch-pause MIN-MAX`(`--batch` 와 함께)로 정한다. 값 규칙은 `core.batch.parse_pause`/`check_pause` 한 곳:
  0 ≤ 최소 ≤ 최대 ≤ 600초만 받아 오타가 조용히 휴식을 없애지 않게 하고, `run_batch` 도 잘못된 쌍은 다운로드 전에
  `ValueError`. 시작 로그에 "영상 사이 휴식: …" 을 남기고, 0~0 은 쉬지 않음(봇 감지 위험). 테스트 25개
  (`tests/test_batch_pause.py`, `tests/test_cli_batch.py`; GUI 입력란과 시작 흐름은 실제 Tk 창).
- **진척 없음 자동 정지 시간 조절 (Phase F-2 후속)** — `LiveOptions.stall_minutes`(기본 5, 0 = 끔)를 GUI 고급/라이브 탭
  "진척 없음 자동 정지(분)" 입력란, CLI `--stall-minutes N`(`--cli`·`--batch`), 봇 환경변수 `YTDLP_STALL_MINUTES` 에서 정한다.
  값 규칙은 `core.live.parse_stall_minutes` 한 곳: 0 이상 1440 이하의 정수만 받아, 초와 헷갈린 큰 수나 오타가 조용히
  워치독을 끄지 않게 한다 (`fragment_retries` 와 같은 방식). "무한" 재시도를 고르면 GUI 의 빨간 안내가 지금의 정지 시간을
  보여 주고, 자동 정지가 꺼져 있으면 멈추지 않을 수 있다는 경고를 낸다. 바꾼 값은 녹화 시작 때 실행 로그에 남는다.
  테스트 32개 (`tests/test_stall_minutes.py`, GUI 입력란은 실제 Tk 창).
- **동시 다운로드 한도를 넘으면 대기열 (Phase J 후속)** — 한도(기본 3)를 넘는 시작을 거부하지 않고 목록에
  `대기 중 (N번째)` 로 올렸다가 자리가 나면 먼저 넣은 순서대로 자동 시작한다 (`JobTable.promote`). 단일 다운로드와
  일괄 모두 해당하고(일괄은 한 번에 하나이므로 대기 중인 일괄이 있으면 새 일괄은 거부), 대기는 최대 30개. 대기 중에 취소하면
  바로 "취소 (대기 중 취소)" 가 되고 시작하지 않는다. 같은 주소가 이미 대기열에 있으면 거부한다. 고급 탭에서 한도를
  올리면 빈 자리만큼 바로 시작한다. 창을 닫는 중에는 대기 중인 것을 시작하지 않는다. [다운로드 시작] 버튼은 한도에서도
  꺼지지 않는다. 시작 시점의 설정(화질·저장 폴더 등)을 눌렀을 때 기준으로 기억해 두었다가 쓴다. 대기가 끝나 시작하면
  경과 시간은 그때부터 센다. 단위 테스트(`tests/test_gui_jobs.py` 에 큐 규칙, `tests/test_gui_queue.py` 에 실제 Tk 창
  구동 10개 — 가짜 스레드로 시작·종료 시점을 직접 정함).
- **동시 다운로드의 파일명 충돌 방지 (Phase J 후속)** — `core/output_claims.py`. 제목이 같은 서로 다른 영상을
  함께 받으면 둘 다 `<제목>.f137.mp4.part` 를 쓰므로 뒤의 것이 앞의 반쪽 파일을 이어받거나 덮어쓴다. `run_download` 가
  라이브 감지 조회로 얻은 영상 정보(`id`·`title`·`channel` 등)로 출력 경로를 미리 계산해 실행 중인 다운로드끼리 등록하고,
  겹치면 나중 것만 `제목 (ID).확장자` 로 저장한다(로그에 알림). 같은 영상이 이미 받는 중이면(YouTube 링크 모양이 달라도)
  `DownloadResult.skipped="busy"` 로 시작하지 않는다(GUI 행은 "실패 (이미 받는 중)"; 일괄 안의 항목이면 실패로 세어 남은 목록에 남는다).
  파일명은 yt-dlp 가 아니라 조회 결과로 계산한 값이라 한계가 있다: 조회에 실패했거나 라이브 감지를 끈 실행은 확인하지
  못하고(다른 다운로드가 돌고 있으면 경고만 남김), yt-dlp 가 자체 문자 치환으로 같게 만드는 이름은 보지 못한다.
  일괄은 분야 폴더에 저장하므로 단일 다운로드와는 보통 겹치지 않는다. 이전에 이미 끝난 파일과의 이름 충돌은 다루지 않는다.
  GUI 의 "제목이 같으면 '제목(ID)' 로 하세요" 시작 안내를 없애고 고급 탭 설명을 바꿨다. 단위 테스트 30개
  (`tests/test_output_claims.py`: 두 스레드로 `run_download` 를 동시에 돌려 이름 변경·중복 거부·해제 확인).
- 라이브 녹화 중 `Cannot find refreshed url for format` 경고를 결과의 `warnings` 에 보관하고,
  GUI 단일·일괄 작업 목록과 CLI 에 `⚠ 라이브 조기 종료 가능` 표시. 성공 종료라도 기록에
  `⚠ 확인 필요`로 남긴다. 일반 경고는 계속 실행 로그에만 저장한다.
- 일괄에서 건너뛴 라이브는 `<목록>.라이브대기.txt` 에 분야별로 보관한다. 다시 실행하면
  성공·이미 받은 항목만 제거하고, 취소·실패·미처리 항목과 다른 실행에서 남긴 URL 은 유지한다.
  남은 목록과 라이브 대기 목록은 임시 파일을 쓴 뒤 교체해 기존 파일의 중간 손상을 방지한다.
- `--gui --batch FILE` 은 일괄 탭에 목록만 미리 연다. [라이브 대기 목록 열기]로 저장된 목록을
  다시 열 수 있다. 시작 전에 파일을 다시 읽으며, 읽기 실패 시 이전 목록으로 잘못 시작하지 않는다.
- 쿠키 건강도 계획에서 봇 차단과 인증 만료를 분리하고, 복구는 같은 쿠키의 인증 성공으로만 판단하도록 정리.
  재시도 계획의 무한값은 내부 `None`, CLI·환경변수 `infinite`로 명시.
- **실패 원인 분류 (Phase C-2 일부)** — `core/failure.py`. 실패한 실행의 ERROR·WARNING 줄로
  인증 만료 / 봇 차단 / PO Token(HTTP 403) / 접근 권한 / 요청 제한 / 기타를 가른다.
  `DownloadResult.failure_kind` 와 실행 로그의 `failure kind:` 줄에 남기고, 원인에 맞는 안내 한 줄을 띄운다.
  쿠키 만료로 세는 것은 yt-dlp 가 직접 "cookies are no longer valid" 라고 한 경우뿐이다 (봇 차단·403·
  접근 권한은 세지 않음). 기존 `_is_bot_error` 는 "both" 속의 "bot" 과 "detected" 가 함께 있어도
  Chrome 쿠키 재시도를 일으켰는데, 단어 경계로 좁힌 분류기로 대체했다.
- **쿠키 건강도 추적 (Phase C-1)** — `core/cookie_health.py`. 확인된 로그인 만료를 `cookie_health.json` 에
  기록한다 (실패 횟수, 마지막 성공·실패 시각, 원인, 최근 50건의 변경 기록). "같은 쿠키" 는 로그인 쿠키값의
  해시로 가린다 — yt-dlp 가 매 실행 뒤 `cookies.txt` 를 다시 쓰기 때문이다. `DownloadOptions.cookie_health` 로
  넣은 경우에만 기록하고(GUI·CLI 는 넣음, 일괄은 이어받음), 처음 감지와 새 로그인으로의 교체는
  `DownloadResult.cookie_event` 로 알려 알림이 한 번씩만 나가게 한다. 로그인이 없는 쿠키 파일은 추적하지 않는다.
  `cookies.txt` 의 expires 열 점검 함수(`login_cookie_expires_at`)도 있으나 아직 쓰는 곳은 없다.
  확인된 성공(`mark_valid`)을 부르는 곳은 아직 없다. 알림(C-3)은 아직이다.
- **라이브 조각 재시도 횟수 조절 (Phase F-2)** — `LiveOptions.fragment_retries`(기본 10, `None`=무한).
  GUI 고급/라이브 탭의 입력란 + "무한" 체크박스, CLI `--fragment-retries N|infinite`(`--cli`·`--batch`),
  봇 환경변수 `YTDLP_FRAGMENT_RETRIES`. `0` 은 재시도 안 함이고 무한은 별도 선택이라 0 이 무한으로 넘어가지
  않는다. 음수·소수·기타 문자열은 거부한다 (봇은 시작하지 않고 오류로 끝남). 무한인데 진척 없음 감시가
  꺼져 있으면 녹화 시작 때 경고한다.
- **다운로드 목록 열 너비** — 실사용 확인(방송 중 라이브 + 방송 직후 영상)에서 기본 창에 열이 잘리는 것을 확인해
  수정. 열 너비를 실제 행 내용에 맞추고 기본 창을 1000px 로 키웠으며, 창 폭에 맞춰 열이 나눠 갖는다(좁아져도 오른쪽 열
  유지). 그래도 잘리는 칸(긴 제목, 다섯 항목 일괄 결과)은 마우스를 올리면 툴팁으로 전체가 보인다.
- **Phase J 실사용 확인** — 방송 중 라이브 + 방송 직후 영상을 실제로 받아 일괄 한 행 표시와 동시 녹화를 확인했다.
  방송 직후 영상은 조각 약 2.3개/초(1.6~1.8MB/s)로 10-05 의 조각당 ~40초에서 정상 속도가 되었다.

검증: 전체 단위 테스트 및 숨긴 Tk 창의 실제 위젯 테스트. 일괄 행·동시 녹화·방송 직후 영상은 위 실사용 확인에서
실제 방송으로 확인했고, 로그인 쿠키와 방송이 끝나는 순간의 동작은 미검증.
분류에 쓴 오류 문구는 실제 실행 로그에서 확인한 것이 아니라 yt-dlp 소스와 기억에 의존한다
(테스트가 샘플마다 출처를 적어 둠).

## 2026-10-07 — GUI 에 쿠키 만료 경고

- **쿠키 만료를 기본 탭에 표시** — 쿠키 건강도(C-1)는 만료를 처음 확인한 실행에만 `DownloadResult.cookie_event` 를 `"invalid"` 로
  달아 주지만, GUI 는 그것을 읽지 않아 정보 탭에 `⚠️ 쿠키 만료로 기록했습니다` 한 줄이 로그 사이에 묻힐 뿐이었다. 이제 (1) 다운로드 목록
  행의 상태에 `⚠ 쿠키 만료 — 다시 추출 필요` 를 붙이고(일괄은 일괄 탭 행·목록 행·끝 요약), (2) `쿠키 만료` 대화상자를 한 번 띄워
  [쿠키 자동 갱신] / [쿠키 업데이트] 로 다시 받으라고 안내한다. 같은 만료는 한 번만 알리고(이미 기록된 만료는 `cookie_event` 가
  비어 있다), 대화상자가 열려 있는 동안 또 만료가 확인돼도 겹쳐 띄우지 않으며, 창을 닫는 중에는 띄우지 않는다. 새 로그인으로 쿠키를
  바꾼 첫 실행은 행에 `새 쿠키 확인됨` 만 붙이고 대화상자는 없다. 문구는 `DownloadResult.cookie_summary`.
  검증: `tests/test_gui_cookie_alert.py` 11개 — 가짜 결과 10개와, 진짜 `run_download`·진짜 건강도 저장소에 yt-dlp 만 흉내 낸 실행
  1개(첫 만료는 행과 대화상자에 나타나고 두 번째 실패는 다시 알리지 않음). 구현을 빼면 9개가 실패한다. 실제 창(`mainloop`)에서
  가짜 만료 결과를 넣어 행 문구가 칸에 잘리지 않고 대화상자 문구가 읽히는 것까지 눈으로 확인했다. **CLI 는 아직 보여 주지 않고, 실제 로그인
  쿠키가 만료되는 실제 실행으로는 확인하지 못했다.**
- **CLI 도 쿠키 만료를 표시** — `main.py --cli`(대화형)와 `--batch` 가 `DownloadResult.cookie_event == "invalid"` 일 때 코어의 `⚠️` 로그 줄과
  별개로 눈에 띄는 안내 4줄을 낸다: 만료 사실, 로그인이 필요한 영상은 못 받고 공개 영상은 받아질 수 있다는 것, 다음 실행의 인증 설정에서 7번
  (쿠키 자동 갱신) 또는 1~3번(브라우저에서 쿠키 업데이트)으로 다시 받으라는 것, 브라우저 쿠키라면 그 브라우저에서 다시 로그인하라는 것.
  단일 실행은 끝난 직후, 일괄은 만료를 확인한 영상 바로 뒤에 내고 끝의 `결과:` 아래에 한 줄(`이번 실행 중 쿠키 만료가 확인되었습니다 (N/M 번째)`)
  로 한 번 더 알린다 — 긴 일괄은 처음 안내가 스크롤로 사라진다. 같은 만료는 한 번만 알린다(`cookie_event` 가 한 번만 달리므로). `새 쿠키 확인됨`
  (`"recovered"`)은 코어가 이미 로그로 알려서 CLI 는 따로 내지 않는다. **종료 코드는 그대로** — 공개 영상만 받아 성공한 실행은 쿠키가 만료됐어도 0 이다.
  검증: `tests/test_cli_cookie_alert.py` 6개 — 가짜 결과와, 진짜 `run_download`·진짜 건강도 저장소에 yt-dlp 만 흉내 낸 실행 1개(첫 만료만 알림).
  구현을 빼면 4개가 실패한다(나머지 2개는 "아무것도 안 나온다" 확인). 실제 출력 모양도 확인했다. 실제 로그인 쿠키가 만료되는 실제 실행으로는
  아직 못 봤다.

- **네트워크 끊김을 실패 원인으로 분류** — `FailureKind.NETWORK`(`network`). 시간 초과(`timed out`)·덜 받음(`bytes read`)·연결 끊김·이름 조회
  실패 문구가 있는 ERROR/WARNING 줄이면 안내 한 줄("인터넷 연결을 확인하고 다시 시도, 쿠키 문제는 아닌 것으로 보입니다")을 내고, 쿠키 건강도에는 세지 않으며
  Chrome 쿠키 재시도도 하지 않는다. 실제 로그(2026-10-05)에는 이 오류가 `[download] Got error: ...Read timed out.. Retrying (N/30)...` 같은
  **재시도 중인 줄**로만 있어(한 실행에서 233회) 이 줄은 분류 대상이 아니다 — ERROR 가 아니라 일반 출력이고, 분류기에 들어가도 `retrying` 이 있으면
  원인으로 세지 않는다. 분류하는 것은 yt-dlp 가 **포기한** 줄이며 그 모양은 yt-dlp 소스에서 읽었고 실제 로그에는 아직 없다. 가장 약한 판정이라 다른
  원인이 함께 있으면 그쪽이 이기고, `Giving up after N retries` 만으로는 판단하지 않으며(라이브 주소 갱신 실패 경고도 그렇게 끝난다) bgutil 사이드카
  줄은 제외한다. 집계 스크립트(`scripts/collect_log_phrases.py`)는 재시도 중인 줄을 `재시도 중 (실패 아님)` 으로 표시한다.
  검증: `tests/test_failure.py`·`test_collect_log_phrases.py`·`test_cookie_health.py` 에 테스트 추가(분류 7가지 문구, 재시도 중·포기만으로·사이드카 음성,
  우선순위, 진짜 `run_download` 경로). 구현을 빼면 4개가 실패하고, 두 가드(`retrying`·`pot:bgutil`)를 빼면 각 가드의 테스트가 실패한다.
  문구 중 실제 로그 근거는 `timed out`·`bytes read` 뿐이고 나머지는 Python/OS 문구를 기억으로 적었다.
- **README 정정** — 쿠키 상태 절의 "알림은 아직 없고 기록만 합니다" 를 GUI·CLI 표시에 맞게 고치고 `network` 분류를 적었다.

## Phase E — 텔레그램 봇 (2026-10-07)

`python main.py --bot` 이 텔레그램 봇이 되었다 (이전에는 `NotImplementedError` 로 끝나는 스텁). 링크를 보내면 받고, 진행 상황을 상태 메시지
한 개에 계속 고쳐 쓰고, 저장 경로(또는 실패 이유)로 마무리한다. 사용법은 README "텔레그램 봇".

| 변경 | 파일 |
|---|---|
| 텔레그램 Bot API 클라이언트 — 표준 라이브러리만(`urllib`), 롱 폴링·보내기·고치기, 토큰은 오류 문구에서 가림 | `core/telegram.py` (신규) |
| 봇 본체: 허용 채팅·링크 호스트 검사, 대기열·동시성 한도, `/status` `/cancel` `/audio` `/help`, 진행률 메시지, 쿠키 만료 알림 | `interfaces/bot.py` |
| 환경변수 `YTDLP_BOT_TOKEN` `YTDLP_BOT_ALLOWED_CHATS` `YTDLP_BOT_ALLOWED_HOSTS` `YTDLP_BOT_MAX_PARALLEL` `YTDLP_BOT_MAX_QUEUED` `YTDLP_AUDIO_LANG` | `interfaces/bot.py` |
| compose 가 토큰·허용 목록 없이는 멈춤, `restart: on-failure:5`, `docker/.env`·`docker/data/` 를 git 이 무시 | `docker/`, `.gitignore` |

설계 판단:
* **닫힌 기본값** — 허용 채팅 목록이 비어 있으면 시작하지 않는다. 목록 밖 채팅에는 한 번만 chat id 를 알려 준다. 링크는 YouTube 만 받는다
  (서버 안쪽 주소로 향하는 링크를 막기 위해). 목록이 비어 있으면 시작하지 않으니 내 chat id 를 알 길이 없는 닭-달걀 문제가 있어, 처음에는
  `0` 을 넣고 시작해 답장으로 id 를 알아내는 방법을 오류 메시지와 README 에 적었다.
* **`python-telegram-bot` 을 쓰지 않았다** — 의존성을 늘리지 않고, 로컬 가짜 서버로 HTTP 계층까지 시험하기 위해서.
* 파일명은 `제목 (영상ID)` — 제목이 같은 영상이 서로를 "이미 받음" 으로 건너뛰지 않게.
* 텔레그램 쪽 실패(수정 실패·요청 제한)가 다운로드를 실패시키지 않고, 끝 메시지는 수정이 안 되면 새 메시지로 보낸다.

검증: `tests/test_telegram_client.py` 21개(가짜 텔레그램 서버로 HTTP 까지), `tests/test_bot.py` 73개(권한·호스트·대기열·취소·진행률 제한·
요청 제한·시작 거부·`run_bot` 전체 배선·가짜 서버 위의 종단 시험). 핵심 방어선 20곳을 일부러 망가뜨려 보니(전체 허용, 호스트 검사 제거, 대기열 한도 제거,
다른 채팅의 작업 취소, 토큰 노출 등) 모두 테스트가 잡았고, 스레드를 쓰는 테스트는 25회 반복해도 실패가 없었다.
**실제 텔레그램에 연결해 시험하지 못했고(토큰이 필요), Docker 로도 띄워 보지 못했다(이 PC 에 docker 없음).** 받은 파일을 돌려주기와 전사는 아직 없다.

### 같은 날 후속: 그룹에서 쓰기와 문서 대조

텔레그램 공식 문서를 읽고 기억에 의존했던 부분을 대조했다 (결과는 TODO.md Phase E).
* **`/video <링크>` 추가.** 그룹의 봇은 privacy mode 때문에 기본으로 링크만 보낸 일반 메시지를 받지 못해, "링크만 보내기" 뿐이던 영상 받기가 그룹에서는
  되지 않았다 (`/audio` 만 됐다). `/video` 는 링크만 보낸 것과 같고, `/video@봇이름` 은 이 봇만 응답한다. README 에 그룹 안내를 적었다
  (`/setprivacy` → Disable 한 뒤 봇을 그룹에서 내보냈다 다시 추가해야 적용).
* **`409`(같은 봇을 다른 곳에서 폴링 중이거나 웹훅 설정)일 때 이유를 설명**하고 종료한다. 웹훅은 지우지 않는다 — 남이 설정한 것일 수 있다.
* README: 받지 못한 메시지는 24시간까지만 보관됨(공식 문서로 확인), 방송 중인 라이브는 끝날 때까지 자리를 차지한다는 점을 적었다.
* 정정: 이전에 "봇은 배포본에 들어가지 않는다" 고 했으나 `build/cli_entry.py` 는 `--bot` 이 오면 `--cli` 를 넣지 않고 CLI 스펙은 `interfaces.bot` 을 포함한다.
  재빌드하면 `yt-dlp-cli.exe --bot` 이 될 가능성이 높다 (이 시점에는 빌드해 보지 않았다 — 아래 "후속 4" 에서 확인됨).

검증: 봇 테스트 77개(4개 추가). 새 동작 세 곳(`/video` 처리, 409 설명, 다른 오류에는 설명 없음)을 일부러 망가뜨리면 테스트가 잡는다.
**privacy mode 의 실제 동작과 `/video` 는 실제 그룹에서 확인하지 못했다.**

### 같은 날 후속 2: 첫 실제 시험에서 나온 개선

사용자 PC 에서 텔레그램 봇을 처음 실제로 돌렸다 (개인 채팅). **성공**: 영상 하나(55 MiB, 22초), 진행률이 상태 메시지 한 개로 고쳐짐, `/status`, 같은 링크 중복 거부.
첫 시도는 환경 때문에 실패했다 — 소스로 실행하면 앱은 PATH 에서 `yt-dlp` 를 먼저 찾는데 고장난 `Python39\Scripts\yt-dlp.exe` 가 잡혀
`No module named 'yt_dlp'` 로 즉시 죽었다 (번들 `_internal\yt-dlp.exe` 를 PATH 앞에 두니 해결). 그 실패에서 세 가지가 드러나 고쳤다.
* **실패 메시지에 원인을 넣는다 (봇).** 답이 `✗ 실패 #1` 과 로그 경로뿐이었다. 이제 안내 다음에 `마지막 오류:` 로 yt-dlp 가 한 말을 최대 3줄 인용한다
  (다운로더 자신의 요약 줄은 뺌, 성공한 실행에는 넣지 않음).
* **`yt_dlp_missing` 분류.** `ModuleNotFoundError: No module named 'yt_dlp'` 는 `ERROR:` 줄이 아니라서 분류기에 닿지 않아 `other` 로 끝났다. 분류기에 닿게 하고,
  다른 모든 판정보다 우선하는 새 종류로 만들어 `pip install` 과 `where yt-dlp` 를 안내한다 (GUI·CLI·봇 모두). README 문제 해결에 PATH 의 낡은 런처 함정을 적었다.
* **라이브 판별 실패를 "일반 영상" 이라 하지 않는다.** 판별이 실패해도 안전하게 `not_live` 로 간주하는데, 로그가 `일반 영상 (live_status=not_live)` 라고 써서 yt-dlp 가 그렇게
  답한 것처럼 읽혔다 (이번 진단이 늦어진 이유). `LiveStatusInfo.probed`(실패한 폴백은 `False`)를 두고 `⚠️ 라이브 여부를 확인하지 못했습니다` 경고를 쓴다.
* `.gitignore` 의 `downloads/` — 봇의 기본 저장 폴더가 저장소 안에 생길 수 있어서 사용자가 직접 추가했다.

검증: 테스트 추가(`tests/test_live_probe.py` 신규 9개, 분류기·실제 실행 재현·봇 메시지). 실제 로그의 트레이스백을 그대로 흉내 낸 `run_download` 종단 시험은 단독 실행에서도 통과한다
(처음엔 `git describe` 가 가짜 프로세스로 가는 순서 의존이 있어 고쳤다). 새 동작 6곳을 일부러 망가뜨리면 모두 테스트가 잡는다.
아직 못 본 것: `/audio`, `/cancel`, 목록 밖 채팅의 답, Ctrl+C 종료, 그룹.

### 같은 날 후속 3: 연결이 끊겨도 끝 메시지를 잃지 않는다

사용자 PC 의 텔레그램 연결이 간헐적으로 끊겼다 (`getUpdates` 가 `WinError 10060` 으로 반복 실패, 첫 줄은 다운로드 중 상태 수정 시간 초과). 재시도 자체는 설계대로였지만
(1→2→4→…→60초, 성공하면 1초로 되돌림), **끝 메시지의 수정이 실패하면 새 메시지로 한 번만 다시 보내고 포기**해서 다운로드는 성공했는데 알림을 못 받을 수 있었다.
측정(토큰 없이 공개 주소만): 왕복 약 275ms 라 호출 한 번이 약 1.1초, 5번 중 1번 시간 초과, 인증서 정상(가로채기 아님).
* 완료·실패·취소 메시지와 쿠키 만료 알림은 실패하면 **보관함**에 넣고 별도 스레드가 5→10→20→40→60초 간격으로 다시 보낸다. 쿠키 알림은 만료 한 번에 한 번만 나가서
  잃으면 영영 모르므로 포함했다. 다시 시도할 만한 오류만(연결 끊김, `429` 는 텔레그램이 말한 시간만큼, `5xx`), 차단됨·채팅 없음·잘못된 토큰은 하지 않는다.
  작업마다 최신 끝 메시지 하나만 보관, 최대 1시간·200개, 봇을 끌 때 마지막으로 한 번 더(첫 실패에서 멈춰 종료가 길어지지 않게). 진행률과 일반 답은 보관하지 않는다.
* 적어도 한 번 전달이다: 시간 초과된 새 메시지가 실제로는 도착했다면 중복될 수 있다.

검증: 보관함 테스트 16개(`tests/test_bot.py`) — 연결 복구 뒤 전달, 간격 10→20→40→60→60, `429` 대기, 거부는 안 보관, 1시간 포기, 최신으로 교체, 진행률·답은 안 보관, 쿠키 알림 전달·중복 없음,
개수 제한, 종료 때 마지막 시도, 실제 스레드와 가짜 텔레그램 서버(502 를 몇 번 주다 통과) 종단 시험. 보관함 장치 14곳을 일부러 망가뜨리면 모두 테스트가 잡고, 25회 반복해도 실패가 없었다.
**실제로 연결이 끊긴 상황에서의 동작은 보지 못했다** (가짜로 끊은 시험뿐). 함께 확인된 것: 사용자 시험에서 `/audio`(MP3 저장)와 `/cancel`(받는 중 취소)이 실제로 동작했고,
앞서 나온 `HTTP 403` 은 같은 링크를 다시 보내니 성공했다 (재현 시도 4회 모두 성공 → 일시적).

### 같은 날 후속 4: 시험으로 생긴 실제 증거 반영

* **`po_token` 안내문**: 실제 `HTTP Error 403` 이 일시적이었고(같은 링크가 다음 시도에 받아짐, 같은 명령 재현 4회 성공) 사용자는 PO Token 사이드카를 쓰지 않는데도
  안내가 사이드카부터 점검하라고 했다. 이제 "일시적일 수 있으니 먼저 한 번 다시 시도해 보세요" 로 시작하고 그다음에 PO Token 점검을 말한다.
* **봇의 진행률 문구**: 스크린샷에 실제로 보인 `⏬ #2 72% · 259.8MB · N/A · ETA --:--` 에서 모르는 값(`N/A`, `--:--`)을 뺀다 → `⏬ #2 72% · 259.8MB`. 라이브도 같다
  (`12.0MB 받음 (라이브)`). GUI·CLI 의 진행 줄은 그대로다.
* **포장된 `yt-dlp-cli.exe --bot` 확인**: 사용자가 `3c748d8` 로 다시 빌드한 `build/dist` 에서 토큰을 비운 채 오프라인으로 실행해, 봇 모듈이 번들돼 있고 토큰이 없다는 안내와 함께
  종료 코드 2 로 끝남을 확인했다 (텔레그램 연결까지는 아님). 이 빌드에는 `dc63b7b` 이후의 변경이 들어 있지 않다.
* 문서 정정: 실제 로그에 `HTTP Error 403` 이 생겼으므로 TODO 의 "403 은 0건", `tests/test_failure.py` 와 `core/failure.py` 의 샘플 출처 표시를 고쳤다 (`[log]` 로).

검증: 테스트 5개 추가(진행률 4, 안내문 1), 새 동작 5곳을 일부러 망가뜨리면 모두 테스트가 잡는다.

### 같은 날 후속 5: 자막의 HTTP 429 가 영상 전체를 실패시킴 → 자막 없이라도 영상을 받는다

사용자가 `94a79fc` 로 다시 빌드한 배포본(GUI)으로 `[한영자막]` 영상을 받다가 **영상 전체가 실패**했다:
`ERROR: Unable to download video subtitles for 'ko': HTTP Error 429: Too Many Requests`. 5분 뒤 같은 영상은 자막까지 받으며 성공했으니 일시적인 요청 제한이다.
yt-dlp 소스를 확인하니 자막 다운로드가 실패하면 `ignoreerrors` 가 `True` 가 아닌 한 영상 다운로드를 중단하는 설계다. 그런데 앱은 이것을 `other` 로 보고 **안내를 내지 않았다**.
* `HTTP Error 429`·`Too Many Requests` 를 요청 제한(`rate_limited`)으로 분류한다 (영상 자체의 429 도 같다). 쿠키 건강도에는 세지 않는다.
* **자막이 실패하면 자막 없이 영상만 한 번 다시 받는다** (사용자 결정 (b): 영상 우선). 자막 옵션을 켠 실행이 `Unable to download video subtitles` 로 실패하면(원인이 429 든 404 든)
  자막 옵션 없이 한 번만 다시 받고, 성공하면 `DownloadResult.subtitles_skipped` 와 `⚠ 자막 없이 받음` 으로 분명히 알린다 — 다운로드 목록·일괄 행·일괄 끝 요약, 기록 `⚠ 확인 필요`, CLI 줄,
  실행 로그의 `subtitles skipped`. 영상은 완료로 치되 자막 파일은 없다. 재시도도 실패하면 그 실패로 분류하고, 재생목록은 첫 시도에서 끝낸 영상을 유지하며, 취소를 존중한다.
  자막이 아닌 실패는 재시도하지 않는다.
* **알려진 한계**: 일괄에서는 이 영상이 보관 기록에 '받음' 으로 남아 같은 목록을 다시 실행해도 건너뛰므로 자막이 채워지지 않는다 (TODO).
* 같은 사용자 시험에서 오디오 트랙 옵션(한국어·원본·영어 우선)이 21트랙 영상에서 실제로 동작함을 로그로 확인했다.

검증: 테스트 추가(분류, 재시도 조건·한 번만·취소·레코드 병합·재시도 실패 분류·기록·GUI·CLI 표시), 새 동작 12곳을 일부러 망가뜨리면 모두 테스트가 잡는다 (한 건은 처음에 못 잡아 테스트를 보강했다).
**실제 yt-dlp 로 자막이 429 로 실패하는 상황은 재현하지 못했다** (가짜 실행 결과로만 시험).

### 같은 날 후속 6: 일괄에서 자막 없이 받은 영상의 자막 채우기

자막 없이 영상만 받는 재시도(후속 5)의 알려진 한계를 고쳤다: 일괄에서는 그 영상이 보관 기록에 '받음' 으로 올라가 같은 목록을 다시 실행해도 통째로 건너뛰어 자막이 영영 채워지지 않았다.
* **재시도는 보관 기록(`--download-archive`)을 쓰지 않는다.** 그 영상이 기록에 오르지 않으므로 같은 목록을 다시 실행하면 yt-dlp 가 자막을 먼저 받고 영상은 "이미 있음" 으로 건너뛴다.
* 일괄 요약은 `완료 N (자막 없음 K)` 로 세고(`BatchSummary.subtitles_skipped`), GUI(요약 줄·정보 탭)와 CLI 가 끝에 "자막 없이 받은 영상 K개: 같은 목록을 다시 실행하면 그 영상은 건너뛰고 자막만 받습니다" 라고 알린다.
* 근거(실제 yt-dlp 로 확인): 보관 기록에 있는 영상은 자막 없이 통째로 건너뛴다 (`has already been recorded in the archive`) — 이것이 원래 문제. yt-dlp 소스의 처리 순서는
  보관 기록 확인 → 자막 쓰기 → "이미 받은 파일" 확인이다. **다시 실행해 자막이 실제로 채워지는 것까지는 못 봤다**: 시험하던 영상에 자막 429 가 연달아 걸려 2분 넘게 풀리지 않았다
  (자막 제한이 쉽게 걸리고 오래 간다는 증거이기도 하다). 한계: 재생목록 항목은 재시도가 보관 기록을 쓰지 않아, 기록에 있지만 지운 영상이 다시 받아질 수 있다.

검증: 테스트 7개 추가(재시도의 보관 기록, 일괄 요약, GUI·CLI 끝 안내), 새 동작 5곳을 일부러 망가뜨리면 모두 테스트가 잡는다.

### 같은 날 후속 7: 봇의 기본 오디오 트랙을 원본으로

사용자 결정(2026-10-07): `YTDLP_AUDIO_LANG` 의 기본값을 `ko` 에서 **원본**으로 바꿨다. 21트랙 영상(원본 영어 + 자동 더빙 20개)에서 `ko` 는 영어 원본이 아니라 **자동 한국어 더빙**을 받았고,
봇의 받은 파일은 전사(Whisper)로 이어질 것이라 원본이 맞다. `ko` 같은 코드를 직접 주면 예전처럼 그 언어를 먼저 고른다. GUI·CLI 의 기본(한국어 우선)은 그대로다.
* `interfaces/bot.py`: `BotConfig.audio_lang` 기본과 환경변수 기본이 `None`(원본), 시작 로그의 설정 줄은 `audio_lang=original`. README·`docker/README.md` 의 표 갱신.
* 같은 대화에서 나온 제안(여러 언어 오디오를 한 파일에 담아 플레이어에서 고르기)은 구현하지 않았고 조사한 것만 TODO.md Phase H 에 적었다.

검증: `tests/test_bot.py::EnvTest` 가 기본 원본(환경변수·데이터클래스·실제 `-f` 인자), `ko` 지정 시 `[language^=ko]` 가 붙는 것, 빈 값이 원본인 것을 본다. 기본값을 두 군데 모두 `ko` 로 되돌려 보니 둘 다 테스트가 잡았다.
실제 텔레그램·영상으로 새 기본값을 돌려 보지는 않았다 (`-f` 인자까지만 시험).

### 같은 날 후속 8: 실제 봇 시험 — 대기 중 `/cancel` 확인, 번호 있는 취소의 빈 테스트 채움

사용자 PC 에서 `c70427c` 의 봇을 동시 1 로 돌려 영상 4개를 보냈다 (#1 완료, #2 `HTTP 403` 으로 실패, #3 받는 중, #4 대기 중). #4 는 `✗ 취소됨 #4 (대기 중)` 로 끝나고 실행 로그가 없다(시작된 적 없음) —
**대기 중 취소를 실제로 봤다.** 번호 없는 `/cancel` 은 설계대로 이 채팅의 작업을 전부 취소해 받는 중이던 #3 도 취소됐다 (사용자는 #4 만 취소한 줄 알았다). 같은 실행의 로그로 새 기본값(원본 오디오)도 확인했다.
* 코드 변경 없음. 기존 테스트는 `/cancel 2` 뒤에 #1 이 "끝났는지"만 봐서 번호를 준 취소가 실행 중인 작업까지 멈춰도 통과했다 — `test_a_numbered_cancel_leaves_the_running_download_alone` 이 #1 이 계속 받아
  `✓ 완료 #1` 로 끝나는 것을 확인한다. 번호가 있어도 활성 작업 전부를 취소하도록 일부러 망가뜨리면 이 테스트가 잡는다.
* 시작하자마자 나는 `HTTP 403` 표본이 2건이 됐다 (TODO.md Phase E). 재시도 구현은 이번 링크를 다시 보낸 결과를 받고 정한다.

같은 날 15:56 에 사용자가 이어서 한 시험: 앞서 403 으로 실패한 링크(`KtquxqP2YnM`)를 다시 보내니 **같은 형식(`299+140`)으로 성공**했고(두 건 모두 다시 보내면 성공), 이어 `/cancel #6` 으로 **받는 중인 작업을 번호로 취소**하는 것을 실제로 봤다
(앞 작업이 23초에 끝나 #6 이 대기하지 않았다). **대기 중인 작업을 번호로 취소하면서 다른 작업은 계속 받는 경우는 아직 못 봤다.**

### 같은 날 후속 9: 시작하자마자 나는 `HTTP 403` 을 한 번 자동으로 다시 시도한다

실제 실행 로그에서 같은 모양의 실패가 세 번 나왔다(`/audio`, `/video`, 그리고 앞 작업을 취소한 직후 시작한 `/video`): 형식 목록을 찍은 직후, 바이트가 하나도 오기 전에
`ERROR: unable to download video data: HTTP Error 403: Forbidden`. 사용자가 세 건 모두 3분 25초 ~ 17분 38초 뒤 다시 보내 성공했고, 실패 직전·직후에 다른 영상은 정상으로 받아졌으니 지속되는 상태가 아니다.
* `run_download` 가 `po_token` 으로 분류되고 `ERROR` 줄이 403 이며 받은 영상·`[download] Destination:` 줄이 없고 취소가 아닐 때(**이 조건은 후속 11 에서 "진행 줄이 보고한 받은 양 1 MiB 미만"으로 바뀌었다**), `HTTP_403_RETRY_DELAY`(10초) 뒤 처음부터 **한 번** 다시 한다.
  두 번째 결과가 첫 결과를 대신하고, 대기 중 취소하면 바로 깨어 두 번째 시도를 하지 않는다. 더 강한 원인(요청 제한·봇 차단)이 같이 있거나 경고뿐이면 하지 않는다.
* 로그: `HTTP 403 — 받은 데이터가 없어 10초 뒤 한 번 다시 시도합니다`, 이어 `재시도 성공!` 또는 `재시도도 실패했습니다`. 재시도도 실패하면 안내가 "자동으로 한 번 다시 시도했지만 같은 오류였습니다" 로 바뀐다.
* **10초는 짐작이다**: 실제 성공은 모두 3분 넘게 뒤였다. 쓰면서 로그의 성공/실패를 모아 대기를 조정한다. 사용자가 짚은 "취소와의 연관"은 세 건 중 한 건만 취소 직후이고 나머지는 정상 완료 뒤라 필요조건이 아니며, 확정도 못 한다 — 표본을 모은다.

검증: `tests/test_http_403_retry.py` 18개(탐지, 재시도 성공·실패·한 번만, 하지 않는 경우 6가지, 취소 두 시점, 대기 시간 상수, 실제 로그의 yt-dlp 출력 줄을 그대로 재생한 종단 3개). 조건과 동작을 일부러 망가뜨린 10곳을 모두 테스트가 잡았다(한 곳은 처음에 못 잡아 테스트를 보강).
기존 테스트 둘(`test_cookie_health`, `test_failure`)은 403 하나만 주는 가짜 실행이라 재시도 때 바닥이 나서 두 번째 시도도 403 을 주게 고쳤다. **실제 YouTube 에서 재시도가 403 을 푸는 것은 아직 못 봤다.**

### 같은 날 후속 10: 봇 콘솔에 실행 버전과 환경변수를 찍는다

사용자가 돌리는 `python main.py --bot` 의 콘솔을 붙여 줄 때 어느 빌드가 돌고 있는지 알 수 없었다 (소스 실행은 고친 뒤에 다시 켜지 않으면 옛 코드가 계속 돈다). 시작하자마자 가장 먼저:
* `[bot] version: 0.1.0 (4d31d13-dirty)` — 배포본은 `_app_version.txt`, 소스는 `VERSION` + `git describe --dirty`(`core.version.app_version`, 실행 로그와 같은 값).
* `[bot] environment:` 아래 `YTDLP_*` 변수를 한 줄씩. **앱 이름공간만** 찍고(PATH·다른 도구의 API 키 등은 안 찍음), 이름에 `TOKEN`·`SECRET`·`KEY`·`PASSWORD`·`PASSWD`·`CREDENTIAL` 이 든 변수는 `<set>`,
  빈 값은 `<empty>`, `YTDLP_BOT_ALLOWED_CHATS` 는 개수만(`<2 chat id(s)>`, `BotConfig.describe` 와 같은 방침), URL 안의 `user:pass@` 는 `***@` 로 가린다. 모르는 이름의 `YTDLP_*` 도 보여 오타가 드러난다.
* 설정을 읽기 **전에** 찍어서 잘못된 값 때문에 시작하지 못할 때도 원인 옆에 보인다.

검증: `tests/test_bot.py` 에 `EnvironmentLinesTest` 6개와 `RunBotTest` 3개 추가(버전이 맨 처음·한 번, 환경변수가 설정보다 앞, 비밀·채팅 id 가 어디에도 안 나옴, 거부된 설정에서도 나옴, 변수가 없을 때 안내).
마스킹·순서·이름공간 제한을 일부러 망가뜨린 8곳을 모두 테스트가 잡았다 (변이 스크립트가 한 번 중간에 멈춰 변이된 파일이 남았는데, 시작 전 백업으로 되돌리고 `diff` 로 동일함을 확인했다). 토큰을 비운 채 실제 콘솔도 확인했다.
**이미 돌고 있는 봇은 옛 코드라 다시 켜야 이 줄들이 나온다.**

### 같은 날 후속 11: 시작 직후 403 의 두 번째 모양까지 재시도한다

재시도를 넣은 코드(`d7f26f9`)로 돌린 봇에서 `P7W9r85qPpI` 가 403 으로 실패했는데 **재시도가 걸리지 않았다**: 로그가 `[download] Destination:` 과 `0.0% got 1.0KB` 를 찍은 뒤 2초 만에 403 이었고 (16:16 의 4번째 사례도 같은 모양), 처음 조건이 `Destination:` 줄이 없을 때만이었기 때문이다.
사용자가 `/video` 를 붙여 다시 보내 4분 55초 뒤 성공했다(`/video` 와 링크만 보내는 것은 같은 경로라 결과와 무관). 그 로그의 `Resuming download at byte 10006282` 로 **실패한 시도가 디스크에 이미 약 10MB 를 받아 두었음**이 드러났다 —
진행 줄은 `got 1.0KB` 한 줄뿐이었다(yt-dlp 가 진행 줄을 드문드문 찍는다). 그러니 이 모양은 "바이트가 오기 전"이 아니라 "받기 시작 2초쯤 뒤"의 403 이다.
* 조건을 `Destination:` 줄 유무에서 **진행 줄이 보고한 받은 양이 1 MiB 미만**(`HTTP_403_RETRY_MAX_BYTES`, `_Output.received` = 진행 줄의 가장 큰 `got`)으로 바꿨다. 두 모양(첫 요청 전, 2초 뒤)을 모두 포함하고, 그 이상 진행한 뒤의 403 은 본 적이 없어 제외한다.
  한계: 보고가 드물어 실제 받은 양과 다르다 — 코드 주석에 적었다. 재시도는 `.part` 를 이어받아 싸다.
* 로그 문구를 `받은 데이터가 없어` 에서 `받기를 시작한 직후라` 로 바로잡았다 (10MB 가 있었는데 "없어"라고 하면 거짓이다).

검증: 테스트 24개(`tests/test_http_403_retry.py`): 보고된 양 기록·최댓값, 한계 직전·한계, 두 모양과 실제 진행 줄 이후의 비재시도, 20:10 로그의 yt-dlp 출력 줄을 그대로 재생한 종단 2개. 조건·한계·기록을 일부러 망가뜨린 12곳을 모두 테스트가 잡았다.
**넓힌 코드로 실제 403 을 재시도해 보지는 못했다** (돌고 있는 봇은 `d7f26f9` 라 재시작이 필요하다).

### 같은 날 후속 12: bgutil 사전 점검이 번들 yt-dlp 의 플러그인을 못 찾던 거짓 경보

봇을 `YTDLP_AUTH_METHOD=bgutil`, `YTDLP_GETPOT_BGUTIL_BASEURL=http://localhost:4416` 로 켠 실제 콘솔(`998c8b5`)에 `⚠️ auth.validate: bgutil-ytdlp-pot-provider 플러그인을 찾을 수 없습니다` 가 떴다.
사전 점검의 첫 단계가 **이 파이썬 환경에 설치된 패키지**만 찾고, 없으면 거기서 실패로 끝내서 — 소스 실행(파이썬 3.12, 플러그인 패키지 없음)이 PATH 의 **번들 `yt-dlp.EXE`** 를 쓰는 이 환경에서는 거짓이었다.
그 yt-dlp 는 플러그인을 자기 옆 `yt-dlp-plugins/<패키지>/yt_dlp_plugins` 에서 읽는다(`-v` 출력의 `Plugin directories` 와 `bgutil:http-2.0.1` 로 확인). 더 나쁘게는 **뒤의 yt-dlp 버전 확인과 사이드카 `/ping` 이 아예 실행되지 않아** 콘솔이 사이드카가 응답하는지 말해 주지 못했다.
* `core/auth.py`: 실제로 쓸 yt-dlp(`yt_dlp_command()`) 실행 파일 옆의 `yt-dlp-plugins/*/yt_dlp_plugins/extractor/getpot_bgutil*.py` 도 찾는다(`_plugin_beside_yt_dlp`). 패키지가 설치돼 있어도 되고, 둘 다 없으면 예전처럼 `bgutil_plugin_missing`.
* 실제 확인: 번들 yt-dlp 를 PATH 에 두고 `validate()` 를 돌리니 `localhost:4416` 은 `연결 성공 … version 2.0.1`, 앱 기본 주소 `127.0.0.1:4416` 은 `bgutil_unreachable`(이제 진짜 원인이 나온다).

검증: `tests/test_bgutil_plugin_check.py` 9개(옆 폴더의 플러그인·세 가지 파일 이름·다른 플러그인·`python -m` 형태·yt-dlp 없음·설치된 패키지, `validate` 가 핑까지 가는 것·접속 불가가 접속 불가로 나오는 것·진짜 없을 때는 핑하지 않는 것). 점검을 일부러 망가뜨린 5곳을 모두 테스트가 잡았다.
**돌고 있는 봇은 옛 코드라 다시 켜야 `✓ auth.validate: bgutil provider 연결 성공` 이 나온다.**

### 같은 날 후속 13: 도커 compose 가 선택 설정을 봇 컨테이너로 넘기지 않던 구멍

원격 리눅스에서 봇을 돌리는 안내를 쓰다가 확인했다: `docker/docker-compose.yml` 의 `downloader.environment` 는 5개(`AUTH_METHOD`, `GETPOT_BGUTIL_BASEURL`, `OUTPUT_DIR`, `BOT_TOKEN`, `BOT_ALLOWED_CHATS`)만 넘기고,
`docker/.env` 는 compose 파일 안의 `${...}` 치환에만 쓰인다. 그래서 `docker/README.md` 표에 있는 `YTDLP_BOT_MAX_PARALLEL`·`YTDLP_BOT_MAX_QUEUED`·`YTDLP_BOT_ALLOWED_HOSTS`·`YTDLP_AUDIO_LANG`·`YTDLP_FRAGMENT_RETRIES`·`YTDLP_STALL_MINUTES`
를 `docker/.env` 에 적어도 **컨테이너에는 닿지 않고 조용히 무시**됐다(예: `MAX_PARALLEL=1` 이 기본 2 로 남음).
* 여섯 개를 `"${NAME:-}"` 로 넘긴다. 비어 있으면 봇이 기본값을 쓴다(봇은 빈 변수를 설정하지 않은 것으로 읽는다 — 테스트로 확인). 필수 둘(`BOT_TOKEN`, `BOT_ALLOWED_CHATS`)은 그대로 없으면 compose 가 멈춘다.
* `tests/test_docker_compose_env.py`: README 표의 변수가 compose 에 모두 있는지(쿠키 파일 경로만 예외), 선택 변수의 기본이 빈 값인지, 필수 둘이 여전히 필수인지, 빈 값이 봇 기본값과 같은 설정을 만드는지. 일부러 망가뜨린 3곳을 모두 잡았다.
**이 PC 에는 docker 가 없어 `docker compose config` 로 문법을 확인하지 못했다** — 파일을 글자로 읽어 시험했을 뿐이다. 리눅스 쪽에서 `docker compose -f docker/docker-compose.yml config` 로 확인할 것.
* 실행 로그도 같은 종류의 구멍이었다: 컨테이너 안(`/root/.ytdlp-downloader/logs`)에만 쌓여 컨테이너를 다시 만들 때(`up --build`, `down`) 사라졌다. `./data/logs` 를 마운트했다(`test_the_run_logs_are_kept_on_the_host`). `cookie_health.json`·`history.json` 은 같은 폴더 위에 있지만 봇에서는 쓰이지 않거나 휴면이라 그대로 뒀다.
알려진 한계: 이미지는 `.git` 을 담지 않아(`.dockerignore`) 컨테이너 콘솔의 `version:` 줄은 커밋 없이 `0.1.0` 만 나온다.

## 진행 중 / 예정 (TODO.md 참조)

- **Phase C-3**: 메신저 알림 훅. 원인별 분류(C-2)와 쿠키 건강도(C-1)는 구현됨. GUI·CLI 의 쿠키 만료 표시와 텔레그램 봇의 쿠키 만료 알림도 구현됨 (봇 밖의 메신저 알림·`Notifier` 추상은 아직)
- 라이브 조기 종료 경고의 추가 샘플 확보, 목록 끌어다 놓기 (TODO.md Phase F-2 / I 후속)
- **Phase D'**: Whisper 전사 파이프라인 (`core/transcriber.py` 구현)
- **Phase E 후속**: 받은 파일 돌려주기, 실제 텔레그램·Docker 시험, 대기열 영속화
- 최종 목표: URL 수신 → Docker 컨테이너 다운로드 → Whisper 전사 → 메신저 응답
