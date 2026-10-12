"""A live probe that could not tell is not reported as a normal video.

Found on 2026-10-07: a source-mode run whose PATH had a broken ``yt-dlp`` launcher
failed the probe, and the log still said "일반 영상 (live_status=not_live)" as if
yt-dlp had reported it. ``LiveStatusInfo.probed`` is False for that fallback.
"""

from __future__ import annotations

import json
import logging
import subprocess
import unittest
from unittest import mock

from core import downloader
from core.downloader import DownloadOptions
from core.live import LiveOptions, LiveStatusInfo, detect_live_status

URL = "https://youtu.be/aaaaaaaaaaa"


def completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["yt-dlp"], returncode, stdout=stdout, stderr="")


class ProbedTest(unittest.TestCase):
    def setUp(self) -> None:
        # A timed-out probe logs a warning on purpose; keep it off stderr.
        quiet = logging.NullHandler()
        logging.getLogger("core.live").addHandler(quiet)
        self.addCleanup(logging.getLogger("core.live").removeHandler, quiet)

    def detect(self, **run_kwargs):
        with mock.patch("core.live.yt_dlp_command", return_value=["yt-dlp"]), \
                mock.patch("core.live.subprocess.run", **run_kwargs):
            return detect_live_status(URL)

    def test_a_probe_that_answered_is_probed_even_when_the_answer_is_not_live(self) -> None:
        info = self.detect(return_value=completed(json.dumps({"live_status": "not_live", "title": "t"})))
        self.assertEqual((info.live_status, info.probed, info.title), ("not_live", True, "t"))

    def test_a_live_answer(self) -> None:
        info = self.detect(return_value=completed(json.dumps({"live_status": "is_live"})))
        self.assertEqual((info.live_status, info.probed), ("is_live", True))

    def test_every_way_the_probe_can_fail_is_not_probed(self) -> None:
        failures = {
            "yt-dlp exited 1 (the broken launcher)": dict(return_value=completed("", 1)),
            "nothing readable": dict(return_value=completed("not json at all\n")),
            "no output": dict(return_value=completed("")),
            "timeout": dict(side_effect=subprocess.TimeoutExpired("yt-dlp", 1)),
            "could not spawn": dict(side_effect=FileNotFoundError("yt-dlp")),
        }
        for name, kwargs in failures.items():
            with self.subTest(name):
                info = self.detect(**kwargs)
                self.assertEqual((info.live_status, info.probed), ("not_live", False))

    def test_no_yt_dlp_at_all_is_not_probed(self) -> None:
        with mock.patch("core.live.yt_dlp_command", side_effect=FileNotFoundError("yt-dlp")):
            info = detect_live_status(URL)
        self.assertFalse(info.probed)

    def test_no_url_is_not_probed(self) -> None:
        self.assertFalse(detect_live_status("").probed)

    def test_a_status_built_by_hand_counts_as_probed(self) -> None:
        # What every existing caller and test builds: the default keeps them as they were.
        self.assertTrue(LiveStatusInfo("not_live").probed)


class LoggingTest(unittest.TestCase):
    def logs(self, info: LiveStatusInfo) -> list[tuple[str, str]]:
        shown: list[tuple[str, str]] = []
        opts = DownloadOptions(url=URL, format_opt="best", output_template="o", live_opts=LiveOptions())
        with mock.patch("core.live.detect_live_status", return_value=info), \
                mock.patch.object(downloader, "_run_once", lambda *a, **k: (True, [], [], [])):
            downloader.run_download(URL, opts, log_callback=lambda line, level: shown.append((line, level)))
        return shown

    def test_a_probe_that_failed_says_so_as_a_warning(self) -> None:
        shown = self.logs(LiveStatusInfo("not_live", probed=False))
        self.assertTrue(any("라이브 여부를 확인하지 못했습니다" in line and level == "warning"
                            for line, level in shown), shown)
        self.assertFalse(any("일반 영상" in line and "live_status" in line for line, _ in shown), shown)

    def test_a_probe_that_answered_still_names_the_kind(self) -> None:
        shown = self.logs(LiveStatusInfo("not_live"))
        self.assertTrue(any("일반 영상 (live_status=not_live)" in line for line, _ in shown), shown)
        self.assertFalse(any("확인하지 못했습니다" in line for line, _ in shown))

    def test_the_failed_probe_does_not_claim_a_live_recording(self) -> None:
        shown = self.logs(LiveStatusInfo("not_live", probed=False))
        self.assertFalse(any("LIVE 감지" in line for line, _ in shown))


if __name__ == "__main__":
    unittest.main()
