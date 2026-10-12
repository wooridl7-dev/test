"""Unit tests for core.downloader: progress parsing and argv assembly.

Run from the repo root with::

    python -m unittest

Standard library only. The one test that drives real yt-dlp code is
skipped when the ``yt_dlp`` package is not importable.
"""

from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

from core import downloader
from core.auth import BgutilPoTokenAuth, CookieFileAuth
from core.downloader import DownloadOptions, ProgressDisplay, _build_cmd
from core.live import LiveOptions, LiveStatusInfo

# Lines yt-dlp printed for ProgressDisplay.TEMPLATE with --newline,
# captured from yt-dlp's own FileDownloader.report_progress.
VOD_LONG_ETA = (
    "[download]  10.0% of    9.54MiB ~        N/A got  976.56KiB "
    "at  976.56KiB/s ETA 01:06:40"
)
FRAGMENT_ESTIMATE = (
    "[download]  33.3% of        N/A ~    8.58MiB got    2.86MiB "
    "at    1.91MiB/s ETA 00:12"
)
LIVE_UNKNOWN = (
    "[download]   0.0% of        N/A ~        N/A got    4.77MiB "
    "at  Unknown B/s ETA Unknown"
)
FINISHED = "[download] 100.0% of    9.54MiB ~ NA got NA at 976.56KiB/s ETA NA"
CONCURRENT_SECOND_STREAM = (
    "2: [download]   0.0% of        N/A ~        N/A got    6.68MiB "
    "at  976.56KiB/s ETA Unknown"
)


class ParseProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pd = ProgressDisplay()

    def test_vod_with_eta_over_an_hour(self) -> None:
        info = self.pd.parse_progress(VOD_LONG_ETA)
        self.assertIsNotNone(info)
        self.assertEqual(info["percentage"], 10.0)
        self.assertTrue(info["percent_known"])
        self.assertEqual(info["total_size"], "9.5MB")
        self.assertEqual(info["speed"], "976.6KB/s")
        self.assertEqual(info["eta"], "01:06:40")  # not truncated to 01:06
        self.assertEqual(info["stream"], 0)

    def test_fragment_download_uses_estimate(self) -> None:
        info = self.pd.parse_progress(FRAGMENT_ESTIMATE)
        self.assertTrue(info["percent_known"])
        self.assertEqual(info["percentage"], 33.3)
        self.assertEqual(info["total_size"], "~8.6MB")
        self.assertEqual(info["eta"], "00:12")

    def test_live_without_any_size_is_percent_unknown(self) -> None:
        # yt-dlp prints "0.0%" here, which must not be read as 0% done.
        info = self.pd.parse_progress(LIVE_UNKNOWN)
        self.assertFalse(info["percent_known"])
        self.assertEqual(info["total_size"], "N/A")
        self.assertEqual(info["speed"], "N/A")
        self.assertEqual(info["eta"], "--:--")
        self.assertAlmostEqual(info["downloaded_bytes"], 4.77 * 1024**2)
        self.assertIn("N/A", info["bar"])

    def test_finished_line_with_missing_fields(self) -> None:
        info = self.pd.parse_progress(FINISHED)
        self.assertEqual(info["percentage"], 100.0)
        self.assertTrue(info["percent_known"])
        self.assertIsNone(info["downloaded_bytes"])
        self.assertEqual(info["eta"], "--:--")

    def test_concurrent_stream_prefix(self) -> None:
        info = self.pd.parse_progress(CONCURRENT_SECOND_STREAM)
        self.assertEqual(info["stream"], 1)
        self.assertAlmostEqual(info["downloaded_bytes"], 6.68 * 1024**2)

    def test_non_progress_lines(self) -> None:
        for line in (
            "[youtube] abc: Downloading webpage",
            "[download] Destination: video.mp4",
            "ERROR: [youtube] abc: Video unavailable",
        ):
            self.assertIsNone(self.pd.parse_progress(line), line)
            self.assertFalse(self.pd.is_progress_line(line), line)

    def test_summary_line_is_progress_but_not_parsed(self) -> None:
        line = "[download] 100% of    9.54MiB in 00:00:10 at 976.56KiB/s"
        self.assertIsNone(self.pd.parse_progress(line))
        self.assertTrue(self.pd.is_progress_line(line))

    def test_format_line(self) -> None:
        known = self.pd.format_line(self.pd.parse_progress(VOD_LONG_ETA))
        self.assertTrue(known.endswith("| 9.5MB | 976.6KB/s | ETA: 01:06:40"))
        unknown = self.pd.format_line(self.pd.parse_progress(LIVE_UNKNOWN))
        self.assertIn("4.8MB 받음", unknown)


