#!/usr/bin/env python3
"""실행 로그에서 실패·경고 문구를 형태별로 집계한다.

앱은 다운로드마다 ``~/.ytdlp-downloader/logs/<YYYYmmdd-HHMMSS>_<id>.log`` (또는
``$YTDLP_LOG_DIR``)에 yt-dlp 출력 전체와 결과를 남긴다 (``core/run_log.py``). 이
스크립트는 그 폴더를 읽어

* 각 실행이 어떻게 끝났는지 (완료 / 실패 / 취소 / 건너뜀 / 끝맺음 없음)와 실패 원인 분류,
* yt-dlp 가 낸 ``ERROR`` · ``WARNING`` 줄 (과 ``--info-pattern`` 에 맞는 정보 줄)을
  영상 ID·주소·숫자 등을 가린 "형태"로 묶은 표,
* 아직 분류되지 않은 실패 (``failure kind: other`` 또는 원인 없음)

를 Markdown (기본) 이나 JSON 으로 낸다. 목적은 ``TODO.md`` 의 두 항목을 실제 문구로
확정하는 것이다:

* C-2 "오류 문구를 실제 로그 샘플로 확정" — ``tests/test_failure.py`` 의 샘플은 yt-dlp
  소스와 기억에서 온 것이라 실제 줄로 바꿔야 한다.
* F-2 "다른 조기 종료 경고는 실제 실행 로그 샘플을 확보한 뒤 감지 패턴 추가".

로그는 쿠키 값을 담지 않지만(쿠키 파일은 경로·크기·수정 시각만), 출력의 예시 줄은 한 번 더
주소의 쿼리 문자열, 긴 토큰, 사용자 폴더 이름을 가린다. 그래도 결과를 어딘가에 붙여 넣기 전에
훑어 보자.

읽기 전용이고 표준 라이브러리만 쓴다 (``core/failure.py`` 를 가져올 수 있으면 "현재 인식"
열을 채우고, 아니면 ``?`` 로 둔다).

사용::

    python scripts/collect_log_phrases.py
    python scripts/collect_log_phrases.py --logs D:\\logs --since 2026-10-01 --output report.md
    python scripts/collect_log_phrases.py --format json --min-count 2
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

LOG_DIR_ENV = "YTDLP_LOG_DIR"

# core/run_log.py: "<YYYYmmdd-HHMMSS>_<video id or host>[_n].log" and
# "[HH:MM:SS] [level] text".
_LOG_NAME = re.compile(r"(\d{8}-\d{6})_[A-Za-z0-9._-]+\.log")
_LINE = re.compile(r"\[(\d\d:\d\d:\d\d)\] \[([A-Za-z]+)\] ?(.*)")
_RESULT = re.compile(r"result: ok=(\w+) cancelled=(\w+) skipped=(\S+)")
_FINISHED = re.compile(r"finished after (\d+):(\d\d):(\d\d)")
_EXIT_CODE = re.compile(r"yt-dlp (?:exit code: |exited with code )(-?\d+)")

# Lines from yt-dlp, as opposed to the app's own messages (Korean notes,
# "yt-dlp exit code: 1", "다운로드 실패.") that share the error/warning levels.
_YT_DLP_LINE = re.compile(r"(?:ERROR:|WARNING:|\[)")
# Info-level yt-dlp lines worth tallying with the errors: how a broadcast
# ends, retries, and the usual access words. Override with --info-pattern.
DEFAULT_INFO_PATTERN = (
    r"no longer live|retrying|giving up|refreshed|fragment|unavailable|private|"
    r"sign in|\bbot\b|403|try again later|premiere|live event|will begin|"
    r"members|age|did not get|incomplete|stream ended|ended"
)
# What marks a run that recorded (or waited for) a broadcast.
_LIVE_MARKERS = ("LIVE 감지", "예정 라이브", "종료된 라이브", "--live-from-start", "post_live")

EXAMPLES_PER_SHAPE = 2
MAX_SHAPE_LENGTH = 300


# ---------------------------------------------------------------- shapes


_URL = re.compile(r"https?://[^\s'\"<>)]+")
# A Windows path may hold spaces inside its folders (up to the next backslash).
_DRIVE_PATH = re.compile(r"[A-Za-z]:\\(?:[^\\\r\n'\"<>]+\\)*[^\\\s'\"<>]*")
_UNIX_PATH = re.compile(r"(?<![\w:/.])/(?:[\w.@+-]+/)+[\w.@+-]*")
_ID_AFTER_TAG = re.compile(r"(\[[\w:.-]+\]) [A-Za-z0-9_-]{11}(?=[:\s])")
# A key or id: long, with digits and letters mixed. Option names such as
# "--cookies-from-browser" are long too, but all letters.
_LONG_TOKEN = re.compile(
    r"(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{20,}"
)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.:]\d+)*(?![\w])")
# Numbers that tell wordings apart and so stay: "HTTP Error 403", "exit code 1".
_KEEP_NUMBER = re.compile(r"(?i)(http error |exit code:? |exited with code |status code )(\d+)")


def _host(url: str) -> str:
    """The site of *url* for a shape: ``youtube.com``; ``ip`` for an address
    such as the local PO Token sidecar's ``127.0.0.1:4416``."""
    match = re.match(r"https?://(?:[^/@]*@)?(\[[^\]]*\]|[^/:?#]+)", url)
    host = match.group(1).lower() if match else ""
    if re.fullmatch(r"[0-9.]+|\[.*\]", host):
        return "ip"
    return ".".join(host.split(".")[-2:]) if host else ""


