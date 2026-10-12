"""An HTTP 403 on the very first media request is tried again once, by itself.

Seen three times in real runs on 2026-10-07 (an ``/audio`` job, a ``/video`` job and a job
that started right after another was cancelled): yt-dlp listed the formats and failed with
``ERROR: unable to download video data: HTTP Error 403: Forbidden`` before a byte arrived.
Each was sent again by hand 3 min 25 s to 17 min 38 s later and worked. Other videos had
downloaded fine seconds before and after, so it was not a lasting state. How soon a retry
can work is not known (all the real successes were minutes later), which is why the wait is
a module constant and the run log says what the retry did.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core import downloader
from core.downloader import DownloadOptions
from core.failure import FailureKind, classify_failure, failure_hint, http_403_error
from tests.test_failure import BOT, HTTP_403, HTTP_429, OTHER_ERROR, PO_TOKEN_MISSING

URL = "https://youtu.be/aaaaaaaaaaa"
RECORD = {"title": "a", "filepath": "/o/a.mp4"}
DESTINATION = "[download] Destination: /o/a.f137.mp4"
# The first progress line of every download, as in the real 403 runs of 16:16 and 20:10
# (the app's --progress-template), and one from a download well under way.
PROBE = "[download]   0.0% of  203.60MiB ~ N/A got   1.00KiB at  499.60KiB/s ETA 06:57"
UNDER_WAY = "[download]  33.1% of  203.60MiB ~ N/A got  67.40MiB at    8.00MiB/s ETA 00:48"


def output(*lines: str, warnings: tuple[str, ...] = (), received: float = 0.0) -> downloader._Output:
    out = downloader._Output()
    out.extend(lines)
    for warning in warnings:
        out.add_warning(warning)
    out.note_received(received)
    return out


class DetectionTest(unittest.TestCase):
    def test_an_error_line_with_the_403(self) -> None:
        self.assertTrue(http_403_error([HTTP_403]))
        self.assertTrue(http_403_error(["  " + HTTP_403]))
        self.assertTrue(http_403_error([OTHER_ERROR, HTTP_403]))

    def test_other_lines_are_not_it(self) -> None:
        self.assertFalse(http_403_error([]))
        self.assertFalse(http_403_error([HTTP_429, OTHER_ERROR, BOT]))
        # A warning that a missing PO Token "may yield HTTP Error 403" is only a warning: the
        # run it came from may have failed for something else.
        self.assertFalse(http_403_error([PO_TOKEN_MISSING]))

    def test_the_sample_progress_lines_are_what_the_app_parses(self) -> None:
        # The samples above stand for the real lines; keep them honest.
        display = downloader.ProgressDisplay()
        self.assertEqual(display.parse_progress(PROBE)["downloaded_bytes"], 1024)
        self.assertEqual(display.parse_progress(UNDER_WAY)["downloaded_bytes"], 67.4 * 1024 ** 2)

    def test_what_a_run_received_is_the_largest_progress_figure(self) -> None:
        out = downloader._Output()
        self.assertEqual(out.received, 0.0)
        for figure in (1024, None, 5.0, 3_000_000, 2_000):
            out.note_received(figure)
        self.assertEqual(out.received, 3_000_000)
        # A plain list (a test double) has no figure: it counts as nothing received.
        self.assertTrue(downloader._received_little([]))

    def test_little_means_under_the_limit_and_the_limit_itself_is_not_little(self) -> None:
        limit = downloader.HTTP_403_RETRY_MAX_BYTES
        self.assertTrue(downloader._received_little(output(received=0)))
        self.assertTrue(downloader._received_little(output(received=1024)))  # the 1.0KB probe
        self.assertTrue(downloader._received_little(output(received=limit - 1)))
        self.assertFalse(downloader._received_little(output(received=limit)))
        self.assertFalse(downloader._received_little(output(received=67.4 * 1024 ** 2)))

    def test_the_hint_says_when_the_retry_was_already_made(self) -> None:
        plain = failure_hint(FailureKind.PO_TOKEN)
        again = failure_hint(FailureKind.PO_TOKEN, retried=True)
        self.assertIn("먼저 한 번 다시 시도해 보세요", plain)
        self.assertNotIn("먼저 한 번 다시 시도해 보세요", again)
        self.assertIn("자동으로 한 번 다시 시도했지만", again)
        for hint in (plain, again):
            self.assertIn("PO Token", hint)
        # Only the 403 hint changes.
        self.assertEqual(
            failure_hint(FailureKind.RATE_LIMITED, retried=True), failure_hint(FailureKind.RATE_LIMITED))


class RetryTest(unittest.TestCase):
    def setUp(self) -> None:
        patch = mock.patch.object(downloader, "HTTP_403_RETRY_DELAY", 0.0)
        patch.start()
        self.addCleanup(patch.stop)

    def run_once(self, *runs, cancel=None, on_attempt=None):
        """run_download over canned _run_once outcomes. Returns (result, log lines,
        number of attempts made)."""
        opts = DownloadOptions(url=URL, format_opt="best", output_template="o")
        outcomes = iter(runs)
        shown: list[tuple[str, str]] = []
        attempts = [0]

        def fake(o, pcb, lcb, cancel_event=None):
            attempts[0] += 1
            if on_attempt is not None:
                on_attempt(attempts[0])
            return next(outcomes)

        with mock.patch.object(downloader, "_run_once", fake):
            result = downloader.run_download(
                URL, opts, log_callback=lambda line, level: shown.append((line, level)),
                cancel_event=cancel)
        return result, shown, attempts[0]

    def test_a_403_before_any_download_is_tried_again_and_can_succeed(self) -> None:
        result, shown, attempts = self.run_once(
            (False, output(HTTP_403), [], []), (True, output(), [RECORD], []))
        self.assertTrue(result.ok)
        self.assertEqual((result.files, result.failure_kind, attempts), ([RECORD], "", 2))
        text = " ".join(line for line, _ in shown)
        self.assertIn("HTTP 403 — 받기를 시작한 직후라", text)
        self.assertIn("한 번 다시 시도합니다", text)
        self.assertIn("재시도 성공", text)
        self.assertIn("다운로드 완료", text)
        self.assertNotIn("다운로드 실패", text)
        self.assertNotIn("다시 시도해 보세요", text)  # no failure hint for a run that worked
        # The run log is what tells later whether the short wait works: it has to say so.
        logged = Path(result.log_path).read_text(encoding="utf-8")
        self.assertIn("한 번 다시 시도합니다", logged)
        self.assertIn("재시도 성공", logged)

    def test_it_is_tried_only_once_and_the_failure_says_so(self) -> None:
        result, shown, attempts = self.run_once(
            (False, output(HTTP_403), [], []), (False, output(HTTP_403), [], []))
        self.assertEqual((result.ok, result.failure_kind, attempts), (False, "po_token", 2))
        text = " ".join(line for line, _ in shown)
        self.assertIn("재시도도 실패했습니다", text)
        self.assertIn("자동으로 한 번 다시 시도했지만", text)  # the hint knows
        self.assertNotIn("먼저 한 번 다시 시도해 보세요", text)

    def test_the_retry_failing_another_way_is_the_failure_reported(self) -> None:
        result, _, attempts = self.run_once(
            (False, output(HTTP_403), [], []), (False, output(HTTP_429), [], []))
        self.assertEqual((result.failure_kind, attempts), ("rate_limited", 2))

    def test_a_failure_without_a_403_is_not_retried(self) -> None:
        for lines in ([HTTP_429], [OTHER_ERROR]):
            with self.subTest(lines):
                result, shown, attempts = self.run_once((False, output(*lines), [], []))
                self.assertEqual((result.ok, attempts), (False, 1))
                self.assertNotIn("한 번 다시 시도합니다", " ".join(line for line, _ in shown))

    def test_a_stronger_cause_next_to_the_403_is_not_retried(self) -> None:
        # A throttle (429) is the diagnosis when it is there too; waiting 10 s would not help.
        result, shown, attempts = self.run_once((False, output(HTTP_429, HTTP_403), [], []))
        self.assertEqual((result.failure_kind, attempts), ("rate_limited", 1))
        self.assertNotIn("한 번 다시 시도합니다", " ".join(line for line, _ in shown))

    def test_a_warning_about_a_possible_403_alone_does_not_start_a_retry(self) -> None:
        # The run failed for another reason, though a PO Token warning made it read as po_token.
        failed = output(OTHER_ERROR, warnings=(PO_TOKEN_MISSING,))
        self.assertIs(classify_failure([OTHER_ERROR, PO_TOKEN_MISSING]), FailureKind.PO_TOKEN)
        result, _, attempts = self.run_once((False, failed, [], []))
        self.assertEqual((result.ok, attempts), (False, 1))

    def test_a_403_right_behind_the_first_progress_line_is_tried_again_too(self) -> None:
        # The 16:16 and 20:10 runs: "[download] Destination:" and "0.0% ... got 1.0KB" were
        # printed, then two seconds later the 403. (Progress lines are sparse: the 20:10 run had
        # ~10 MB on disk by then, and its retry resumed from it.) Same case, still the start.
        result, shown, attempts = self.run_once(
            (False, output(DESTINATION, PROBE, HTTP_403, received=1024), [], []),
            (True, output(), [RECORD], []))
        self.assertEqual((result.ok, attempts), (True, 2))
        self.assertIn("한 번 다시 시도합니다", " ".join(line for line, _ in shown))

    def test_a_403_after_real_data_is_not_retried(self) -> None:
        # A download that was under way (a big .part exists): that is not this case.
        result, shown, attempts = self.run_once(
            (False, output(DESTINATION, UNDER_WAY, HTTP_403, received=67.4 * 1024 ** 2), [], []))
        self.assertEqual((result.ok, result.failure_kind, attempts), (False, "po_token", 1))
        self.assertIn("먼저 한 번 다시 시도해 보세요", Path(result.log_path).read_text(encoding="utf-8"))
        self.assertNotIn("한 번 다시 시도합니다", " ".join(line for line, _ in shown))

    def test_the_limit_decides(self) -> None:
        limit = downloader.HTTP_403_RETRY_MAX_BYTES
        _, _, under = self.run_once((False, output(HTTP_403, received=limit - 1), [], []),
                                    (False, output(HTTP_403), [], []))
        _, _, at = self.run_once((False, output(HTTP_403, received=limit), [], []))
        self.assertEqual((under, at), (2, 1))

    def test_a_playlist_that_already_finished_a_video_is_not_retried(self) -> None:
        result, _, attempts = self.run_once((False, output(HTTP_403), [RECORD], []))
        self.assertEqual((result.ok, attempts), (False, 1))
        self.assertEqual(result.files, [RECORD])

    def test_a_cancel_during_the_first_attempt_is_not_retried(self) -> None:
        cancel = threading.Event()
        result, shown, attempts = self.run_once(
            (False, output(HTTP_403), [], []), cancel=cancel,
            on_attempt=lambda n: cancel.set())
        self.assertEqual((result.cancelled, result.ok, attempts), (True, False, 1))
        self.assertEqual(result.failure_kind, "")  # a cancelled run is not "failed"
        # ... and it must not announce a retry it will not make.
        self.assertNotIn("한 번 다시 시도합니다", " ".join(line for line, _ in shown))

    def test_a_cancel_during_the_wait_ends_it_at_once_and_makes_no_second_attempt(self) -> None:
        cancel = threading.Event()
        threading.Timer(0.05, cancel.set).start()
        with mock.patch.object(downloader, "HTTP_403_RETRY_DELAY", 30.0):
            started = time.monotonic()
            result, _, attempts = self.run_once((False, output(HTTP_403), [], []), cancel=cancel)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual((result.cancelled, attempts), (True, 1))

    def test_the_wait_is_the_constant_and_is_not_skipped_without_a_cancel_event(self) -> None:
        with mock.patch.object(downloader, "HTTP_403_RETRY_DELAY", 7.0), \
                mock.patch.object(downloader.time, "sleep") as sleep:
            self.run_once((False, output(HTTP_403), [], []), (True, output(), [RECORD], []))
        sleep.assert_called_once_with(7.0)

    def test_a_download_that_works_the_first_time_waits_for_nothing(self) -> None:
        with mock.patch.object(downloader.time, "sleep") as sleep:
            result, _, attempts = self.run_once((True, output(), [RECORD], []))
        self.assertEqual((result.ok, attempts), (True, 1))
        sleep.assert_not_called()


VIDEO = "KtquxqP2YnM"
BGUTIL_DOWN = (
    "WARNING: [youtube] [pot:bgutil:http] Error reaching GET http://127.0.0.1:4416/ping "
    "(caused by TransportError). Please make sure that the server is reachable at http://127.0.0.1:4416."
)
# yt-dlp's own words in the real run of 2026-10-07 15:48 (log 20261007-154812_KtquxqP2YnM.log):
# it stopped right after the format list, no "[download] Destination:" line.
REAL_403_RUN = [
    f"[youtube] Extracting URL: https://youtu.be/{VIDEO}",
    f"[youtube] {VIDEO}: Downloading webpage",
    f"[youtube] {VIDEO}: Downloading visionos player API JSON",
    f"[youtube] {VIDEO}: Downloading m3u8 information",
    BGUTIL_DOWN,
    f"[info] {VIDEO}: Downloading 1 format(s): 299+140",
    "ERROR: unable to download video data: HTTP Error 403: Forbidden",
]
# The other shape (16:16 and 20:10): yt-dlp printed "Destination:" and the first progress line,
# two seconds later the 403.
REAL_403_AFTER_PROBE = REAL_403_RUN[:-1] + [
    f"[download] Destination: /o/t ({VIDEO}).f299.mp4", PROBE, REAL_403_RUN[-1]]
RECORD_LINE = downloader._RESULT_MARKER + (
    '{"id": "%s", "title": "t", "filepath": "/o/t (%s).mp4", "language": "ko"}' % (VIDEO, VIDEO))
# ... and of the run of the same video at 15:56, which worked.
REAL_GOOD_RUN = [
    f"[youtube] Extracting URL: https://youtu.be/{VIDEO}",
    f"[youtube] {VIDEO}: Downloading webpage",
    BGUTIL_DOWN,
    f"[info] {VIDEO}: Downloading 1 format(s): 299+140",
    f"[download] Destination: /o/t ({VIDEO}).f299.mp4",
    f'[Merger] Merging formats into "/o/t ({VIDEO}).mp4"',
    RECORD_LINE,
]


class RealRunTest(unittest.TestCase):
    """run_download over the text of real yt-dlp runs (a fake process, the real lines)."""

    def run_with(self, *runs: tuple[list[str], int]):
        from tests.test_downloader import _FakeProcess

        processes = [_FakeProcess([(line + "\n").encode("utf-8") for line in lines], code)
                     for lines, code in runs]
        shown: list[tuple[str, str]] = []
        opts = DownloadOptions(url=f"https://youtu.be/{VIDEO}", format_opt="best", output_template="o")
        # Popen is patched for the whole process and app_version() runs `git describe` through it.
        with mock.patch.object(downloader, "HTTP_403_RETRY_DELAY", 0.0), \
                mock.patch.object(downloader, "app_version", return_value="test"), \
                mock.patch.object(downloader, "yt_dlp_command", return_value=["yt-dlp"]), \
                mock.patch.object(downloader.subprocess, "Popen", side_effect=processes) as popen:
            result = downloader.run_download(
                opts.url, opts, log_callback=lambda line, level: shown.append((line, level)))
        return result, shown, popen.call_count

    def test_the_real_403_run_followed_by_the_real_good_run(self) -> None:
        result, shown, launches = self.run_with((REAL_403_RUN, 1), (REAL_GOOD_RUN, 0))
        self.assertTrue(result.ok)
        self.assertEqual(launches, 2)
        self.assertEqual([f["filepath"] for f in result.files], [f"/o/t ({VIDEO}).mp4"])
        logged = Path(result.log_path).read_text(encoding="utf-8")
        for expected in ("한 번 다시 시도합니다", "재시도 성공", "다운로드 완료"):
            self.assertIn(expected, logged)

    def test_the_real_403_run_twice_fails_once_and_says_it_tried_again(self) -> None:
        result, _, launches = self.run_with((REAL_403_RUN, 1), (REAL_403_RUN, 1))
        self.assertEqual((result.ok, result.failure_kind, launches), (False, "po_token", 2))
        logged = Path(result.log_path).read_text(encoding="utf-8")
        self.assertIn("재시도도 실패했습니다", logged)
        self.assertIn("자동으로 한 번 다시 시도했지만", logged)

    def test_the_real_403_two_seconds_after_the_first_progress_line_is_tried_again(self) -> None:
        # The run of 2026-10-07 20:10 (log 20261007-201008_P7W9r85qPpI.log), and 16:16 before it.
        result, shown, launches = self.run_with((REAL_403_AFTER_PROBE, 1), (REAL_GOOD_RUN, 0))
        self.assertEqual((result.ok, launches), (True, 2))
        self.assertIn("재시도 성공", Path(result.log_path).read_text(encoding="utf-8"))

    def test_the_same_403_after_real_data_is_not_retried(self) -> None:
        under_way = REAL_403_AFTER_PROBE[:-1] + [UNDER_WAY, REAL_403_AFTER_PROBE[-1]]
        result, _, launches = self.run_with((under_way, 1))
        self.assertEqual((result.ok, result.failure_kind, launches), (False, "po_token", 1))


if __name__ == "__main__":
    unittest.main()
