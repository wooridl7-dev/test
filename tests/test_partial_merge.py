"""Unit tests for core.partial_merge (merging a stopped live recording)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import partial_merge
from core.partial_merge import find_parts, merge_partial_recording


class _Dir(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # Title characters the console code page would have mangled.
        self.target = self.dir / "＂뉴스＂ ⧸ 채널..mp4"

    def write(self, name: str, data: bytes = b"x") -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path


class FindPartsTest(_Dir):
    def test_per_format_and_combined_parts(self) -> None:
        stem = self.target.stem
        video = self.write(f"{stem}.f137.mp4.part")
        audio = self.write(f"{stem}.f140-1.mp4.part")
        self.write(f"{stem}.f137.mp4.part-Frag5.part")   # fragment in progress
        self.write(f"{stem}.f137.mp4.part-Frag4")        # fragment, not a part
        self.write(f"{stem}.f137.mp4.ytdl")
        self.write(f"{stem}.ko.vtt.part")                # subtitles
        self.write(f"other.f137.mp4.part")
        # Empty is normal with several fragments at a time (data in -FragN).
        empty = self.write(f"{stem}.f999.mp4.part", b"")
        self.assertEqual(find_parts(self.target), sorted([video, audio, empty]))

    def test_single_combined_format(self) -> None:
        combined = self.write(f"{self.target.stem}.mp4.part")
        self.assertEqual(find_parts(self.target), [combined])

    def test_format_id_skips_leftovers_of_another_quality(self) -> None:
        # Same title recorded before at 720p: its parts are not this download's.
        stem = self.target.stem
        video = self.write(f"{stem}.f137.mp4.part")
        audio = self.write(f"{stem}.f140-drc.m4a.part")
        self.write(f"{stem}.f136.mp4.part")
        self.write(f"{stem}.mp4.part")
        self.assertEqual(find_parts(self.target, "137+140-drc"), sorted([video, audio]))

    def test_format_id_of_a_single_combined_format(self) -> None:
        combined = self.write(f"{self.target.stem}.mp4.part")
        self.write(f"{self.target.stem}.webm.part")
        self.write(f"{self.target.stem}.f95.mp4.part")
        self.assertEqual(find_parts(self.target, "95"), [combined])


class PendingFragmentsTest(_Dir):
    def test_only_finished_fragments_after_the_appended_index(self) -> None:
        part = self.write("x.f137.mp4.part")
        (self.dir / "x.f137.mp4.ytdl").write_text(
            json.dumps({"downloader": {"current_fragment": {"index": 3}}}), encoding="utf-8")
        self.write("x.f137.mp4.part-Frag2")        # already appended
        frag10 = self.write("x.f137.mp4.part-Frag10")
        frag4 = self.write("x.f137.mp4.part-Frag4")
        self.write("x.f137.mp4.part-Frag11.part")  # still downloading
        self.assertEqual(partial_merge._pending_fragments(part), [frag4, frag10])

    def test_without_ytdl_every_finished_fragment_counts(self) -> None:
        part = self.write("x.f137.mp4.part")
        frag1 = self.write("x.f137.mp4.part-Frag1")
        self.assertEqual(partial_merge._pending_fragments(part), [frag1])


class MergeTest(_Dir):
    def _ffmpeg(self, returncode: int = 0, seen: list | None = None):
        """Stand-in for subprocess.run(ffmpeg ...): records the inputs'
        bytes and writes the output file."""
        def run(cmd, **_kw):
            inputs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"]
            if seen is not None:
                seen.append((cmd, [Path(p).read_bytes() for p in inputs]))
            if returncode == 0:
                Path(cmd[-1]).write_bytes(b"merged")
            return mock.Mock(returncode=returncode, stderr="Invalid data found\n")
        return run

    def _merge(self, run, ffmpeg: str | None = "ffmpeg", format_id: str | None = None):
        logs: list[tuple[str, str]] = []
        with mock.patch.object(partial_merge, "find_ffmpeg", return_value=ffmpeg), \
                mock.patch.object(partial_merge.subprocess, "run", side_effect=run):
            out = merge_partial_recording(
                str(self.target), lambda m, lv: logs.append((m, lv)), format_id)
        return out, logs

    def test_only_the_given_formats_are_merged(self) -> None:
        stem = self.target.stem
        self.write(f"{stem}.f137.mp4.part", b"V")
        self.write(f"{stem}.f140.mp4.part", b"A")
        self.write(f"{stem}.f136.mp4.part", b"old 720p")
        seen: list = []
        self._merge(self._ffmpeg(seen=seen), format_id="137+140")
        self.assertEqual(seen[0][1], [b"V", b"A"])

    def test_ffmpeg_gets_the_time_left(self) -> None:
        self.write(f"{self.target.stem}.f140.mp4.part")
        timeouts: list = []

        def run(cmd, **kw):
            timeouts.append(kw["timeout"])
            raise partial_merge.subprocess.TimeoutExpired(cmd, kw["timeout"])
        out, logs = self._merge(run)
        self.assertIsNone(out)
        self.assertTrue(0 < timeouts[0] <= partial_merge.MERGE_TIMEOUT)
        # Not the whole ffmpeg argv that TimeoutExpired would print.
        self.assertEqual(logs[-1], ("✗ 받은 부분 합치기 실패: 20분 안에 끝나지 않았습니다", "error"))

    def test_copying_fragments_stops_at_the_deadline(self) -> None:
        stem = self.target.stem
        self.write(f"{stem}.f137.mp4.part", b"V1")
        self.write(f"{stem}.f137.mp4.part-Frag2", b"V2")
        clock = iter([0.0, partial_merge.MERGE_TIMEOUT + 1.0])
        with mock.patch.object(partial_merge.time, "monotonic", lambda: next(clock)):
            out, logs = self._merge(self._ffmpeg())
        self.assertIsNone(out)
        self.assertIn("20분", logs[-1][0])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         sorted([f"{stem}.f137.mp4.part", f"{stem}.f137.mp4.part-Frag2"]))

    def test_ctrl_c_during_merge_leaves_no_half_file(self) -> None:
        stem = self.target.stem
        self.write(f"{stem}.f137.mp4.part", b"V1")
        self.write(f"{stem}.f137.mp4.part-Frag2", b"V2")

        def run(cmd, **_kw):
            Path(cmd[-1]).write_bytes(b"half")
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self._merge(run)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         sorted([f"{stem}.f137.mp4.part", f"{stem}.f137.mp4.part-Frag2"]))

    def test_merges_parts_with_pending_fragments_into_a_new_file(self) -> None:
        stem = self.target.stem
        self.write(f"{stem}.f137.mp4.part", b"V1")
        self.write(f"{stem}.f137.mp4.part-Frag7", b"V2")    # not yet appended
        self.write(f"{stem}.f140.mp4.part", b"A1")
        existing = self.write(f"{stem} (부분 녹화).mp4", b"old")
        seen: list = []
        out, logs = self._merge(self._ffmpeg(seen=seen))

        self.assertEqual(out, str(self.dir / f"{stem} (부분 녹화) 2.mp4"))  # no overwrite
        self.assertEqual(existing.read_bytes(), b"old")
        cmd, input_bytes = seen[0]
        self.assertEqual(input_bytes, [b"V1V2", b"A1"])  # fragment appended to a copy
        self.assertEqual(cmd[cmd.index("-map"):cmd.index("-c")], ["-map", "0", "-map", "1"])
        self.assertIn("-n", cmd)
        # Originals untouched (resumable), temp copies removed.
        self.assertEqual((self.dir / f"{stem}.f137.mp4.part").read_bytes(), b"V1")
        self.assertFalse([p for p in self.dir.iterdir() if p.name.endswith(".tmp")])
        self.assertEqual(logs[-2][1], "info")
        self.assertIn(Path(out).name, logs[-2][0])

    def test_empty_parts_with_piled_up_fragments(self) -> None:
        # Several fragments at a time: yt-dlp appended nothing yet.
        stem = self.target.stem
        self.write(f"{stem}.f137.mp4.part", b"")
        self.write(f"{stem}.f137.mp4.part-Frag2", b"V2")
        self.write(f"{stem}.f137.mp4.part-Frag1", b"V1")
        self.write(f"{stem}.f140.mp4.part", b"")
        self.write(f"{stem}.f140.mp4.part-Frag1", b"A1")
        self.write(f"{stem}.f999.mp4.part", b"")  # empty, no fragments: skipped
        seen: list = []
        out, logs = self._merge(self._ffmpeg(seen=seen))
        self.assertIsNotNone(out)
        self.assertEqual(seen[0][1], [b"V1V2", b"A1"])
        self.assertIn("조각 3개", logs[0][0])

    def test_nothing_to_merge(self) -> None:
        self.write(f"{self.target.stem}.f140.mp4.part", b"")  # no data at all
        out, logs = self._merge(self._ffmpeg())
        self.assertIsNone(out)
        self.assertEqual(logs, [])

    def test_missing_ffmpeg_is_reported(self) -> None:
        self.write(f"{self.target.stem}.f140.mp4.part")
        out, logs = self._merge(self._ffmpeg(), ffmpeg=None)
        self.assertIsNone(out)
        self.assertEqual([lv for _, lv in logs], ["warning"])

    def test_failed_merge_leaves_no_output(self) -> None:
        self.write(f"{self.target.stem}.f140.mp4.part")

        def run(cmd, **_kw):
            Path(cmd[-1]).write_bytes(b"half")
            return mock.Mock(returncode=1, stderr="Invalid data found\n")
        out, logs = self._merge(run)
        self.assertIsNone(out)
        self.assertEqual(logs[-1], ("✗ 받은 부분 합치기 실패: Invalid data found", "error"))
        self.assertFalse((self.dir / f"{self.target.stem} (부분 녹화).mp4").exists())


if __name__ == "__main__":
    unittest.main()