# A Windows user folder may hold spaces: take it up to the next backslash.
_USER_DIR_WIN = re.compile(r"(?i)([A-Za-z]:\\Users\\)(?:[^\\\r\n'\"]+(?=\\)|[^\\\s'\"]+)")
_USER_DIR_UNIX = re.compile(r"(/(?:home|Users)/)[^/\s]+")


def mask_example(text: str) -> str:
    """*text* with what could identify the person or open an account removed:
    a link's query string, long tokens, the user folder in a path."""
    text = _URL.sub(lambda m: m.group(0).split("?", 1)[0], text)
    text = _USER_DIR_WIN.sub(r"\1<user>", text)
    text = _USER_DIR_UNIX.sub(r"\1<user>", text)
    return _LONG_TOKEN.sub("<token>", text)


def shape_of(text: str) -> str:
    """The wording of *text* without what changes from run to run - video
    ids, links, paths, counters - so equal wordings group together."""
    shape = _URL.sub(lambda m: f"<url {_host(m.group(0))}>".replace(" >", ">"), text)
    shape = _DRIVE_PATH.sub("<path>", shape)
    shape = _UNIX_PATH.sub("<path>", shape)
    shape = _LONG_TOKEN.sub("<token>", shape)
    shape = _ID_AFTER_TAG.sub(r"\1 <id>", shape)
    # Protect the numbers that matter while the rest become N.
    kept: list[str] = []

    def keep(match: re.Match) -> str:
        kept.append(match.group(0))
        return f"\x00K{len(kept) - 1}\x00"  # a letter before the digits: not a number

    shape = _KEEP_NUMBER.sub(keep, shape)
    shape = _NUMBER.sub("N", shape)
    shape = re.sub(r"\x00K(\d+)\x00", lambda m: kept[int(m.group(1))], shape)
    shape = re.sub(r"\s+", " ", shape).strip()
    return shape[:MAX_SHAPE_LENGTH]


# ------------------------------------------------------------ one log file


@dataclass
class Phrase:
    level: str
    text: str


@dataclass
class LogRecord:
    name: str
    started: Optional[datetime] = None
    url: str = ""
    app_version: str = ""
    outcome: str = "unfinished"  # ok / failed / cancelled / skipped:<why> / unfinished
    failure_kind: str = ""
    exit_codes: list[int] = field(default_factory=list)
    seconds: Optional[int] = None
    live: bool = False
    phrases: list[Phrase] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.outcome == "ok" or self.outcome.startswith("skipped")


def _outcome(ok: str, cancelled: str, skipped: str) -> str:
    if cancelled == "True":
        return "cancelled"
    if skipped not in ("-", ""):
        return f"skipped:{skipped}"
    return "ok" if ok == "True" else "failed"


