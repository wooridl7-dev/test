"""Unit tests for interfaces.cli.run_batch_cli (main.py --batch)."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from core.auth import NoAuth
from core.batch import BatchSummary
from core.downloader import DownloadResult
from interfaces import cli


class RunBatchCliTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.list = self.dir / "내youtube.txt"
        self.list.write_text(
            "# 요리\nhttps://youtu.be/aaaaaaaaaaa\n# 투자\nhttps://youtu.be/bbbbbbbbbbb\n",
            encoding="utf-8",
        )
        self.output = os.path.join(str(self.dir), "%(title)s.%(ext)s")
        for name, value in [
            ("_check_js_runtime_interactive", None),
            ("get_download_options", ("1", "best")),
            ("get_subtitle_options", {}),
            ("get_audio_track_option", ("원본", None)),
            ("get_output_options", self.output),
            ("get_cookie_options", NoAuth()),
        ]:
            patcher = mock.patch.object(cli, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.history = mock.Mock()
        patcher = mock.patch.object(cli, "HistoryStore", return_value=self.history)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, answer: str = "", states=("ok", "ok"), **cli_kwargs):
        def fake_run_batch(items, base_opts, *, item_callback, remaining_path, **_kw):
            self.base_opts, self.remaining_path, self.batch_kwargs = base_opts, remaining_path, _kw
            results = []
            for index, (item, state) in enumerate(zip(items, states)):
                result = DownloadResult(ok=state == "ok", title=f"영상{index}")
                item_callback(index, item, state, result)
                results.append(result)
            return BatchSummary(items=list(items), states=list(states), results=results)

        out = io.StringIO()
        with mock.patch.object(cli, "run_batch", side_effect=fake_run_batch) as run, \
                mock.patch("builtins.input", return_value=answer), redirect_stdout(out):
            code = cli.run_batch_cli(str(self.list), **cli_kwargs)
        return code, run, out.getvalue()

    def test_runs_the_list_once_asked(self) -> None:
        code, run, out = self._run()
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertEqual(self.base_opts.output_template, self.output)
        self.assertEqual(self.remaining_path.name, "내youtube.남은목록.txt")
        self.assertIn("분야 2개 · 2개", out)
        self.assertIn("[2/2] 투자 ✓ 영상1", out)
        # One history entry per video, saved under its category folder.
        folders = [c.args[0].output_dir for c in self.history.add.call_args_list]
        self.assertEqual(folders, [str(self.dir / "요리"), str(self.dir / "투자")])

    def test_the_default_pause_is_used_and_shown(self) -> None:
        _, _, out = self._run()
        self.assertEqual(self.batch_kwargs["pause"], (5.0, 15.0))
        self.assertIn("영상 사이 휴식: 5~15초", out)

    def test_a_chosen_pause_reaches_run_batch_and_is_shown(self) -> None:
        _, _, out = self._run(pause=(2.0, 30.0))
        self.assertEqual(self.batch_kwargs["pause"], (2.0, 30.0))
        self.assertIn("영상 사이 휴식: 2~30초", out)
        _, _, out = self._run(pause=(0.0, 0.0))
        self.assertEqual(self.batch_kwargs["pause"], (0.0, 0.0))
        self.assertIn("영상 사이 휴식: 쉬지 않음", out)

    def test_failures_give_exit_code_1(self) -> None:
        code, _, _ = self._run(states=("ok", "failed"))
        self.assertEqual(code, 1)

    def test_declining_does_not_download(self) -> None:
        code, run, _ = self._run(answer="n")
        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_missing_list_file(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.run_batch_cli(str(self.dir / "없음.txt")), 1)
        self.assertIn("목록 파일을 읽지 못했습니다", out.getvalue())


if __name__ == "__main__":
    unittest.main()
