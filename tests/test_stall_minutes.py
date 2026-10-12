"""Tests for LiveOptions.stall_minutes and where it can be set.

``stall_minutes`` is how long a live recording may receive no new data
before the watchdog stops it; ``0`` turns the watchdog off. As with
``fragment_retries``, a typo must never quietly switch the safety net off.

Run from the repo root with::

    python -m unittest

Standard library only.
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
from core import downloader
from core.history import HistoryStore
from core.live import (
    DEFAULT_STALL_MINUTES,
    MAX_STALL_MINUTES,
    LiveOptions,
    parse_stall_minutes,
)
from interfaces import bot


class ParseTest(unittest.TestCase):
    def test_minutes(self) -> None:
        for text, expected in (("0", 0), ("1", 1), ("5", 5), ("007", 7), (" 30 ", 30),
                               (str(MAX_STALL_MINUTES), MAX_STALL_MINUTES)):
            with self.subTest(text):
                self.assertEqual(parse_stall_minutes(text), expected)

    def test_zero_turns_the_watchdog_off(self) -> None:
        self.assertEqual(parse_stall_minutes("0"), 0)

    def test_everything_else_is_refused(self) -> None:
        for text in ("-1", "+3", "1.5", "1e3", "abc", "", "  ", "off", "none", "infinite",
                     "5분", "１０", str(MAX_STALL_MINUTES + 1), "86400"):
            with self.subTest(text):
                with self.assertRaises(ValueError):
                    parse_stall_minutes(text)

    def test_the_error_says_what_is_accepted(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_stall_minutes("86400")
        self.assertIn(str(MAX_STALL_MINUTES), str(caught.exception))
        self.assertIn("'86400'", str(caught.exception))


class LiveOptionsTest(unittest.TestCase):
    def test_default_is_five(self) -> None:
        self.assertEqual(LiveOptions().stall_minutes, DEFAULT_STALL_MINUTES)
        self.assertEqual(DEFAULT_STALL_MINUTES, 5)

    def test_valid_values(self) -> None:
        for value in (0, 1, 5, 240, MAX_STALL_MINUTES):
            with self.subTest(value):
                self.assertEqual(LiveOptions(stall_minutes=value).stall_minutes, value)

    def test_invalid_values_are_refused(self) -> None:
        for value in (-1, 1.5, "5", None, True, False, [], float("inf")):
            with self.subTest(value):
                with self.assertRaises(ValueError):
                    LiveOptions(stall_minutes=value)

    def test_copies_keep_the_value(self) -> None:
        copy = dataclasses.replace(LiveOptions(stall_minutes=0), _detected_status=None)
        self.assertEqual(copy.stall_minutes, 0)


class NotesTest(unittest.TestCase):
    @staticmethod
    def _notes(**kwargs) -> list[tuple[str, str]]:
        logs: list[tuple[str, str]] = []
        downloader._log_live_recording_notes(LiveOptions(**kwargs), lambda m, lv: logs.append((m, lv)))
        return logs

    def test_the_default_adds_nothing(self) -> None:
        self.assertEqual([lv for _, lv in self._notes()], ["info"])

    def test_a_chosen_time_is_noted_for_the_log(self) -> None:
        logs = self._notes(stall_minutes=20)
        self.assertEqual([lv for _, lv in logs], ["info", "info"])
        self.assertIn("20분", logs[1][0])
        self.assertIn("꺼짐", self._notes(stall_minutes=0)[1][0])

    def test_infinite_retries_without_the_watchdog_still_warn(self) -> None:
        warnings = [m for m, lv in self._notes(fragment_retries=None, stall_minutes=0) if lv == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("자동 정지", warnings[0])

    def test_an_archive_download_says_the_setting_does_not_apply(self) -> None:
        logs: list[str] = []
        downloader._maybe_warn_live_opts_unused(
            LiveOptions(stall_minutes=15), lambda m, lv: logs.append(m))
        self.assertEqual(len(logs), 1)
        self.assertIn("stall_minutes=15", logs[0])


class CommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(main, "yt_dlp_missing_hint", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _refused(self, argv: list[str]) -> None:
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            main.main(argv)
        self.assertEqual(caught.exception.code, 2)

    def test_cli_gets_the_minutes(self) -> None:
        for text, expected in (("0", 0), ("15", 15)):
            with self.subTest(text), mock.patch("interfaces.cli.run_cli", return_value=0) as run:
                self.assertEqual(main.main(["--cli", "--stall-minutes", text]), 0)
                run.assert_called_once_with(stall_minutes=expected)

    def test_the_equals_form_works_too(self) -> None:
        with mock.patch("interfaces.cli.run_cli", return_value=0) as run:
            main.main(["--cli", "--stall-minutes=9"])
        run.assert_called_once_with(stall_minutes=9)

    def test_batch_gets_the_minutes(self) -> None:
        with mock.patch("interfaces.cli.run_batch_cli", return_value=0) as run:
            main.main(["--batch", "list.txt", "--stall-minutes", "0"])
            main.main(["--cli", "--batch", "list.txt", "--stall-minutes", "30"])
        self.assertEqual(run.call_args_list, [
            mock.call("list.txt", stall_minutes=0),
            mock.call("list.txt", stall_minutes=30),
        ])

    def test_both_live_flags_can_be_given_together(self) -> None:
        with mock.patch("interfaces.cli.run_cli", return_value=0) as run:
            main.main(["--cli", "--fragment-retries", "infinite", "--stall-minutes", "20"])
        run.assert_called_once_with(fragment_retries=None, stall_minutes=20)

    def test_without_the_flag_nothing_extra_is_passed(self) -> None:
        with mock.patch("interfaces.cli.run_cli", return_value=0) as run:
            main.main(["--cli"])
        run.assert_called_once_with()

    def test_bad_values_are_refused_before_anything_runs(self) -> None:
        for argv in (["--cli", "--stall-minutes", "-1"],
                     ["--cli", "--stall-minutes=-1"],
                     ["--cli", "--stall-minutes", "1.5"],
                     ["--cli", "--stall-minutes", "abc"],
                     ["--cli", "--stall-minutes", ""],
                     ["--cli", "--stall-minutes", "86400"],
                     ["--cli", "--stall-minutes"]):
            with self.subTest(argv), mock.patch("interfaces.cli.run_cli") as run:
                self._refused(argv)
                run.assert_not_called()

    def test_the_other_modes_say_where_to_set_it(self) -> None:
        for argv in (["--stall-minutes", "3"],
                     ["--gui", "--stall-minutes", "3"],
                     ["--gui", "--batch", "list.txt", "--stall-minutes", "3"],
                     ["--bot", "--stall-minutes", "3"]):
            with self.subTest(argv):
                self._refused(argv)

    def test_the_help_mentions_it(self) -> None:
        self.assertIn("--stall-minutes", main.build_parser().format_help())


class BotEnvTest(unittest.TestCase):
    @staticmethod
    def _config(value: str | None = None) -> bot.BotConfig:
        env = {k: v for k, v in os.environ.items() if k != "YTDLP_STALL_MINUTES"}
        if value is not None:
            env["YTDLP_STALL_MINUTES"] = value
        with mock.patch.dict(os.environ, env, clear=True):
            return bot.config_from_env()

    def test_unset_or_empty_is_the_default(self) -> None:
        self.assertEqual(self._config().stall_minutes, 5)
        self.assertEqual(self._config("").stall_minutes, 5)
        self.assertEqual(self._config("  ").stall_minutes, 5)

    def test_minutes_and_zero(self) -> None:
        self.assertEqual(self._config("0").stall_minutes, 0)
        self.assertEqual(self._config(" 25 ").stall_minutes, 25)

    def test_a_typo_is_refused_not_guessed(self) -> None:
        for value in ("-1", "1.5", "abc", "off", "86400"):
            with self.subTest(value), self.assertRaises(ValueError) as caught:
                self._config(value)
            self.assertIn("YTDLP_STALL_MINUTES", str(caught.exception))

    def test_the_config_hands_it_to_the_live_options(self) -> None:
        self.assertEqual(self._config("3").live_options().stall_minutes, 3)
        self.assertEqual(self._config("0").live_options().stall_minutes, 0)
        self.assertIn("stall_minutes=3", self._config("3").describe())

    def test_the_retries_setting_is_kept_alongside(self) -> None:
        env = {"YTDLP_FRAGMENT_RETRIES": "2", "YTDLP_STALL_MINUTES": "7"}
        with mock.patch.dict(os.environ, env):
            live = bot.config_from_env().live_options()
        self.assertEqual((live.fragment_retries, live.stall_minutes), (2, 7))

    def test_run_bot_stops_on_a_bad_value(self) -> None:
        with mock.patch.dict(os.environ, {"YTDLP_STALL_MINUTES": "-1"}), \
                mock.patch.object(bot.logging, "basicConfig"), \
                mock.patch.object(bot, "handle_url") as handle, \
                self.assertLogs(bot.logger, "ERROR") as logs:
            self.assertEqual(bot.run_bot(), 2)
        handle.assert_not_called()
        self.assertIn("YTDLP_STALL_MINUTES", logs.output[0])


class GuiFieldTest(unittest.TestCase):
    """The advanced tab's field, in a withdrawn Tk window."""

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
        self.addCleanup(self.root.update)  # run pending idle callbacks before the window goes
        from interfaces import gui
        self.gui = gui
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        self.app = gui.DownloaderGUI(self.root, history=HistoryStore(directory / "history.json"))
        self.app.output_path_var.set(str(directory))
        self.root.update()

    def _stall(self, text: str | int | None = None) -> int:
        if text is not None:
            self.app.live_stall_spin.delete(0, "end")
            self.app.live_stall_spin.insert(0, str(text))
        return self.app._collect_options("https://youtu.be/abcdefghijk").live_opts.stall_minutes

    def test_the_default_is_five(self) -> None:
        self.assertEqual(self._stall(), 5)

    def test_minutes_reach_the_options_and_zero_stays_zero(self) -> None:
        self.assertEqual(self._stall(0), 0)
        self.assertEqual(self._stall(1), 1)
        self.assertEqual(self._stall(45), 45)

    def test_a_half_edited_field_falls_back_to_the_default(self) -> None:
        for text in ("", "abc", "1.5", "-"):
            with self.subTest(text):
                self.assertEqual(self._stall(text), 5)

    def test_numbers_outside_the_field_are_clamped(self) -> None:
        self.assertEqual(self._stall("-4"), 0)
        self.assertEqual(self._stall("99999"), self.gui._MAX_STALL_SPIN)

    def test_the_infinite_warning_follows_the_auto_stop_time(self) -> None:
        self.app.live_retries_infinite_var.set(True)
        self._stall(12)
        self.app._sync_retries_widgets()
        self.assertIn("12분", self.app.live_retries_note_var.get())
        self._stall(0)
        self.app._sync_retries_widgets()
        self.assertIn("꺼짐", self.app.live_retries_note_var.get())
        self.assertIn("2026-04-26", self.app.live_retries_note_var.get())
        self.app.live_retries_infinite_var.set(False)
        self.app._sync_retries_widgets()
        self.assertEqual(self.app.live_retries_note_var.get(), "")

    def test_editing_the_field_updates_the_warning_without_another_click(self) -> None:
        self.app.live_retries_infinite_var.set(True)
        self.app.live_stall_var.set(30)  # a write to the field's variable
        self.root.update()
        self.assertIn("30분", self.app.live_retries_note_var.get())


if __name__ == "__main__":
    unittest.main()
