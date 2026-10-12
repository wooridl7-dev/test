"""Tests for core.output_claims and its use by core.downloader.run_download:
different videos with one title must not share an output file while they
download together."""

from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

from core import downloader
from core.downloader import DownloadOptions
from core.filename_limit import fit_template
from core.live import LiveOptions, LiveStatusInfo
from core.output_claims import (
    OUTPUT_CLAIMS,
    OutputClaim,
    OutputClaims,
    output_key,
    reserve_output,
    with_video_id,
)

TITLE = "%(title)s.%(ext)s"


def _info(video_id: str, title: str = "같은 제목", **extra) -> dict:
    return {"id": video_id, "title": title, **extra}


class OutputKeyTest(unittest.TestCase):
    def test_same_title_gives_the_same_key_and_different_titles_do_not(self) -> None:
        self.assertEqual(output_key(TITLE, _info("a")), output_key(TITLE, _info("b")))
        self.assertNotEqual(output_key(TITLE, _info("a")), output_key(TITLE, _info("a", "다른 제목")))

    def test_id_in_the_template_separates_videos(self) -> None:
        template = "%(title)s (%(id)s).%(ext)s"
        self.assertNotEqual(output_key(template, _info("a")), output_key(template, _info("b")))

    def test_extension_is_ignored(self) -> None:
        # an audio run and a video run of one name share their .part files
        self.assertEqual(
            output_key("%(title)s.%(ext)s", _info("a", ext="mp4")),
            output_key("%(title)s.%(ext)s", _info("b", ext="mp3")),
        )

    def test_names_that_differ_only_in_case_or_filename_unsafe_characters_clash(self) -> None:
        self.assertEqual(
            output_key(TITLE, _info("a", "Live: Q/A?")), output_key(TITLE, _info("b", "live QA"))
        )

    def test_directory_is_part_of_the_key(self) -> None:
        self.assertNotEqual(
            output_key(os.path.join("out", "뉴스", TITLE), _info("a")),
            output_key(os.path.join("out", "음악", TITLE), _info("a")),
        )

    def test_literal_percent_and_missing_fields(self) -> None:
        # category folders escape "%" as "%%"
        self.assertIn("100%", output_key(os.path.join("100%%", TITLE), _info("a")))
        self.assertEqual(
            output_key("%(channel)s - %(title)s.%(ext)s", {"title": "t"}),
            output_key("NA - %(title)s.%(ext)s", {"title": "t"}),
        )
        # yt-dlp's field forms we do not read: the key still exists, and is the same for all videos
        self.assertEqual(
            output_key("%(.{id})j.%(ext)s", _info("a")), output_key("%(.{id})j.%(ext)s", _info("b"))
        )

    def test_printf_flags_do_not_hide_the_field(self) -> None:
        self.assertEqual(
            output_key("%(title).50s.%(ext)s", _info("a")), output_key("%(title)s.%(ext)s", _info("b")),
        )


class WithVideoIdTest(unittest.TestCase):
    def test_id_goes_before_the_extension(self) -> None:
        self.assertEqual(with_video_id("%(title)s.%(ext)s"), "%(title)s (%(id)s).%(ext)s")

    def test_only_the_name_part_is_changed(self) -> None:
        self.assertEqual(
            with_video_id(os.path.join("out", "%(ext)s", "%(title)s.%(ext)s")),
            os.path.join("out", "%(ext)s", "%(title)s (%(id)s).%(ext)s"),
        )

    def test_name_without_an_extension_field_gets_the_id_at_the_end(self) -> None:
        self.assertEqual(with_video_id("%(title)s"), "%(title)s (%(id)s)")

    def test_template_that_already_names_the_id_is_left_alone(self) -> None:
        self.assertIsNone(with_video_id("%(title)s (%(id)s).%(ext)s"))
        self.assertIsNone(with_video_id("%(id)s.%(ext)s"))


