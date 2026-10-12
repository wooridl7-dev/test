"""Unit tests for main.yt_dlp_missing_hint()."""

from __future__ import annotations

import sys
import unittest
from unittest import mock

import main

_MODULE_CMD = [sys.executable, "-m", "yt_dlp"]


class YtDlpMissingHintTest(unittest.TestCase):
    def test_module_available(self) -> None:
        with mock.patch("core.cookies.yt_dlp_command", return_value=_MODULE_CMD), \
                mock.patch("importlib.util.find_spec", return_value=object()):
            self.assertIsNone(main.yt_dlp_missing_hint())

    def test_standalone_binary_on_path(self) -> None:
        with mock.patch("core.cookies.yt_dlp_command", return_value=["/usr/bin/yt-dlp"]), \
                mock.patch("importlib.util.find_spec", return_value=None):
            self.assertIsNone(main.yt_dlp_missing_hint())

    def test_frozen_bundle_skips_lookup(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch("core.cookies.yt_dlp_command",
                           side_effect=FileNotFoundError("no yt-dlp.exe")):
            self.assertIsNone(main.yt_dlp_missing_hint())

    def test_module_missing(self) -> None:
        with mock.patch("core.cookies.yt_dlp_command", return_value=_MODULE_CMD), \
                mock.patch("importlib.util.find_spec", return_value=None):
            hint = main.yt_dlp_missing_hint()
        self.assertIsNotNone(hint)
        self.assertIn(sys.executable, hint)

    def test_main_exits_before_dispatch(self) -> None:
        with mock.patch.object(main, "yt_dlp_missing_hint", return_value="hint"), \
                mock.patch("sys.stderr"):
            self.assertEqual(main.main(["--cli"]), 1)


class DispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(main, "yt_dlp_missing_hint", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_batch_runs_the_batch_cli(self) -> None:
        with mock.patch("interfaces.cli.run_batch_cli", return_value=0) as run:
            self.assertEqual(main.main(["--batch", "list.txt"]), 0)
            self.assertEqual(main.main(["--cli", "--batch", "list.txt"]), 0)
        self.assertEqual(run.call_args_list, [mock.call("list.txt")] * 2)

    def test_batch_preloads_the_gui(self) -> None:
        run = mock.Mock(return_value=0)
        with mock.patch.dict(sys.modules, {"interfaces.gui": mock.Mock(run_gui=run)}):
            self.assertEqual(main.main(["--gui", "--batch", "목록.txt"]), 0)
        run.assert_called_once_with(batch_path="목록.txt")

    def test_batch_is_not_for_the_bot(self) -> None:
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            main.main(["--bot", "--batch", "list.txt"])


if __name__ == "__main__":
    unittest.main()
