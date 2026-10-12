# scripts/refresh_cookies.py — YouTube 쿠키 자동 갱신

## 목적

`refresh_cookies.py`는 Playwright 를 사용해 전용 브라우저 프로필에서 YouTube 쿠키를
Netscape 포맷(`cookies.txt`)으로 추출하는 로컬 전용 스크립트입니다.
한 번 로그인해 두면 Windows 작업 스케줄러 또는 Linux cron 으로 주기 실행하여
앱에서 매번 쿠키를 수동 추출하지 않아도 최신 상태를 유지할 수 있습니다.

---

## 초기 설정 (1회)

```powershell
# 1. 가상환경 생성 및 활성화
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell
# source .venv/bin/activate          # Linux / macOS

# 2. 의존성 설치
pip install -r scripts/requirements.txt

# 3. Playwright 브라우저 바이너리 설치
python -m playwright install chromium

# 4. 첫 실행 (헤드풀) — 브라우저 창에서 YouTube 로그인
python scripts/refresh_cookies.py
# 로그인 완료 후 창이 자동으로 닫히고 ~/cookies.txt 가 생성됩니다.
```

> ⚠️ 첫 실행 시에는 `--auto` 를 사용하지 마세요. 반드시 헤드풀로 실행해 로그인을 완료해야 합니다.

---

## 수동 갱신

로그인 세션이 살아있는 동안은 `--auto` 플래그로 헤드리스 갱신이 가능합니다.

```powershell
python scripts/refresh_cookies.py --auto --output "$env:USERPROFILE\cookies.txt"
```

로그인이 만료된 경우 exit 코드 4 가 반환됩니다. 이때는 `--auto` 없이 한 번 실행하여
다시 로그인하세요.

---

## Windows 작업 스케줄러 등록

PowerShell 에서 실행합니다 (관리자 권한 불필요).

```powershell
$action = New-ScheduledTaskAction `
    -Execute "$PWD\.venv\Scripts\python.exe" `
    -Argument "$PWD\scripts\refresh_cookies.py --auto --output $env:USERPROFILE\cookies.txt"

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 3am

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable

Register-ScheduledTask -TaskName "RefreshYTDLPCookies" `
    -Action $action -Trigger $trigger -Settings $settings
```

등록 후 작업 스케줄러(taskschd.msc) → "작업 스케줄러 라이브러리" 에서
`RefreshYTDLPCookies` 항목을 선택하고 "지금 실행" 으로 테스트할 수 있습니다.

---

## Linux cron 등록

```bash
crontab -e
```

아래 줄을 추가합니다 (매주 월요일 오전 3시 실행):

```
0 3 * * 1 /path/to/.venv/bin/python /path/to/scripts/refresh_cookies.py --auto --output /home/user/cookies.txt >> /var/log/refresh_cookies.log 2>&1
```

경로는 실제 환경에 맞게 수정하세요.

---

## 로그 & exit code 의미

| exit code | 의미 | 복구 방법 |
|-----------|------|-----------|
| `0` | 정상 완료 — cookies.txt 갱신됨 | 없음 |
| `1` | playwright 미설치 또는 중단(Ctrl+C) | `pip install -r scripts/requirements.txt` 후 `python -m playwright install chromium` |
| `2` | 브라우저 실행 실패 | `python -m playwright install chromium` 재실행 또는 프로필 폴더 삭제 후 재시도 |
| `3` | YouTube 페이지 로드 실패 (네트워크 오류) | 네트워크 확인 후 재시도 |
| `4` | 로그인 필요 (`--auto` 모드에서 감지) | `--auto` 없이 실행하여 수동 로그인 후 다시 스케줄러 가동 |
| `5` | 파일 쓰기 실패 | `--output` 경로 및 디렉토리 쓰기 권한 확인 |

Windows 작업 스케줄러에서는 "최근 실행 결과" 가 `0x0` 이면 성공입니다.
exit 4(`0x4`) 가 반복되면 수동 로그인이 필요합니다.

---

## 트러블슈팅

**"playwright: command not found" 또는 ModuleNotFoundError**

```powershell
pip install -r scripts/requirements.txt
python -m playwright install chromium
```

**"로그인 페이지가 뜸" (--auto 실행 시 exit 4)**

`--auto` 없이 한 번 실행하여 브라우저 창에서 직접 로그인하세요:

```powershell
python scripts/refresh_cookies.py --browser chromium
```

**"앱에서 이 파일이 안 쓰임"**

앱 실행 후 "쿠키파일 찾기" 버튼을 눌러 이 스크립트가 생성한 `cookies.txt` 경로를
직접 선택하세요. 경로를 한 번 지정하면 이후 자동 갱신된 파일을 그대로 사용합니다.

**"Chrome 프로필이 이상해짐" (오류 반복, 쿠키 저장 안 됨)**

전용 프로필 폴더를 삭제하고 처음부터 다시 로그인하세요:

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.ytdlp-cookie-refresher"
python scripts/refresh_cookies.py
```