class OutputClaimsTest(unittest.TestCase):
    def test_second_owner_cannot_take_a_held_name_until_the_first_leaves(self) -> None:
        claims = OutputClaims()
        first, second = object(), object()
        claims.enter(first)
        claims.enter(second)
        self.assertIsNone(claims.claim(first, OutputClaim("k", "a")))
        held = claims.claim(second, OutputClaim("k", "b"))
        self.assertEqual((held.key, held.video_id), ("k", "a"))
        claims.leave(first)
        self.assertIsNone(claims.claim(second, OutputClaim("k", "b")))

    def test_an_owner_may_claim_again_and_replaces_its_earlier_name(self) -> None:
        claims = OutputClaims()
        owner, other = object(), object()
        self.assertIsNone(claims.claim(owner, OutputClaim("old")))
        self.assertIsNone(claims.claim(owner, OutputClaim("old")))
        self.assertIsNone(claims.claim(owner, OutputClaim("new")))
        self.assertIsNone(claims.claim(other, OutputClaim("old")))  # freed by the replacement

    def test_others_running_counts_entered_owners_without_a_name(self) -> None:
        claims = OutputClaims()
        me, other = object(), object()
        claims.enter(me)
        self.assertEqual(claims.others_running(me), 0)
        claims.enter(other)
        self.assertEqual(claims.others_running(me), 1)
        claims.leave(other)
        claims.leave(other)  # leaving twice is harmless
        self.assertEqual(claims.others_running(me), 0)


class ReserveOutputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.claims = OutputClaims()
        self.first, self.second, self.third = object(), object(), object()

    def test_free_name_is_kept(self) -> None:
        reservation = reserve_output(self.claims, self.first, TITLE, _info("a"))
        self.assertEqual((reservation.template, reservation.renamed, reservation.busy), (TITLE, False, None))

    def test_same_title_other_video_gets_the_id_added(self) -> None:
        reserve_output(self.claims, self.first, TITLE, _info("a"))
        reservation = reserve_output(self.claims, self.second, TITLE, _info("b"))
        self.assertTrue(reservation.renamed)
        self.assertEqual(reservation.template, "%(title)s (%(id)s).%(ext)s")
        self.assertIsNone(reservation.busy)

    def test_the_renamed_name_is_held_too(self) -> None:
        reserve_output(self.claims, self.first, TITLE, _info("a"))
        reserve_output(self.claims, self.second, TITLE, _info("b"))
        # a third video of the same title also gets its own id name, not b's
        third = reserve_output(self.claims, self.third, TITLE, _info("c"))
        self.assertTrue(third.renamed)
        self.assertIsNone(third.busy)

    def test_same_video_is_refused(self) -> None:
        reserve_output(self.claims, self.first, TITLE, _info("a"))
        reservation = reserve_output(self.claims, self.second, TITLE, _info("a"), "https://x/other-shape")
        self.assertTrue(reservation.same_video)
        self.assertEqual(reservation.busy.video_id, "a")
        self.assertFalse(reservation.renamed)

    def test_same_video_is_refused_even_when_the_template_has_the_id(self) -> None:
        template = "%(title)s (%(id)s).%(ext)s"
        reserve_output(self.claims, self.first, template, _info("a"))
        reservation = reserve_output(self.claims, self.second, template, _info("a"))
        self.assertTrue(reservation.same_video)

    def test_clash_that_cannot_be_renamed_is_refused(self) -> None:
        # no id in either probe: both expand "%(id)s" to NA, and the name already has the id field
        template = "%(title)s (%(id)s).%(ext)s"
        reserve_output(self.claims, self.first, template, {"title": "t"})
        reservation = reserve_output(self.claims, self.second, template, {"title": "t"})
        self.assertIsNotNone(reservation.busy)
        self.assertFalse(reservation.same_video)

    def test_a_refused_owner_holds_nothing(self) -> None:
        reserve_output(self.claims, self.first, TITLE, _info("a"))
        reserve_output(self.claims, self.second, TITLE, _info("a"))  # refused
        self.claims.leave(self.first)
        self.assertFalse(reserve_output(self.claims, self.third, TITLE, _info("c")).renamed)

    def test_name_is_free_again_after_the_holder_leaves(self) -> None:
        reserve_output(self.claims, self.first, TITLE, _info("a"))
        self.claims.leave(self.first)
        self.assertFalse(reserve_output(self.claims, self.second, TITLE, _info("b")).renamed)


