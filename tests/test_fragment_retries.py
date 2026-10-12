"""Tests for LiveOptions.fragment_retries and where it can be set.

``--fragment-retries`` counts retries, so ``0`` means none; yt-dlp's keyword
for retrying forever is ``infinite`` (checked with ``yt-dlp --help``). The
tests keep the two apart: 0 must never turn into infinite.

Run from the repo root with::

    python -m unittest

Standard library only.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
from core import downloader
from core.downloader import DownloadOptions
from core.history import HistoryStore
from core.live import (
    DEFAULT_FRAGMENT_RETRIES,
    LiveOptions,
    fragment_retries_arg,
    parse_fragment_retries,
)
from interfaces import bot


def _live_cmd(**live_kwargs) -> list[str]:
    live = LiveOptions(**live_kwargs)
    live._detected_status = "is_live"
    return downloader._build_cmd(DownloadOptions(
        url="https://youtu.be/x", format_opt="best", output_template="o", live_opts=live))


def _value(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


class ParseTest(unittest.TestCase):
    def test_counts(self) -> None:
        for text, expected in (("0", 0), ("1", 1), ("10", 10), ("007", 7), (" 25 ", 25),
                               ("999999999999", 999999999999)):
            with self.subTest(text):
                self.assertEqual(parse_fragment_retries(text), expected)

    def test_zero_is_not_infinite(self) -> None:
        self.assertEqual(parse_fragment_retries("0"), 0)
        self.assertIsNotNone(parse_fragment_retries("0"))

    def test_the_keyword_is_infinite_in_any_case(self) -> None:
        for text in ("infinite", "INFINITE", " Infinite "):
            with self.subTest(text):
                self.assertIsNone(parse_fragment_retries(text))

    def test_everything_else_is_refused(self) -> None:
        for text in ("-1", "-0", "+3", "1.5", "1e3", "abc", "", "  ", "inf", "none", "None",
                     "unlimited", "1 0", "0x10", "１０", "١٠", "10회"):
            with self.subTest(text):
                with self.assertRaises(ValueError):
                    parse_fragment_retries(text)

    def test_the_error_names_the_accepted_forms(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_fragment_retries("-1")
        self.assertIn("infinite", str(caught.exception))
        self.assertIn("'-1'", str(caught.exception))


class ArgTest(unittest.TestCase):
    def test_the_value_for_yt_dlp(self) -> None:
        self.assertEqual(fragment_retries_arg(10), "10")
        self.assertEqual(fragment_retries_arg(0), "0")  # a number, not "infinite"
        self.assertEqual(fragment_retries_arg(None), "infinite")

    def test_parse_and_arg_round_trip(self) -> None:
        for text in ("0", "10", "300", "infinite"):
            self.assertEqual(fragment_retries_arg(parse_fragment_retries(text)), text)


class LiveOptionsTest(unittest.TestCase):
    def test_default_is_ten(self) -> None:
        self.assertEqual(LiveOptions().fragment_retries, DEFAULT_FRAGMENT_RETRIES)
        self.assertEqual(DEFAULT_FRAGMENT_RETRIES, 10)

    def test_valid_values(self) -> None:
        for value in (0, 1, 10, 5000, None):
            with self.subTest(value):
                self.assertEqual(LiveOptions(fragment_retries=value).fragment_retries, value)

    def test_invalid_values_are_refused(self) -> None:
        for value in (-1, 1.5, "10", "infinite", True, False, [], float("inf")):
            with self.subTest(value):
                with self.assertRaises(ValueError):
                    LiveOptions(fragment_retries=value)

    def test_copies_keep_the_value(self) -> None:
        import dataclasses
        copy = dataclasses.replace(LiveOptions(fragment_retries=None), _detected_status=None)
        self.assertIsNone(copy.fragment_retries)


class CommandTest(unittest.TestCase):
    def test_default_is_unchanged(self) -> None:
        self.assertEqual(_value(_live_cmd(), "--fragment-retries"), "10")

    def test_a_count_is_passed_as_it_is(self) -> None:
        for retries in (0, 3, 30):
            with self.subTest(retries):
                self.assertEqual(_value(_live_cmd(fragment_retries=retries), "--fragment-retries"),
                                 str(retries))

    def test_infinite_only_for_none(self) -> None:
        self.assertEqual(_value(_live_cmd(fragment_retries=None), "--fragment-retries"), "infinite")
        self.assertNotEqual(_value(_live_cmd(fragment_retries=0), "--fragment-retries"), "infinite")

    def test_not_added_outside_a_live_recording(self) -> None:
        cmd = downloader._build_cmd(DownloadOptions(
            url="https://youtu.be/x", format_opt="best", output_template="o",
            live_opts=LiveOptions(fragment_retries=0)))
        self.assertNotIn("--fragment-retries", cmd)


class NotesTest(unittest.TestCase):
    @staticmethod
    def _notes(**kwargs) -> list[tuple[str, str]]:
        logs: list[tuple[str, str]] = []
        downloader._log_live_recording_notes(LiveOptions(**kwargs), lambda m, lv: logs.append((m, lv)))
        return logs

    def test_defaults_add_nothing(self) -> None:
        self.assertEqual([lv for _, lv in self._notes()], ["info"])

    def test_a_chosen_count_is_noted_for_the_log(self) -> None:
        logs = self._notes(fragment_retries=0)
        self.assertEqual([lv for _, lv in logs], ["info", "info"])
        self.assertIn("0회", logs[1][0])
        self.assertIn("무한", self._notes(fragment_retries=None)[1][0])

    def test_infinite_without_the_watchdog_is_warned_about(self) -> None:
        logs = self._notes(fragment_retries=None, stall_minutes=0)
        warnings = [m for m, lv in logs if lv == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("2026-04-26", warnings[0])

    def test_no_warning_when_something_can_still_stop_it(self) -> None:
        for kwargs in ({"fragment_retries": None, "stall_minutes": 5},
                       {"fragment_retries": 10, "stall_minutes": 0},
                       {"fragment_retries": 0, "stall_minutes": 0}):
            with self.subTest(kwargs):
                self.assertEqual([lv for _, lv in self._notes(**kwargs) if lv == "warning"], [])

    def test_an_archive_download_says_the_setting_does_not_apply(self) -> None:
        logs: list[str] = []
        downloader._maybe_warn_live_opts_unused(
            LiveOptions(fragment_retries=3), lambda m, lv: logs.append(m))
        self.assertEqual(len(logs), 1)
        self.assertIn("fragment_retries=3", logs[0])
        logs.clear()
        downloader._maybe_warn_live_opts_unused(LiveOptions(), lambda m, lv: logs.append(m))
        self.assertEqual(logs, [])


class CommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(main, "yt_dlp_missing_hint", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _refused(self, argv: list[str]) -> None:
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            main.main(argv)
        self.assertEqual(caught.exception.code, 2)

    def test_cli_gets_the_count(self) -> None:
        for text, expected in (("0", 0), ("7", 7), ("infinite", None)):
            with self.subTest(text), mock.patch("interfaces.cli.run_cli", return_value=0) as run:
                self.assertEqual(main.main(["--cli", "--fragment-retries", text]), 0)
                run.assert_called_once_with(fragment_retries=expected)

    def test_the_equals_form_works_too(self) -> None:
        with mock.patch("interfaces.cli.run_cli", return_value=0) as run:
            main.main(["--cli", "--fragment-retries=3"])
        run.assert_called_once_with(fragment_retries=3)

    def test_batch_gets_the_count(self) -> None:
        with mock.patch("interfaces.cli.run_batch_cli", return_value=0) as run:
            main.main(["--batch", "list.txt", "--fragment-retries", "0"])
            main.main(["--cli", "--batch", "list.txt", "--fragment-retries", "infinite"])
        self.assertEqual(run.call_args_list, [
            mock.call("list.txt", fragment_retries=0),
            mock.call("list.txt", fragment_retries=None),
        ])

    def test_without_the_flag_nothing_extra_is_passed(self) -> None:
        # Absent must stay different from "infinite", which is None.
        with mock.patch("interfaces.cli.run_cli", return_value=0) as run:
            main.main(["--cli"])
        run.assert_called_once_with()

    def test_bad_values_are_refused_before_anything_runs(self) -> None:
        for argv in (["--cli", "--fragment-retries", "-1"],
                     ["--cli", "--fragment-retries=-1"],
                     ["--cli", "--fragment-retries", "1.5"],
                     ["--cli", "--fragment-retries", "abc"],
                     ["--cli", "--fragment-retries", ""],
                     ["--cli", "--fragment-retries"]):
            with self.subTest(argv), mock.patch("interfaces.cli.run_cli") as run:
                self._refused(argv)
                run.assert_not_called()

    def test_the_other_modes_say_where_to_set_it(self) -> None:
        for argv in (["--fragment-retries", "3"],
                     ["--gui", "--fragment-retries", "3"],
                     ["--gui", "--batch", "list.txt", "--fragment-retries", "3"],
                     ["--bot", "--fragment-retries", "3"]):
            with self.subTest(argv):
                self._refused(argv)

    def test_the_help_mentions_it(self) -> None:
        self.assertIn("--fragment-retries", main.build_parser().format_help())


class BotEnvTest(unittest.TestCase):
    @staticmethod
    def _config(value: str | None = None) -> bot.BotConfig:
        env = {k: v for k, v in os.environ.items() if k != "YTDLP_FRAGMENT_RETRIES"}
        if value is not None:
            env["YTDLP_FRAGMENT_RETRIES"] = value
        with mock.patch.dict(os.environ, env, clear=True):
            return bot.config_from_env()

    def test_unset_or_empty_is_the_default(self) -> None:
        self.assertEqual(self._config().fragment_retries, 10)
        self.assertEqual(self._config("").fragment_retries, 10)
        self.assertEqual(self._config("  ").fragment_retries, 10)

    def test_counts_and_infinite(self) -> None:
        self.assertEqual(self._config("0").fragment_retries, 0)
        self.assertEqual(self._config(" 25 ").fragment_retries, 25)
        self.assertIsNone(self._config("infinite").fragment_retries)

    def test_a_typo_is_refused_not_guessed(self) -> None:
        for value in ("-1", "1.5", "abc", "unlimited"):
            with self.subTest(value), self.assertRaises(ValueError) as caught:
                self._config(value)
            self.assertIn("YTDLP_FRAGMENT_RETRIES", str(caught.exception))

    def test_the_config_hands_it_to_the_live_options(self) -> None:
        self.assertEqual(self._config("3").live_options().fragment_retries, 3)
        self.assertIsNone(self._config("infinite").live_options().fragment_retries)
        self.assertIn("fragment_retries=3", self._config("3").describe())

    def test_run_bot_stops_on_a_bad_value(self) -> None:
        with mock.patch.dict(os.environ, {"YTDLP_FRAGMENT_RETRIES": "-1"}), \
                mock.patch.object(bot.logging, "basicConfig"), \
                mock.patch.object(bot, "handle_url") as handle, \
                self.assertLogs(bot.logger, "ERROR") as logs:
            self.assertEqual(bot.run_bot(), 2)
        handle.assert_not_called()
        self.assertIn("YTDLP_FRAGMENT_RETRIES", logs.output[0])


class GuiFieldTest(unittest.TestCase):
    """The advanced tab's field and checkbox, in a withdrawn Tk window."""

    def setUp(self) -> None:
        try:
            import tkinter as tk
        except ImportError:
            self.skipTest("tkinter is unavailable")
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("Tk display is unavailable")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        from interfaces import gui
        self.gui = gui
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        self.app = gui.DownloaderGUI(self.root, history=HistoryStore(directory / "history.json"))
        self.app.output_path_var.set(str(directory))
        self.root.update()

    def _retries(self, count: str | int | None = None, *, infinite: bool = False):
        if count is not None:
            # A disabled field ignores typing, as for a person.
            self.app.live_retries_spin.configure(state="normal")
            self.app.live_retries_spin.delete(0, "end")
            self.app.live_retries_spin.insert(0, str(count))
        self.app.live_retries_infinite_var.set(infinite)
        self.app._sync_retries_widgets()
        return self.app._collect_options("https://youtu.be/abcdefghijk").live_opts.fragment_retries

    def test_the_default_is_ten(self) -> None:
        self.assertEqual(self._retries(), 10)

    def test_a_count_reaches_the_options_and_zero_stays_zero(self) -> None:
        self.assertEqual(self._retries(0), 0)
        self.assertEqual(self._retries(3), 3)
        self.assertEqual(self._retries(250), 250)

    def test_the_checkbox_means_infinite_whatever_the_count_says(self) -> None:
        self.assertIsNone(self._retries(0, infinite=True))
        self.assertIsNone(self._retries(5, infinite=True))
        self.assertEqual(self._retries(5, infinite=False), 5)

    def test_a_half_edited_field_falls_back_to_the_default(self) -> None:
        for text in ("", "abc", "1.5", "-"):
            with self.subTest(text):
                self.assertEqual(self._retries(text), 10)

    def test_numbers_outside_the_field_are_clamped(self) -> None:
        self.assertEqual(self._retries("-4"), 0)
        self.assertEqual(self._retries("99999"), self.gui._MAX_RETRIES_SPIN)

    def test_infinite_disables_the_count_and_warns(self) -> None:
        self.app.live_retries_infinite_var.set(True)
        self.app._sync_retries_widgets()
        self.assertEqual(str(self.app.live_retries_spin.cget("state")), "disabled")
        self.assertIn("무한", self.app.live_retries_note_var.get())
        self.app.live_retries_infinite_var.set(False)
        self.app._sync_retries_widgets()
        self.assertEqual(str(self.app.live_retries_spin.cget("state")), "normal")
        self.assertEqual(self.app.live_retries_note_var.get(), "")


if __name__ == "__main__":
    unittest.main()
