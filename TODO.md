# TODO

장기 목표: 메신저 봇 연동 → URL 수신 → yt-dlp 다운로드 → Whisper 전사/분석.
본 문서는 아직 구현되지 않은 작업을 단계별로 기록한다.

---

## Phase C — 쿠키 만료 감지 & 알림 훅

### 배경
YouTube 쿠키(`SAPISID`, `LOGIN_INFO` 등)는 수일~수주 단위로 만료된다.
서버형 봇 운영 시 쿠키가 만료되면 모든 다운로드가 조용히 실패하므로,
**실패를 감지하고 사용자(운영자)에게 알려 재추출을 유도**하는 루프가 필요하다.

### 권장 아키텍처
```
[로컬 PC] --cron 매일 1회-->  update_cookies() --> cookies.txt --> [object storage / scp]
                                                                         │
[봇 서버]  download() --- 실패 감지 → CookieHealth.mark_invalid() --- 메신저 알림
           │
           └── 같은 쿠키의 인증 성공 확인 시 CookieHealth.mark_valid()
```

### 구현 체크리스트

#### C-1. 쿠키 건강도 추적 (core/cookie_health.py)
- [x] `CookieHealth` (2026-10-06): `cookie_id`, `last_success_at`, `last_failure_at`, `failure_count`,
  `last_error_kind`, `events`(최근 50건의 변경 기록 — 만료 주기를 읽는 용도). `core/cookies.py` 가 아니라
  `core/history.py` 처럼 모듈을 따로 두었다
- [x] `cookie_health.json` 에 저장하는 `CookieHealthStore`. 위치는 `$YTDLP_COOKIE_HEALTH_FILE` →
  `$XDG_STATE_HOME/ytdlp-bot/` → `~/.ytdlp-downloader/` 순 (이름은 점 없는 `cookie_health.json`).
  원자적 저장, 손상 시 `.bak` 로 옮기고 새로 시작, 읽기 오류 때 덮어쓰지 않음
- [x] `is_healthy()`: 확인된 만료가 마지막 확인된 성공 뒤로 `failure_threshold`(기본 1)회 이상이면 불건강.
  다른 쿠키로 물으면 건강 (그 쿠키가 실패하는 것은 본 적이 없다)
- [x] **"같은 쿠키" = 로그인 쿠키(`SAPISID` 등)값의 해시** (`cookie_identity`). yt-dlp 가 매 실행 뒤 `cookies.txt` 를
  다시 쓰므로 파일 전체 해시는 쓸 수 없다. 새로 로그인한 쿠키는 지문이 달라 기록이 새로 시작되고, 그것이 재추출을
  복구로 보는 길이다 (`sync`). 로그인이 없는 쿠키 파일은 만료될 수 없으니 추적하지 않는다. yt-dlp 는 만료를
  감지하면 로그인을 뺀 채 파일을 저장하므로, 실행 **전에** 지문을 읽는다. 브라우저 쿠키는 `browser:<이름>` 뿐이라
  교체를 증명하지 못한다. **2026-10-05 의 실제 `cookies.txt` 에는 로그인 쿠키가 없었다** (방문자 쿠키만) —
  이 사용자의 지금 설정에서는 만료 추적이 걸릴 대상이 없다. (2026-10-07: 작업 폴더에는 오래된 로그인 쿠키 파일이
  있었다 — 루트 `cookies.txt`(04-17), `cookies.txt-`(01-23), 둘 다 `__Secure-3PSID`·`__Secure-3PAPISID` 를 가졌고 서버에서 아직 유효한지는 확인하지 않았다 — 그리고
  쓰이지 않는 `build/cookies.txt`(05-02, 로그인 없음). **사용자 결정으로 셋 다 삭제했다**: 로그인이 꼭 필요한 유료 채널이 없고, 필요하면 브라우저 쿠키를 다시 추출하면 된다
  (사용자: 브라우저가 실행 중이지 않으면 추출이 된다). 지금 디스크에 남은 쿠키는 배포본의 방문자 쿠키(로그인 없음)뿐이다. 그래서 **쿠키 만료 추적·경고(Phase C 의 C-1~C-3 첫 단계)는
  지금 걸릴 대상이 없는 휴면 기능**이다 — 코드와 테스트는 그대로 두고, 로그인 쿠키를 쓰기 시작하면 그때 실제 시험한다.)
- [x] 테스트: 확인된 인증 만료일 때만 `is_healthy()` 가 false 로 바뀐다. 봇 차단·403·결제·멤버·비공개·연령·
  요청 제한·기타는 기록을 바꾸지 않는다 (`tests/test_cookie_health.py`). 쿠키 값은 가짜(`FAKE-...`)
- [x] (선택) `cookies.txt` 의 expires 열로 로그인 쿠키(`LOGIN_INFO`, `SAPISID`)의 만료 시각 점검:
  `login_cookie_expires_at(path)`. 함수만 있고 아직 어디서도 부르지 않는다. 서버가 만료 전에 무효화한 쿠키는
  못 잡으므로 힌트로만 쓴다

#### C-2. 실패 감지 패턴 (core/failure.py, core/downloader.py)
- [x] 실패한 실행의 출력을 **원인별로 분류**한다 (2026-10-06). `core/failure.py::classify_failure` 가
  `FailureKind` 를 돌려주고 `_is_bot_error` 를 대체했다. ERROR 줄과 WARNING 줄을 본다 (`_run_once` 가
  WARNING 을 `captured.warnings` 로 따로 모음). 결과는 `DownloadResult.failure_kind`, 실행 로그의
  `failure kind:` 줄, 원인별 안내 한 줄(`failure_hint`). 쿠키 재추출로 풀리는 것만 쿠키 건강도 실패로 센다
  (`counts_against_cookie_health`):
  - 인증 만료 (`Login required` 계열, yt-dlp 의 "account cookies are no longer valid") → 쿠키를
    쓰는 중이고 인증 만료가 확인된 경우만 센다
  - 봇 차단 (`Sign in to confirm you're not a bot`) → 별도 `bot_blocked`. 쿠키 만료로 확정하지 않으며
    세지 않는다. 쿠키 없이 받다가 걸리면 기존대로 Chrome 쿠키로 한 번 재시도
  - PO Token 문제 (`HTTP Error 403` 은 이쪽일 수 있다) → **403 하나로 쿠키 만료로 판정하지 않는다.**
    쿠키 재추출 대신 PO Token 점검을 안내하고, 사이드카 응답 실패 경고가 있으면 함께 알린다
  - 콘텐츠 접근 권한 (결제, 멤버 전용, 비공개, 연령·지역 제한) → 쿠키 만료를 판정하지 않는다
  - 요청 제한 (`This content isn't available, try again later` — yt-dlp 소스는 최대 1시간 제한이라 설명) 은
    별도 `rate_limited`
  - 네트워크 끊김 (재시도를 포기한 연결 오류) 은 별도 `network` (2026-10-07, 아래 항목). 가장 약한 판정이라 다른 원인이 보이면 그쪽이 우선
  - **yt-dlp 가 시작조차 못 함** 은 별도 `yt_dlp_missing` (2026-10-07). 실제 로그(`20261007-052228`)에서 확인한 문구 `ModuleNotFoundError: No module named 'yt_dlp'` 는
    `ERROR:` 로 시작하지 않는 트레이스백 끝줄이라 분류기에 닿지 않았다 — `_failure_lines` 가 이제 `ModuleNotFoundError:`·`ImportError:`·`Fatal error in launcher` 로 시작하는 줄도
    넘긴다. 영상에 대해 아무 말도 못 한 것이라 **다른 모든 판정보다 우선**하고, 안내는 `python -m pip install -U yt-dlp` 와 `where yt-dlp`(PATH 에서 먼저 잡히는 것)를 가리킨다.
    다른 모듈이 없다는 오류(`No module named 'brotli'` 등)는 이 판정이 아니다. `Fatal error in launcher`(distlib 런처 문구)는 기억에 의존한 것
  - [ ] 방송 직후 멤버 전용으로 바뀐 라이브를 따로 알리는 것은 아직 없다 (접근 권한으로만 분류됨). 실사용에서 겪은 영상:
    `q4cbjDRa6F4`, `J4lzQL2PluA` (이전 세션 인수인계 기록). **2026-10-07 로그 확인**: `q4cbjDRa6F4` 의 실행 로그
    (`20261005-173953`)에는 멤버 전용 문구가 없고 `Did not get any data blocks` 만 있다. `J4lzQL2PluA` 는 로그가 없다. 그래서
    "방송 직후 멤버 전용으로 바뀐다" 는 전제가 로그로는 뒷받침되지 않는다 — 실제 오류 줄이 로그에 남을 때까지 보류한다
    ("멤버 전용으로 바뀌었으니 계정 권한을 확인하세요" 같은 별도 안내는 그 줄을 본 뒤에 정한다)
- [x] 해당 쿠키의 인증 만료가 확인됐을 때만 `mark_invalid` 호출 (2026-10-06). `run_download` 가 한다 —
  실패한 실행(`failure_kind=auth_expired`)과, **성공한 실행에서도** yt-dlp 가 "cookies are no longer valid" 라고
  한 경우 (공개 영상은 쿠키 없이 받아진다). 자동 Chrome 재시도는 설정한 로그인이 아니므로 기록하지 않는다
- [ ] 같은 쿠키의 인증 성공을 확인했을 때만 `CookieHealth.mark_valid()` 호출.
  공개 영상·아카이브 건너뛰기·다른 인증 방식의 성공은 복구 근거로 쓰지 않는다.
  `mark_valid` 는 있지만 **부르는 곳이 없다** — 일반 다운로드의 성공은 증거가 못 된다. 지금 복구로 보는 것은
  새 로그인으로 교체(`sync`)뿐이다. 후보: 로그인이 있어야만 되는 요청(`:ytwatchlater`, `:ythistory` 같은
  로그인 필요 피드)을 한 번 보내 보는 프로브. 실제 로그인 쿠키로 확인해야 한다 (미확인)
- [x] 부작용 없이 호출 가능하도록 주입 방식 (2026-10-06): `DownloadOptions.cookie_health` 에 `CookieHealthStore` 를
  넣은 경우에만 기록하고 (기본 `None` = 파일을 건드리지 않음), 일괄은 `base_opts` 로 그대로 이어받는다.
  GUI·CLI 는 넣는다. 알림은 `DownloadResult.cookie_event`(`"invalid"` 처음 감지 / `"recovered"` 새 로그인으로
  교체)가 있을 때만 하면 반복 없이 한 번씩이다 (C-3). 기록 파일을 못 써도 다운로드는 실패하지 않는다
- [x] **HTTP 429 를 요청 제한(`rate_limited`)으로 분류** (2026-10-07): 사용자 GUI(94a79fc 빌드)가 `[한영자막]` 자막(`ko,en`)과 함께 받다가
  `ERROR: Unable to download video subtitles for 'ko': HTTP Error 429: Too Many Requests` 로 **영상 전체가 실패**했다 (5분 뒤 같은 영상은 성공 — 일시적). yt-dlp 소스를 확인하니
  자막 다운로드 실패는 `ignoreerrors` 가 `True` 가 아니면 `DownloadError` 로 영상을 중단하는 설계다 (`YoutubeDL._write_subtitles`). 앱은 이것을 `other` 로 보고 **안내를 한 줄도 내지 않았다**.
  이제 `HTTP Error 429`·`Too Many Requests` 는 `rate_limited` 이다 (영상 자체의 429 도 같다). 쿠키 건강도에는 세지 않는다.
