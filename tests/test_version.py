"""Unit tests for core.version.app_version()."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import version


class AppVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        version.app_version.cache_clear()
        self.addCleanup(version.app_version.cache_clear)

    def test_source_checkout_uses_version_file_and_git(self) -> None:
        expected_base = (version._REPO_ROOT / "VERSION").read_text().strip()
        with mock.patch.object(version, "_git_describe", return_value="abc1234"):
            self.assertEqual(version.app_version(), f"{expected_base} (abc1234)")

    def test_source_checkout_without_git(self) -> None:
        with mock.patch.object(version, "_git_describe", return_value=""):
            self.assertNotIn("(", version.app_version())

    def test_frozen_app_reads_bundled_file(self) -> None:
        with tempfile.TemporaryDirectory() as meipass:
            Path(meipass, version.BUNDLED_VERSION_FILE).write_text(
                "0.1.0 (723dc02)\n", encoding="utf-8"
            )
            with mock.patch.object(sys, "frozen", True, create=True), \
                 mock.patch.object(sys, "_MEIPASS", meipass, create=True):
                self.assertEqual(version.app_version(), "0.1.0 (723dc02)")


if __name__ == "__main__":
    unittest.main()
