"""Tests for scripts/collect_log_phrases.py, the tally of failure and warning
wordings in the run logs.

The logs here are written with the app's own ``RunLog`` (so the format is the
real one) and by hand where a log needs a shape that is hard to produce.
Every wording below is made up for the test: no real run log has had a
failure wording yet (see TODO.md, Phase C-2).

Run from the repo root with::

    python -m unittest

Standard library only.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

from core.run_log import RunLog

_SPEC = importlib.util.spec_from_file_location(
    "collect_log_phrases", Path(__file__).resolve().parent.parent / "scripts" / "collect_log_phrases.py"
)
clp = importlib.util.module_from_spec(_SPEC)
sys.modules["collect_log_phrases"] = clp
_SPEC.loader.exec_module(clp)

BOT = "ERROR: [youtube] abcdefghijk: Sign in to confirm you’re not a bot. Use --cookies-from-browser"
LOGIN = "ERROR: [youtube:tab] Login details are needed to download this content."
UNKNOWN = "ERROR: [youtube] abcdefghijk: Something nobody wrote a rule for (code 42)"
REFRESH = ("WARNING: [youtube] Cannot find refreshed url for format 137; please report this "
           "issue on https://github.com/yt-dlp/yt-dlp/issues?q= . Retrying (1/3)...")
SIDECAR = ("WARNING: [youtube] [pot:bgutil:http] Error reaching GET http://127.0.0.1:4416/ping "
           "(caused by TransportError). Please make sure that the server is reachable.")


def write_log(folder: Path, stamp: str, video: str, lines: list[tuple[str, str]],
              outcome: str = "ok", kind: str = "", version: str = "0.1.0 (abc)") -> Path:
    """A log in the app's real format: header, *lines* as (level, text), then
    the closing lines ``run_download`` writes. *outcome* None writes no result."""
    path = folder / f"{stamp}_{video}.log"
    log = RunLog(path)
    log.write(f"url: https://youtu.be/{video}?si=SECRETSECRETSECRETSECRET", "info")
    log.write(f"app version: {version}", "info")
    for level, text in lines:
        log.write(text, level)
    if kind:
        log.write(f"failure kind: {kind}", "result")
    if outcome == "ok":
        log.write("result: ok=True cancelled=False skipped=- files=['C:\\\\o\\\\v.mp4']", "result")
    elif outcome == "failed":
        log.write("result: ok=False cancelled=False skipped=- files=[]", "result")
    elif outcome == "cancelled":
        log.write("result: ok=False cancelled=True skipped=- files=[]", "result")
    elif outcome == "live":
        log.write("result: ok=False cancelled=False skipped=live files=[]", "result")
    log.finish()
    return path


class ShapeTest(unittest.TestCase):
    def test_ids_links_and_counters_are_taken_out(self) -> None:
        a = clp.shape_of("ERROR: [youtube] dQw4w9WgXcQ: Video unavailable. See https://youtu.be/x?si=1")
        b = clp.shape_of("ERROR: [youtube] 9bZkp7q19f0: Video unavailable. See https://youtu.be/y")
        self.assertEqual(a, b)
        self.assertIn("<id>", a)
        self.assertEqual(
            clp.shape_of("Retrying fragment 12 (3/10)..."), clp.shape_of("Retrying fragment 7 (1/10)..."))

    def test_the_http_status_and_exit_code_are_kept(self) -> None:
        self.assertIn("HTTP Error 403", clp.shape_of("ERROR: HTTP Error 403: Forbidden"))
        self.assertNotEqual(clp.shape_of("ERROR: HTTP Error 403: Forbidden"),
                            clp.shape_of("ERROR: HTTP Error 429: Too Many Requests"))
        self.assertIn("exit code: 1", clp.shape_of("yt-dlp exit code: 1"))

    def test_links_become_their_site(self) -> None:
        shape = clp.shape_of("see https://www.youtube.com/watch?v=dQw4w9WgXcQ and "
                             "https://rr3---sn-x.googlevideo.com/videoplayback?sig=A")
        self.assertIn("<url youtube.com>", shape)
        self.assertIn("<url googlevideo.com>", shape)
        self.assertEqual(clp.shape_of("GET http://127.0.0.1:4416/ping"), "GET <url ip>")

    def test_paths_and_tokens(self) -> None:
        self.assertEqual(clp.shape_of("cannot write C:\\Users\\홍 길동\\Videos\\a.mp4"),
                         "cannot write <path>")
        self.assertEqual(clp.shape_of("in /home/alice/x/y.txt now"), "in <path> now")
        self.assertEqual(clp.shape_of("key abcdefghijklmnopqrstuvwxyz0123456789"), "key <token>")

    def test_long_lines_are_cut(self) -> None:
        self.assertLessEqual(len(clp.shape_of("x " * 500)), clp.MAX_SHAPE_LENGTH)

    def test_two_wordings_stay_apart(self) -> None:
        self.assertNotEqual(clp.shape_of(BOT), clp.shape_of(LOGIN))


class MaskTest(unittest.TestCase):
    def test_what_could_identify_or_open_an_account_is_removed(self) -> None:
        masked = clp.mask_example(
            "GET https://x.example/a?sig=SECRET&token=ABCDEFGHIJKLMNOPQRSTUVWXYZ "
            "C:\\Users\\Some Name\\Videos\\a.mp4 /home/alice/x ABCDEFGHIJKLMNOPQRSTUVWXYZ0123")
        self.assertNotIn("SECRET", masked)
        self.assertNotIn("Some Name", masked)
        self.assertNotIn("alice", masked)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", masked)
        self.assertIn("https://x.example/a", masked)
        self.assertIn("C:\\Users\\<user>\\Videos\\a.mp4", masked)

    def test_ordinary_text_is_untouched(self) -> None:
        self.assertEqual(clp.mask_example(BOT), BOT)  # holds "--cookies-from-browser"
        self.assertEqual(clp.mask_example(REFRESH.split(" on ")[0]), REFRESH.split(" on ")[0])


class ParseLogTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def _parse(self, *args, pattern: str | None = None, **kwargs):
        import re
        path = write_log(self.dir, *args, **kwargs)
        return clp.parse_log(path, re.compile(pattern, re.I) if pattern else None)

    def test_outcomes(self) -> None:
        cases = {"ok": "ok", "failed": "failed", "cancelled": "cancelled", "live": "skipped:live",
                 None: "unfinished"}
        for outcome, expected in cases.items():
            with self.subTest(outcome):
                record = self._parse("20261005-100000", f"vid{expected[:7]:_<8}"[:11].replace(":", "_"),
                                     [], outcome=outcome)
                self.assertEqual(record.outcome, expected)
        self.assertTrue(clp.LogRecord("x", outcome="skipped:live").succeeded)
        self.assertFalse(clp.LogRecord("x", outcome="unfinished").succeeded)

    def test_header_time_kind_and_exit_codes(self) -> None:
        record = self._parse(
            "20261005-143015", "abcdefghijk",
            [("error", "yt-dlp exit code: 1"), ("result", "yt-dlp exited with code 0")],
            outcome="failed", kind="bot_blocked")
        self.assertEqual(record.started, datetime(2026, 10, 5, 14, 30, 15))
        self.assertEqual(record.app_version, "0.1.0 (abc)")
        self.assertEqual(record.failure_kind, "bot_blocked")
        self.assertEqual(sorted(record.exit_codes), [0, 1])
        self.assertIsNotNone(record.seconds)
        self.assertIn("youtu.be/abcdefghijk", record.url)  # the report masks it, not the parser

    def test_only_yt_dlp_lines_are_collected_not_the_apps_own_messages(self) -> None:
        record = self._parse("20261005-100000", "abcdefghijk", [
            ("error", BOT), ("error", "다운로드 실패."), ("error", "yt-dlp exit code: 1"),
            ("warning", REFRESH), ("warning", "⚠️ 라이브 스트림이 5분간 진척 없음"),
            ("info", "[download] Destination: a.mp4"), ("progress", "progress[0] 5.0% of 1MiB"),
        ], outcome="failed")
        self.assertEqual([(p.level, p.text[:12]) for p in record.phrases],
                         [("error", "ERROR: [yout"), ("warning", "WARNING: [yo")])

    def test_info_lines_only_when_the_pattern_asks(self) -> None:
        lines = [("info", "[youtube] abcdefghijk: Video is no longer live. Retrying"),
                 ("info", "[download] Destination: a.mp4"), ("info", "just an app note")]
        self.assertEqual(self._parse("20261005-100000", "aaaaaaaaaaa", lines).phrases, [])
        record = self._parse("20261005-100001", "bbbbbbbbbbb", lines, pattern=r"no longer live")
        self.assertEqual([p.level for p in record.phrases], ["info"])
        self.assertIn("no longer live", record.phrases[0].text)

    def test_a_multi_line_warning_is_one_phrase(self) -> None:
        record = self._parse("20261005-100000", "abcdefghijk", [
            ("warning", "WARNING: [youtube] first line of a long warning"),
            ("warning", "  second line, indented"),
            ("warning", "\tthird line"),
            ("warning", "WARNING: [youtube] a separate warning"),
        ])
        self.assertEqual(len(record.phrases), 2)
        self.assertIn("second line, indented third line", record.phrases[0].text)

    def test_a_live_run_is_marked(self) -> None:
        self.assertFalse(self._parse("20261005-100000", "aaaaaaaaaaa", []).live)
        self.assertTrue(self._parse("20261005-100001", "bbbbbbbbbbb",
                                    [("info", "🔴 LIVE 감지 — 첫 부분부터 캐치업 시작")]).live)

    def test_a_file_that_is_not_a_log_is_read_without_failing(self) -> None:
        path = self.dir / "20261005-100000_abcdefghijk.log"
        path.write_bytes(b"\xff\xfe garbage \n[12:00:00] [error] ERROR: still found\nnot a line\n")
        record = clp.parse_log(path)
        self.assertEqual([p.text for p in record.phrases], ["ERROR: still found"])
        self.assertEqual(record.outcome, "unfinished")


class CollectTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        write_log(self.dir, "20261001-090000", "aaaaaaaaaaa", [("error", BOT)],
                  outcome="failed", kind="bot_blocked")
        write_log(self.dir, "20261002-090000", "bbbbbbbbbbb", [("error", BOT.replace("abcdefghijk", "zzzzzzzzzzz"))],
                  outcome="failed", kind="bot_blocked")
        write_log(self.dir, "20261003-090000", "ccccccccccc", [("error", LOGIN)],
                  outcome="failed", kind="auth_expired")
        write_log(self.dir, "20261004-090000", "ddddddddddd", [("error", UNKNOWN)], outcome="failed")
        write_log(self.dir, "20261005-090000", "eeeeeeeeeee",
                  [("info", "🔴 LIVE 감지"), ("warning", REFRESH), ("warning", SIDECAR)])
        write_log(self.dir, "20261006-090000", "fffffffffff", [("warning", SIDECAR)])
        write_log(self.dir, "20261007-090000", "ggggggggggg", [], outcome="cancelled")
        (self.dir / "notes.txt").write_text("not a log", encoding="utf-8")

    def _shape(self, report, needle: str):
        found = [s for s in report.shapes if needle in s.key]
        self.assertEqual(len(found), 1, [s.key for s in report.shapes])
        return found[0]

    def test_logs_are_counted_and_other_files_ignored(self) -> None:
        report = clp.collect(self.dir)
        self.assertEqual(len(report.logs), 7)
        outcomes = {log.outcome for log in report.logs}
        self.assertEqual(outcomes, {"failed", "ok", "cancelled"})

    def test_one_wording_with_different_ids_is_one_shape(self) -> None:
        shape = self._shape(clp.collect(self.dir), "not a bot")
        self.assertEqual((shape.count, len(shape.logs)), (2, 2))
        self.assertEqual(shape.kinds, {"bot_blocked": 2})
        self.assertEqual((len(shape.failed_logs), len(shape.ok_logs)), (2, 0))
        self.assertEqual(len(shape.examples), 2)

    def test_the_current_classifier_reading_is_reported(self) -> None:
        report = clp.collect(self.dir)
        self.assertEqual(self._shape(report, "not a bot").recognized, "bot_blocked")
        self.assertEqual(self._shape(report, "Login details").recognized, "auth_expired")
        self.assertEqual(self._shape(report, "nobody wrote").recognized, "미분류")
        self.assertEqual(self._shape(report, "refreshed url").recognized, "조기 종료 경고")
        self.assertEqual(self._shape(report, "bgutil").recognized, "사이드카 응답 실패 경고")

    def test_a_dropped_connection_is_a_retry_while_it_lasts_and_a_network_failure_when_given_up(self) -> None:
        # [log] the retry line as run logs have it (2026-10-05); the give-up line is
        # built from yt-dlp's source (RetryManager.report_retry), no log has it yet.
        host = "HTTPSConnectionPool(host='rr12---sn-3u-bh2zy.googlevideo.com', port=443): Read timed out."
        write_log(self.dir, "20261008-090000", "hhhhhhhhhhh",
                  [("error", f"[download] Got error: {host}. Retrying (1/30)..."),
                   ("error", f"ERROR: \r[download] Got error: {host}. Giving up after 29 retries")],
                  outcome="failed", kind="network")
        report = clp.collect(self.dir)
        retrying = [s for s in report.shapes if "Got error" in s.key and "Retrying" in s.key]
        gave_up = [s for s in report.shapes if "Got error" in s.key and "Giving up" in s.key]
        self.assertEqual([s.recognized for s in retrying], ["재시도 중 (실패 아님)"])
        self.assertEqual([s.recognized for s in gave_up], ["network"])

    def test_live_and_successful_runs_are_told_apart(self) -> None:
        shape = self._shape(clp.collect(self.dir), "refreshed url")
        self.assertEqual((len(shape.live_logs), len(shape.ok_logs), len(shape.failed_logs)), (1, 1, 0))
        sidecar = self._shape(clp.collect(self.dir), "bgutil")
        self.assertEqual((sidecar.count, len(sidecar.logs), len(sidecar.live_logs)), (2, 2, 1))

    def test_the_table_is_ordered_by_count_and_min_count_filters(self) -> None:
        report = clp.collect(self.dir)
        counts = [s.count for s in report.shapes]
        self.assertEqual(counts, sorted(counts, reverse=True))
        frequent = clp.collect(self.dir, min_count=2)
        self.assertTrue(frequent.shapes and all(s.count >= 2 for s in frequent.shapes))
        self.assertLess(len(frequent.shapes), len(report.shapes))

    def test_since_drops_older_logs(self) -> None:
        report = clp.collect(self.dir, since=datetime(2026, 10, 5))
        self.assertEqual([log.name[:8] for log in report.logs], ["20261005", "20261006", "20261007"])

    def test_unclassified_failures(self) -> None:
        names = [log.name for log in clp.unclassified_failures(clp.collect(self.dir))]
        self.assertEqual(names, ["20261004-090000_ddddddddddd.log"])  # no kind; the cancelled one is not a failure

    def test_info_lines_join_the_table_by_pattern(self) -> None:
        write_log(self.dir, "20261008-090000", "hhhhhhhhhhh",
                  [("info", "[youtube] hhhhhhhhhhh: Video is no longer live. Retrying")])
        with_info = clp.collect(self.dir)
        self.assertTrue(any("no longer live" in s.key for s in with_info.shapes))
        without = clp.collect(self.dir, info_pattern=None)
        self.assertFalse(any("no longer live" in s.key for s in without.shapes))


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        write_log(self.dir, "20261001-090000", "aaaaaaaaaaa",
                  [("error", UNKNOWN + " at https://youtu.be/x?sig=TOPSECRETVALUE")], outcome="failed")
        write_log(self.dir, "20261002-090000", "bbbbbbbbbbb", [("warning", REFRESH)])

    def test_markdown_has_the_sections_and_masks_examples(self) -> None:
        text = clp.render_markdown(clp.collect(self.dir))
        for heading in ("# 실행 로그 문구 집계", "## 결과", "## 분류되지 않은 실패", "## 문구 표"):
            self.assertIn(heading, text)
        self.assertIn("20261001-090000_aaaaaaaaaaa.log", text)
        self.assertIn("미분류", text)
        self.assertNotIn("TOPSECRETVALUE", text)
        self.assertNotIn("SECRETSECRET", text)

    def test_markdown_for_no_logs_says_so(self) -> None:
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(empty.rmdir)
        self.assertIn("읽을 로그가 없습니다", clp.render_markdown(clp.collect(empty)))

    def test_a_pipe_in_a_wording_does_not_break_the_table(self) -> None:
        write_log(self.dir, "20261003-090000", "ccccccccccc",
                  [("error", "ERROR: a | b | c went wrong")], outcome="failed")
        text = clp.render_markdown(clp.collect(self.dir))
        row = next(line for line in text.splitlines() if "went wrong" in line and line.startswith("|"))
        self.assertIn("a \\| b \\| c", row)

    def test_json_is_valid_and_masked(self) -> None:
        data = json.loads(clp.render_json(clp.collect(self.dir)))
        self.assertEqual(len(data["logs"]), 2)
        self.assertEqual(data["unclassified_failures"], ["20261001-090000_aaaaaaaaaaa.log"])
        self.assertTrue(data["shapes"])
        raw = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("TOPSECRETVALUE", raw)
        self.assertNotIn("SECRETSECRET", raw)


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        write_log(self.dir, "20261001-090000", "aaaaaaaaaaa", [("error", BOT)],
                  outcome="failed", kind="bot_blocked")

    def _main(self, *argv: str):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = clp.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_prints_a_markdown_report(self) -> None:
        code, out, _ = self._main("--logs", str(self.dir))
        self.assertEqual(code, 0)
        self.assertIn("not a bot", out)

    def test_json_to_a_file(self) -> None:
        target = self.dir / "report.json"
        code, out, err = self._main("--logs", str(self.dir), "--format", "json", "--output", str(target))
        self.assertEqual((code, out), (0, ""))
        self.assertIn("report.json", err)
        self.assertEqual(len(json.loads(target.read_text(encoding="utf-8"))["logs"]), 1)

    def test_a_missing_folder_is_reported(self) -> None:
        code, _, err = self._main("--logs", str(self.dir / "없음"))
        self.assertEqual(code, 1)
        self.assertIn("로그 폴더가 없습니다", err)

    def test_a_bad_pattern_is_reported(self) -> None:
        code, _, err = self._main("--logs", str(self.dir), "--info-pattern", "([")
        self.assertEqual(code, 2)
        self.assertIn("정규식", err)

    def test_bad_arguments_are_refused(self) -> None:
        for argv in (["--since", "yesterday"], ["--min-count", "0"], ["--min-count", "x"],
                     ["--format", "xml"]):
            with self.subTest(argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                clp.main(["--logs", str(self.dir), *argv])
            self.assertEqual(caught.exception.code, 2)

    def test_the_default_folder_follows_the_environment(self) -> None:
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {clp.LOG_DIR_ENV: str(self.dir)}):
            code, out, _ = self._main()
        self.assertEqual(code, 0)
        self.assertIn("not a bot", out)


if __name__ == "__main__":
    unittest.main()