- [x] **자막이 실패하면 자막 없이 영상을 받는다** (2026-10-07, 사용자가 (b) 로 결정): `run_download` 가 `Unable to download video subtitles` 로 실패하고 자막을 요청했었다면(`subtitle_download_failed`,
  원인이 429 든 404 든) 자막 옵션 없이 **한 번만** 다시 받는다. 성공하면 `DownloadResult.subtitles_skipped=True`, 요약 `⚠ 자막 없이 받음`: 다운로드 목록·일괄 행·일괄 끝 요약(`⚠ 자막 없이 받은 영상 있음`),
  기록 상태 `warning`(`⚠ 확인 필요`), CLI 일괄 줄(`제목 — ⚠ 자막 없이 받음`), 실행 로그의 `subtitles skipped` 줄, 그리고 이때 실행 로그 경로를 보여 준다. 재시도도 실패하면 그 실패로 분류한다
  (재시도가 원인을 모르면 첫 실패의 것). 재생목록은 첫 시도에서 끝낸 영상을 유지하고 중복은 뺀다. 취소를 존중한다. 자막이 아닌 실패는 재시도하지 않는다.
  (이전에 만든 "자막을 끄고 받으세요" 안내는 도달할 수 없게 되어 지웠다.)
- [x] **일괄에서 자막 없이 받은 영상의 자막 채우기** (2026-10-07): 자막 없이 받는 재시도는 `download_archive` 를 쓰지 않는다 → 그 영상은 보관 기록에 오르지 않아 같은 목록을 다시 실행하면 이 영상만
  자막을 다시 받아 본다. 일괄 요약은 `완료 N (자막 없음 K)`, GUI 요약·정보 탭과 CLI 끝에 "자막 없이 받은 영상 K개: 같은 목록을 다시 실행하면 그 영상은 건너뛰고 자막만 받습니다".
  **확인한 것**: ① 보관 기록에 있는 영상은 `has already been recorded in the archive` 로 자막도 없이 통째로 건너뛴다 (실제 yt-dlp 로 시험 — 이게 원래 문제였다). ② yt-dlp 소스(`YoutubeDL.py`)의
  처리 순서가 보관 기록 확인(1650행) → `process_info`: 자막 쓰기(3391행) → "이미 받은 파일" 확인(3517행) 이라, 보관 기록에 없고 영상 파일이 있으면 자막이 먼저 받아지고 영상은 건너뛴다.
  **확인하지 못한 것**: 영상이 있는 채로 다시 실행해 **자막이 실제로 채워지는 것까지는 네트워크로 보지 못했다** — 시험하던 영상에 자막 429 가 연달아 걸렸다(아래).
  알려진 한계: 재생목록 항목은 재시도가 보관 기록을 쓰지 않아, 보관 기록에 있지만 사용자가 지운 영상이 다시 받아질 수 있다
- [x] **실제 yt-dlp 의 자막 429 를 직접 겪었다** (2026-10-07): 위 시험 중 같은 영상에 자막을 몇 번 요청하자 `Unable to download video subtitles for 'en': HTTP Error 429` 가 걸렸고
  2분 넘게 풀리지 않았다 — **자막 엔드포인트의 제한은 쉽게 걸리고 오래 간다.** 일괄에서 자막을 받으면 간격을 두어도 같은 일이 생길 수 있다는 뜻이다. 앱의 `run_download` 를 같은 시점에 돌렸을 때는
  제한이 이미 풀려 있어 자막까지 정상으로 받아졌다 (재시도 경로를 실제로 타 보지는 못했다)
- [ ] 오류 문구를 실제 로그 샘플로 확정한다. **2026-10-07 집계** (`python scripts/collect_log_phrases.py`, 사용자 PC 의 실행 로그
  18개, 2026-10-05 ~ 10-07, 봇·GUI 시험 로그 포함): 실제 줄이 생긴 것 — **`HTTP Error 429`(자막)** 1건(`rate_limited`, 위 항목), **`ERROR: unable to download video data: HTTP Error 403: Forbidden`** 1건(`po_token` 으로 올바르게 분류,
  다만 실제 원인은 **일시적** — 같은 링크가 다음 시도에 받아졌다. 그래서 안내문을 "일시적일 수 있으니 먼저 한 번 다시 시도"로 시작하게 바꿨다),
  `ModuleNotFoundError: No module named 'yt_dlp'`(`yt_dlp_missing`), 연결 오류의 재시도 중 줄(`network` 은 포기한 줄만 센다), bgutil 사이드카 응답 실패 경고.
  **아직 실제 줄이 0건**: 인증 만료·봇 차단·접근 권한(결제·멤버·비공개·연령)·요청 제한. 이 종류의 지금 문구는 여전히 yt-dlp 소스(`extractor/youtube/_base.py`,
  `_video.py`)와 YouTube 문구(기억)에 의존한다. `tests/test_failure.py` 가 샘플마다 출처(`[log]`/`[source]`/`[youtube]`/`[stdlib]`)를 적어 두었으니, 실제 줄이
  로그에 남으면 교체한다. 로그가 쌓이면 같은 명령을 다시 돌린다 (`scripts/README.md`)
- [x] 연결이 끊겨 실패한 실행을 `network` 로 분류한다 (2026-10-07, `core/failure.py::FailureKind.NETWORK`): 시간 초과(`timed out`)·덜 받음(`bytes read`)·
  연결 끊김·이름 조회 실패 문구가 있는 ERROR/WARNING 줄이면 `network` 이고, 안내는 "인터넷 연결을 확인하고 다시 시도, 쿠키 문제는 아닌 것으로
  보입니다". 쿠키 건강도에는 세지 않고 Chrome 쿠키 재시도도 하지 않는다. **실제 로그의 증거 범위**: 2026-10-05 로그에는 이 오류가 **재시도 중인 줄**
  (`[download] Got error: HTTPSConnectionPool(host='...googlevideo.com', port=443): Read timed out.. Retrying (N/30)...`, 한 실행에서 233회,
  `N bytes read. Retrying` 이 다른 실행에서 2회)로만 있다. 이 줄은 ERROR 가 아니라 일반 출력이라 분류기에는 들어가지 않고, 들어가도 `retrying` 이 있으면
  원인으로 세지 않는다 (재시도로 나아질 수 있고, 다른 이유로 실패한 실행을 잘못 읽게 된다). 분류하는 것은 yt-dlp 가 **포기한** 줄
  (`ERROR: ... Got error: <오류>. Giving up after N retries` 와 `Unable to download webpage: <오류>`)이며, 그 모양은 yt-dlp 소스
  (`downloader/common.py`, `utils/_utils.py::RetryManager`)에서 읽었고 **실제 로그에는 아직 없다**. 함정 두 가지를 막았다: `Giving up after` 만으로는
  판단하지 않고(라이브 주소 갱신 실패 경고도 그렇게 끝난다), bgutil 사이드카 줄(`pot:bgutil`)은 제외한다. 다른 원인이 함께 있으면 그쪽이 이긴다.
  `scripts/collect_log_phrases.py` 는 재시도 중인 줄을 `재시도 중 (실패 아님)` 으로 표시해 더는 `미분류` 문구 후보로 보이지 않게 했다.
  문구 중 `timed out`·`bytes read` 만 실제 로그 근거가 있고 나머지(getaddrinfo, connection reset 등)는 Python/OS 문구를 기억으로 적었다
  (`tests/test_failure.py` 의 `[stdlib]` 표시)

#### C-3. 알림 훅 (core/notifier.py — 신규)
- [x] (첫 단계, 메신저 없이 가능) **GUI** 가 `DownloadResult.cookie_event` 를 화면에 보여 준다 (2026-10-07): `"invalid"` 이면 다운로드 목록 행
  (일괄은 일괄 탭 행·목록 행·끝 요약)에 `⚠ 쿠키 만료 — 다시 추출 필요` 를 붙이고 `쿠키 만료` 대화상자를 한 번 띄운다. `"recovered"` 는 행에
  `새 쿠키 확인됨` 만. 같은 만료는 한 번만 알리고, 대화상자는 겹치지 않으며 창을 닫는 중에는 띄우지 않는다. 문구는
  `DownloadResult.cookie_summary`, 동작은 `interfaces/gui.py::_on_cookie_event`·`_alert_cookie_expired`, 테스트는
  `tests/test_gui_cookie_alert.py`. (이전 기록의 "로그 파일을 열기 전에는 모른다" 는 과장이었다: 코어가 `쿠키 만료로 기록했습니다.` 와
  `failure_hint` 를 로그 콜백으로 보내 정보 탭에는 `⚠️` 줄로 나왔다. 없던 것은 기본 탭의 눈에 띄는 표시였다)
- [x] 같은 표시를 CLI 에도 (2026-10-07): `--cli` 는 실행 직후, `--batch` 는 만료를 확인한 영상 뒤에 안내 4줄(7번 쿠키 자동 갱신 / 1~3번 브라우저
  쿠키 업데이트)을 내고 일괄은 `결과:` 아래에 한 줄 더 알린다. 종료 코드는 그대로다. `interfaces/cli.py::_print_cookie_expired`,
  테스트 `tests/test_cli_cookie_alert.py`
- [ ] **보류(사용자 결정, 2026-10-07):** GUI·CLI 둘 다 **실제 로그인 쿠키가 만료되는 실제 실행으로는 못 봤다** (가짜 yt-dlp 로만 확인). 만료된 실제 쿠키로 `auth_expired` 줄이
  찍히는 모양도 미확인 (위 C-2 "오류 문구" 항목). 로그인이 필요한 채널이 없어 쿠키를 로그인 상태로 쓸 계획이 없으니, 쓰기 시작할 때까지 보류한다 (`mark_valid` 프로브도 같다)
- [ ] (선택) 대화상자를 놓쳐도 만료 상태가 남아 보이게 하는 지속 배너. 단 yt-dlp 가 만료를 감지하면 `cookies.txt` 에서 로그인을 빼고 다시 쓰므로
  GUI 를 다시 켠 뒤에는 파일만 봐서는 "만료됐던 쿠키" 인지 알 수 없다 — 저장소의 기록(`cookie_health.json`)을 읽어야 한다
- [ ] `Notifier` 추상 클래스: `notify(level: Literal['info','warn','error'], message: str) -> None`
- [ ] 구현체:
  - `StdoutNotifier` — 개발/로컬용
  - `TelegramNotifier` — Bot API token + chat_id (env 변수)
  - `DiscordNotifier` — webhook URL
  - `SlackNotifier` — webhook URL
- [ ] 설정 파일: `config.toml` 또는 env 변수로 선택
- [ ] 같은 종류의 알림은 반복하지 않는다 (예: N시간에 한 번). 실패 뒤 첫 성공(`mark_valid`)에는 "복구됨" 을 한 번만 알린다
- [x] 쿠키 만료 시 메시지 (2026-10-07, 텔레그램 봇): 만료 사실, 로그인 필요한 영상은 못 받음, 쿠키 파일을 다시 받아 교체하라는 안내. "마지막 성공: 3일 전" 같은
  경과 시간은 넣지 않았다. 봇 밖(GUI·CLI)의 메신저 알림은 아직 (아래 `Notifier`)

#### C-4. 로컬 PC 측 쿠키 갱신 스크립트
- [x] `scripts/refresh_cookies.py` (신규 경로)
- [x] Windows 작업 스케줄러 / Linux cron 예시 문서화
- [x] 메인 앱 GUI 에 "쿠키 자동 갱신" 버튼 (Playwright + Chromium 번들, pip 불필요)
- [ ] 갱신 후 object storage / SCP 업로드 예시
- [ ] 갱신 실패 시 로컬 알림 (GUI 팝업 or 메신저 DM)

