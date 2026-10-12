"""Tests for how a live recording that ends is judged, and for post_live.

The output lines are from real logs of 2026-10-05: a 1:13:59 recording of a
1:14:00 broadcast ended with yt-dlp exit code 1 although the merged file was
complete, and a download of a just-ended broadcast ran at one fragment per
~40 s.

Run from the repo root with::

    python -m unittest

Standard library only.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from core import downloader
from core.downloader import DownloadOptions
from core.live import LiveOptions, LiveStatusInfo
from tests.test_downloader import _FakeProcess

URL = "https://www.youtube.com/live/lhn1jI3s5Fk"
FINAL = "C:\\Users\\user\\Downloads\\[홍사훈쑈] 방송.mp4"
EMPTY = b"ERROR: Did not get any data blocks\n"


def _skip(n: int) -> bytes:
    return f"[download] fragment not found; Skipping fragment {n} ...\n".encode()


def _marker(marker: str, data: dict) -> bytes:
    return (marker + json.dumps(data) + "\n").encode("ascii")  # "j" escapes non-ASCII


RECORD = _marker(downloader._RESULT_MARKER, {"id": "lhn1jI3s5Fk", "filepath": FINAL})
TARGET = _marker(downloader._TARGET_MARKER, {"filename": FINAL, "format_id": "137+140"})
# The tail of the real log: both streams hit the end of the broadcast.
END_OF_STREAM = [
    EMPTY, _skip(889), EMPTY, _skip(889), EMPTY, _skip(890), EMPTY, _skip(890),
    b"[Merger] Merging formats into ...\n",
]


class ExitCodeAfterEndOfStreamTest(unittest.TestCase):
    OPTS = DownloadOptions(url=URL, format_opt="best", output_template="o")

    def _run(self, lines: list[bytes], returncode: int = 1):
        logs: list[tuple[str, str]] = []
        with mock.patch.object(downloader, "yt_dlp_command", return_value=["yt-dlp"]), \
                mock.patch.object(downloader.subprocess, "Popen",
                                  return_value=_FakeProcess(lines, returncode)):
            outcome = downloader._run_once(
                self.OPTS, lambda _info: None, lambda line, level: logs.append((line, level)))
        return outcome, logs

    def test_skipped_end_fragments_with_a_finished_file_count_as_success(self) -> None:
        (ok, captured, records, _), logs = self._run([TARGET, *END_OF_STREAM, RECORD])
        self.assertTrue(ok)
        self.assertEqual([r["filepath"] for r in records], [FINAL])
        notes = [line for line, level in logs if level == "warning"]
        self.assertEqual(len(notes), 1)
        self.assertIn("889, 890", notes[0])
        self.assertIn("종료 코드 1", notes[0])
        self.assertFalse([line for line, level in logs if line.startswith("yt-dlp exit code")])

    def test_a_real_error_next_to_them_is_still_a_failure(self) -> None:
        lines = [TARGET, *END_OF_STREAM, b"ERROR: unable to download video data: HTTP Error 403\n", RECORD]
        (ok, *_), logs = self._run(lines)
        self.assertFalse(ok)
        self.assertIn(("yt-dlp exit code: 1", "error"), logs)

    def test_no_finished_file_is_still_a_failure(self) -> None:
        (ok, *_), _ = self._run([TARGET, *END_OF_STREAM])  # no after_move record
        self.assertFalse(ok)

    def test_a_target_that_was_not_finished_is_still_a_failure(self) -> None:
        other = _marker(downloader._RESULT_MARKER, {"filepath": "C:\\out\\other.mp4"})
        (ok, *_), _ = self._run([TARGET, *END_OF_STREAM, other])
        self.assertFalse(ok)

    def test_the_error_without_a_skipped_fragment_is_still_a_failure(self) -> None:
        (ok, *_), _ = self._run([TARGET, EMPTY, RECORD])
        self.assertFalse(ok)

    def test_only_exit_code_1_is_reinterpreted(self) -> None:
        (ok, *_), _ = self._run([TARGET, *END_OF_STREAM, RECORD], returncode=2)
        self.assertFalse(ok)

    def test_a_clean_run_is_unchanged(self) -> None:
        (ok, _, _, _), logs = self._run([TARGET, RECORD], returncode=0)
        self.assertTrue(ok)
        self.assertFalse([1 for _, level in logs if level == "warning"])

    def test_many_skipped_fragments_are_summarised(self) -> None:
        lines = [TARGET]
        for n in range(100, 110):
            lines += [EMPTY, _skip(n)]
        (ok, *_), logs = self._run([*lines, RECORD])
        self.assertTrue(ok)
        note = next(line for line, level in logs if level == "warning")
        self.assertIn("100, 101, 102, 103, 104, 105 외 4개", note)

    def test_helper_needs_every_condition(self) -> None:
        captured = ["ERROR: Did not get any data blocks", "[download] fragment not found; Skipping fragment 5 ..."]
        records = [{"filepath": FINAL}]
        targets = [{"filename": FINAL}]
        self.assertEqual(downloader._skipped_fragments(captured, records, targets), [5])
        self.assertEqual(downloader._skipped_fragments(captured, records, []), [5])  # a normal video
        self.assertIsNone(downloader._skipped_fragments([], records, targets))
        self.assertIsNone(downloader._skipped_fragments(captured, [], targets))


class PostLiveTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(downloader, "yt_dlp_command", return_value=["yt-dlp"])
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _cmd(status: str, **live_kw) -> list[str]:
        live = LiveOptions(**live_kw)
        live._detected_status = status
        return downloader._build_cmd(
            DownloadOptions(url=URL, format_opt="best", output_template="o", live_opts=live))

    def test_post_live_is_downloaded_without_ranged_chunks(self) -> None:
        # The server holds each ranged fragment request ~20 s (one fragment per ~40 s).
        self.assertNotIn("--http-chunk-size", self._cmd("post_live"))

    def test_other_statuses_keep_their_chunk_setting(self) -> None:
        self.assertIn("--http-chunk-size", self._cmd("was_live"))
        self.assertIn("--http-chunk-size", self._cmd("not_live"))
        self.assertNotIn("--http-chunk-size", self._cmd("is_live"))
        # Detection switched off: nothing is known, so the VOD default stays.
        self.assertIn("--http-chunk-size", self._cmd("post_live", enabled=False))

    def test_post_live_is_not_treated_as_a_recording(self) -> None:
        cmd = self._cmd("post_live")
        self.assertNotIn("--live-from-start", cmd)
        self.assertNotIn("--wait-for-video", cmd)

    def _run_download_logs(self, status: str) -> list[str]:
        logs: list[str] = []
        opts = DownloadOptions(url=URL, format_opt="best", output_template="o",
                               live_opts=LiveOptions())
        with mock.patch("core.live.detect_live_status", return_value=LiveStatusInfo(status)), \
                mock.patch.object(downloader, "_run_once", lambda *a, **k: (True, [], [], [])):
            downloader.run_download(URL, opts, log_callback=lambda line, _lv: logs.append(line))
        return logs

    def test_the_kind_of_video_is_named_in_the_log(self) -> None:
        post = self._run_download_logs("post_live")
        self.assertTrue(any("종료된 라이브" in line for line in post), post)
        plain = self._run_download_logs("not_live")
        self.assertTrue(any("일반 영상 (live_status=not_live)" in line for line in plain), plain)


if __name__ == "__main__":
    unittest.main()