@unittest.skipUnless(
    __import__("importlib").util.find_spec("yt_dlp"), "yt_dlp not installed"
)
class TemplateMatchesYtDlpTest(unittest.TestCase):
    """Render TEMPLATE through the installed yt-dlp and parse it back.

    Guards against yt-dlp renaming the progress fields the template uses.
    """

    def test_round_trip(self) -> None:
        import io

        import yt_dlp
        from yt_dlp.downloader.common import FileDownloader

        out = io.StringIO()
        ydl = yt_dlp.YoutubeDL({
            "progress_template": {"download": ProgressDisplay.TEMPLATE},
            "progress_with_newline": True,
        })
        fd = FileDownloader(ydl, ydl.params)
        fd._prepare_multiline_status()
        fd._multiline.stream = out
        base = {"status": "downloading", "info_dict": {}, "filename": "x"}
        fd.report_progress({
            **base, "downloaded_bytes": 2_000_000, "total_bytes": 8_000_000,
            "speed": 1e6, "eta": 6,
        })
        fd.report_progress({
            **base, "downloaded_bytes": 3_000_000, "speed": None, "eta": None,
        })

        lines = [line for line in out.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 2, out.getvalue())
        first, second = (ProgressDisplay().parse_progress(l) for l in lines)
        self.assertEqual(first["percentage"], 25.0)
        self.assertEqual(first["eta"], "00:06")
        self.assertFalse(second["percent_known"])
        self.assertAlmostEqual(second["downloaded_bytes"], 3_000_000, delta=10_000)


class BuildCmdTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            downloader, "yt_dlp_command", return_value=["yt-dlp"]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _opts(**kw) -> DownloadOptions:
        return DownloadOptions(
            url="https://youtu.be/x", format_opt="best", output_template="o", **kw
        )

    @staticmethod
    def _value(cmd: list[str], flag: str) -> str:
        return cmd[cmd.index(flag) + 1]

    def test_base_command(self) -> None:
        cmd = _build_cmd(self._opts())
        self.assertEqual(cmd[0], "yt-dlp")
        self.assertEqual(cmd[-1], "https://youtu.be/x")
        self.assertIn("--newline", cmd)
        self.assertEqual(self._value(cmd, "--progress-template"), ProgressDisplay.TEMPLATE)
        self.assertEqual(self._value(cmd, "--merge-output-format"), "mp4")
        self.assertEqual(self._value(cmd, "--http-chunk-size"), "10M")
        self.assertNotIn("--live-from-start", cmd)
        self.assertNotIn("-x", cmd)

    def test_audio_extraction(self) -> None:
        cmd = _build_cmd(self._opts(audio_opts={"format": "mp3"}))
        self.assertIn("-x", cmd)
        self.assertEqual(self._value(cmd, "--audio-format"), "mp3")
        self.assertNotIn("--merge-output-format", cmd)

    def test_subtitles(self) -> None:
        cmd = _build_cmd(self._opts(subtitle_opts={
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["ko", "en"],
        }))
        self.assertIn("--write-subs", cmd)
        self.assertIn("--write-auto-subs", cmd)
        self.assertEqual(self._value(cmd, "--sub-langs"), "ko,en")

    def test_cookie_file_auth(self) -> None:
        cmd = _build_cmd(self._opts(auth=CookieFileAuth(path="c.txt")))
        self.assertEqual(self._value(cmd, "--cookies"), "c.txt")

    def test_legacy_cookie_dict(self) -> None:
        cmd = _build_cmd(self._opts(cookie_opts={"cookiesfrombrowser": ("firefox",)}))
        self.assertEqual(self._value(cmd, "--cookies-from-browser"), "firefox")

    def test_bgutil_passes_base_url_extractor_arg(self) -> None:
        # bgutil plugin 2.x reads the address only from this extractor
        # arg; without it, it silently uses http://127.0.0.1:4416.
        auth = BgutilPoTokenAuth(provider_url="http://bgutil:4416")
        cmd = _build_cmd(self._opts(auth=auth))
        self.assertEqual(
            self._value(cmd, "--extractor-args"),
            "youtubepot-bgutilhttp:base_url=http://bgutil:4416",
        )
        self.assertEqual(auth.env_overrides(), {})

    def test_live_flags_only_when_effective(self) -> None:
        not_detected = _build_cmd(self._opts(live_opts=LiveOptions()))
        self.assertNotIn("--live-from-start", not_detected)

        live = LiveOptions(concurrent_fragments=8, wait_minutes=30)
        live._detected_status = "is_live"
        cmd = _build_cmd(self._opts(live_opts=live))
        self.assertIn("--live-from-start", cmd)
        self.assertEqual(self._value(cmd, "--concurrent-fragments"), "8")
        self.assertEqual(self._value(cmd, "--fragment-retries"), "10")
        self.assertEqual(self._value(cmd, "--wait-for-video"), "60-1800")
        self.assertEqual(self._value(cmd, "--remux-video"), "mp4")

    def test_live_defaults_append_fragments_as_they_arrive(self) -> None:
        live = LiveOptions()
        live._detected_status = "is_live"
        cmd = _build_cmd(self._opts(live_opts=live))
        # One fragment at a time: appended to the .part file immediately
        # instead of piling up as -FragN files until the stream ends.
        self.assertEqual(self._value(cmd, "--concurrent-fragments"), "1")
        # Ranged chunk requests made each live fragment take ~20 s.
        self.assertNotIn("--http-chunk-size", cmd)
        # The final name is announced up front so a stopped recording can
        # be merged; VOD downloads do not need it.
        self.assertIn(downloader._TARGET_TEMPLATE, cmd)
        self.assertNotIn(downloader._TARGET_TEMPLATE, _build_cmd(self._opts()))

    def test_download_archive(self) -> None:
        self.assertNotIn("--download-archive", _build_cmd(self._opts()))
        cmd = _build_cmd(self._opts(download_archive="/out/.batch-archive.txt"))
        self.assertEqual(self._value(cmd, "--download-archive"), "/out/.batch-archive.txt")

    def test_live_recording_notes(self) -> None:
        logs: list[tuple[str, str]] = []
        downloader._log_live_recording_notes(LiveOptions(), lambda m, lv: logs.append((m, lv)))
        self.assertEqual([lv for _, lv in logs], ["info"])
        self.assertIn("0 KB", logs[0][0])
        logs.clear()
        downloader._log_live_recording_notes(
            LiveOptions(concurrent_fragments=8), lambda m, lv: logs.append((m, lv)))
        self.assertEqual([lv for _, lv in logs], ["info", "warning"])

    def test_live_disabled_ignores_detection_and_force(self) -> None:
        live = LiveOptions(enabled=False, force=True)
        live._detected_status = "is_live"
        self.assertNotIn("--live-from-start", _build_cmd(self._opts(live_opts=live)))

    def test_audio_lang_prefers_that_track(self) -> None:
        opts = DownloadOptions(
            url="https://youtu.be/x", format_opt="bv+ba/b", output_template="o",
            audio_lang="KO",
        )
        self.assertEqual(opts.audio_lang, "ko")
        self.assertEqual(
            self._value(_build_cmd(opts), "-f"),
            "bv+ba[language^=ko][language!$=-desc]/bv+ba/b",
        )

    def test_bad_audio_lang_fails_when_building_options(self) -> None:
        with self.assertRaises(ValueError):
            self._opts(audio_lang="ko]")

    def test_no_audio_lang_keeps_format(self) -> None:
        opts = DownloadOptions(url="https://youtu.be/x", format_opt="bv+ba/b", output_template="o")
        self.assertEqual(self._value(_build_cmd(opts), "-f"), "bv+ba/b")

    def test_finished_videos_are_printed_without_going_quiet(self) -> None:
        cmd = _build_cmd(self._opts())
        self.assertEqual(self._value(cmd, "--print"), downloader._RESULT_TEMPLATE)
        self.assertTrue(downloader._RESULT_TEMPLATE.startswith("after_move:"))
        # --print alone would turn on --quiet and hide the progress lines.
        self.assertIn("--no-quiet", cmd)


class _FakeProcess:
    """Just enough of subprocess.Popen for _run_once."""

    def __init__(self, lines: list[bytes], returncode: int = 0) -> None:
        self.stdout = iter(lines)
        self.returncode = returncode
        self.pid = 0

    def wait(self, timeout=None) -> int:
        return self.returncode

    def poll(self) -> int:
        return self.returncode