> 구현: [scripts/refresh_cookies.py](scripts/refresh_cookies.py) (Playwright 기반, Windows 작업 스케줄러/Linux cron 연동)

#### C-5. PO Token 대응 (✅ Phase D 로 이관 완료)
- [x] `bgutil-ytdlp-pot-provider` 사이드카 기반 자동 토큰 주입 (`core/auth.py::BgutilPoTokenAuth`)
- [x] `docker/docker-compose.yml` 로 사이드카 배포 경로 제공
- [x] `CookieHealth` 에 `po_token_valid` 필드 추가 고려 (C-1 과 함께) — 넣지 않기로 했다. PO Token 문제는
  `failure_kind=po_token` 과 사이드카 응답(`BgutilPoTokenAuth.validate`)으로 이미 보이고, 쿠키 판정과 섞으면
  403 을 쿠키 탓으로 읽게 된다

> 참고: **OAuth2 는 yt-dlp 에서 공식 제거됨**
> yt-dlp 2024.10.22 에 TV device code flow 지원이 추가되었으나 YouTube 가 차단하여 2024.11 에 [커밋 52c0ffe](https://github.com/yt-dlp/yt-dlp/commit/52c0ffe40ad6e8404d93296f575007b05b04c686) 로 제거됨 ([이슈 #11462](https://github.com/yt-dlp/yt-dlp/issues/11462)). 서드파티 플러그인(`coletdjnz/yt-dlp-youtube-oauth2`)도 OBSOLETE. **OAuth 복구는 로드맵에 포함하지 않음.**

### 완료 기준
- 쿠키 만료로 다운로드가 인증 실패한 뒤 10분 이내 운영자 메신저에 알림 수신
  (감지는 다운로드가 실패할 때만 일어난다. 요청이 없는 동안의 만료는 알 수 없고, C-1 의 선택 항목이
  그 한계를 줄인다)
- 같은 원인의 알림은 반복되지 않고, 복구되면 한 번 알린다
- 로컬에서 갱신 스크립트 수동 실행 후 봇이 다음 요청부터 정상 동작
- `cookie_health.json` 의 `events` 로 만료 주기 관측 가능

---

## Phase D — 인증 추상화 + bgutil PO Token (✅ 완료)

- [x] `core/auth.py` — `Authenticator` 프로토콜 + `CookieFileAuth` / `BrowserCookieAuth` / `BgutilPoTokenAuth` / `NoAuth` 구현
- [x] `DownloadOptions.auth` 필드 도입 + 기존 `cookie_opts` dict 하위호환 shim
- [x] GUI / CLI 인증 방식 선택 UI (쿠키파일 / 브라우저쿠키 / PO토큰(bgutil) / 미인증)
- [x] `interfaces/bot.py` 환경변수 기반 설정 (`YTDLP_AUTH_METHOD`, `YTDLP_GETPOT_BGUTIL_BASEURL`)
- [x] `docker/docker-compose.yml` + `docker/Dockerfile.downloader` + `docker/README.md`

## Phase F — 라이브 스트림 감지 + 캐치업 (✅ 완료)

- [x] `core/live.py` — `detect_live_status`, `LiveOptions`, `LiveStatusInfo`
- [x] `core/downloader.py` 라이브 플래그 자동 주입
- [x] GUI `ttk.Notebook` 3탭 구조 + 고급/라이브 옵션
- [x] ffmpeg 번들 (`build.ps1` + specs)

## Phase F-2 — fragment retry 튜닝 + end-of-stream 워치독 (일부 완료)

### 배경
2026-04-26 사고: 2시간 19분짜리 YouTube 라이브 녹화 중 방송이 fragment
8343 에서 종료되었으나, `core/downloader.py` 의 `--fragment-retries
infinite` 설정 때문에 yt-dlp 가 존재하지 않는 fragment 8344 를 영원히
재시도하며 빠져나오지 못함. GUI 의 "취소" 버튼은 cooperative 만이라
실제로 멈추지 않아 작업 관리자에서 강제 종료 후 PowerShell 로 .ts +
마지막 fragment + 오디오 수동 합성을 거쳐야 했음.

### 즉시 조치 (✅ 완료, 2026-04-26)
- [x] `--fragment-retries` 를 `infinite` → `10` 으로 변경
  (`core/downloader.py` 의 `_build_cmd`, 지금은 `LiveOptions.fragment_retries` 가 값을 정한다 — 줄 번호는 코드가 바뀌며 어긋나므로 적지 않는다)
- [x] docstring 업데이트 ([core/live.py](core/live.py))

### 향후 고도화 (검토 대상)
- [x] `LiveOptions.fragment_retries: int | None = 10` 필드 도입 (2026-10-06) — 사용자별
  네트워크 사정 (모바일 핫스팟 등 장시간 끊김) 에 맞춰 GUI/CLI 에서 조정
- [x] GUI 라이브 탭에 입력 필드 + 안내 (2026-10-06, "끊김 시 조각 재시도(회)" · "0 = 재시도 안 함").
  값 규칙: yt-dlp 의 `--fragment-retries` 는 숫자 0 이 "재시도 없음", 무한은 `infinite` 키워드다
  (`yt-dlp --help` 로 확인). 입력한 숫자는 그대로 넘기고, 무한은 별도 선택(체크박스)으로 두어
  0 을 무한으로 잘못 넘기지 않게 한다. 무한을 골랐는데 워치독(`stall_minutes`)이 꺼져 있으면 경고 —
  2026-04-26 사고(없는 조각을 영원히 재시도)를 막을 장치가 없어진다
  내부 표현은 0 이상 정수 또는 `None`(무한). CLI·환경변수는 0 이상 정수 또는
  `infinite` 를 받아 같은 값으로 변환하며, 음수·소수·기타 문자열은 거부한다
- [x] CLI 인자 노출 (`--fragment-retries N|infinite`, 2026-10-06)
- [x] `interfaces/bot.py` 환경변수 (`YTDLP_FRAGMENT_RETRIES`, 2026-10-06)
  - 구현: 값 규칙은 `core/live.py` 의 `parse_fragment_retries` / `fragment_retries_arg` 한 곳에 있다.
    `LiveOptions` 는 bool·음수·소수·문자열을 `ValueError` 로 거부한다 (`True` 가 1 로 통하지 않게)
  - GUI: 입력란(0–1000) + "무한" 체크박스. 체크하면 입력란이 꺼지고 빨간 안내가 뜬다. 입력란의 **글자를 직접**
    읽는다 (연결된 `IntVar` 는 "1.5" 를 1 로 읽는다). 비었거나 숫자가 아니면 10, 범위를 벗어나면 0–1000 으로
    자르고, 0 은 0 이다
  - CLI: `--cli` 와 `--batch` 에서만 받는다 (배포본 `yt-dlp-cli.exe` 도). GUI·봇 모드에서 주면 어디서 설정하는지
    알리고 거부한다. 주지 않은 것과 `infinite`(=`None`)를 섞지 않으려고 안 준 경우엔 인자를 아예 넘기지 않는다
  - 봇: 안 정하거나 빈 값이면 10, 잘못된 값은 `ValueError`, `run_bot` 은 오류를 알리고 2 로 끝난다
    (오타가 조용히 0 이나 무한이 되지 않도록)
  - 무한 + `stall_minutes` 꺼짐은 라이브 녹화를 시작할 때 경고한다. (`stall_minutes` 는 2026-10-06 부터 GUI·CLI·봇에서
    조절할 수 있으므로 GUI 에서도 생길 수 있다 — 아래 항목)
  - `--fragment-retries` 는 라이브 녹화에만 붙는다. 보관 영상·post_live·일반 영상에는 적용되지 않아, 바꾼 값이
    있으면 "적용되지 않습니다" 안내가 나온다. 기본값이 아닌 값은 녹화 시작 때 실행 로그에 남긴다
- [x] `stall_minutes` 를 GUI·CLI·봇에서 조절 (2026-10-06): GUI 고급/라이브 탭 "진척 없음 자동 정지(분)" 입력란(0–240, 0 = 끔),
  CLI `--stall-minutes N` (`--cli`·`--batch` 에서만, GUI·봇 모드에서 주면 어디서 정하는지 알리고 거부), 봇 환경변수
  `YTDLP_STALL_MINUTES`. 값 규칙은 `core/live.py` 의 `parse_stall_minutes` 한 곳 — 0 이상 1440 이하의 정수만 받고(초와 헷갈린 큰 수·
  음수·소수·문자열은 거부해 오타가 조용히 워치독을 끄지 않게 한다), `LiveOptions` 는 bool·음수·비정수를 `ValueError` 로 거부한다.
  GUI 는 입력란의 글자를 직접 읽고(비었거나 숫자가 아니면 5, 범위를 벗어나면 0–240 으로 자름), "무한" 재시도를 고르면 빨간 안내가
  지금의 정지 시간(또는 꺼짐 경고)을 보여 준다. 기본값이 아닌 값은 녹화 시작 때 실행 로그에 남기고, 보관 영상·일반 영상에는
  적용되지 않는다는 안내에도 포함한다. **실제 방송이 끝나는 순간에 워치독이 멈추는지는 아직 못 확인했다 (기존과 같음).**
- [x] **End-of-stream 워치독** (2026-05-13) — `StallWatchdog` 데몬
  스레드를 `core/downloader.py` 에 도입. `LiveOptions.stall_minutes`
  (기본 5분, 0=비활성) 동안 progress 콜백이 호출되지 않으면
  `process.terminate()` 송신. DASH 라이브에서 manifest 가
  dynamic→static 전환을 누락해 yt-dlp 가 무한 polling 루프에 갇히던
  사례 (2026-05-13 메타코드M 라이브) 대응.
- [x] 멈춘 라이브 녹화 자동 머지 (2026-09-28) — `core/partial_merge.py`.
  취소/종료/Ctrl+C/워치독으로 멈추면 `.part` + 아직 안 붙인 `-FragN` 을
  `<제목> (부분 녹화).mp4` 로 remux. 원본은 이어받기용으로 유지 (자동 정리는 안 함)
- [x] **라이브 캐치업 속도·안전성** (2026-09-28) — 라이브에서
  `--http-chunk-size` 제거(조각당 ~20초 지연 원인), 기본 병렬 16 → 1
  (병렬 2 이상은 yt-dlp 가 녹화가 끝날 때까지 이어 붙이지 않음)
- [x] 실행 로그 영구 저장 (2026-10-05) — `core/run_log.py`. 다운로드마다
  `~/.ytdlp-downloader/logs/` 에 yt-dlp 출력 전체 + 종료 코드 + 결과를 저장하고,
  `--no-warnings` 를 빼서 경고를 로그 파일에만 남김. 기록 항목에 `log_file` 연결, 기록 탭 [로그 열기].
  (2026-10-05 지난 라이브가 "실패" 로 기록됐는데 원인을 확인할 로그가 없던 일이 계기)
- [x] 방송 끝에서 yt-dlp 종료 코드 1 이어도 파일이 완성됐으면 완료로 처리 (2026-10-05) — 실행 로그로 원인 확인:
  방송이 끝날 때 마지막 조각 요청이 비어 yt-dlp 가 `ERROR: Did not get any data blocks` 를 내고 그 조각을
  건너뛴 뒤에도 병합·이동까지 마치지만 종료 코드는 1 로 남음 (`downloader/http.py` 의 report_error).
  오류가 그것뿐이고, 건너뛴 조각이 있고, 시작한 모든 영상의 최종 파일이 만들어졌고, 종료 코드가 1 일 때만 완료 + ⚠ 안내.
  다른 오류가 하나라도 있거나 파일이 없으면 그대로 실패. (`core.downloader._skipped_fragments`)
- [x] 방송이 막 끝난 상태(post_live)는 `--http-chunk-size` 를 빼고 받음 (2026-10-05) — 조각당 ~40초(68분에
  21MB)로 거의 멈춘 것처럼 받던 원인. 라이브 캐치업 때 확인한 "범위 요청을 서버가 ~20초 붙잡음" 과 같은 증상.
  was_live(완전히 끝난 뒤) 는 일반 영상 경로로 12MiB/s 정상.
- [x] 라이브 주소 갱신 실패 경고 표시 (2026-10-06) — yt-dlp 의
  `Cannot find refreshed url for format` 경고를 `DownloadResult.warnings` 에 보관하고
  GUI 단일/일괄 작업 상태·기록·CLI 에 조기 종료 가능성을 표시. 일반 경고는 파일에만 저장.
  성공 여부는 그대로 유지하며 녹화 길이와 로그 확인을 안내한다.
  **실제 로그에서 확인한 문구가 아니다**: 2026-10-07 기준 실행 로그 8개(2026-10-05 ~ 10-07)에 조기 종료 경고는 0건이고,
  이 문구와 테스트 샘플은 yt-dlp 소스(`extractor/youtube/_video.py`)에서 유도했다. 실제 출력은
  영상 ID 접두 없이 `...for format 137; please report this issue ... yt-dlp -U. Retrying (1/3)...`,
  세 번 실패하면 `. Giving up after 3 retries`. 실제 녹화에서 같은 경고가 로그에 남으면 샘플을 교체한다
- [ ] 다른 조기 종료 경고는 실제 실행 로그 샘플을 확보한 뒤 감지 패턴 추가. (`scripts/collect_log_phrases.py` 표에서 `라이브` 가 있고
  성공으로 끝난 실행의 문구가 후보 — 실제 녹화 길이와 대조해 확정한다. 2026-10-07 에 실제 로그 8개로 돌렸지만 라이브 실행은 2개뿐이고 둘 다 결과가 `unfinished` 라 성공한 라이브 녹화에서 나온
  후보 문구가 없다. 라이브를 끝까지 녹화해 성공한 로그가 쌓이면 다시 돌린다)
  `Video is no longer live. Retrying` 은 방송이 끝났다는 정상 신호라 조기 종료로 세지 않는다
- [x] GUI "취소" 가 실제로 자식 프로세스를 죽이도록 보강 (2026-09-24) —
  `download(cancel_event=...)` + `taskkill /T` 로 프로세스 트리 종료.
  Linux 는 프로세스 그룹 단위(`start_new_session` + `killpg`)로 종료 (2026-09-24).

## Phase J — 기본 탭 다운로드 목록 + 동시 다운로드 (✅ 완료, 2026-10-05)

- [x] 기본 탭 상단 "다운로드 목록" — 작업별 상태·받은 양·속도·경과·마지막 수신, 1초 갱신
  (`interfaces/gui_jobs.py`, Tk 없는 작업 표 + 단위 테스트)
- [x] 동시 다운로드 (기본 3, 고급 탭에서 1~5), 같은 영상 중복 거부, 선택 취소 / 전체 취소
- [x] [다운로드 시작] 버튼을 기본 탭으로 이동 (정보 탭에는 로그만)
- [x] 일괄을 목록의 한 행으로 표시 (2026-10-06, Phase I 참조)

### 후속 (검토 대상)
- [x] 한도를 넘는 시작을 거부하지 않고 자동 대기열로 (2026-10-06): `JobTable` 에 `QUEUED` 상태와 `promote()`. 단일·일괄 모두
  `대기 중 (N번째)` 로 올라가 먼저 넣은 순서대로 시작하고, 대기 중 취소는 즉시 반영, 대기는 최대 30개(`MAX_QUEUED`),
  같은 주소는 대기열에서도 중복 거부, 창을 닫는 중에는 시작하지 않는다. 설정은 시작 버튼을 누른 시점 기준.
  **실사용 확인 (2026-10-06): 배포본 `02513a0` 을 사용자 PC(Windows)에서 돌려 대기열이 동작함을 확인했다 (사용자 보고: "성공").**
  어떤 영상·몇 개로 했는지, 대기 중 취소·일괄 대기·창 닫기 같은 세부 경우를 봤는지는 전달받지 못했다 (Tk 창 테스트는 가짜 스레드·가짜 다운로드).
  대기 중에도 창을 닫을 때의 확인 문구는 "진행 중" 개수만 센다
- [x] 실사용 확인 2 (2026-10-07 15:12–15:14, `ab6b44a` 배포본 **GUI**, 동시 다운로드 1, 사용자 시험을 실행 로그 3개·기록으로 읽음): 두 번째 영상(약 243MB)이 15:14:05 에 끝나자 **같은 초에** 세 번째(라이브)가
  대기열에서 시작했고(자동 승격), 3초 뒤 15:14:08 에 취소돼 `cancelled=True`, `files=[]`, 기록 상태 `cancelled`, `Downloads` 에 `.part` 등 남은 파일 없음. 봇이 아니라 GUI 의 시험이다
  (로그에 `[ytdlp-gui-result]`, 저장 이름 `제목.mp4`, 봇 프로세스 없음). **대기 중인 채로 취소한 것은 아니다** — 대기 중에 취소한 작업은 시작된 적이 없어 실행 로그가 생기지 않는데 이 작업은 로그가 있다.
  GUI 의 "대기 중 취소"(`JobTable.cancel` 의 대기 분기)와 봇의 `/cancel`(대기 중 → `✗ 취소됨 #N (대기 중)`)은 여전히 실제로 못 봤다.
- [x] 제목이 같은 서로 다른 영상이 겹칠 때 파일명 충돌 (2026-10-06, `core/output_claims.py`): `run_download` 가 라이브 감지
  조회의 `id`·`title`·`channel` 로 출력 경로를 미리 계산해 실행 중인 다운로드끼리 등록한다. 겹치면 나중 것만
  `제목 (ID)` 로 저장하고 로그에 알린다. 같은 영상(링크 모양이 달라도)은 `skipped="busy"` 로 시작하지 않는다 (GUI 행
  "실패 (이미 받는 중)"). 한계: 조회 실패·라이브 감지 끔은 이름을 몰라 못 막고(다른 다운로드가 돌면 경고만),
  yt-dlp 가 자체 문자 치환으로 같게 만드는 이름은 못 본다. 일괄은 분야 폴더라 보통 겹치지 않는다. **실제 yt-dlp 로
  제목이 같은 두 영상을 동시에 받아 보는 확인은 아직 못 했다 (테스트는 가짜 조회·가짜 yt-dlp 실행). 새 배포본으로 해 보는 것은
  사용자가 보류함 (2026-10-06).** 이미 끝난 파일과의
  이름 충돌(yt-dlp 가 "already downloaded" 로 건너뜀)은 다루지 않는다
- [ ] 동시 다운로드 때 yt-dlp 가 종료하며 `cookies.txt` 를 동시에 다시 쓰는 경합 (확률은 낮아 손대지 않음)
- [ ] (낮음) 테스트를 돌리면 끝에 `invalid command name "...tick_jobs"` Tcl 메시지가 찍힌다 (2026-10-07, Windows). `interfaces/gui.py` 의
  `_on_jobs_changed` 가 1초 타이머(`_tick_id`)를 걸지만 `after_cancel` 하는 곳이 없어, 창이 먼저 지워진 뒤 타이머가 도는 것으로 보인다.
  테스트는 모두 통과하고 앱은 창을 닫으면 프로세스가 끝나니 영향은 출력 소음뿐일 듯하다 (원인은 확인하지 않았다)
- [x] 실사용 확인 (2026-10-06): 시사타파TV 방송 중 라이브 + 뉴스공장 방송 직후(post_live) 영상, 각 약 3분,
  임시 폴더에 받고 지움 (소스 HEAD 의 GUI + 빌드본과 같은 yt-dlp 2026.08.19, 쿠키는 방문자 쿠키 사본)
  - 일괄은 목록에서 한 행 ("📚 일괄 2/2", 받은 양·속도·ETA·마지막 수신). 방송 중인 라이브는 건너뛰어
    `.라이브대기.txt` 에 저장하고 일괄 탭 행은 "라이브라 건너뜀". 취소하면 "취소 (완료 0 · 라이브라 건너뜀 1 · 취소 1)",
    남은 목록 저장. 라이브 단일 녹화를 함께 돌려 두 행 표시 (🔴 LIVE 캐치업, 영상·오디오별 용량·속도), 기록 3건
  - post_live: 조각 약 2.3개/초, 1.6–1.8MB/s 를 계속 유지 (10-05 에는 조각당 ~40초). 85분 방송(~720MB)은
    약 7분 분량. 병렬 없이 조각 하나씩 받는 속도라 지연이 조각마다 쌓인다
  - 라이브 캐치업: 173초에 32분 분량 (약 11배속). 취소 뒤 부분 녹화 합치기 정상 (219.6MB mp4)
  - 실행 로그의 경고는 bgutil 사이드카 응답 실패 2건뿐. 조기 종료·`refreshed url` 경고는 3분 동안 없었다
  - 확인하지 못한 것: 방송이 끝나는 순간의 동작, 오래 돌려야 나오는 문제, 로그인 쿠키
  - 테스트 방법 주의: GUI 를 `root.update()` 만으로 돌리면 작업 스레드의 `root.after()` 가 "main thread is not in
    main loop" 로 실패하고 `_on_ui` 가 그 오류를 삼켜 일괄 탭 행이 "대기"에 머문다 (호출마다 ~1초씩 막히기도 함).
    앱의 문제가 아니라 시험 방식의 문제이며, `mainloop()` 위에서 돌려야 실제와 같다
- [x] 다운로드 목록의 열이 기본 창(800px)에서 잘리던 것 수정 (2026-10-06): "영상 113.4MB 받음 · 오디…",
  "1.8MB/s ETA 06…", "취소 (완료 0 · 라이브라 건너뜀…", 라이브 제목
  - 열 너비를 Treeview 글꼴(맑은 고딕 9)로 잰 실제 행 내용에 맞췄다 (라이브의 영상·오디오 용량 226px, 속도 둘 111px,
    일괄 결과 227px + 칸 여백). 기본 창은 800 → 1000px. 창 폭에 맞춰 대상·상태·받은 양 열이 나눠 갖고
    (`fit_widths`), 좁아져도 오른쪽 열이 밀려나지 않는다. ttk 는 창이 줄어도 늘어나는 열을 줄여 주지 않아
    오른쪽 열(속도·경과·마지막 수신)이 보이지 않았다
  - 그래도 긴 내용(제목, 다섯 항목짜리 일괄 결과, 두 스트림 VOD 의 ETA)은 칸에 안 들어가므로, 잘린 칸에 마우스를
    잠깐 올리면 전체 글이 툴팁으로 뜬다 (`interfaces/tk_tooltip.py`, 다운로드 목록과 일괄 탭). 칸 폭은 설정값이 아니라
    화면에 그려진 폭(`bbox`)으로 잰다
- [ ] (후보, 미검증) post_live 는 조각 목록이 끝난 상태(`Total fragments: 1027`)라 라이브와 달리 `-N` 병렬이
  안전할 수 있다. 빨라지는 만큼 요청 수가 늘어 봇 감지 위험이 있으므로 실측 뒤에 판단

## Phase I — 일괄 다운로드 + 분야별 폴더 (✅ 완료, 2026-09-28)

- [x] `core/batch.py` — `# 분야` 목록 파싱, 중복 제거, 분야별 폴더, 5~15초 휴식, 남은 목록 저장
- [x] `--download-archive` 로 다시 실행 시 받은 영상 건너뛰기, 방송 중·예정 라이브 건너뛰기
- [x] GUI '일괄' 탭, CLI `main.py --batch FILE`
- [x] GUI 일괄을 기본 탭 다운로드 목록의 한 행으로 표시, 동시 다운로드 한도에 1건으로 포함 (2026-10-06,
  작업 테이블과의 병합) — 일괄 탭 [취소] 는 일괄만, [전체 취소] 는 전부. 일괄이 받는 중인 URL 은 중복 거부.

### 후속 (검토 대상)
- [x] `--gui --batch FILE` 로 일괄 탭에 미리 열기 (2026-10-06). 자동 다운로드 없이 설정 확인 후 시작
- [ ] GUI 에서 목록 파일을 끌어다 놓기
- [x] 영상 사이 휴식 시간을 설정으로 노출 (2026-10-06): GUI '일괄' 탭 "영상 사이 휴식(초)" 최소·최대 입력란(0–600, 입력란의
  글자를 직접 읽고 비었거나 숫자가 아니면 5/15, 범위를 벗어나면 자르고, 최대가 최소보다 작으면 최소로 맞춘다), CLI
  `--batch-pause MIN-MAX` (`--batch` 와 함께만, `--gui` 에서는 거부). 값 규칙은 `core/batch.py` 의 `parse_pause`·`check_pause`
  한 곳 — 0 ≤ 최소 ≤ 최대 ≤ 600초, 오타가 조용히 휴식을 없애지 않게 거부하고 `run_batch` 도 잘못된 쌍은 하나도 받기 전에
  `ValueError`. `0~0` 은 쉬지 않음(봇 감지 위험, 안내문에 적음). 시작 로그에 "영상 사이 휴식: 5~15초" 를 남긴다.
  봇은 일괄이 없어 해당 없음. **실제 일괄을 돌려 간격이 지켜지는지는 아직 못 확인했다 (테스트는 가짜 대기·가짜 다운로드).**
- [x] 건너뛴 라이브를 `<목록>.라이브대기.txt` 에 따로 모아 두기 (2026-10-06).
  GUI [라이브 대기 목록 열기] / CLI 재실행 지원. 기존 항목과 합치고 성공·이미 받은 항목만 제거.
  중간 취소 시 미처리 URL 보존, 임시 파일 교체로 저장 중 실패해도 기존 목록 보존

## Phase H — 오디오 트랙 선택 + 다운로드 기록 (✅ 완료, 2026-09-25)

- [x] 오디오 트랙 옵션: 한국어 우선(기본) / 원본 / 영어 우선 / 일본어 우선
  — `presets.prefer_audio_language()` + `DownloadOptions.audio_lang`, GUI/CLI
- [x] 라이브 감지 조회 결과로 받을 트랙을 로그에 표시 (`🔊 오디오 트랙 N개 — …`)
- [x] 다운로드 기록 `core/history.py` — GUI URL ▼ + '기록' 탭(검색/재사용/삭제), CLI `h`
- [x] `run_download()` / `DownloadResult` — 영상별 제목·최종 경로·오디오 언어

### 후속 (검토 대상)
- [x] 오디오 트랙 옵션 실사용 확인 (2026-10-07, 21트랙 영상): 한국어 우선은 **자동 더빙(ko)** 을, 원본은 `en-US` 원본을, 영어 우선은 `en-US` 를 골랐다 (실행 로그의 `🔊 오디오 트랙 21개 — …` 줄).
  한국어 우선 + 원본 한국어 트랙이 없는 영상 = AI 더빙을 받는다는 점은 이 옵션의 설명에 적어 두면 좋다
- [ ] 영상별 실제 오디오 트랙 목록을 조회해 그중에서 고르기 (지금은 언어 선호만 지정)
- [x] `interfaces/bot.py` 환경변수 `YTDLP_AUDIO_LANG` (2026-10-07): `original` 은 원본 트랙, `ko` 같은 코드는 그 언어 우선, 잘못된 코드는 시작 거부.
  **기본값은 `original`** (사용자 결정, 2026-10-07 — 처음엔 GUI 와 같은 `ko` 였으나 21트랙 영상에서 영어 원본이 아니라 자동 한국어 더빙을 받아 전사용으로는 부적합).
  GUI·CLI 의 기본은 그대로 한국어 우선이다 (사용자가 바꾸지 않았다). 시작 로그의 설정 줄은 `audio_lang=original`.
- [ ] (사용자 제안, 2026-10-07) **여러 언어 오디오를 한 파일에 담아 플레이어에서 고르기.** 가능은 하다 — yt-dlp 의 `--audio-multistreams` 와 `-f "bv*+ba[language^=ko]+ba[language^=en]"` 식 선택자,
  병합은 `FFmpegMergerPP` 가 `-map i:a:0` 로 오디오를 전부 넣는다 (yt-dlp 2026.08.19 소스로 읽음). 이 앱에 없는 것: (1) 여러 언어를 고르는 옵션(지금은 언어 하나 우선), (2) `--embed-metadata` —
  스트림별 `language` 태그는 yt-dlp 의 `FFmpegMetadataPP` 가 쓰는데, 이 후처리는 `--embed-metadata`·`--embed-chapters`·`--embed-info-json` 중 하나가 있을 때만 붙는다
  (`yt_dlp/__init__.py` 700행). 이 앱은 셋 다 넘기지 않으니(코드 전체에서 `metadata` 옵션 없음 확인) 병합해도 언어 태그가 안 써져 플레이어에 트랙 이름이 비어 보일 수 있다
  (받은 조각 파일 자체에 태그가 있는지는 확인하지 않았다). 켜면 제목·설명 같은 다른 태그도 함께 들어간다,
  (3) 컨테이너: 지금은 `--merge-output-format mp4` 고정 — 트랙 선택은 mkv 가 더 무난하다. **실제로 받아 플레이어에서 트랙이 보이는지는 못 봤다.** 비용: 트랙마다 오디오 전체를 따로 받는다(요청 수↑ → 429·봇 감지 위험),
  21트랙 영상에서 "전부" 는 비현실적이라 원본 + 고른 언어 몇 개로 제한해야 한다
- [ ] 기록 끄기 옵션 / 기록 내보내기 (필요해지면)

## 파일명 길이 한도 (Linux)

- [x] "File name too long" 수정 (2026-10-06, `core/filename_limit.py`): 사용자 Ubuntu 정보탭 로그
  `ERROR: unable to open for writing: [Errno 36] File name too long: '…/이번 주택은 … #풀하우스.f299.mp4.part'` 로 확정. 제목이 98자·248바이트
  한글이라 `.f299.mp4.part` 가 붙은 이름이 262바이트로 Linux 한도 255바이트를 넘었다 (완성본 `.mp4` 는 252바이트). 저장 폴더의 한도를
  읽어 `%(title).NB` (바이트 단위 자르기)로 묶는다. 짧은 제목은 그대로, 영상 정보가 있으면 실제로 넘칠 때만 줄이고 알린다.
  yt-dlp 2026.08.19 로 4가지 파일명 형식 × 한도 255/143 × 여러 제목을 확인했다 (`tests/test_filename_limit.py`).
- [ ] 실제 Ubuntu 에서 같은 영상(`https://youtu.be/D1WknJLGrIc`)을 받아 이제 되는지 확인 — 클라우드에서는 YouTube 접속이 막혀 제목을
  가져오지 못했고 사용자 로그의 제목으로만 재현했다. 확인할 것: 받는 중 `.part` 가 생기는지, 완성 파일 이름이 제목 앞부분인지,
  정보 탭에 `제목이 길어 파일명이 …` 안내가 나오는지
- [ ] 한계: (1) 영상 정보를 읽지 못해 제목을 모르면 `" * : < > ? | / \` 가 많은 긴 제목은 못 막는다 (고정 여유 10바이트 안에서만).
  (2) 암호화된 홈(143바이트)에서 채널명이 매우 길면(120바이트 상한) '채널명 - 제목' 형식은 제목 최소치(24바이트)로도 못 맞출 수 있다.
  (3) 저장 폴더·분야 폴더 이름 자체가 한도를 넘는 경우는 다루지 않는다. (4) Windows 는 글자 수와 전체 경로 260자 한도라 건드리지 않았다 —
  긴 저장 경로 + 긴 제목에서 같은 종류의 실패가 나는지는 확인하지 않았다

## 배포본 재빌드 (사용자 PC, Windows)

- [x] 배포본 재빌드 (2026-10-06, 사용자 PC): `main` 의 `02513a0` 으로 `build/build.ps1` 을 돌려 `yt-dlp-downloader-02513a0-win-x64.zip`
  (597.3 MB, build.ps1 출력)을 만들었다. 이전 배포본(`f1229a7`)에 없던 변경이 모두 들어 있다 — 다운로드 목록 열 너비 수정(`267eeb2`),
  동시 다운로드 파일명 충돌 방지, 자동 대기열, 진척 없음 자동 정지·일괄 휴식 시간 설정. 빌드(`build/build.ps1`)는 Windows 에서만 돌릴 수
  있어 클라우드에서는 하지 못했고, 로그의 "BUILD COMPLETE" 까지만 전달받았다. 이전 세션 기록에 따르면 빌드가 `build/dist` 를 지우고
  `cookies.txt` 를 자리표시자로 다시 쓰므로, 쓰던 쿠키는 따로 복원해야 한다. **2026-10-07 확인: `build/dist/yt-dlp-downloader/cookies.txt` 는 자리표시자 그대로다**
  (673바이트, 데이터 행 0 — 복원되지 않았다. GUI 가 다른 경로의 쿠키 파일을 쓰는지는 모른다). `build/dist` 에는 이전 빌드 zip 3개
  (`38d1f81`, `8c0b3f9`, `b7618f0`, 합계 약 1.7 GiB) 가 남아 있다 — 지울지는 사용자가 정한다.
  `scripts/` 의 도구(쿠키 갱신·로그 집계)는 빌드 스펙에 없어 배포본에 들어가지 않는다 — 소스 폴더에서 실행한다
- [x] 배포본 재빌드 (2026-10-07 14:51, 사용자 PC): **`ab6b44a`(지금의 HEAD)로 다시 빌드**했다. 2026-10-07 14:56 에 파일로 확인한 것 — `build/dist/yt-dlp-downloader/_internal/_app_version.txt` 가
  `0.1.0 (ab6b44a)`, `yt-dlp-gui.exe`·`yt-dlp-cli.exe` 수정 시각 14:51:52, `yt-dlp-downloader-ab6b44a-win-x64.zip`(597.4 MiB, 14:52:32). 그래서 `94a79fc` 빌드에 없던 것이 모두 들어 있다 —
  `HTTP 429` 분류(`rate_limited`), 자막 다운로드가 실패하면 자막 없이 영상 받기(`6be4dd7`), 일괄에서 자막 없이 받은 영상의 자막 보충(`ab6b44a`).
  `build/dist/yt-dlp-downloader/cookies.txt` 는 다시 **자리표시자**다(673바이트, 데이터 행 0 — 재빌드가 덮어쓴다. 쓰던 것은 방문자 쿠키뿐이라 잃은 로그인은 없다).
  `build/dist` 에는 `94a79fc` zip 과 이번 zip 둘뿐이다 — **이전 빌드 zip 6개(약 3.5 GiB)는 지금 없다.** `build.ps1` 은 `-Clean` 일 때만 `dist` 전체를 지우는데 `94a79fc` zip 이 남아 있으니
  이번 빌드가 지운 것은 아니고, 사용자가 지운 것으로 보이나 확인하지는 않았다. `Downloads\bot-test` 의 시험 파일 3개(355 MiB)는 그대로다.
  **이 빌드를 실행해 본 것은 아니다** (파일·버전 표시만 확인). 이 빌드로 한 시험의 실행 로그도 아직 없다 — `~/.ytdlp-downloader/logs` 의 가장 새 로그(18개째)는 13:17 이라 빌드(14:51)보다 앞선다.
- [x] 배포본 재빌드 (2026-10-07 12:31, 사용자 PC): **`94a79fc`(그때의 HEAD)로 다시 빌드**해 `yt-dlp-downloader-94a79fc-win-x64.zip`(626,416,512 B = 597.4 MiB)을 만들었다.
  그래서 아래 "들어 있지 않은 것" 목록은 이제 해당 없다 — 마지막 오류 줄, `yt_dlp_missing`, 라이브 판별 문구, 끝 메시지 재전송, `po_token` 안내문, 진행률 `N/A` 정리가 모두 들어 있다
  (이후 만든 `429` 분류는 들어 있지 않다). `build/dist/yt-dlp-downloader/cookies.txt` 는 이제 자리표시자가 아니다(965바이트, 데이터 9행) — 다만 **로그인 쿠키는 없다**(방문자 쿠키만, 이름만 확인).
  이전 빌드 zip 6개(`02513a0`, `38d1f81`, `3c748d8`, `8c0b3f9`, `b7618f0`, `c594526`, 합계 약 3.5 GiB)가 남아 있었다 (14:56 확인: 지금은 없다 — 위 `ab6b44a` 항목). 사용자가 이 빌드(GUI)로 같은 21트랙 영상을 오디오 선택(한국어·원본·영어 우선)을 바꿔
  받았고 로그에 `한국어(ko) 선택`·`원본(en-US) 선택`·`영어(en-US) 선택` 이 나와 **오디오 트랙 옵션이 실제로 동작함**을 확인했다 (Phase H).
- [x] 배포본 재빌드 (2026-10-07 05:55, 사용자 PC): `3c748d8` 로 빌드해 `yt-dlp-downloader-3c748d8-win-x64.zip`(626,407,716 B = 597.4 MiB)을 만들었다 (그 사이 `c594526` 로도 한 번 빌드한 zip 이 있다).
  들어 있는 것: GUI·CLI 쿠키 만료 표시, `network` 분류, 텔레그램 봇, `/video`. **들어 있지 않은 것**(`dc63b7b` 이후): 실패 메시지의 마지막 오류 줄, `yt_dlp_missing` 분류, 라이브 판별 실패 문구,
  끝 메시지 재전송(보관함), `po_token` 안내문과 진행률 `N/A` 정리. `build/dist/yt-dlp-downloader/cookies.txt` 는 또 자리표시자(673바이트)다. `build/dist` 에는 이전 빌드 zip
  5개(`02513a0`, `38d1f81`, `8c0b3f9`, `b7618f0`, `c594526`, 합계 약 2.9 GiB)가 남아 있다 — 지울지는 사용자가 정한다.
  **포장된 `yt-dlp-cli.exe --bot` 이 동작함을 확인했다** (2026-10-07): 토큰을 비운 채 오프라인으로 실행하면 봇 모듈이 번들돼 있어 토큰이 없다는 안내와 함께 종료 코드 2 로 끝난다.
  이 방식으로는 텔레그램 연결까지는 확인하지 못했다.
- [x] 새 배포본의 대기열 실사용 확인 (2026-10-06): 성공 (위 Phase J 후속 항목 참조)
- [ ] 새 배포본의 나머지 확인 — **사용자가 보류함 (2026-10-06)**. 하게 되면 이 순서로:
  1. 파일명 충돌 방지: 제목이 같은 서로 다른 영상 두 개를 동시에 받아 두 번째 파일명에 ` (ID)` 가 붙는지 (조회 결과로 미리 계산한 이름이
     yt-dlp 가 실제로 쓰는 이름과 어긋나지 않는지가 핵심)
  2. 새 입력란 배치: 고급/라이브 탭 "진척 없음 자동 정지(분)", 일괄 탭 "영상 사이 휴식(초)" 이 기본 창에서 깨지지 않는지, "무한" 재시도를
     고르면 빨간 안내가 보이는지
  3. 일괄 휴식 `0~0` 으로 짧은 목록을 돌려 로그에 "영상 사이 휴식: 쉬지 않음" 이 나오는지
  4. 로그 집계: 소스 폴더에서 `python scripts\collect_log_phrases.py --output report.md` 를 돌려 결과를 받아 C-2·F-2 항목을 정리
     (**2026-10-07 했음** — 결과는 C-2·F-2 항목에 적었다. 새 로그가 쌓이면 다시 돌린다)

## Phase G — 배포 zip 사이즈 다이어트 (보류)

### 배경
2026-05-01 빌드 통합 완료 시점 기준 배포 zip 약 1.4GB. (2026-10-06 재측정: 압축을 푼 폴더
1,391MB, zip 583MB — '약 1.4GB' 는 지금 폴더 크기에 해당하고 zip 은 그보다 작다. 아래 후보별
크기도 현재 빌드로 다시 측정한 뒤 판단하고, 측정은 빌드 하나(폴더 + zip) 기준으로 한다.
이후 빌드는 폴더 1397MiB · zip 597MiB 로 기록되어 있다 (이전 세션 인수인계 기록 — 이 환경에서는 빌드·측정을 못 해 확인하지 못했고,
위 MB 수치와 단위를 같게 쟀는지도 알 수 없다. 다음 측정 때 한 방법으로 폴더와 zip 을 다시 재서 한 번에 고쳐 적는다.)
`02513a0` 빌드(2026-10-06)를 2026-10-07 에 한 방법(폴더는 `du -sb`, zip 은 파일 크기)으로 재 보니 폴더 1,458,751,422 B = 1,391.2 MiB,
zip 626,337,619 B = 597.3 MiB 다. build.ps1 이 출력한 "597.3 MB" 는 MiB 단위였다. 위 인수인계의 '폴더 1397MiB' 와는 6 MiB 어긋나는데
이유는 확인하지 않았다 — 앞으로는 이 방법으로 잰 값을 기준으로 삼는다.
**오프라인/저속망
USB 배포 시나리오를 감안하여 현 사이즈 유지**. 향후 네트워크 배포가
주류가 되거나 저장 공간 압박이 생기면 아래 항목 검토.

### 후보 (효과 예상 큰 순)
- [ ] **Playwright Chromium localization 정리** (현재 ~672MB) — `locales/*.pak`
  중 한국어/영어만 남기고 제거. 수백 MB 절감 가능. 빌드 hook 으로 자동화.
- [ ] **ffmpeg/ffprobe 경량 빌드** (현재 합산 387MB) — gyan.dev `essentials_build`
  대신 `essentials_build_shared` (DLL 분리) 또는 `core` 빌드. 50~80% 절감.
  단, 라이센스/코덱 확인 필요.
- [ ] **deno lazy 다운로드** (현재 122MB) — JS challenge 가 실제로 필요할 때만
  on-demand fetch. 오프라인 환경에서는 무용지물이 될 수 있어 USB 배포에는
  부적합. 온라인 배포 전용 옵션.
- [ ] UPX 압축 — DLL/exe 적용 시 안티바이러스 오탐 리스크. 비추천.

## Phase D' — Whisper 전사 파이프라인 (별도 이슈로 추적)

- [ ] `core/transcriber.py` 백엔드 구현 (faster-whisper / whisper.cpp / onnx-ryzen)
- [ ] 백엔드 선택: `config.toml` 또는 env 변수
- [ ] Ryzen AI NPU 전용 경로와 x86 GPU 경로 분리
- [ ] 전사 결과 포맷 (txt, srt, vtt, json with timestamps)
- [ ] 긴 영상 청크 분할 처리

## Phase E — 메신저 봇 연동 (텔레그램 구현됨, 2026-10-07)

- [x] `interfaces/bot.py` 구현: `BotService`(동작)와 `core/telegram.py`(전송)를 나눴다. `python main.py --bot`, 사용법은 README "텔레그램 봇"
- [x] 첫 타깃: **Telegram**, 표준 라이브러리(`urllib`)만 쓰는 클라이언트. `python-telegram-bot` 은 쓰지 않았다 — 배포 의존성을 늘리지 않고
  로컬 가짜 서버(`tests/fake_telegram.py`)로 HTTP 계층까지 시험할 수 있어서다. 롱 폴링(`getUpdates`)이라 공개 주소·웹훅이 필요 없다
- [x] 인증: 허용된 chat_id 화이트리스트 (`YTDLP_BOT_ALLOWED_CHATS`). **비어 있으면 시작하지 않는다**(닫힌 기본값). 목록 밖의 채팅에는 처음 한 번만
  chat id 를 알려 준다 (그 id 를 알아야 목록에 넣을 수 있다 — 처음엔 `0` 을 넣고 시작하는 방법을 안내). 링크는 YouTube 만 (`YTDLP_BOT_ALLOWED_HOSTS` 로 확장),
  서버 안쪽 주소를 건드리는 링크를 막는 기본값
- [x] 다운로드 큐 & 동시성 제한 (`YTDLP_BOT_MAX_PARALLEL` 1–5 기본 2, `YTDLP_BOT_MAX_QUEUED` 기본 20), 같은 링크 중복 거부, `/status`, `/cancel [#번호]`
  (대기 중은 즉시, 받는 중은 프로세스 종료 + 라이브는 받은 부분 병합), 종료 시 전부 취소
- [x] 진행률 메시지 edit: 상태 메시지 한 개를 고쳐 쓴다 (4초에 한 번, 같은 글은 건너뜀, 요청 제한(429)이면 텔레그램이 말한 만큼 쉼). 텔레그램 쪽
  실패로 다운로드가 실패하지는 않고, 끝 메시지는 수정이 안 되면 새 메시지로 보낸다
- [x] 쿠키 만료 알림(C-3 의 메신저 쪽): `DownloadResult.cookie_event` 가 `invalid`/`recovered` 일 때 허용된 모든 채팅에 한 번 알린다
- [x] 그룹에서 쓰기 (2026-10-07): 텔레그램 **privacy mode** 라 그룹의 봇은 기본으로 링크만 보낸 일반 메시지를 받지 못하는데(공식 문서 `bots/features`) 영상 받기가
  "링크만 보내기" 뿐이었다. `/video <링크>` 를 추가해 `/audio` 와 같이 그룹에서 명령으로 쓸 수 있게 했고(`/video@봇이름` 은 이 봇만 응답, 다른 봇을 가리킨 명령은 무시),
  README 에 그룹 안내(`/setprivacy` → Disable 뒤 봇 재추가)를 적었다. "일반 메시지를 받지 못한다" 는 문서의 "privacy mode 에서 받는 것" 목록에서 읽은 것이고
  링크 메시지가 명시된 것은 아니다 — 실제 그룹에서 확인할 것
- [x] 같은 봇을 다른 곳에서 폴링 중이거나 웹훅이 설정돼 있으면(`getUpdates` 가 `409`) 이유를 설명하고 종료 코드 2. **웹훅을 지우지(`deleteWebhook`) 않기로 했다** —
  남이 의도적으로 설정한 웹훅일 수 있어서 말없이 지우면 안 된다
- [x] 봇 콘솔에 실행 버전과 `YTDLP_*` 환경변수를 시작할 때 찍는다 (2026-10-07, 사용자 요청): `version: 0.1.0 (커밋)` 과 변수 목록(비밀은 `<set>`, 허용 채팅은 개수, PATH 등은 안 찍음). 사용자의 실제 콘솔로는 봇을 다시 켠 뒤에 확인된다 (아직 못 봄)
- [ ] **bgutil 사이드카가 Windows 에서 `127.0.0.1` 로 안 닿는다** (2026-10-07 확인, 사용자 PC): 사이드카는 WSL2(NAT 모드) 안의 podman 컨테이너이고 Windows 의 `wslrelay` 가 4416 을 **`::1`(IPv6)에서만** 받는다 —
  `http://127.0.0.1:4416` 은 연결 거부, `http://localhost:4416`·`http://[::1]:4416`·WSL IP 는 응답. 앱의 기본 주소가 `127.0.0.1:4416`(봇·GUI·CLI, 인증이 `none` 일 때 yt-dlp 플러그인의 기본값도)이라 **봇의 모든 실행 로그에 `Error reaching GET http://127.0.0.1:4416/ping`**
  이 있었고 PO Token 은 한 번도 쓰이지 않았다. `base_url=http://localhost:4416` 로 번들 yt-dlp 를 `--simulate` 로 한 번 돌리니 플러그인(`bgutil:http-2.0.1`)이 gvs PO Token 을 받았고 서버(v2.0.1)가 `Generating POT` 을 찍었다.
  봇에서 쓰려면 `YTDLP_AUTH_METHOD=bgutil`, `YTDLP_GETPOT_BGUTIL_BASEURL=http://localhost:4416`. **시작 직후 403 과의 관계는 가설일 뿐 확인하지 않았다** — 쓰면서 403 빈도를 비교한다. 열린 질문: 앱의 기본 주소를 `localhost` 로 바꿀지(리눅스·도커에서도 `localhost` 는 동작한다),
  WSL 컨테이너가 WSL 종료 뒤 자동으로 안 떠서(재시작 정책 없음) 사이드카가 꺼져 있으면 어떻게 알릴지
- [ ] 받은 파일을 텔레그램으로 돌려주기 — 지금 답장은 봇이 도는 컴퓨터의 저장 경로다. `sendDocument` 는 50MB 한도라 큰 파일은 링크 방식이 필요
  (README 한계 참조)
- [ ] 전사 결과 파일 전송 (대용량 시 링크 방식) — Phase D' 이후
- [x] **실제 텔레그램 첫 시험 (2026-10-07, 사용자 PC·개인 채팅)**: 성공. 확인됨 — 토큰으로 시작·허용 채팅 확인(`0` 으로 시작해 chat id 알아내기 포함), `/video` 로 영상 하나
  (55 MiB, 22초, 영상+오디오 병합된 mp4, 저장 경로가 답장과 일치), **진행률이 상태 메시지 한 개로 고쳐짐**(스크린샷: `⏬ #2 72% · 259.8MB`), `/status`, 같은 링크 중복 거부
  (`이미 받는 중이거나 대기 중입니다 (#2)`). 첫 시도는 봇 문제가 아니라 환경 때문에 실패했다: 소스 실행이 PATH 에서 `yt-dlp` 를 먼저 찾는데 고장난 낡은
  `Python39\Scripts\yt-dlp.exe`(`No module named 'yt_dlp'`)가 잡혔다. 번들 `_internal` 을 PATH 앞에 두고 다시 하니 됐다. 이 일로 아래 개선 세 가지가 나왔다
  (실패 메시지에 마지막 오류 줄, `yt_dlp_missing` 분류, 라이브 판별 실패 문구).
- [x] 실제 시험 추가 확인 (2026-10-07, 사용자): **`/audio`** 로 MP3 가 저장됨(`제목 (영상ID).mp3`), **`/cancel`** 로 받는 중인 작업이 `✗ 취소됨 #2` 로 끝남.
  첫 `/audio` 는 `HTTP 403`(분류 `po_token`)으로 실패했지만 같은 링크를 다시 보내니 성공했다. 앱이 쓴 명령을 그대로 재현해도(4회: 단독 1 + 병렬 3) 모두 성공했고,
  형식별 1KB 요청이 전부 206 이며, 앞 작업(`#2`)은 05:37:03 에 끝나 겹치지도 않았다 → 일시적 403 으로 판단한다 (표본 1건이라 단정은 아님). 그 영상은 오디오 트랙이 21개
  (원본 영어 + 자동 더빙 20개)라 `YTDLP_AUDIO_LANG=ko` 가 **한국어 자동 더빙**을 골랐다 — 전사(Whisper) 용이면 `original` 이 낫다 → **봇의 기본값을 `original` 로 바꿨다** (2026-10-07, 위 `YTDLP_AUDIO_LANG` 항목).
- [x] **실제 시험 (2026-10-07 15:42–15:50, 사용자 PC, 소스 `c70427c` 의 `python main.py --bot`, 개인 채팅, 동시 1)**: ① **대기 중인 작업의 취소를 봤다** — #4 가 `✗ 취소됨 #4 (대기 중)` 로 끝났고 #4 의 실행 로그는 없다(시작된 적 없음).
  ② 번호 없는 `/cancel` 은 **이 채팅의 받는 중·대기 중 작업을 전부** 취소한다 (설계: 도움말 "번호가 없으면 이 채팅의 전부") — 받는 중이던 #3(580.6MB, 33% 지점)도 15:50:22 에 `✗ 취소됨 #3` 이 됐다.
  사용자는 "#4 를 취소했는데 #3 도 취소됐다" 고 느꼈는데, 붙여 준 대화에 `/cancel` 줄은 없고 끝의 `//cancel` 표기로 번호 없이 보냈다고 **추정**했다(확인하지 못함). 번호를 주면(`/cancel 4`, `/cancel #4`) 그 작업만 취소하는 것은
  코드(`BotService._cancel`)와 새 테스트(`test_a_numbered_cancel_leaves_the_running_download_alone`)로 확인했다. 기존 테스트는 이 경우에 실행 중인 작업이 계속 받는지를 보지 않았다(끝났는지만 봤다).
  **그 뒤 15:56 에 번호를 준 `/cancel #6` 을 실제로 봤다** — 단, 앞 작업(#5, 18MB)이 23초에 끝나 #6 이 곧바로 시작했으므로 **받는 중인 #6 을 취소**한 것이다(11초째 취소, `files=[]`). 대기 중인 작업을 번호로 취소하면서
  다른 작업이 계속 받는 경우는 **실제로 못 봤다**.
  ③ 같은 실행에서 **새 기본값(원본 오디오)이 실제로 적용된 것을 로그로 봤다** — `yt-dlp command` 의 `-f` 에 `[language^=ko]` 가 없고 `🔊 오디오 트랙 2개 — 원본(ko) 선택`.
  아직 못 본 것: 목록 밖 채팅의 거부 답, 실제 Windows 에서 Ctrl+C 종료, 그룹에서 `/video` 와 privacy mode 의 실제 동작, **대기 중인 작업을 번호로 취소하고 받는 중인 작업은 계속되는 것**
  (시험법: 오래 걸리는 영상 A 와 아무 영상 B 를 **한 메시지에** 함께 보내 둘 다 대기열에 넣고 바로 `/cancel #B번호` — 580MB 영상은 약 90초 걸린다. 앞서는 앞 작업이 너무 빨리 끝나 대기 상태를 못 만들었다).
- [x] **시작하자마자 `HTTP 403` 이 나면 한 번 자동으로 다시 시도한다** (2026-10-07, 구현 — 아래 "구현"). 표본은 **3건**이다 — 첫 `/audio`(재시도에 성공)와 2026-10-07 15:48 의 `/video`(`KtquxqP2YnM`, 로그 `20261007-154812_KtquxqP2YnM.log`): `Downloading 1 format(s): 299+140` 직후
  받은 바이트 0 으로 `unable to download video data: HTTP Error 403: Forbidden`, 12초에 실패(`po_token`). **같은 링크를 사용자가 약 8분 뒤(15:56) 다시 보내니 성공했다**(`20261007-155616_KtquxqP2YnM.log`, 같은 `299+140`, 23초). 첫 건도 다시 보내니 성공했으니 **두 건 모두 다시 보내면 성공**이다.
  **세 번째**: 16:04 `V73LFPxvEUY` (`137+140`, 로그 `20261007-160406_V73LFPxvEUY.log`) — 같은 모양으로 11초에 실패, 사용자가 다시 보내 3분 25초 뒤(16:07:43) 성공(`20261007-160743_V73LFPxvEUY.log`, 23초).
  **세 건 모두 다시 하면 성공**, 간격은 3분 25초 ~ 17분 38초.
  **구현**: `core/downloader.py::run_download` — `po_token` 으로 분류되고, `ERROR` 줄이 `HTTP Error 403`(`core.failure.http_403_error`, 경고만 있는 것은 제외)이고, 끝낸 영상이 없고(`records` 비어 있음), **진행 줄이 보고한 받은 양이 1 MiB 미만**(`HTTP_403_RETRY_MAX_BYTES`, `captured.received` — 이 값은 진행 줄의 가장 큰 `got` 이다. 처음 구현은 `Destination:` 줄이 없을 때만이었고 4·5번째 사례가 나와 넓혔다 — 아래),
  취소가 아닐 때만 `HTTP_403_RETRY_DELAY`(10초, 취소하면 즉시 깸) 뒤 처음부터 **한 번** 다시 한다(받던 `.part` 는 yt-dlp 가 이어받는다). 두 번째 결과가 첫 결과를 대신한다(성공이면 그대로 완료, 실패면 그 실패로 분류). 로그에 `HTTP 403 — 받기를 시작한 직후라 10초 뒤 한 번 다시 시도합니다` 와 `재시도 성공!`/`재시도도 실패했습니다`가 남고,
  재시도도 실패하면 안내가 "자동으로 한 번 다시 시도했지만 같은 오류였습니다. 몇 분 뒤 다시 보내면 받아진 적이 있습니다"로 바뀐다. 더 강한 원인(요청 제한·봇 차단 등)이 같이 있으면 하지 않는다.
  **10초는 짐작이다 — 실제 성공은 모두 3분 넘게 뒤였고 몇 초 뒤 다시 하면 풀리는지는 모른다.** 사용하며 로그의 `재시도 성공/실패` 를 모아, 실패가 많으면 대기를 늘리거나 몇 분 뒤 2차 시도를 넣는다 (사용자 결정 2026-10-07: 케이스를 미리 다 시험하기 어려우니 쓰면서 패턴이 보이면 대응).
  **취소와의 연관(사용자 추정)**: 세 번째 건은 앞 작업을 취소한 직후(2초 뒤)에 시작해 났다. 하지만 첫·둘째 건은 앞 작업이 정상 완료된 뒤(둘째는 2초 뒤)였고 첫 건은 5분 뒤였으며 취소와 무관하다 → 취소가 필요조건은 아니다. 취소가 원인일 만한 경로도 못 찾았다(403 은 YouTube 응답이고
  취소는 yt-dlp 프로세스를 죽일 뿐; 이 봇 실행의 로그에는 `cookie file:` 줄이 없어 쿠키 파일도 쓰지 않았다). 취소 직후 시작한 다음 작업 표본은 이 한 건뿐이라 **연관 없음으로 확정하지도 못한다** — 모아 가며 본다.
  **네 번째, 모양이 다른 403** (16:16:08 `81SzuX_qzP4`, 580MB, `137+140-1`, 재시도 도입 전 코드 `c70427c`, 로그 `20261007-161608_81SzuX_qzP4.log`): `[download] Destination:` 이 **찍힌 뒤** `0.0% got 1.0KB`(모든 받기의 첫 진행 줄) 다음 2초 만에 `unable to download video data: HTTP Error 403` —
  바이트가 오기 전이 아니라 첫 조각 직후다. 앞 작업(V73, 16:08 정상 완료) 뒤 8분이라 취소와도 무관. 48분 뒤(17:04) 다시 보내니 성공(`4d31d13`, 403 없음).
  **다섯 번째, 같은 모양** (20:10:08 `P7W9r85qPpI`, 203.6MB, `299+140-2`, 코드 `d7f26f9` = 재시도 포함, 로그 `20261007-201008_P7W9r85qPpI.log`): `Destination:` 과 `0.0% got 1.0KB` 다음 2초 만에 403 — `Destination` 줄 때문에 **재시도가 걸리지 않았다**.
  사용자가 `/video` 를 붙여 다시 보냈지만(`/video` 와 링크만 보내는 것은 같은 경로다, `interfaces/bot.py` 의 `command in ("audio", "video", None)`) **4분 55초 뒤(20:15:17) 성공**(`20261007-201517_P7W9r85qPpI.log`, 43초).
  **중요한 발견**: 이 재시도 로그에 `Resuming download at byte 10006282` 가 있다 — 실패한 시도가 진행 줄은 `got 1.0KB` 한 줄만 찍었는데 **디스크에는 약 10MB 가 이미 받아져 있었다**(yt-dlp 는 진행 줄을 드문드문 찍는다). 그래서 이 403 은 "바이트가 오기 전"이 아니라
  **받기 시작 2초쯤 뒤, 10MB 정도 받은 직후**(조각 단위 요청의 다음 요청으로 보인다 — 추정)에 났다. 로그만 봐서는 같은 모양인 16:16 건도 마찬가지였을 수 있다 (그 재시도는 `Resuming download at byte 354922862` 였지만 그 사이 취소된 같은 영상의 `.part` 가 있었을 수 있어 단정하지 않는다).
  **그래서 조건을 넓혔다**: "진행 줄이 보고한 양이 1 MiB 미만이면 시작 직후로 본다" (한계: 보고가 드문드문이라 실제로 받은 양과 다르다 — 주석에 적었다). 그 이상 진행한 뒤의 403 은 아직 본 적이 없어 재시도하지 않는다. 이어받기 덕에 이 재시도는 싸다.
  **재시도가 실제로 쓰인 로그는 아직 없다**(`d7f26f9` 에서는 이 모양에 걸리지 않았고, 넓힌 코드는 아직 돌려 보지 못했다).
  단서 하나(원인으로 단정하지 않음): 성공한 재시도의 로그에만 `Downloading player 1b3be681-main`·`[jsc:deno] Solving JS challenges using deno` 줄이 있다. 성공으로 끝난 실행 로그 16개(16:03 기준, 전체 27개) 중 이 줄이 있는 것은 이것 하나이고 나머지 15개에는 없어서 403 의 원인이라고 볼 근거는 못 된다.
  다섯 건 모두 형식 선택 직후(첫 요청이나 2초 안)에 났고(로그로 확인) 형식은 제각각이다(`140-10` 오디오 전용 더빙 트랙, `299+140`, `137+140`, `137+140-1`, `299+140-2`) — 특정 형식 탓으로 보이지는 않는다.
  다시 하면 성공한 간격: 17분 38초, 7분 52초, 3분 25초, 48분, 4분 55초 (몇 초 뒤의 재시도는 아직 못 봄)
- [x] 끝 메시지를 연결이 불안정해도 잃지 않는다 (2026-10-07): 사용자 PC 의 텔레그램 연결이 간헐적으로 끊겼다(`WinError 10060` 반복, 첫 줄은 다운로드 중 상태 수정 시간 초과).
  측정: 왕복 약 275ms 라 호출 한 번이 약 1.1초(TCP 275 + TLS 275 + 응답 550ms), 한 번 새 연결을 맺는 구조에서 5번 중 1번 10초 시간 초과, 인증서는 정상(GoDaddy, 가로채기 아님).
  이전에는 끝 메시지의 수정이 실패하면 새 메시지로 **한 번**만 다시 보내고 포기해, 다운로드가 성공해도 알림을 못 받을 수 있었다. 이제 완료·실패·취소 메시지와 **쿠키 만료 알림**
  (한 번만 나가므로 잃으면 영영 모른다)은 실패하면 보관함에 넣고 별도 스레드가 5→10→20→40→60초 간격(마지막 간격 반복)으로 다시 보낸다. 다시 시도할 만한 오류만
  (연결 끊김·`429`(텔레그램이 말한 시간만큼 기다림)·`5xx`) — 차단됨(403)·채팅 없음·잘못된 토큰은 안 한다. 작업마다 최신 끝 메시지 하나만 보관, 최대 1시간·200개,
  종료 때 마지막으로 한 번 더 시도(첫 실패에서 멈춤). 진행률 갱신과 `/status` 같은 답은 보관하지 않는다. 보내다 시간 초과된 새 메시지가 실제로는 도착했다면 중복될 수 있다(적어도 한 번 전달).
- [ ] 같은 연결 재사용(keep-alive) — 지금은 호출마다 새 TCP+TLS 연결이라 불안정한 경로에서 실패할 기회가 많다. `core/telegram.py` 가 `urllib.request` 대신 스레드별 `http.client` 연결을 쓰도록
- [ ] 연속 실패 뒤 복구했을 때 로그 한 줄 (`연속 N번 실패 후 복구`) — 지금은 복구했는지 로그만으로 알 수 없다
- [x] 진행률 메시지가 `⏬ #2 72% · 259.8MB · N/A · ETA --:--` 처럼 `N/A`·`--:--` 를 그대로 보여 주던 것 (yt-dlp 가 속도·남은 시간을 모를 때, 실제 스크린샷) —
  이제 모르는 항목은 뺀다 (`⏬ #2 72% · 259.8MB`, 라이브는 `12.0MB 받음 (라이브)`) (2026-10-07). **봇만** 고쳤다: GUI 의 정보 탭 진행 줄과 CLI 의 진행 줄(`ProgressDisplay.format_line`)은
  아직 `N/A` 를 그대로 보여 준다
- [x] 실패 메시지에 yt-dlp 가 한 말을 넣는다 (2026-10-07): 첫 시험의 실패 답이 `✗ 실패 #1` 과 로그 경로뿐이라 원인을 알 수 없었고, 봇이 서버에서 돌면 로그 경로를 열어 볼 수도 없다.
  이제 안내 한 줄 다음에 `마지막 오류:` 로 오류 줄을 최대 3개(중복 없이, 줄마다 200자까지) 인용한다. 다운로더 자신의 요약 줄(`다운로드 실패.`, `yt-dlp exit code: N`)은 뺀다
  (실제 `run_download` 로 그 문구와 어긋나지 않는지 시험으로 고정). 성공한 실행에는 넣지 않는다.
  **텔레그램 공식 문서(2026-10-07)와 대조한 결과**: 확인됨 — 받지 못한 메시지는 24시간까지만 보관, `getUpdates` 의 `offset`·`timeout`(초),
  `reply_parameters`(`message_id`, `allow_sending_without_reply`), 오류 응답이 `ok:false`+`error_code`+`description`, 웹훅이 있으면 `getUpdates` 가 동작하지 않음.
  **기억에 의존하고 문서에서 못 찾음** — 텍스트 4096자 한도, `ResponseParameters.retry_after`, `disable_web_page_preview` 의 폐기 여부, 오류 문구
  `message is not modified`(틀리면 같은 글 수정이 경고와 30초 쉼으로 끝나는데 같은 글은 아예 보내지 않아 드물다), 같은 봇 두 곳 폴링 때 `409`.
  첫 실제 시험에서 어긋나면 `core/telegram.py` 를 고친다
- [ ] (2026-10-08) 컨테이너 콘솔의 `version:` 줄에 커밋이 안 나온다(`.dockerignore` 가 `.git` 을 뺌) — 빌드 인자로 커밋을 넘겨 `core/version.py` 가 읽게 할지. 원격 리눅스에서 compose 로 실제로 띄워 본 결과도 아직 없다.
- [ ] Docker 로 띄워 보지 못했다 (이 PC 에 docker 가 없음). `docker/docker-compose.yml` 은 토큰·허용 목록이 없으면 `compose` 가 멈추게 했고(`${VAR:?}`),
  `restart: on-failure:5` 로 바꿨다. `docker compose config` 로 문법을 확인해야 한다
- [ ] 진행률 수정이 yt-dlp 출력을 읽는 스레드에서 동기 HTTP 호출(제한 시간 15초)로 일어난다 — 네트워크가 느리면 출력 처리가 그만큼 늦어진다. 실패하면 30초 동안
  다시 시도하지 않아 한도는 있지만, 문제가 되면 별도 전송 스레드로 뺀다
- [ ] 대기 중 작업의 순번(앞에 몇 개)을 보여 주고 갱신하지 않는다 (`#2 대기 중` 뿐)
- [ ] 대기열·`/status` 번호를 재시작 뒤에도 유지(영속화) — 지금은 재시작하면 대기열이 사라진다
- [ ] 봇으로 받은 영상을 다운로드 기록(`history.json`)에도 남길지 — 지금은 남기지 않는다 (GUI 기록 탭에 안 보임)
- [ ] 운영자와 일반 사용자 구분 (지금은 허용된 채팅이면 모두 같은 권한이고, 쿠키 알림은 허용된 모든 채팅에 간다)
- [ ] `core/notifier.py`(C-3)와의 관계 정하기: 지금 봇이 알림을 직접 보낸다. 알림 대상이 봇 밖(GUI·CLI·스크립트)에도 필요해지면 `Notifier` 로 뽑아낸다
- [ ] 다른 메신저(Discord / KakaoTalk): `core/telegram.py` 와 `BotService` 의 경계(전송 ↔ 동작)가 그 길을 열어 둔 것이다. 필요해질 때 결정

## 개발 환경 메모 (클라우드 / Linux)

- 테스트는 repo 루트에서 `python -m unittest` (표준 라이브러리만). Tk 위젯을 쓰는 테스트는 화면이 없으면 `skipTest` 로 건너뛴다.
- 화면 없는 Linux 에서도 GUI 테스트를 돌릴 수 있다 (2026-10-06 확인, Ubuntu 24.04 클라우드 컨테이너):
  `apt-get install -y python3-tk xvfb` 후 `xvfb-run -a /usr/bin/python3.12 -m unittest`. 기본 `python3` 가 Tk 없는 3.13 이면 시스템
  3.12 를 쓴다. 이렇게 돌린 전체 499개 중 1개가 실패한다:
  `tests/test_tk_tooltip.py::JobsColumnsTest::test_the_default_window_fits_a_live_recording_row` — 열 너비가 Windows 글꼴(맑은 고딕)로
  잰 값이라 그 글꼴이 없는 Linux 에서는 128px < 129.6px 로 어긋난다. 이번 변경과 무관하다 (변경을 되돌려도 같은 실패). Windows 에서는
  2026-10-07 에 확인했다: 사용자 PC 에서 `python -m unittest` 531개 통과(스킵 6), 위 열 너비 테스트도 통과. 스킵 6개는 `yt_dlp` 미설치
  1개, 글자 수로 세는 Windows 한도라 건너뛰는 `tests/test_filename_limit.py` 의 `WithYtDlpTest` 4개, POSIX 파일 모드 1개다 — 즉 긴 제목
  파일명 수정의 yt-dlp 와 맞춰 보는 테스트는 Windows 에서는 돌지 않는다. (2026-10-07 `ab6b44a` 에서 다시 돌려 722개 통과, 스킵 6.)
- 실제 GUI 동작·실제 방송 다운로드·PyInstaller 빌드는 클라우드에서 검증할 수 없다. 그런 항목은 위 체크리스트에 "미확인"으로 적어 두었다.