def parse_log(path: Path, info_pattern: Optional["re.Pattern[str]"] = None) -> LogRecord:
    """Read one run log. Unreadable bytes are replaced; lines that do not
    look like log lines are ignored."""
    record = LogRecord(name=path.name)
    match = _LOG_NAME.fullmatch(path.name)
    if match:
        try:
            record.started = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S")
        except ValueError:
            pass
    previous: Optional[Phrase] = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            parsed = _LINE.fullmatch(raw.rstrip("\r\n"))
            if parsed is None:
                continue
            _stamp, level, text = parsed.groups()
            if any(marker in text for marker in _LIVE_MARKERS):
                record.live = True
            if level == "result":
                _read_result_line(record, text)
                previous = None
                continue
            if level == "info":
                if text.startswith("url: "):
                    record.url = text[5:].strip()
                elif text.startswith("app version: "):
                    record.app_version = text[13:].strip()
            code = _EXIT_CODE.search(text)
            if code and level in ("error", "info"):
                record.exit_codes.append(int(code.group(1)))
            if level == "progress":
                previous = None
                continue
            if level == "warning" and text[:1] in (" ", "\t") and previous is not None \
                    and previous.level == "warning":
                previous.text += " " + text.strip()  # a multi-line warning's follow-up
                continue
            previous = None
            keep = (
                level in ("error", "warning") and _YT_DLP_LINE.match(text) is not None
            ) or (
                level == "info" and _YT_DLP_LINE.match(text) is not None
                and info_pattern is not None and info_pattern.search(text) is not None
            )
            if keep:
                previous = Phrase(level, text)
                record.phrases.append(previous)
    return record


def _read_result_line(record: LogRecord, text: str) -> None:
    result = _RESULT.match(text)
    if result:
        record.outcome = _outcome(*result.groups())
        return
    if text.startswith("failure kind: "):
        record.failure_kind = text[len("failure kind: "):].strip()
        return
    finished = _FINISHED.match(text)
    if finished:
        h, m, s = (int(g) for g in finished.groups())
        record.seconds = h * 3600 + m * 60 + s
        return
    code = _EXIT_CODE.search(text)
    if code:
        record.exit_codes.append(int(code.group(1)))
    elif text.startswith("aborted by "):
        record.outcome = "aborted"


# --------------------------------------------------------------- the report


@dataclass
class Shape:
    key: str
    count: int = 0
    levels: collections.Counter = field(default_factory=collections.Counter)
    logs: set = field(default_factory=set)
    ok_logs: set = field(default_factory=set)
    failed_logs: set = field(default_factory=set)
    live_logs: set = field(default_factory=set)
    kinds: collections.Counter = field(default_factory=collections.Counter)
    examples: list = field(default_factory=list)  # [(log name, masked line)]
    recognized: str = "?"


@dataclass
class Report:
    directory: str
    logs: list[LogRecord]
    shapes: list[Shape]
    skipped_files: list[str]


def _recognizer() -> Callable[[str, str], str]:
    """``(level, text) -> what the app already makes of this line``. Uses
    ``core.failure`` when the repository is importable from here."""
    root = Path(__file__).resolve().parent.parent
    if (root / "core" / "failure.py").is_file() and str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from core.failure import FailureKind, classify_failure, sidecar_unreachable
    except Exception:  # run from somewhere without the sources
        return lambda _level, _text: "?"

    def recognize(level: str, text: str) -> str:
        if "cannot find refreshed url for format" in text.lower():
            return "조기 종료 경고"
        if sidecar_unreachable([text]):
            return "사이드카 응답 실패 경고"
        lower = text.lower()
        if lower.startswith("[download] got error") and "retrying (" in lower:
            # yt-dlp is retrying a dropped connection; it prints this as plain
            # output (the run log tags it "error"), not as an ERROR line. It is
            # no failure cause, so it needs no wording in core/failure.py.
            return "재시도 중 (실패 아님)"
        kind = classify_failure([text])
        if kind is not FailureKind.OTHER:
            return kind.value
        return "미분류" if level in ("error", "warning") else "-"

    return recognize


def collect(
    directory: Path,
    *,
    since: Optional[datetime] = None,
    info_pattern: Optional[str] = DEFAULT_INFO_PATTERN,
    min_count: int = 1,
) -> Report:
    """Read the run logs in *directory* (those started *since*) and tally them."""
    pattern = re.compile(info_pattern, re.IGNORECASE) if info_pattern else None
    recognize = _recognizer()
    logs: list[LogRecord] = []
    skipped: list[str] = []
    for path in sorted(p for p in directory.iterdir() if _LOG_NAME.fullmatch(p.name)):
        try:
            record = parse_log(path, pattern)
        except OSError:
            skipped.append(path.name)
            continue
        if since is not None and record.started is not None and record.started < since:
            continue
        logs.append(record)

    shapes: dict[str, Shape] = {}
    for record in logs:
        for phrase in record.phrases:
            key = shape_of(phrase.text)
            shape = shapes.get(key)
            if shape is None:
                shape = shapes[key] = Shape(key, recognized=recognize(phrase.level, phrase.text))
            shape.count += 1
            shape.levels[phrase.level] += 1
            shape.logs.add(record.name)
            (shape.ok_logs if record.succeeded else shape.failed_logs).add(record.name)
            if record.live:
                shape.live_logs.add(record.name)
            if record.failure_kind:
                shape.kinds[record.failure_kind] += 1
            if len(shape.examples) < EXAMPLES_PER_SHAPE and all(
                name != record.name for name, _ in shape.examples
            ):
                shape.examples.append((record.name, mask_example(phrase.text)[:600]))
    ordered = sorted(
        (s for s in shapes.values() if s.count >= min_count),
        key=lambda s: (-s.count, s.key),
    )
    return Report(str(directory), logs, ordered, skipped)