class RunOnceOutputTest(unittest.TestCase):
    OPTS = DownloadOptions(url="https://youtu.be/x", format_opt="best", output_template="o")
    RECORD = {"id": "x", "title": "\uc81c\ubaa9", "filepath": "C:\\out\\v.mp4", "language": "ko"}

    def _run(self, lines: list[bytes], returncode: int = 0, cancel_event=None):
        logs: list[str] = []
        with mock.patch.object(downloader, "yt_dlp_command", return_value=["yt-dlp"]), \
                mock.patch.object(downloader.subprocess, "Popen",
                                  return_value=_FakeProcess(lines, returncode)):
            result = downloader._run_once(
                self.OPTS, lambda _info: None, lambda line, _level: logs.append(line),
                cancel_event,
            )
        return result, logs

    def test_result_lines_are_collected_not_logged(self) -> None:
        # yt-dlp's "j" conversion writes non-ASCII as \u escapes.
        record_line = (downloader._RESULT_MARKER + json.dumps(self.RECORD) + "\n").encode("ascii")
        target = {  # the name is lost in cp949 output
            "filename": "C:\\out\\\uff02\ub274\uc2a4\uff02 \u29f8 v.mp4",
            "format_id": "137+140",
        }
        lines = [
            (downloader._TARGET_MARKER + json.dumps(target) + "\n").encode("ascii"),
            (downloader._TARGET_MARKER + json.dumps({"format_id": "1"}) + "\n").encode("ascii"),
            b"[download] Destination: C:\\out\\v.mp4\n",
            record_line,
            (downloader._RESULT_MARKER + "{broken\n").encode("ascii"),
        ]
        (ok, captured, records, targets), logs = self._run(lines)
        self.assertTrue(ok)
        self.assertEqual(records, [self.RECORD])
        self.assertEqual(targets, [target])
        self.assertEqual(captured, ["[download] Destination: C:\\out\\v.mp4"])
        self.assertIn(captured[0], logs)
        self.assertFalse([line for line in logs if line.startswith("[ytdlp-gui-")])

    def test_cancel_after_a_clean_exit_still_succeeds(self) -> None:
        event = threading.Event()
        event.set()
        (ok, _, _, _), _ = self._run([b"[download] done\n"], returncode=0, cancel_event=event)
        self.assertTrue(ok)
        (ok, _, _, _), _ = self._run([b"[download] 5%\n"], returncode=1, cancel_event=event)
        self.assertFalse(ok)