Linux:
```bash
rm -rf ~/.ytdlp-cookie-refresher
python scripts/refresh_cookies.py
```

---

## 보안 주의

> ⚠️ `cookies.txt` 는 YouTube 로그인 세션 정보를 담고 있습니다.
> 이 파일이 외부에 유출되면 제3자가 귀하의 계정으로 YouTube 에 접근할 수 있습니다.

- Git 저장소에 커밋하지 마세요 (`.gitignore` 에 `cookies.txt` 포함 권장).
- 클라우드 스토리지 공유 폴더에 저장하지 마세요.
- 접근 권한을 본인만 읽을 수 있도록 제한하세요 (`chmod 600 ~/cookies.txt`).

---

# scripts/collect_log_phrases.py — 실행 로그의 실패·경고 문구 집계

## 목적

앱은 다운로드마다 `~/.ytdlp-downloader/logs/<시각>_<영상ID>.log` 에 yt-dlp 출력 전체와 결과를 남깁니다
(`$YTDLP_LOG_DIR` 로 폴더 변경). 이 스크립트는 그 폴더를 **읽기만** 해서

- 실행이 어떻게 끝났는지(완료 / 실패 / 취소 / 건너뜀 / 끝맺음 없음)와 실패 원인 분류(`failure kind:`) 개수,
- yt-dlp 의 `ERROR` · `WARNING` 줄(과 방송 종료·재시도 같은 정보 줄)을 영상 ID·주소·숫자를 가린 "형태"로 묶은 표
  (횟수, 로그 수, 그 문구가 있던 실행이 성공/실패였는지, 라이브 녹화였는지, 지금의 분류기가 어떻게 읽는지),
- 원인이 분류되지 않은 실패와 그 마지막 오류 줄

을 Markdown 또는 JSON 으로 냅니다. `TODO.md` 의 두 항목을 **실제 문구로 확정**하는 용도입니다 — 로그는 사용자 PC 에만 있어
클라우드 개발 환경에서는 돌려 볼 수 없습니다.

- **C-2 "오류 문구를 실제 로그 샘플로 확정"**: `분류되지 않은 실패` 의 줄과 표에서 `미분류` 로 읽힌 오류가 `core/failure.py` 에
  규칙을 더할 근거입니다. 실제 줄을 `tests/test_failure.py` 에 `[log]` 샘플로 넣고 지금의 `[source]`/`[youtube]` 샘플을 바꿉니다.
- **F-2 "다른 조기 종료 경고"**: 표에서 `라이브` 열이 0 이 아니고 `성공/실패` 가 성공 쪽인 문구가 후보입니다(녹화가 끝까지 되지 않았는데
  완료로 기록된 실행에 붙은 경고). 실제 녹화 길이와 대조해 확정한 뒤 `core/downloader.py::_is_live_end_warning` 에 패턴을 더합니다.
  `Video is no longer live. Retrying` 은 방송이 끝났다는 정상 신호라 조기 종료로 세지 않습니다.

## 사용법

```powershell
# 기본 폴더의 로그 전체를 화면에 (Markdown)
python scripts/collect_log_phrases.py

# 폴더·기간 지정, 파일로 저장
python scripts/collect_log_phrases.py --logs "$env:USERPROFILE\.ytdlp-downloader\logs" --since 2026-10-01 --output report.md

# 두 번 이상 나온 문구만, JSON 으로
python scripts/collect_log_phrases.py --format json --min-count 2 --output report.json
```

| 옵션 | 뜻 |
|---|---|
| `--logs DIR` | 로그 폴더 (기본 `$YTDLP_LOG_DIR` 또는 `~/.ytdlp-downloader/logs`) |
| `--since YYYY-MM-DD` | 이 날짜 이후에 시작한 로그만 |
| `--format md\|json` | 출력 형식 (기본 md) |
| `--min-count N` | 이 횟수 이상 나온 문구만 표에 |
| `--info-pattern REGEX` | 오류·경고가 아니어도 표에 넣을 yt-dlp 정보 줄 (기본: `no longer live`, `retrying`, `giving up`, `refreshed`, `fragment`, `403`, `try again later` 등). 빈 문자열이면 정보 줄 제외 |
| `--output FILE` | 파일로 저장 |

앱 소스 폴더(`core/failure.py`)에서 실행하면 "현재 인식" 열이 채워지고, 로그만 따로 가져온 곳에서는 `?` 로 나옵니다.
표준 라이브러리만 쓰므로 `pip install` 이 필요 없습니다.

## 개인정보

로그는 쿠키 값을 담지 않지만(쿠키 파일은 경로·크기·수정 시각만), 결과의 예시 줄은 한 번 더 주소의 쿼리 문자열, 숫자와 글자가 섞인
긴 토큰, 사용자 폴더 이름(`C:\Users\<이름>`)을 가립니다. 그래도 결과를 어딘가에 붙여 넣기 전에 훑어 보세요.