def unclassified_failures(report: Report) -> list[LogRecord]:
    """Failed runs whose cause the app did not name (or named "other")."""
    return [
        log for log in report.logs
        if log.outcome in ("failed", "aborted", "unfinished")
        and log.failure_kind in ("", "other")
    ]


# --------------------------------------------------------------- rendering


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: Report) -> str:
    logs = report.logs
    out: list[str] = ["# 실행 로그 문구 집계", ""]
    started = [log.started for log in logs if log.started]
    period = f"{min(started):%Y-%m-%d} ~ {max(started):%Y-%m-%d}" if started else "-"
    out += [f"- 폴더: `{report.directory}`", f"- 로그 {len(logs)}개 ({period})"]
    if report.skipped_files:
        out.append(f"- 읽지 못한 파일 {len(report.skipped_files)}개: " + ", ".join(report.skipped_files[:5]))
    versions = sorted({log.app_version for log in logs if log.app_version})
    if versions:
        out.append("- 앱 버전: " + ", ".join(versions))
    out.append("")
    if not logs:
        out.append("읽을 로그가 없습니다. 로그 폴더(`--logs`)와 `--since` 를 확인하세요.")
        return "\n".join(out) + "\n"

    out += ["## 결과", "", "| 결과 | 로그 | 그중 라이브 |", "|---|---:|---:|"]
    outcomes = collections.Counter(log.outcome for log in logs)
    live = collections.Counter(log.outcome for log in logs if log.live)
    for name, count in sorted(outcomes.items(), key=lambda kv: (-kv[1], kv[0])):
        out.append(f"| {_cell(name)} | {count} | {live.get(name, 0)} |")
    kinds = collections.Counter(log.failure_kind for log in logs if log.failure_kind)
    if kinds:
        out += ["", "실패 원인 분류 (`failure kind:`): "
                + ", ".join(f"{k} {n}" for k, n in kinds.most_common())]
    codes = collections.Counter(code for log in logs for code in set(log.exit_codes))
    if codes:
        out += ["", "yt-dlp 종료 코드: " + ", ".join(f"{c}: {n}회" for c, n in sorted(codes.items()))]

    bad = unclassified_failures(report)
    out += ["", "## 분류되지 않은 실패", ""]
    if not bad:
        out.append("없음 — 실패한 실행은 모두 원인이 분류되었습니다.")
    else:
        out.append(
            "원인이 없거나 `other` 인 실패입니다. 여기의 줄이 `core/failure.py` 에 새 문구를 "
            "넣을 근거입니다 (`tests/test_failure.py` 에 `[log]` 샘플로)."
        )
        for log in bad[:30]:
            out += ["", f"### {log.name}  ·  {log.outcome} · 분류 {log.failure_kind or '없음'}"
                    + (" · 라이브" if log.live else "")]
            tail = [p for p in log.phrases if p.level == "error"][-3:] or log.phrases[-3:]
            if tail:
                out += ["", "```"] + [mask_example(p.text)[:600] for p in tail] + ["```"]
            else:
                out += ["", "(yt-dlp 의 오류·경고 줄이 없음)"]
        if len(bad) > 30:
            out += ["", f"… 외 {len(bad) - 30}개"]

    out += ["", "## 문구 표", ""]
    if not report.shapes:
        out.append("집계된 오류·경고 줄이 없습니다.")
    else:
        out += [
            "횟수 순. '현재 인식' 은 지금의 분류기가 그 줄을 어떻게 읽는지 — `미분류` 는 오류·경고인데 "
            "아무 규칙에도 안 걸린 것. '성공/실패 로그' 는 그 문구가 있던 실행이 끝난 모양이고, "
            "'라이브' 는 그중 방송을 녹화한 실행 수입니다 (조기 종료 경고 후보는 라이브 + 성공에서 찾으세요).",
            "",
            "| 횟수 | 로그 | 수준 | 현재 인식 | 성공/실패 | 라이브 | 문구 |",
            "|---:|---:|---|---|---:|---:|---|",
        ]
        for s in report.shapes:
            levels = "/".join(l for l, _ in s.levels.most_common())
            out.append(
                f"| {s.count} | {len(s.logs)} | {levels} | {_cell(s.recognized)} | "
                f"{len(s.ok_logs)}/{len(s.failed_logs)} | {len(s.live_logs)} | `{_cell(s.key)}` |"
            )
        out += ["", "### 예시 줄 (가림 처리됨)"]
        for s in report.shapes[:40]:
            out += ["", f"**{s.count}회 · {s.recognized}**", "```"]
            for name, text in s.examples:
                out.append(f"[{name}] {text}")
            out.append("```")
    return "\n".join(out) + "\n"


