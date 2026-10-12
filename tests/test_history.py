"""Unit tests for core.history (download history file)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.downloader import DownloadResult
from core.history import HistoryEntry, HistoryStore, default_history_path


class HistoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # A missing parent folder is created on the first save.
        self.path = Path(tmp.name) / "sub" / "history.json"
        self.store = HistoryStore(self.path, limit=3)

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(self.store.entries(), [])
        self.assertEqual(self.store.recent(), [])

    def test_newest_first_and_persisted(self) -> None:
        self.store.add(HistoryEntry(url="u1", title="첫 번째"))
        self.store.add(HistoryEntry(url="u2"))
        self.assertEqual([e.url for e in self.store.entries()], ["u2", "u1"])
        reloaded = HistoryStore(self.path).entries()
        self.assertEqual([(e.url, e.title) for e in reloaded], [("u2", ""), ("u1", "첫 번째")])
        self.assertIn("첫 번째", self.path.read_text(encoding="utf-8"))

    def test_limit_drops_oldest(self) -> None:
        for n in range(5):
            self.store.add(HistoryEntry(url=f"u{n}"))
        self.assertEqual([e.url for e in self.store.entries()], ["u4", "u3", "u2"])

    def test_remove_and_clear(self) -> None:
        first = HistoryEntry(url="u1")
        self.store.add(first)
        self.store.add(HistoryEntry(url="u2"))
        self.store.remove([first.id])
        self.assertEqual([e.url for e in self.store.entries()], ["u2"])
        self.store.clear()
        self.assertEqual(self.store.entries(), [])

    def test_recent_keeps_newest_per_url(self) -> None:
        self.store.limit = 10
        for url, title in (("a", "old"), ("b", ""), ("a", "new")):
            self.store.add(HistoryEntry(url=url, title=title))
        recent = self.store.recent()
        self.assertEqual([(e.url, e.title) for e in recent], [("a", "new"), ("b", "")])
        self.assertEqual(len(self.store.recent(1)), 1)

    def test_corrupt_file_is_moved_aside(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("core.history", "WARNING"):
            self.assertEqual(self.store.entries(), [])
        self.assertTrue(self.path.with_name("history.json.bak").exists())
        self.store.add(HistoryEntry(url="u1"))
        self.assertEqual([e.url for e in self.store.entries()], ["u1"])

    def test_bom_is_accepted(self) -> None:
        self.path.parent.mkdir(parents=True)
        payload = json.dumps({"version": 1, "entries": [{"url": "u1"}]})
        self.path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))  # Notepad BOM
        self.assertEqual([e.url for e in self.store.entries()], ["u1"])

    def test_non_utf8_file_is_moved_aside_not_fatal(self) -> None:
        self.path.parent.mkdir(parents=True)
        payload = json.dumps({"entries": [{"url": "u", "title": "제목"}]}, ensure_ascii=False)
        self.path.write_bytes(payload.encode("cp949"))
        with self.assertLogs("core.history", "WARNING"):
            self.assertEqual(self.store.entries(), [])
        self.assertTrue(self.path.with_name("history.json.bak").exists())

    def test_read_error_never_overwrites_history(self) -> None:
        self.store.add(HistoryEntry(url="u1"))
        before = self.path.read_bytes()
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("locked")):
            with self.assertLogs("core.history", "WARNING"):
                self.assertEqual(self.store.entries(), [])  # display: empty
            with self.assertRaises(OSError):
                self.store.add(HistoryEntry(url="u2"))
            with self.assertRaises(OSError):
                self.store.remove(["x"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_leaves_no_temp_files(self) -> None:
        self.store.add(HistoryEntry(url="u1"))
        self.store.add(HistoryEntry(url="u2"))
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["history.json"])

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_saved_file_gets_the_umask_mode_not_0600(self) -> None:
        umask = os.umask(0o022)
        self.addCleanup(os.umask, umask)
        self.store.add(HistoryEntry(url="u1"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)

    def test_replace_is_retried_while_the_file_is_busy(self) -> None:
        real_replace = os.replace
        calls: list[int] = []

        def flaky_replace(src, dst):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError("in use by another process")
            real_replace(src, dst)
        with mock.patch("core.history.os.replace", side_effect=flaky_replace), \
                mock.patch("core.history.time.sleep"):
            self.store.add(HistoryEntry(url="u1"))
        self.assertEqual(len(calls), 2)
        self.assertEqual([e.url for e in self.store.entries()], ["u1"])

    def test_unusable_entries_are_skipped(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 1, "entries": [
            {"url": ""}, "junk", {"url": "u", "files": 5}, {"url": "ok", "id": "x1"},
        ]}), encoding="utf-8")
        self.assertEqual([(e.url, e.id) for e in self.store.entries()], [("ok", "x1")])


class HistoryEntryTest(unittest.TestCase):
    def test_defaults(self) -> None:
        entry = HistoryEntry(url="u")
        self.assertEqual(len(entry.id), 12)
        self.assertRegex(entry.time, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_from_result(self) -> None:
        result = DownloadResult(ok=True, title="T", files=[
            {"id": "x", "title": "T", "filepath": "/out/T.mp4", "language": "ko"},
        ])
        entry = HistoryEntry.from_result("u", result, "/out")
        self.assertEqual(
            (entry.status, entry.title, entry.audio_language, entry.files, entry.output_dir),
            ("ok", "T", "ko", ["/out/T.mp4"], "/out"),
        )

    def test_from_result_status(self) -> None:
        cancelled = HistoryEntry.from_result("u", DownloadResult(ok=False, cancelled=True))
        failed = HistoryEntry.from_result("u", DownloadResult(ok=False))
        self.assertEqual((cancelled.status, failed.status), ("cancelled", "failed"))
        self.assertEqual((failed.audio_language, failed.files), ("", []))
        live = HistoryEntry.from_result("u", DownloadResult(ok=False, skipped="live"))
        archived = HistoryEntry.from_result("u", DownloadResult(ok=True, skipped="archived"))
        self.assertEqual((live.status, archived.status), ("skipped", "skipped"))


class DefaultPathTest(unittest.TestCase):
    def test_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"YTDLP_HISTORY_FILE": "~/h.json"}):
            self.assertEqual(default_history_path(), Path("~/h.json").expanduser())

    def test_home_default(self) -> None:
        with mock.patch.dict(os.environ, {"YTDLP_HISTORY_FILE": ""}):
            self.assertEqual(
                default_history_path(), Path.home() / ".ytdlp-downloader" / "history.json"
            )


if __name__ == "__main__":
    unittest.main()