class RunDownloadTest(unittest.TestCase):
    URL = "https://youtu.be/x"
    OPTS = DownloadOptions(url=URL, format_opt="best", output_template="o")

    @staticmethod
    def _fake_run_once(*runs: tuple):
        """Stand-in for _run_once returning (success, captured, records) per call."""
        results = iter(runs)

        def fake(opts, pcb, lcb, cancel_event=None):
            return next(results)
        return fake

    def test_success_reports_finished_files(self) -> None:
        record = {"id": "x", "title": "제목", "filepath": "/o/제목.mp4", "language": "ko"}
        with mock.patch.object(downloader, "_run_once",
                               self._fake_run_once((True, [], [record], []))):
            result = downloader.run_download(self.URL, self.OPTS)
        self.assertEqual((result.ok, result.cancelled), (True, False))
        self.assertEqual(result.files, [record])
        self.assertEqual((result.title, result.audio_language), ("제목", "ko"))

    def test_playlist_is_named_after_the_playlist(self) -> None:
        records = [{"title": "a", "playlist_title": "P"}, {"title": "b", "playlist_title": "P"}]
        with mock.patch.object(downloader, "_run_once", self._fake_run_once((True, [], records, []))):
            self.assertEqual(downloader.run_download(self.URL, self.OPTS).title, "P")

    def test_failure_keeps_the_probe_title(self) -> None:
        opts = DownloadOptions(url=self.URL, format_opt="best", output_template="o",
                               live_opts=LiveOptions())
        with mock.patch("core.live.detect_live_status",
                        return_value=LiveStatusInfo("not_live", title="조회 제목")), \
                mock.patch.object(downloader, "_run_once", self._fake_run_once((False, [], [], []))):
            result = downloader.run_download(self.URL, opts)
        self.assertEqual((result.ok, result.cancelled), (False, False))
        self.assertEqual((result.title, result.files), ("조회 제목", []))

    def test_bot_retry_does_not_repeat_files(self) -> None:
        first = {"title": "a", "filepath": "/o/a.mp4"}
        second = {"title": "b", "filepath": "/o/b.mp4"}
        bot = ["ERROR: Sign in to confirm you're not a bot"]
        with mock.patch.object(downloader, "_run_once", self._fake_run_once(
            (False, bot, [first], []),
            (True, [], [first, second], []),  # retry: "a" already downloaded
        )):
            result = downloader.run_download(self.URL, self.OPTS)
        self.assertTrue(result.ok)
        self.assertEqual(result.files, [first, second])

    def test_cancelled_before_start(self) -> None:
        event = threading.Event()
        event.set()
        with mock.patch.object(downloader, "_run_once") as run_once:
            result = downloader.run_download(self.URL, self.OPTS, cancel_event=event)
        run_once.assert_not_called()
        self.assertEqual((result.ok, result.cancelled), (False, True))

    def test_cancelled_while_running(self) -> None:
        event = threading.Event()

        def fake(opts, pcb, lcb, cancel_event=None):
            event.set()  # the user pressed 취소; yt-dlp was killed
            return False, [], [], []
        with mock.patch.object(downloader, "_run_once", fake):
            result = downloader.run_download(self.URL, self.OPTS, cancel_event=event)
        self.assertEqual((result.ok, result.cancelled), (False, True))

    def test_archived_video_is_reported_as_skipped(self) -> None:
        captured = ["[youtube] abc: 제목 has already been recorded in the archive"]
        with mock.patch.object(downloader, "_run_once",
                               self._fake_run_once((True, captured, [], []))):
            result = downloader.run_download(self.URL, self.OPTS)
        self.assertEqual((result.ok, result.skipped), (True, "archived"))
        with mock.patch.object(downloader, "_run_once",
                               self._fake_run_once((True, [], [], []))):
            self.assertEqual(downloader.run_download(self.URL, self.OPTS).skipped, "")

    def test_skip_live_does_not_record(self) -> None:
        for status in ("is_live", "is_upcoming"):
            run_once = mock.Mock()
            with mock.patch("core.live.detect_live_status",
                            return_value=LiveStatusInfo(status, title="방송")), \
                    mock.patch.object(downloader, "_run_once", run_once):
                result = downloader.run_download(self.URL, self._live_opts(), skip_live=True)
            run_once.assert_not_called()
            self.assertEqual((result.ok, result.skipped, result.title), (False, "live", "방송"))

    def test_skip_live_still_downloads_a_finished_broadcast(self) -> None:
        with mock.patch("core.live.detect_live_status",
                        return_value=LiveStatusInfo("was_live", title="지난 방송")), \
                mock.patch.object(downloader, "_run_once",
                                  self._fake_run_once((True, [], [], []))):
            result = downloader.run_download(self.URL, self._live_opts(), skip_live=True)
        self.assertEqual((result.ok, result.skipped), (True, ""))

    def test_download_wrapper_returns_bool(self) -> None:
        with mock.patch.object(downloader, "_run_once", self._fake_run_once((True, [], [], []))):
            self.assertIs(downloader.download(self.URL, self.OPTS), True)

    TARGET = {"filename": "C:\\out\\v.mp4", "format_id": "137+140"}

    def _live_opts(self, detected: str = "is_live") -> DownloadOptions:
        return DownloadOptions(url=self.URL, format_opt="best", output_template="o",
                               live_opts=LiveOptions())

    def _run_live(self, run_once, detected: str = "is_live", cancel_event=None):
        with mock.patch("core.live.detect_live_status",
                        return_value=LiveStatusInfo(detected, title="방송")), \
                mock.patch.object(downloader, "_run_once", run_once), \
                mock.patch.object(downloader, "merge_partial_recording",
                                  return_value="C:\\out\\v (부분 녹화).mp4") as merge:
            result = downloader.run_download(self.URL, self._live_opts(),
                                             cancel_event=cancel_event)
        return result, merge

    def test_stopped_live_recording_is_merged(self) -> None:
        event = threading.Event()

        def run_once(opts, pcb, lcb, cancel_event=None):
            event.set()  # 취소 / 종료 while recording
            return False, [], [], [self.TARGET, dict(self.TARGET)]
        result, merge = self._run_live(run_once, cancel_event=event)
        merge.assert_called_once()  # duplicate target merged once
        self.assertEqual(merge.call_args.args[0], "C:\\out\\v.mp4")
        self.assertEqual(merge.call_args.args[2], "137+140")  # only this download's parts
        self.assertEqual((result.ok, result.cancelled, result.title), (False, True, "방송"))
        self.assertEqual(result.files, [
            {"title": "방송", "filepath": "C:\\out\\v (부분 녹화).mp4", "partial": True}])

    def test_failed_live_recording_is_merged(self) -> None:
        # e.g. the stall watchdog killed yt-dlp
        result, merge = self._run_live(self._fake_run_once((False, [], [], [self.TARGET])))
        merge.assert_called_once()
        self.assertFalse(result.cancelled)
        self.assertTrue(result.files[0]["partial"])

    def test_finished_or_non_live_downloads_are_not_merged(self) -> None:
        _, merge = self._run_live(self._fake_run_once((True, [], [], [self.TARGET])))
        merge.assert_not_called()  # yt-dlp merged it itself
        _, merge = self._run_live(self._fake_run_once((False, [], [], [self.TARGET])),
                                  detected="not_live")
        merge.assert_not_called()