def render_json(report: Report) -> str:
    def shape(s: Shape) -> dict:
        return {
            "shape": s.key, "count": s.count, "logs": len(s.logs),
            "levels": dict(s.levels), "recognized": s.recognized,
            "ok_logs": len(s.ok_logs), "failed_logs": len(s.failed_logs),
            "live_logs": len(s.live_logs), "kinds": dict(s.kinds),
            "examples": [{"log": n, "text": t} for n, t in s.examples],
        }

    def log(r: LogRecord) -> dict:
        data = dataclasses.asdict(r)
        data["started"] = r.started.isoformat() if r.started else None
        data["url"] = mask_example(r.url)
        data["phrases"] = [{"level": p.level, "text": mask_example(p.text)} for p in r.phrases]
        return data

    return json.dumps({
        "directory": report.directory,
        "logs": [log(r) for r in report.logs],
        "unclassified_failures": [r.name for r in unclassified_failures(report)],
        "shapes": [shape(s) for s in report.shapes],
        "skipped_files": report.skipped_files,
    }, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------- main


def default_log_dir() -> Path:
    """``$YTDLP_LOG_DIR`` if set, else ``~/.ytdlp-downloader/logs`` (as the app does)."""
    override = os.environ.get(LOG_DIR_ENV)
    return Path(override).expanduser() if override else Path.home() / ".ytdlp-downloader" / "logs"


def _since(text: str) -> datetime:
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"날짜는 YYYY-MM-DD 형식이어야 합니다: {text!r}") from None


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"1 이상의 정수여야 합니다: {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="실행 로그의 실패·경고 문구를 형태별로 집계합니다 (읽기 전용).",
    )
    parser.add_argument("--logs", type=Path, metavar="DIR",
                        help="로그 폴더 (기본: $YTDLP_LOG_DIR 또는 ~/.ytdlp-downloader/logs)")
    parser.add_argument("--since", type=_since, metavar="YYYY-MM-DD",
                        help="이 날짜 이후에 시작한 로그만")
    parser.add_argument("--format", choices=("md", "json"), default="md", help="출력 형식 (기본 md)")
    parser.add_argument("--min-count", type=_positive, default=1, metavar="N",
                        help="이 횟수 이상 나온 문구만 표에 (기본 1)")
    parser.add_argument("--info-pattern", default=DEFAULT_INFO_PATTERN, metavar="REGEX",
                        help="오류·경고가 아니어도 표에 넣을 yt-dlp 정보 줄의 정규식 (대소문자 무시). "
                        "빈 문자열이면 정보 줄은 넣지 않음")
    parser.add_argument("--output", type=Path, metavar="FILE", help="파일로 저장 (기본: 화면)")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(None if argv is None else list(argv))
    directory = args.logs or default_log_dir()
    if not directory.is_dir():
        print(f"로그 폴더가 없습니다: {directory}", file=sys.stderr)
        return 1
    try:
        re.compile(args.info_pattern or "")
    except re.error as exc:
        print(f"--info-pattern 이 올바른 정규식이 아닙니다: {exc}", file=sys.stderr)
        return 2
    report = collect(directory, since=args.since, info_pattern=args.info_pattern or None,
                     min_count=args.min_count)
    text = render_json(report) if args.format == "json" else render_markdown(report)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"저장했습니다: {args.output}", file=sys.stderr)
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # a Windows console is not UTF-8
        except (AttributeError, OSError):
            pass
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
