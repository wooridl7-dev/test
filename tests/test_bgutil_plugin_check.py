"""The bgutil pre-flight check must look where yt-dlp looks for its plugin.

Found on 2026-10-07 in a real bot console: a source run (Python 3.12 without the plugin
package) that uses the bundled ``yt-dlp.exe`` printed "bgutil-ytdlp-pot-provider 플러그인을 찾을 수
없습니다" although that yt-dlp loads the plugin from ``yt-dlp-plugins`` beside itself (its own
``-v`` output lists the directory and ``bgutil:http-2.0.1``). The failed first check also kept
the ping of the sidecar from ever running, so the console never said whether it answered.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import auth
from core.auth import BgutilPoTokenAuth

PLUGIN_FILE = "yt-dlp-plugins/bgutil-pot-provider/yt_dlp_plugins/extractor/getpot_bgutil_http.py"


class _Beside(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.exe = self.dir / "yt-dlp.exe"
        self.exe.write_bytes(b"")

    def add_plugin(self, relative: str = PLUGIN_FILE) -> None:
        path = self.dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# plugin\n", encoding="utf-8")

    def patched(self, command, packages: bool = False):
        """No plugin package in this interpreter (unless *packages*); yt-dlp resolves to *command*."""
        spec = object() if packages else None
        return mock.patch.multiple(
            auth,
            yt_dlp_command=mock.Mock(return_value=command),
            importlib=mock.Mock(util=mock.Mock(find_spec=mock.Mock(return_value=spec))),
        )


class PluginAvailableTest(_Beside):
    def test_the_plugin_beside_the_yt_dlp_that_runs_counts(self) -> None:
        self.add_plugin()
        with self.patched([str(self.exe)]):
            self.assertTrue(auth._bgutil_plugin_available())

    def test_a_yt_dlp_without_the_plugin_beside_it_does_not(self) -> None:
        with self.patched([str(self.exe)]):
            self.assertFalse(auth._bgutil_plugin_available())
        self.add_plugin("yt-dlp-plugins/other-plugin/yt_dlp_plugins/extractor/something_else.py")
        with self.patched([str(self.exe)]):
            self.assertFalse(auth._bgutil_plugin_available())  # a different plugin is not it

    def test_every_provider_file_name_counts(self) -> None:
        for name in ("getpot_bgutil.py", "getpot_bgutil_http.py", "getpot_bgutil_script.py"):
            with self.subTest(name):
                self.add_plugin(f"yt-dlp-plugins/bgutil-pot-provider/yt_dlp_plugins/extractor/{name}")
                with self.patched([str(self.exe)]):
                    self.assertTrue(auth._bgutil_plugin_available())

    def test_the_python_module_form_has_nothing_to_look_beside(self) -> None:
        self.add_plugin()  # next to the temp exe, but the command is python -m yt_dlp
        with self.patched([sys.executable, "-m", "yt_dlp"]):
            self.assertFalse(auth._bgutil_plugin_available())

    def test_no_yt_dlp_at_all_is_not_an_error(self) -> None:
        with mock.patch.multiple(
            auth, yt_dlp_command=mock.Mock(side_effect=FileNotFoundError("none")),
            importlib=mock.Mock(util=mock.Mock(find_spec=mock.Mock(return_value=None))),
        ):
            self.assertFalse(auth._bgutil_plugin_available())

    def test_an_installed_package_still_counts_on_its_own(self) -> None:
        # No yt-dlp.exe plugin folder, but the pip package is importable here.
        with self.patched([str(self.exe)], packages=True):
            self.assertTrue(auth._bgutil_plugin_available())


class ValidateTest(_Beside):
    def auth(self) -> BgutilPoTokenAuth:
        return BgutilPoTokenAuth(provider_url="http://localhost:4416")

    def test_with_the_plugin_beside_yt_dlp_the_sidecar_is_pinged(self) -> None:
        self.add_plugin()
        pinged = mock.Mock(return_value=(True, '/ping -> HTTP 200 {"version":"2.0.1"}'))
        with self.patched([str(self.exe)]), \
                mock.patch.object(BgutilPoTokenAuth, "_yt_dlp_version_ok", return_value=(True, "2026.08.19")), \
                mock.patch.object(BgutilPoTokenAuth, "_probe_http", pinged):
            result = self.auth().validate()
        self.assertTrue(result.success, result.message)
        self.assertIn("2.0.1", result.message)
        pinged.assert_called_once()

    def test_an_unreachable_sidecar_is_reported_as_that_not_as_a_missing_plugin(self) -> None:
        self.add_plugin()
        with self.patched([str(self.exe)]), \
                mock.patch.object(BgutilPoTokenAuth, "_yt_dlp_version_ok", return_value=(True, "2026.08.19")), \
                mock.patch.object(BgutilPoTokenAuth, "_probe_http", return_value=(False, "refused")):
            result = self.auth().validate()
        self.assertEqual((result.success, result.error_kind), (False, "bgutil_unreachable"))

    def test_a_really_missing_plugin_is_still_reported_and_nothing_is_pinged(self) -> None:
        pinged = mock.Mock()
        with self.patched([str(self.exe)]), mock.patch.object(BgutilPoTokenAuth, "_probe_http", pinged):
            result = self.auth().validate()
        self.assertEqual((result.success, result.error_kind), (False, "bgutil_plugin_missing"))
        pinged.assert_not_called()


if __name__ == "__main__":
    unittest.main()
