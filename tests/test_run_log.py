"""Unit tests for core.run_log and its use by core.downloader.

Run from the repo root with::

    python -m unittest

Standard library only.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from core import downloader
from core.downloader import DownloadOptions, DownloadResult
from core.history import HistoryEntry
from core.live import LiveOptions, LiveStatusInfo
from core.run_log import (
    PROGRESS_EVERY,
    RunLog,
    activate,
    current_run_log,
    default_log_dir,
)
from tests.test_downloader import LIVE_UNKNOWN, VOD_LONG_ETA, _FakeProcess

URL = "https://www.youtube.com/live/J4lzQL2PluA?si=x"

# The live "refreshed url" warning as yt-dlp prints it. NOT from a real run log
# (none had it yet, 2026-10-06): built from yt-dlp's source, youtube/_video.py
# (retry.error = f'Cannot find refreshed url for format {itag}{bug_reports_message()}')
# through RetryManager.report_retry, which adds the retry counter. No video id
# prefix. Replace with a real line once a recording has produced one.
BUG_REPORT = (
    "please report this issue on  https://github.com/yt-dlp/yt-dlp/issues?q= , "
    "filling out the appropriate issue template. "
    "Confirm you are on the latest version using  yt-dlp -U")
REFRESH_WARNING = (
    f"WARNING: [youtube] Cannot find refreshed url for format 137; {BUG_REPORT}. Retrying (1/3)...")
REFRESH_GIVE_UP = (
    f"WARNING: [youtube] Cannot find refreshed url for format 137; {BUG_REPORT}. Giving up after 3 retries")
# Same retry loop, but the broadcast really ended: normal, not an early end.
NO_LONGER_LIVE = "WARNING: [youtube] J4lzQL2PluA: Video is no longer live. Retrying (1/3)..."


class _TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "logs"

    def new_log(self, url: str = URL, **kwargs) -> RunLog:
        log = RunLog.create(url, self.dir, **kwargs)
        self.addCleanup(log.finish)  # close the file so the folder can go
        return log

    def read(self, log: RunLog) -> str:
        return log.path.read_text(encoding="utf-8")


class RunLogFileTest(_TempDirTest):
    def test_create_names_the_file_after_the_video_and_writes_the_header(self) -> None:
        log = self.new_log(URL, header=["url: " + URL, "app version: 1"])
        self.assertRegex(log.path.name, r"^\d{8}-\d{6}_J4lzQL2PluA\.log$")
        text = self.read(log)
        self.assertIn("[info] url: " + URL, text)
        self.assertIn("[info] app version: 1", text)

    def test_lines_are_flushed_immediately(self) -> None:
        log = self.new_log()
        log.write("still running", "warning")
        self.assertIn("[warning] still running", self.read(log))  # file not closed yet

    def test_multiline_text_gets_one_stamp_per_line(self) -> None:
        log = self.new_log()
        log.write("a\r\nb")
        lines = [ln for ln in self.read(log).splitlines() if ln.endswith(("a", "b"))]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(ln.startswith("[") and "[info]" in ln for ln in lines))

    def test_same_url_in_the_same_second_gets_its_own_file(self) -> None:
        first = self.new_log()
        second = self.new_log()
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(len(list(self.dir.glob("*.log"))), 2)

    def test_non_youtube_url_uses_the_host(self) -> None:
        log = self.new_log("https://example.com/watch?id=7")
        self.assertTrue(log.path.name.endswith("_example.com.log"))

    def test_finish_writes_the_result_and_closes(self) -> None:
        log = self.new_log()
        log.finish("result: ok=False")
        log.finish("ignored: already closed")
        text = self.read(log)
        self.assertIn("[result] result: ok=False", text)
        self.assertIn("[result] finished after 00:00:00", text)
        self.assertNotIn("ignored", text)
        self.assertFalse(log.enabled)

    def test_old_logs_are_pruned(self) -> None:
        self.dir.mkdir(parents=True)
        for n in range(5):
            (self.dir / f"2020010{n}-000000_x.log").write_text("old")
        (self.dir / "notes.txt").write_text("not a log")
        self.new_log(URL, keep=3)
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(len([n for n in names if n.endswith(".log")]), 3)
        self.assertNotIn("20200100-000000_x.log", names)  # oldest went first
        self.assertIn("notes.txt", names)

    def test_unwritable_folder_disables_the_log_without_raising(self) -> None:
        blocker = self.dir.parent / "file"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("a file where the folder should be")
        log = RunLog.create(URL, blocker / "logs")
        self.assertFalse(log.enabled)
        self.assertEqual(log.path_str, "")
        log.write("nothing happens")
        log.progress({"stream": 0, "percent_known": False, "downloaded": "1B", "speed": "1B/s"})
        log.finish("done")

    def test_size_limit_stops_writing_with_a_marker(self) -> None:
        log = self.new_log(URL, max_bytes=300)
        for n in range(50):
            log.write(f"line {n}")
        text = self.read(log)
        self.assertIn("log size limit reached", text)
        self.assertLess(len(text.encode("utf-8")), 500)
        self.assertEqual(text.count("log size limit reached"), 1)

    def test_size_limit_counts_bytes_not_characters(self) -> None:
        log = self.new_log(URL, max_bytes=2000)
        for _ in range(100):
            log.write("한" * 20)  # 60 bytes of UTF-8 each
        size = len(log.path.read_bytes())
        self.assertLess(size, 2000 + 300)  # the limit, plus the one marker line

    def test_closing_lines_survive_a_full_log(self) -> None:
        log = self.new_log(URL, max_bytes=300)
        for n in range(200):
            log.write(f"WARNING: spam {n}", "warning")  # fills the cap
        log.write("yt-dlp exit code: 1", "error")
        log.finish("result: ok=False cancelled=False files=[]")
        text = self.read(log)
        self.assertIn("log size limit reached", text)
        self.assertIn("[error] yt-dlp exit code: 1", text)
        self.assertIn("[result] result: ok=False", text)
        self.assertIn("[result] finished after", text)
        self.assertNotIn("spam 199", text)

    def test_reserve_is_bounded_too(self) -> None:
        log = self.new_log(URL, max_bytes=100)
        with mock.patch("core.run_log.RESERVE_BYTES", 500):
            for n in range(500):
                log.write(f"error line {n}", "error")
        self.assertLess(len(log.path.read_bytes()), 100 + 500 + 300)

    def test_prune_leaves_foreign_files_alone(self) -> None:
        self.dir.mkdir(parents=True)
        mine = [self.dir / f"2020010{n}-000000_x.log" for n in range(4)]
        foreign = [self.dir / "other.log", self.dir / "app-2020.log", self.dir / "20200101-000000.log"]
        for path in mine + foreign:
            path.write_text("x")
        self.new_log(URL, keep=2)
        for path in foreign:
            self.assertTrue(path.exists(), path.name)
        self.assertEqual(sum(p.exists() for p in mine), 1 + 0)  # keep=2 incl. the new log

    def test_prune_goes_on_after_a_file_that_cannot_be_deleted(self) -> None:
        self.dir.mkdir(parents=True)
        old = [self.dir / f"2020010{n}-000000_x.log" for n in range(4)]
        for path in old:
            path.write_text("x")
        real_unlink = Path.unlink

        def flaky_unlink(self_path, *args, **kwargs):
            if self_path.name == old[0].name:  # e.g. still open in a running recording
                raise PermissionError("WinError 32")
            return real_unlink(self_path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", flaky_unlink):
            self.new_log(URL, keep=2)
        self.assertTrue(old[0].exists())  # skipped, not fatal
        self.assertFalse(old[1].exists())  # the rest were still pruned
        self.assertFalse(old[2].exists())

    def test_malformed_urls_do_not_break_log_creation(self) -> None:
        for bad in ("https://[abc/x", "https://example.com／watch", "", "   ", "not a url at all"):
            log = self.new_log(bad)
            self.assertTrue(log.enabled, bad)
            self.assertRegex(log.path.name, r"^\d{8}-\d{6}_[A-Za-z0-9._-]+\.log$")

    def test_credentials_in_the_url_stay_out_of_the_file_name(self) -> None:
        log = self.new_log("https://user:secret@example.com/watch?id=1")
        self.assertNotIn("secret", log.path.name)
        self.assertTrue(log.path.name.endswith("_example.com.log"))

    def test_create_survives_an_unexpected_error(self) -> None:
        with mock.patch("core.run_log._slug", side_effect=RuntimeError("boom")):
            log = RunLog.create(URL, self.dir)
        self.assertFalse(log.enabled)


class RunLogProgressTest(_TempDirTest):
    @staticmethod
    def _info(line: str, stream: int = 0) -> dict:
        info = downloader.ProgressDisplay().parse_progress(line)
        info["stream"] = stream
        return info

    def test_progress_is_throttled_per_stream(self) -> None:
        now = [100.0]
        log = self.new_log(URL, clock=lambda: now[0])
        log.progress(self._info(LIVE_UNKNOWN, 0))
        log.progress(self._info(LIVE_UNKNOWN, 1))  # other stream: own clock
        now[0] += PROGRESS_EVERY - 1
        log.progress(self._info(LIVE_UNKNOWN, 0))  # too soon
        now[0] += 2
        log.progress(self._info(LIVE_UNKNOWN, 0))
        text = self.read(log)
        self.assertEqual(text.count("progress[0]"), 2)
        self.assertEqual(text.count("progress[1]"), 1)

    def test_progress_text(self) -> None:
        log = self.new_log()
        log.progress(self._info(VOD_LONG_ETA))
        self.assertIn("progress[0] 10.0% of 9.5MB got 976.6KB at 976.6KB/s ETA 01:06:40", self.read(log))
        live = self.new_log("https://example.com/b")
        live.progress(self._info(LIVE_UNKNOWN))
        self.assertIn("progress[0] got 4.8MB at N/A", self.read(live))

    def test_wrappers_log_and_still_call_the_callback(self) -> None:
        log = self.new_log()
        seen_log: list[tuple[str, str]] = []
        seen_progress: list[dict] = []
        log.wrap(lambda line, level: seen_log.append((line, level)))("hello", "error")
        log.wrap_progress(seen_progress.append)(self._info(LIVE_UNKNOWN))
        self.assertEqual(seen_log, [("hello", "error")])
        self.assertEqual(len(seen_progress), 1)
        text = self.read(log)
        self.assertIn("[error] hello", text)
        self.assertIn("progress[0]", text)


class CurrentRunLogTest(_TempDirTest):
    def test_disabled_outside_a_run_and_set_inside(self) -> None:
        self.assertFalse(current_run_log().enabled)
        current_run_log().write("no-op, no error")
        log = self.new_log()
        with activate(log):
            self.assertIs(current_run_log(), log)
        self.assertFalse(current_run_log().enabled)

    def test_default_dir_follows_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {"YTDLP_LOG_DIR": "~/somewhere"}):
            self.assertEqual(default_log_dir(), Path("~/somewhere").expanduser())
        env = {k: v for k, v in os.environ.items() if k != "YTDLP_LOG_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(default_log_dir().parts[-2:], (".ytdlp-downloader", "logs"))


class RunOnceLoggingTest(_TempDirTest):
    """What _run_once puts into the log of the surrounding run."""

    def _run(self, lines: list[bytes], live: bool, returncode: int = 0):
        opts = DownloadOptions(url=URL, format_opt="best", output_template="o",
                               live_opts=LiveOptions(enabled=True, force=live))
        log = self.new_log()
        logs: list[str] = []
        with mock.patch.object(downloader, "yt_dlp_command", return_value=["yt-dlp"]), \
                mock.patch.object(downloader.subprocess, "Popen",
                                  return_value=_FakeProcess(lines, returncode)), \
                activate(log):
            outcome = downloader._run_once(
                opts, lambda _info: None, lambda line, _level: logs.append(line))
        return outcome, logs, self.read(log)

    def test_refresh_warning_is_shown_only_for_live_runs(self) -> None:
        for warning in (REFRESH_WARNING, REFRESH_GIVE_UP):
            for live in (True, False):
                with self.subTest(warning=warning[-30:], live=live):
                    lines = [warning.encode() + b"\n", b"[download] Destination: v.mp4\n"]
                    (ok, captured, _, _), logs, text = self._run(lines, live=live)
                    self.assertTrue(ok)
                    self.assertIn("[warning] " + warning, text)
                    self.assertEqual(warning in logs, live)
                    self.assertNotIn(warning, captured)  # and so is the bot-detection input
                    self.assertIn("[download] Destination: v.mp4", captured)

    def test_ordinary_live_warnings_are_not_early_end_warnings(self) -> None:
        for warning in ("WARNING: [youtube] x: HTTP Error 403: Forbidden", NO_LONGER_LIVE):
            with self.subTest(warning=warning):
                (_, captured, _, _), logs, text = self._run([(warning + "\n").encode()], live=True)
                self.assertIn(warning, text)
                self.assertNotIn(warning, logs)
                self.assertNotIn(warning, captured)

    def test_indented_continuation_lines_of_a_warning_stay_in_the_file(self) -> None:
        lines = [
            b"WARNING: [youtube] x: Falling back to generic n function search\n",
            b"         player = https://www.youtube.com/s/player/abc/base.js\n",
            b"[download] Destination: v.mp4\n",
            b"    indented but not after a warning\n",
        ]
        (_, captured, _, _), logs, text = self._run(lines, live=False)
        self.assertIn("[warning]          player = https://www.youtube.com/s/player/abc/base.js", text)
        self.assertEqual(captured, ["[download] Destination: v.mp4", "    indented but not after a warning"])
        shown = [line for line in logs if not line.startswith("yt-dlp command:")]
        self.assertEqual(shown, ["[download] Destination: v.mp4", "    indented but not after a warning"])

    def test_marker_lines_and_exit_code_are_logged(self) -> None:
        record = downloader._RESULT_MARKER + json.dumps({"id": "x", "filepath": "C:\\o\\v.mp4"})
        target = downloader._TARGET_MARKER + json.dumps({"filename": "C:\\o\\v.mp4"})
        lines = [(target + "\n").encode("ascii"), (record + "\n").encode("ascii")]
        _, _, text = self._run(lines, live=False)
        self.assertIn(record, text)
        self.assertIn(target, text)
        self.assertIn("yt-dlp exited with code 0", text)

    def test_nonzero_exit_is_reported_through_the_log_callback(self) -> None:
        # The callback is what a real run wraps, so it reaches the file there.
        _, logs, _ = self._run([b"[download] x\n"], live=False, returncode=1)
        self.assertIn("yt-dlp exit code: 1", logs)

    def test_environment_notes_name_the_cookie_file_but_not_its_contents(self) -> None:
        cookie = self.dir.parent / "cookies.txt"
        cookie.parent.mkdir(parents=True, exist_ok=True)
        cookie.write_text("SECRET-COOKIE-VALUE")
        notes = downloader._describe_run_environment(["yt-dlp", "--cookies", str(cookie)])
        joined = "\n".join(notes)
        self.assertIn(str(cookie.resolve()), joined)
        self.assertIn("19 bytes", joined)
        self.assertNotIn("SECRET", joined)
        missing = downloader._describe_run_environment(["yt-dlp", "--cookies", "nope.txt"])
        self.assertIn("not found", missing[-1])
        self.assertEqual(len(downloader._describe_run_environment(["yt-dlp"])), 1)


class RunDownloadLoggingTest(_TempDirTest):
    OPTS = DownloadOptions(url=URL, format_opt="best", output_template="o")

    def _run_download(self, run_once, **kwargs):
        with mock.patch.dict(os.environ, {"YTDLP_LOG_DIR": str(self.dir)}), \
                mock.patch.object(downloader, "_run_once", run_once):
            return downloader.run_download(URL, self.OPTS, **kwargs)

    def test_result_carries_the_log_and_it_has_everything(self) -> None:
        record = {"id": "x", "title": "T", "filepath": "C:\\o\\v.mp4"}
        shown: list[tuple[str, str]] = []

        def run_once(opts, pcb, lcb, cancel_event=None):
            lcb("from yt-dlp", "info")
            return True, [], [record], []
        result = self._run_download(run_once, log_callback=lambda m, lv: shown.append((m, lv)))
        self.assertTrue(result.log_path)
        text = Path(result.log_path).read_text(encoding="utf-8")
        self.assertIn("[info] url: " + URL, text)
        self.assertIn("app version:", text)
        self.assertIn("[info] from yt-dlp", text)
        self.assertIn("다운로드 완료", text)  # run_download's own message
        self.assertIn("result: ok=True cancelled=False skipped=- files=['C:\\\\o\\\\v.mp4']", text)
        self.assertIn(("from yt-dlp", "info"), shown)  # callback still called

    def test_failed_run_is_logged_as_failed(self) -> None:
        result = self._run_download(lambda opts, pcb, lcb, cancel_event=None: (False, [], [], []))
        self.assertFalse(result.ok)
        self.assertIn("result: ok=False", Path(result.log_path).read_text(encoding="utf-8"))

    def test_refresh_warning_survives_a_successful_exit_and_is_deduplicated(self) -> None:
        warning = REFRESH_WARNING

        def run_once(opts, pcb, lcb, cancel_event=None):
            lcb(warning, "warning")
            lcb(warning, "warning")
            return True, [], [], []

        shown = []
        result = self._run_download(run_once, log_callback=lambda line, level: shown.append(line))
        self.assertTrue(result.ok)
        self.assertEqual(result.warnings, [warning])
        self.assertEqual(shown.count(warning), 1)
        self.assertIn("조기 종료", result.warning_summary)
        self.assertEqual(HistoryEntry.from_result(URL, result).status, "warning")
        self.assertIn(result.warning_summary, Path(result.log_path).read_text(encoding="utf-8"))

    def test_an_exception_is_logged_and_still_raised(self) -> None:
        def run_once(opts, pcb, lcb, cancel_event=None):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self._run_download(run_once)
        (log_file,) = self.dir.glob("*.log")
        text = log_file.read_text(encoding="utf-8")
        self.assertIn("aborted by KeyboardInterrupt", text)
        self.assertIn("finished after", text)

    def test_concurrent_runs_keep_separate_logs(self) -> None:
        # The GUI runs several downloads at once, one thread each.
        start = threading.Barrier(3)

        def run_once(opts, pcb, lcb, cancel_event=None):
            name = threading.current_thread().name
            start.wait(timeout=5)  # all three are inside their run now
            for n in range(20):
                downloader.current_run_log().write(f"{name} raw line {n}")
                lcb(f"{name} callback line {n}", "info")
            return True, [], [], []

        results: dict[str, DownloadResult] = {}

        def worker(name: str) -> None:
            with mock.patch.dict(os.environ, {"YTDLP_LOG_DIR": str(self.dir)}):
                results[name] = downloader.run_download(
                    f"https://example.com/{name}", self.OPTS)

        with mock.patch.object(downloader, "_run_once", run_once):
            threads = [threading.Thread(target=worker, args=(f"job{n}",), name=f"job{n}")
                       for n in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual(len(results), 3)
        self.assertEqual(len({r.log_path for r in results.values()}), 3)
        for name, result in results.items():
            text = Path(result.log_path).read_text(encoding="utf-8")
            self.assertEqual(text.count(f"{name} raw line"), 20, name)
            self.assertEqual(text.count(f"{name} callback line"), 20, name)
            for other in results:
                if other != name:
                    self.assertNotIn(f"{other} ", text.split("\n", 3)[3], name)

    def test_the_summary_says_why_nothing_was_downloaded(self) -> None:
        # A live broadcast skipped by a batch (no yt-dlp run at all) ...
        opts = DownloadOptions(url=URL, format_opt="best", output_template="o",
                               live_opts=LiveOptions())
        with mock.patch.dict(os.environ, {"YTDLP_LOG_DIR": str(self.dir)}), \
                mock.patch("core.live.detect_live_status",
                           return_value=LiveStatusInfo("is_live", title="방송")):
            live = downloader.run_download(URL, opts, skip_live=True)
        self.assertEqual(live.skipped, "live")
        self.assertIn("result: ok=False cancelled=False skipped=live",
                      Path(live.log_path).read_text(encoding="utf-8"))
        # ... and a video that is already in the download archive.
        archived = self._run_download(lambda opts, pcb, lcb, cancel_event=None: (
            True, ["[download] x: has already been recorded in the archive"], [], []))
        self.assertEqual(archived.skipped, "archived")
        self.assertIn("result: ok=True cancelled=False skipped=archived",
                      Path(archived.log_path).read_text(encoding="utf-8"))

    def test_malformed_url_still_returns_a_result(self) -> None:
        # yt-dlp is what judges a URL; a log-file name must not pre-empt it.
        for bad in ("https://[abc/x", "https://example.com／watch"):
            with mock.patch.dict(os.environ, {"YTDLP_LOG_DIR": str(self.dir)}), \
                    mock.patch.object(downloader, "_run_once",
                                      lambda *a, **k: (False, [], [], [])):
                result = downloader.run_download(bad, DownloadOptions(
                    url=bad, format_opt="best", output_template="o"))
            self.assertFalse(result.ok, bad)  # a result, not an exception
            self.assertTrue(result.log_path, bad)

    def test_exception_carries_the_log_path(self) -> None:
        def run_once(opts, pcb, lcb, cancel_event=None):
            raise RuntimeError("unexpected")
        with self.assertRaises(RuntimeError) as ctx:
            self._run_download(run_once)
        (log_file,) = self.dir.glob("*.log")
        self.assertEqual(ctx.exception.log_path, str(log_file))

    def test_unwritable_log_folder_does_not_stop_the_download(self) -> None:
        blocker = self.dir.parent / "file"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("x")
        with mock.patch.dict(os.environ, {"YTDLP_LOG_DIR": str(blocker / "logs")}), \
                mock.patch.object(downloader, "_run_once",
                                  lambda *a, **k: (True, [], [], [])):
            result = downloader.run_download(URL, self.OPTS)
        self.assertTrue(result.ok)
        self.assertEqual(result.log_path, "")


class BuildCommandWarningsTest(unittest.TestCase):
    def test_warnings_are_never_switched_off(self) -> None:
        # They go to the run log (and only there); see RunOnceLoggingTest.
        with mock.patch.object(downloader, "yt_dlp_command", return_value=["yt-dlp"]):
            plain = downloader._build_cmd(
                DownloadOptions(url=URL, format_opt="best", output_template="o"))
            live_opts = LiveOptions()
            live_opts._detected_status = "is_live"
            live = downloader._build_cmd(DownloadOptions(
                url=URL, format_opt="best", output_template="o", live_opts=live_opts))
        self.assertNotIn("--no-warnings", plain)
        self.assertNotIn("--no-warnings", live)


class HistoryLogLinkTest(unittest.TestCase):
    def test_entry_keeps_the_log_path(self) -> None:
        entry = HistoryEntry.from_result(URL, DownloadResult(ok=False, log_path="C:\\logs\\a.log"))
        self.assertEqual(entry.log_file, "C:\\logs\\a.log")
        restored = HistoryEntry.from_dict(json.loads(json.dumps(entry.__dict__)))
        self.assertEqual(restored.log_file, "C:\\logs\\a.log")

    def test_old_entries_without_a_log_still_load(self) -> None:
        entry = HistoryEntry.from_dict({"url": URL, "status": "failed"})
        self.assertEqual(entry.log_file, "")


if __name__ == "__main__":
    unittest.main()