class RunDownloadClaimsTest(unittest.TestCase):
    """Two run_download calls on different threads, like the GUI's."""

    INFOS = {
        "https://t/a": LiveStatusInfo("not_live", "같은 제목", raw=_info("AAA")),
        "https://t/b": LiveStatusInfo("not_live", "같은 제목", raw=_info("BBB")),
        "https://t/c": LiveStatusInfo("not_live", "다른 제목", raw=_info("CCC", "다른 제목")),
        "https://t/a2": LiveStatusInfo("not_live", "같은 제목", raw=_info("AAA")),
        "https://t/none": LiveStatusInfo("not_live"),  # the probe failed
    }

    def setUp(self) -> None:
        self.assertEqual(OUTPUT_CLAIMS.others_running(object()), 0)  # nothing leaked from another test
        self.templates: dict[str, str] = {}
        self.started = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

        def fake_run_once(opts, pcb, lcb, cancel_event=None):
            self.templates[opts.url] = opts.output_template
            if opts.url == "https://t/a":
                self.started.set()
                self.assertTrue(self.release.wait(10))
            return True, [], [], []

        patches = [
            mock.patch("core.live.detect_live_status", side_effect=lambda url, auth=None: self.INFOS[url]),
            mock.patch.object(downloader, "_run_once", fake_run_once),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _run(self, url: str, log=None):
        opts = DownloadOptions(url=url, format_opt="best", output_template=TITLE,
                               live_opts=LiveOptions())
        return downloader.run_download(
            url, opts, log_callback=(lambda line, level: log.append((line, level))) if log is not None else None
        )

    def _first_running(self) -> threading.Thread:
        worker = threading.Thread(target=self._run, args=("https://t/a",), daemon=True)
        worker.start()
        self.assertTrue(self.started.wait(10))
        return worker

    def _finish(self, worker: threading.Thread) -> None:
        self.release.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())

    def test_second_video_with_the_same_title_is_renamed(self) -> None:
        worker = self._first_running()
        log: list = []
        result = self._run("https://t/b", log)
        self._finish(worker)
        self.assertTrue(result.ok)
        self.assertEqual(self.templates["https://t/a"], TITLE)
        self.assertEqual(self.templates["https://t/b"], "%(title)s (%(id)s).%(ext)s")
        self.assertTrue(any("(ID)" in line and level == "warning" for line, level in log))

    def test_video_with_another_title_is_not_touched(self) -> None:
        worker = self._first_running()
        self._run("https://t/c")
        self._finish(worker)
        self.assertEqual(self.templates["https://t/c"], TITLE)

    def test_the_same_video_is_not_started_twice(self) -> None:
        worker = self._first_running()
        log: list = []
        result = self._run("https://t/a2", log)
        self._finish(worker)
        self.assertEqual((result.ok, result.skipped, result.cancelled), (False, "busy", False))
        self.assertNotIn("https://t/a2", self.templates)  # yt-dlp never ran
        self.assertTrue(any("이미 받는 중" in line and level == "error" for line, level in log))

    def test_name_is_free_again_once_the_first_download_ended(self) -> None:
        self._finish(self._first_running())
        self._run("https://t/b")
        self.assertEqual(self.templates["https://t/b"], TITLE)

    def test_a_run_that_raises_frees_its_name(self) -> None:
        with mock.patch.object(downloader, "_run_once", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._run("https://t/a")
        self.assertEqual(OUTPUT_CLAIMS.others_running(object()), 0)
        self.release.set()
        self._run("https://t/b")
        self.assertEqual(self.templates["https://t/b"], TITLE)

    def test_unreadable_video_info_warns_while_another_download_runs(self) -> None:
        worker = self._first_running()
        log: list = []
        result = self._run("https://t/none", log)
        self._finish(worker)
        self.assertTrue(result.ok)
        # not renamed; without the video's info only the length bound (fit_template) is added
        self.assertEqual(self.templates["https://t/none"], fit_template(TITLE).template)
        self.assertTrue(any("파일명이 겹치는지 확인하지" in line for line, _ in log))

    def test_unreadable_video_info_is_quiet_when_nothing_else_runs(self) -> None:
        log: list = []
        self._run("https://t/none", log)
        self.assertFalse(any("겹치는지" in line for line, _ in log))

    def test_download_with_the_probe_off_is_not_claimed(self) -> None:
        worker = self._first_running()
        opts = DownloadOptions(url="https://t/b", format_opt="best", output_template=TITLE)
        result = downloader.run_download("https://t/b", opts)
        self._finish(worker)
        self.assertTrue(result.ok)
        self.assertEqual(self.templates["https://t/b"], fit_template(TITLE).template)


if __name__ == "__main__":
    unittest.main()