def _audio_fmt(language: str, preference: int = -1) -> dict:
    return {"vcodec": "none", "acodec": "mp4a.40.2", "language": language,
            "language_preference": preference}


class AudioTrackNoteTest(unittest.TestCase):
    # Probe info shaped like yt-dlp's --dump-json for a dubbed video: formats
    # run from least to most preferred, so description tracks come early.
    DUBBED = {"formats": [
        {"vcodec": "avc1.4d401f", "acodec": "none", "language": None},
        _audio_fmt("ko-desc", -10),
        _audio_fmt("en-US-desc", -10),
        _audio_fmt("ja"),
        _audio_fmt("ko"),
        _audio_fmt("en-US", 10),
        _audio_fmt("en-US", 10),
    ]}

    def test_preferred_language_found(self) -> None:
        self.assertEqual(
            downloader._audio_track_note(self.DUBBED, "ko"),
            "\U0001f50a 오디오 트랙 5개 — 한국어(ko) 선택",
        )

    def test_prefix_match_like_the_format_filter(self) -> None:
        # en-US-desc comes first in the list but is never the pick.
        self.assertEqual(
            downloader._audio_track_note(self.DUBBED, "en"),
            "\U0001f50a 오디오 트랙 5개 — 영어(en-US) 선택",
        )

    def test_tie_goes_to_the_later_format_like_yt_dlp(self) -> None:
        info = {"formats": [_audio_fmt("ko"), _audio_fmt("ko-KR"), _audio_fmt("en", 10)]}
        self.assertEqual(
            downloader._audio_track_note(info, "ko"),
            "\U0001f50a 오디오 트랙 3개 — 한국어(ko-KR) 선택",
        )

    def test_description_track_alone_does_not_count(self) -> None:
        info = {"formats": [_audio_fmt("ko-desc", -10), _audio_fmt("en", 10)]}
        self.assertEqual(
            downloader._audio_track_note(info, "ko"),
            "\U0001f50a 오디오 트랙 2개 — 한국어 없음, 원본(en)으로 받습니다",
        )

    def test_preferred_language_missing_falls_back_to_original(self) -> None:
        self.assertEqual(
            downloader._audio_track_note(self.DUBBED, "fr"),
            "\U0001f50a 오디오 트랙 5개 — fr 없음, 원본(en-US)으로 받습니다",
        )

    def test_original_choice(self) -> None:
        self.assertEqual(
            downloader._audio_track_note(self.DUBBED, None),
            "\U0001f50a 오디오 트랙 5개 — 원본(en-US) 선택",
        )

    def test_single_track_is_silent(self) -> None:
        self.assertIsNone(downloader._audio_track_note({"formats": [_audio_fmt("en")]}, "ko"))
        self.assertIsNone(downloader._audio_track_note({}, "ko"))


class MissingYtDlpTest(unittest.TestCase):
    def test_download_fails_cleanly_without_yt_dlp(self) -> None:
        # Frozen build without yt-dlp.exe: log an error, do not raise.
        logs: list[tuple[str, str]] = []
        with mock.patch.object(
            downloader, "yt_dlp_command",
            side_effect=FileNotFoundError("no yt-dlp.exe"),
        ):
            ok = downloader.download(
                "https://youtu.be/x",
                DownloadOptions(url="https://youtu.be/x", format_opt="best",
                                output_template="o"),
                log_callback=lambda line, level: logs.append((line, level)),
            )
        self.assertFalse(ok)
        self.assertIn(("yt-dlp not found: no yt-dlp.exe", "error"), logs)


if __name__ == "__main__":
    unittest.main()
