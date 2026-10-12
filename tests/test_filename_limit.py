"""Tests for core.filename_limit: keeping output file names inside the file
system's limit (255 bytes on Linux, 143 on an encrypted home), and its use by
core.downloader.run_download.

The case that started it is a real log line from Ubuntu (2026-10-06)::

    ERROR: unable to open for writing: [Errno 36] File name too long:
    '/home/user/Downloads/이번 주택은 굉장하네요~ ... #풀하우스.f299.mp4.part'

whose Korean title is 98 characters and 248 bytes: with ``.f299.mp4.part`` the
name is 262 bytes.

Run from the repo root with::

    python -m unittest

Standard library only. The test that drives yt-dlp's own name making is skipped
when the ``yt_dlp`` package is not importable.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest import mock

from core import downloader, filename_limit
from core.downloader import DownloadOptions
from core.filename_limit import (
    MIN_TITLE_BYTES,
    OTHER_FIELD_CAP,
    RESERVE_BYTES,
    fit_template,
    name_max_for,
)
from core.live import LiveOptions, LiveStatusInfo
from core.output_claims import output_key

# The title from the real log line above (98 characters, 248 bytes).
REAL_TITLE = (
    "이번 주택은 굉장하네요~ 목조주택에 경질폼과 우레탄판넬 단열!! 완전 퍼펙트 합니다. "
    "#전원주택#농가주택#소형주택#타이니하우스#체류형쉼터#가성비주택#모듈러주택#이동주택#풀하우스"
)
TITLE = "%(title)s.%(ext)s"
# The longest thing yt-dlp or the app appends to a name (see RESERVE_BYTES).
WORST_SUFFIX = 36

posix = mock.patch.object(filename_limit, "_limits_in_bytes", return_value=True)
windows = mock.patch.object(filename_limit, "_limits_in_bytes", return_value=False)


def _info(title: str, **extra) -> dict:
    return {"id": "D1WknJLGrIc", "title": title, "channel": "채널", "upload_date": "20261001",
            "ext": "mp4", **extra}


def _fit(template: str, info=None, name_max: int = 255):
    with posix:
        return fit_template(os.path.join("dir", template), info, name_max)


def _tail(fitted) -> str:
    return os.path.basename(fitted.template)


class RealCaseTest(unittest.TestCase):
    def test_the_real_title_is_98_characters_and_248_bytes(self) -> None:
        self.assertEqual((len(REAL_TITLE), len(REAL_TITLE.encode())), (98, 248))
        self.assertGreater(len((REAL_TITLE + ".f299.mp4.part").encode()), 255)

    def test_it_is_bounded_so_the_part_file_fits(self) -> None:
        fitted = _fit(TITLE, _info(REAL_TITLE))
        self.assertTrue(fitted.cut)
        self.assertEqual(_tail(fitted), f"%(title).{fitted.title_bound}B.%(ext)s")
        shown = REAL_TITLE.encode()[: fitted.title_bound].decode("utf-8", "ignore")
        self.assertLessEqual(len((shown + ".f299.mp4.part").encode()), 255)
        self.assertLessEqual(len(shown.encode()) + WORST_SUFFIX + len(".mp4"), 255)


class NameMaxTest(unittest.TestCase):
    def test_a_real_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(1 <= name_max_for(tmp) <= 255)

    def test_a_folder_that_does_not_exist_is_asked_through_its_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seen = []

            def pathconf(path, name):
                seen.append(path)
                if not os.path.isdir(path):
                    raise FileNotFoundError(path)
                return 143

            with mock.patch.object(filename_limit.os, "pathconf", pathconf, create=True):
                self.assertEqual(name_max_for(os.path.join(tmp, "a", "b")), 143)
            self.assertEqual(os.path.abspath(seen[-1]), os.path.abspath(tmp))

    def test_what_the_system_reports_is_used_but_not_above_255(self) -> None:
        with mock.patch.object(filename_limit.os, "pathconf", create=True, return_value=143):
            self.assertEqual(name_max_for("."), 143)
        with mock.patch.object(filename_limit.os, "pathconf", create=True, return_value=4096):
            self.assertEqual(name_max_for("."), 255)

    def test_when_it_cannot_be_asked_it_is_255(self) -> None:
        with mock.patch.object(filename_limit.os, "pathconf", create=True,
                               side_effect=OSError("no")):
            self.assertEqual(name_max_for("."), 255)
        with mock.patch.object(filename_limit.os, "pathconf", create=True,
                               side_effect=ValueError("bad name")):
            self.assertEqual(name_max_for("."), 255)
        with mock.patch.object(filename_limit.os, "pathconf", create=True,
                               side_effect=AttributeError("windows")):
            self.assertEqual(name_max_for("."), 255)


class FitTest(unittest.TestCase):
    def test_a_name_that_fits_is_left_alone(self) -> None:
        for title in ("짧은 제목", "x" * 100, "가" * 60):
            with self.subTest(title):
                fitted = _fit(TITLE, _info(title))
                self.assertEqual(fitted.template, os.path.join("dir", TITLE))
                self.assertFalse(fitted.cut)
                self.assertIsNone(fitted.title_bound)

    def test_the_title_is_bounded_in_bytes_not_characters(self) -> None:
        fitted = _fit(TITLE, _info("가" * 98))
        room = 255 - RESERVE_BYTES - len(".".encode()) - 4  # "." and the 4 bytes for %(ext)s
        self.assertEqual(fitted.title_bound, room - room % 3)  # whole characters only
        self.assertTrue(fitted.cut)

    def test_other_name_parts_take_room_from_the_title(self) -> None:
        title = _info("가" * 98)
        plain = _fit(TITLE, title).title_bound
        channel = _fit("%(channel)s - %(title)s.%(ext)s", title).title_bound
        date = _fit("%(upload_date)s - %(title)s.%(ext)s", title).title_bound
        with_id = _fit("%(title)s (%(id)s).%(ext)s", title).title_bound
        self.assertLess(channel, plain)
        self.assertLess(date, plain)
        self.assertLess(with_id, plain)
        # the real size of the channel is used, not a guess
        longer = _fit("%(channel)s - %(title)s.%(ext)s", _info("가" * 98, channel="채널" * 10))
        self.assertLess(longer.title_bound, channel)

    def test_channel_names_are_capped_when_bounds_are_applied(self) -> None:
        fitted = _fit("%(channel)s - %(title)s.%(ext)s", _info("가" * 98))
        self.assertIn(f"%(channel).{OTHER_FIELD_CAP}B", fitted.template)
        short = _fit("%(channel)s - %(title)s.%(ext)s", _info("짧음"))
        self.assertNotIn("B", _tail(short).replace("%(title)s", ""))  # untouched

    def test_without_the_video_info_the_title_is_always_bounded(self) -> None:
        fitted = _fit(TITLE, None)
        self.assertIsNotNone(fitted.title_bound)
        self.assertFalse(fitted.cut)  # a title it does not know cannot be called cut
        self.assertEqual(fitted.title_bound, _fit(TITLE, {}).title_bound)
        # the other parts are assumed at typical sizes, so there is less room
        self.assertLess(_fit("%(channel)s - %(title)s.%(ext)s", None).title_bound,
                        fitted.title_bound)

    def test_a_smaller_limit_gives_a_smaller_bound(self) -> None:
        big = _fit(TITLE, _info("가" * 98), 255).title_bound
        small = _fit(TITLE, _info("가" * 98), 143).title_bound
        self.assertEqual(small, 143 - RESERVE_BYTES - 5 - ((143 - RESERVE_BYTES - 5) % 3))
        self.assertLess(small, big)

    def test_the_title_is_never_cut_below_the_floor(self) -> None:
        self.assertEqual(_fit(TITLE, _info("가" * 98), 60).title_bound, MIN_TITLE_BYTES)
        self.assertEqual(_fit(TITLE, None, 20).title_bound, MIN_TITLE_BYTES)

    def test_special_characters_that_grow_are_counted_exactly(self) -> None:
        # yt-dlp turns " * : < > ? | / \ into 3-byte full-width characters, after the cut
        title = "|" * 100  # 100 bytes as typed, 300 in the name
        fitted = _fit(TITLE, _info(title))
        self.assertTrue(fitted.cut)
        room = 255 - RESERVE_BYTES - 5
        self.assertEqual(fitted.title_bound, room // 3)  # one byte typed, three in the name
        self.assertLessEqual(fitted.title_bound * 3, room)

    def test_a_bound_already_there_is_kept_when_tighter_and_tightened_when_looser(self) -> None:
        tight = _fit("%(title).60B.%(ext)s", _info("가" * 98))
        self.assertEqual(tight.template, os.path.join("dir", "%(title).60B.%(ext)s"))  # fits: kept
        loose = _fit("%(title).900B.%(ext)s", _info("가" * 98))
        self.assertLess(loose.title_bound, 900)
        # a smaller limit tightens it below the bound that was already there
        smaller = _fit("%(title).100B.%(ext)s", _info("가" * 98), 143)
        self.assertLess(smaller.title_bound, 100)

    def test_fitting_twice_changes_nothing(self) -> None:
        once = _fit(TITLE, _info(REAL_TITLE))
        with posix:
            twice = fit_template(once.template, _info(REAL_TITLE), 255)
        self.assertEqual(twice.template, once.template)
        no_info = _fit(TITLE, None)
        with posix:
            self.assertEqual(fit_template(no_info.template, None, 255).template, no_info.template)

    def test_an_id_added_later_is_fitted_again(self) -> None:
        info = _info("가" * 98)
        first = _fit(TITLE, info)
        renamed = first.template.replace(".%(ext)s", " (%(id)s).%(ext)s")
        with posix:
            second = fit_template(renamed, info, 255)
        self.assertLess(second.title_bound, first.title_bound)
        name = "가" * (second.title_bound // 3) + " (D1WknJLGrIc).mp4"
        self.assertLessEqual(len(name.encode()) + WORST_SUFFIX - 4, 255)

    def test_templates_without_a_plain_title_are_left_alone(self) -> None:
        for template in ("%(id)s.%(ext)s", "%(title|제목)s.%(ext)s", "fixed.%(ext)s"):
            with self.subTest(template):
                fitted = _fit(template, _info(REAL_TITLE))
                self.assertEqual(fitted.template, os.path.join("dir", template))
                self.assertIsNone(fitted.title_bound)

    def test_literal_text_and_percent_signs_are_counted_in_bytes(self) -> None:
        plain = _fit(TITLE, _info("가" * 98)).title_bound
        longer_text = _fit("제목: %(title)s.%(ext)s", _info("가" * 98)).title_bound
        self.assertLess(longer_text, plain)
        percent = _fit("100%%%(title)s.%(ext)s", _info("가" * 98)).title_bound
        self.assertLessEqual(percent, plain - 1)

    def test_the_folder_part_is_not_touched(self) -> None:
        with posix:
            fitted = fit_template(os.path.join("a", "%(title)s", "%(title)s.%(ext)s"),
                                  _info(REAL_TITLE), 255)
        self.assertEqual(os.path.dirname(fitted.template), os.path.join("a", "%(title)s"))

    def test_nothing_changes_where_limits_count_characters(self) -> None:
        with windows:
            fitted = fit_template(os.path.join("dir", TITLE), _info(REAL_TITLE), 255)
        self.assertEqual(fitted.template, os.path.join("dir", TITLE))
        self.assertEqual((fitted.name_max, fitted.title_bound, fitted.cut), (0, None, False))


class NameClashKeyTest(unittest.TestCase):
    def test_titles_that_only_differ_after_the_cut_have_the_same_key(self) -> None:
        template = os.path.join("dir", "%(title).60B.%(ext)s")
        a = output_key(template, _info("가" * 40 + "A"))
        b = output_key(template, _info("가" * 40 + "B"))
        self.assertEqual(a, b)  # both are cut to the same 60 bytes
        self.assertNotEqual(output_key(template, _info("가" * 19 + "다른")),
                            output_key(template, _info("가" * 21)))

    def test_a_cut_in_the_middle_of_a_character_drops_it(self) -> None:
        self.assertEqual(output_key("%(title).4B.x", {"title": "가나"}),
                         output_key("%(title).3B.x", {"title": "가나"}))

    def test_a_character_precision_counts_characters(self) -> None:
        self.assertEqual(output_key("%(title).2s.x", {"title": "abc"}),
                         output_key("%(title).2s.x", {"title": "abd"}))


class RunDownloadTest(unittest.TestCase):
    URL = "https://t/long"

    def setUp(self) -> None:
        self.templates: dict[str, str] = {}
        self.logs: list[tuple[str, str]] = []
        self.infos: dict[str, LiveStatusInfo] = {}

        def fake_run_once(opts, pcb, lcb, cancel_event=None):
            self.templates[opts.url] = opts.output_template
            return True, [], [], []

        patches = [
            mock.patch("core.live.detect_live_status",
                       side_effect=lambda url, auth=None: self.infos[url]),
            mock.patch.object(downloader, "_run_once", fake_run_once),
            mock.patch.object(filename_limit, "_limits_in_bytes", return_value=True),
            mock.patch.object(filename_limit, "name_max_for", return_value=255),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _run(self, url: str, title: str, template: str = TITLE, with_info: bool = True):
        self.infos[url] = LiveStatusInfo(
            "not_live", title, raw=_info(title, id=url[-3:]) if with_info else {})
        opts = DownloadOptions(url=url, format_opt="best",
                               output_template=os.path.join("out", template),
                               live_opts=LiveOptions())
        return downloader.run_download(
            url, opts, log_callback=lambda line, level: self.logs.append((line, level)))

    def test_a_long_title_is_bounded_and_the_user_is_told(self) -> None:
        self.assertTrue(self._run(self.URL, REAL_TITLE).ok)
        self.assertRegex(os.path.basename(self.templates[self.URL]), r"^%\(title\)\.\d+B\.%\(ext\)s$")
        warnings = [line for line, level in self.logs if level == "warning" and "제목이 길어" in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("255바이트", warnings[0])
        self.assertIn(REAL_TITLE, warnings[0])

    def test_a_short_title_is_left_alone_and_nothing_is_said(self) -> None:
        self._run(self.URL, "짧은 제목")
        self.assertEqual(self.templates[self.URL], os.path.join("out", TITLE))
        self.assertFalse(any("제목이 길어" in line for line, _ in self.logs))

    def test_without_the_probe_the_title_is_bounded_anyway_and_nothing_is_said(self) -> None:
        self._run(self.URL, "", with_info=False)
        self.assertRegex(os.path.basename(self.templates[self.URL]), r"^%\(title\)\.\d+B\.%\(ext\)s$")
        self.assertFalse(any("제목이 길어" in line for line, _ in self.logs))

    def test_a_name_that_only_differs_after_the_cut_is_still_told_apart(self) -> None:
        # Two videos whose long titles agree for the first 200+ bytes end up with the
        # same file name after the cut: the second one must get its id.
        first_title = "가" * 90 + "-하나"
        second_title = "가" * 90 + "-둘"
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def run_once(opts, pcb, lcb, cancel_event=None):
            self.templates[opts.url] = opts.output_template
            if opts.url.endswith("a1"):
                started.set()
                release.wait(10)
            return True, [], [], []

        with mock.patch.object(downloader, "_run_once", run_once):
            worker = threading.Thread(target=self._run, args=("https://t/a1", first_title), daemon=True)
            worker.start()
            self.assertTrue(started.wait(10))
            self._run("https://t/b2", second_title)
            release.set()
            worker.join(10)
        first = os.path.basename(self.templates["https://t/a1"])
        second = os.path.basename(self.templates["https://t/b2"])
        self.assertNotIn("%(id)", first)
        self.assertIn("(%(id)s)", second)  # renamed
        # and the renamed name was fitted again: the id fits within the limit
        bound = int(second.split(".")[1].split("B")[0])
        name = "가" * (bound // 3) + " (b2).mp4"
        self.assertLessEqual(len(name.encode()) + WORST_SUFFIX - 4, 255)


@unittest.skipIf(os.name == "nt", "limits count characters on Windows")
class WithYtDlpTest(unittest.TestCase):
    """The names yt-dlp itself makes from the fitted templates stay inside the limit."""

    def setUp(self) -> None:
        try:
            import yt_dlp
        except ImportError:
            self.skipTest("yt_dlp is not importable")
        self.ydl = yt_dlp.YoutubeDL({"quiet": True})

    def _name(self, template: str, info: dict) -> str:
        return os.path.basename(self.ydl.prepare_filename(info, outtmpl=template))

    def _titles(self) -> list[str]:
        base = ["가나다라마바사아자차카타파하", "日本語のタイトル", "emoji 😀🎉 title ", "Hello, world! "]
        specials = ['"인용" | 부제: 질문? / 경로\\ * <태그> ', "#해시#태그#"]
        out = []
        for unit in base + specials:
            for n in (1, 5, 10, 20, 40, 60, 80, 100):
                out.append((unit * n)[:100])
        out.append(REAL_TITLE)
        return out

    def test_names_fit_for_every_preset_and_limit_with_the_video_info(self) -> None:
        templates = ["%(title)s.%(ext)s", "%(channel)s - %(title)s.%(ext)s",
                     "%(upload_date)s - %(title)s.%(ext)s", "%(title)s (%(id)s).%(ext)s"]
        for name_max in (255, 143):
            for template in templates:
                for title in self._titles():
                    info = _info(title, channel="한글채널이름")
                    fitted = _fit(template, info, name_max)
                    name = self._name(_tail(fitted), info)
                    with self.subTest(name_max=name_max, template=template, title=title[:20]):
                        self.assertLessEqual(len(name.encode()) + WORST_SUFFIX, name_max, name)

    def test_names_fit_without_the_video_info_for_titles_without_special_characters(self) -> None:
        for template in ("%(title)s.%(ext)s", "%(title)s (%(id)s).%(ext)s",
                         "%(upload_date)s - %(title)s.%(ext)s"):
            for title in self._titles():
                if any(char in '"*:<>?|/\\' for char in title):
                    continue
                info = _info(title)
                name = self._name(_tail(_fit(template, None)), info)
                with self.subTest(template=template, title=title[:20]):
                    self.assertLessEqual(len(name.encode()) + WORST_SUFFIX, 255, name)

    def test_the_cut_name_is_still_the_start_of_the_title(self) -> None:
        info = _info(REAL_TITLE)
        name = self._name(_tail(_fit(TITLE, info)), info)
        self.assertTrue(name.endswith(".mp4"))
        self.assertTrue(REAL_TITLE.startswith(name[: -len(".mp4")]), name)

    def test_a_title_that_fits_gives_the_same_name_as_before(self) -> None:
        info = _info("짧은 제목")
        self.assertEqual(self._name(_tail(_fit(TITLE, info)), info), "짧은 제목.mp4")


if __name__ == "__main__":
    unittest.main()
