"""Unit tests for interfaces.gui_jobs: job bookkeeping and list rows.

Run from the repo root with::

    python -m unittest

Standard library only; no Tk needed.
"""

from __future__ import annotations

import threading
import unittest

from core.downloader import ProgressDisplay
from interfaces.gui_jobs import (
    BATCH,
    DOWNLOAD,
    MAX_QUEUED,
    STALE_SECONDS,
    DownloadJob,
    JobRejected,
    JobTable,
    format_ago,
    format_elapsed,
    job_row,
)
from tests.test_downloader import (
    CONCURRENT_SECOND_STREAM,
    LIVE_UNKNOWN,
    VOD_LONG_ETA,
)

LIVE_VIDEO = (
    "1: [download]   0.0% of        N/A ~        N/A got    1.02GiB "
    "at  118.00KiB/s ETA Unknown"
)


def _info(line: str) -> dict:
    info = ProgressDisplay().parse_progress(line)
    assert info is not None, line
    return info


class FormatTest(unittest.TestCase):
    def test_elapsed(self) -> None:
        self.assertEqual(format_elapsed(0), "00:00:00")
        self.assertEqual(format_elapsed(59.9), "00:00:59")
        self.assertEqual(format_elapsed(3600 + 62), "01:01:02")
        self.assertEqual(format_elapsed(100 * 3600), "100:00:00")
        self.assertEqual(format_elapsed(-5), "00:00:00")

    def test_ago(self) -> None:
        self.assertEqual(format_ago(2), "2초 전")
        self.assertEqual(format_ago(125), "2분 전")
        self.assertEqual(format_ago(3600 + 600), "1시간 10분 전")


class JobRowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.job = DownloadJob(id=1, url="https://www.youtube.com/watch?v=abc", started_at=100.0)

    def test_before_any_progress(self) -> None:
        row = job_row(self.job, now=107.0)
        self.assertEqual(
            row.values,
            ("1", "youtube.com/watch?v=abc", "시작 중", "연결/대기 중", "-", "00:00:07", "-"),
        )
        self.assertFalse(row.stale)

    def test_waiting_for_a_scheduled_live_is_not_stale(self) -> None:
        self.job.badge = "⏰ 예정 대기"
        row = job_row(self.job, now=100.0 + 3 * 3600)
        self.assertEqual(row.values[2], "⏰ 예정 대기")
        self.assertFalse(row.stale)

    def test_vod_with_known_percent(self) -> None:
        self.job.record_progress(_info(VOD_LONG_ETA), now=110.0)
        row = job_row(self.job, now=112.0)
        self.assertEqual(row.values[2], "받는 중")
        self.assertEqual(row.values[3], "10.0% / 9.5MB")
        self.assertEqual(row.values[4], "976.6KB/s ETA 01:06:40")
        self.assertEqual(row.values[6], "2초 전")

    def test_live_unknown_size(self) -> None:
        self.job.record_progress(_info(LIVE_UNKNOWN), now=110.0)
        row = job_row(self.job, now=110.0)
        self.assertEqual(row.values[3], "4.8MB 받음")
        self.assertEqual(row.values[4], "N/A")  # no ETA suffix for live

    def test_two_streams_are_labelled(self) -> None:
        self.job.record_progress(_info(LIVE_VIDEO), now=110.0)
        self.job.record_progress(_info(CONCURRENT_SECOND_STREAM), now=111.0)
        row = job_row(self.job, now=111.0)
        self.assertEqual(row.values[3], "영상 1.0GB 받음 · 오디오 6.7MB 받음")
        self.assertEqual(row.values[4], "118.0KB/s · 976.6KB/s")

    def test_stale_after_a_minute_without_bytes(self) -> None:
        self.job.record_progress(_info(LIVE_UNKNOWN), now=110.0)
        self.assertFalse(job_row(self.job, now=110.0 + STALE_SECONDS).stale)
        row = job_row(self.job, now=110.0 + STALE_SECONDS + 1)
        self.assertTrue(row.stale)
        self.assertTrue(row.values[6].startswith("⚠ "))

    def test_repeated_identical_lines_do_not_count_as_activity(self) -> None:
        # A stuck live manifest-poll loop keeps printing the same byte count.
        self.job.record_progress(_info(LIVE_UNKNOWN), now=110.0)
        self.job.record_progress(_info(LIVE_UNKNOWN), now=150.0)
        self.assertEqual(self.job.last_update, 110.0)
        row = job_row(self.job, now=110.0 + STALE_SECONDS + 1)
        self.assertTrue(row.stale)

    def test_grown_bytes_refresh_the_last_update(self) -> None:
        self.job.record_progress(_info(LIVE_UNKNOWN), now=110.0)
        self.job.record_progress(_info(LIVE_VIDEO), now=150.0)  # other stream
        self.assertEqual(self.job.last_update, 150.0)
        grown = LIVE_UNKNOWN.replace("4.77MiB", "5.10MiB")
        self.job.record_progress(_info(grown), now=170.0)
        self.assertEqual(self.job.last_update, 170.0)

    def test_unknown_byte_count_counts_as_activity(self) -> None:
        finished = "[download] 100.0% of    9.54MiB ~ NA got NA at 976.56KiB/s ETA NA"
        self.job.record_progress(_info(finished), now=110.0)
        self.job.record_progress(_info(finished), now=120.0)
        self.assertEqual(self.job.last_update, 120.0)

    def test_no_stale_flag_while_merging_after_100_percent(self) -> None:
        done = "[download] 100.0% of    9.54MiB ~ NA got    9.54MiB at 976.56KiB/s ETA NA"
        self.job.record_progress(_info(done), now=110.0)
        row = job_row(self.job, now=110.0 + 10 * STALE_SECONDS)
        self.assertFalse(row.stale)
        self.assertFalse(row.values[6].startswith("⚠"))

    def test_no_stale_flag_after_cancel_requested(self) -> None:
        self.job.record_progress(_info(LIVE_UNKNOWN), now=110.0)
        self.job.cancel_event.set()  # merging the partial recording
        self.assertFalse(job_row(self.job, now=110.0 + 10 * STALE_SECONDS).stale)

    def test_finished_job_shows_no_speed(self) -> None:
        self.job.record_progress(_info(VOD_LONG_ETA), now=110.0)
        self.job.finish("failed", now=120.0)
        row = job_row(self.job, now=130.0)
        self.assertEqual(row.values[4], "-")
        self.assertEqual(row.values[3], "10.0% / 9.5MB")  # last progress stays

    def test_record_progress_does_not_mutate_a_snapshot(self) -> None:
        self.job.record_progress(_info(LIVE_UNKNOWN), now=1.0)
        snapshot = self.job.streams
        self.job.record_progress(_info(CONCURRENT_SECOND_STREAM), now=2.0)
        self.assertEqual(set(snapshot), {0})
        self.assertEqual(set(self.job.streams), {0, 1})

    def test_cancel_requested_then_finished(self) -> None:
        self.job.cancel_event.set()
        self.assertEqual(job_row(self.job, now=101.0).values[2], "취소 중")
        self.job.finish("cancelled", now=105.0, title="방송", note="부분 저장")
        row = job_row(self.job, now=999.0)
        self.assertEqual(row.values[1], "방송")
        self.assertEqual(row.values[2], "취소 (부분 저장)")
        self.assertEqual(row.values[5], "00:00:05")  # frozen at the end
        self.assertEqual(row.values[6], "-")
        self.assertFalse(row.stale)

    def test_final_labels(self) -> None:
        self.job.finish("done", now=101.0)
        self.assertEqual(job_row(self.job, now=102.0).values[2], "✓ 완료")
        self.job.finish("failed", now=101.0)
        self.assertEqual(job_row(self.job, now=102.0).values[2], "✗ 실패")


class BatchJobTest(unittest.TestCase):
    LABEL = "일괄: list.txt (12개)"

    def test_a_batch_is_one_job_that_counts_towards_the_cap(self) -> None:
        table = JobTable(max_parallel=2)
        batch = table.add_batch(self.LABEL)
        self.assertEqual((batch.kind, batch.url, batch.tag), (BATCH, self.LABEL, "[#1]"))
        self.assertEqual(table.add("https://example.com/a").kind, DOWNLOAD)
        self.assertEqual(table.running_count(), 2)
        waiting = table.add("https://example.com/b")  # over the cap: waits
        self.assertTrue(waiting.queued)
        self.assertEqual(table.running_count(), 2)

    def test_batch_waits_at_the_cap_and_only_one_exists_at_a_time(self) -> None:
        table = JobTable(max_parallel=1)
        table.add("https://example.com/a")
        waiting = table.add_batch(self.LABEL)
        self.assertEqual((waiting.kind, waiting.queued), (BATCH, True))
        with self.assertRaises(JobRejected) as ctx:  # a waiting batch counts as one
            table.add_batch("일괄: other.txt (3개)")
        self.assertIn("이미 진행 중", str(ctx.exception))
        table = JobTable(max_parallel=3)
        first = table.add_batch(self.LABEL)
        with self.assertRaises(JobRejected) as ctx:
            table.add_batch("일괄: other.txt (3개)")
        self.assertIn("이미 진행 중", str(ctx.exception))
        first.finish("done", now=1.0)
        table.add_batch("일괄: other.txt (3개)")  # fine once the first one ended

    def test_a_waiting_batch_starts_when_a_slot_frees(self) -> None:
        table = JobTable(max_parallel=1)
        single = table.add("https://example.com/a")
        batch = table.add_batch(self.LABEL)
        self.assertEqual(table.promote(), [])
        single.finish("done", now=1.0)
        self.assertEqual(table.promote(), [batch])
        self.assertTrue(batch.running)

    def test_a_label_is_never_compared_as_a_url(self) -> None:
        table = JobTable()
        table.add_batch("일괄: [list].txt (12개)")  # "[" is not valid in a host name
        table.add("https://example.com/a")  # must not trip over the label

    def test_a_download_of_the_url_a_batch_is_on_is_refused(self) -> None:
        table = JobTable()
        batch = table.add_batch(self.LABEL)
        table.add("https://www.youtube.com/watch?v=AAAAAAAAAAA")  # batch between items
        batch.start_item("📚 일괄 1/12", "https://youtu.be/BBBBBBBBBBB")
        with self.assertRaises(JobRejected):
            table.add("https://www.youtube.com/live/BBBBBBBBBBB?si=x")
        batch.start_item("📚 일괄 2/12", "https://youtu.be/CCCCCCCCCCC")
        table.add("https://www.youtube.com/watch?v=BBBBBBBBBBB")  # that item is done

    def test_start_item_forgets_the_last_items_progress(self) -> None:
        batch = JobTable().add_batch(self.LABEL)
        batch.start_item("📚 일괄 1/12", "https://example.com/a")
        batch.record_progress(_info(LIVE_UNKNOWN), now=10.0)
        self.assertTrue(batch.streams and batch.last_update == 10.0)
        batch.start_item("📚 일괄 2/12", "https://example.com/b")
        self.assertEqual((batch.streams, batch.last_update, batch.active_url),
                         ({}, None, "https://example.com/b"))

    def test_row_of_a_running_and_a_finished_batch(self) -> None:
        batch = DownloadJob(id=3, url=self.LABEL, started_at=100.0, kind=BATCH)
        self.assertEqual(job_row(batch, now=101.0).values[2], "시작 중")
        batch.start_item("📚 일괄 4/12", "https://example.com/a")
        batch.record_progress(_info(VOD_LONG_ETA), now=110.0)
        row = job_row(batch, now=112.0)
        self.assertEqual(row.values[1:4], (self.LABEL, "📚 일괄 4/12", "10.0% / 9.5MB"))
        batch.cancel_event.set()
        self.assertEqual(job_row(batch, now=113.0).values[2], "취소 중")
        batch.finish("cancelled", now=120.0, note="완료 3 · 취소 9")
        done = job_row(batch, now=999.0)
        self.assertEqual(done.values[2:7], ("취소 (완료 3 · 취소 9)", "-", "-", "00:00:20", "-"))
        self.assertEqual(batch.active_url, "")

    def test_end_item_frees_the_url_and_clears_the_row(self) -> None:
        table = JobTable()
        batch = table.add_batch(self.LABEL)
        url = "https://www.youtube.com/watch?v=BBBBBBBBBBB"
        batch.start_item("📚 일괄 1/2", url)
        batch.record_progress(_info(VOD_LONG_ETA), now=10.0)
        with self.assertRaises(JobRejected):
            table.add(url)
        batch.end_item()  # the item ended; the next one starts after a pause
        self.assertEqual((batch.streams, batch.last_update, batch.active_url), ({}, None, ""))
        self.assertFalse(job_row(batch, now=10.0 + 10 * STALE_SECONDS).stale)
        table.add(url)  # a download of it is fine during the pause

    def test_a_quiet_gap_between_items_is_not_a_stall(self) -> None:
        batch = DownloadJob(id=1, url=self.LABEL, started_at=0.0, kind=BATCH)
        batch.start_item("📚 일괄 1/2", "https://example.com/a")
        batch.record_progress(_info(LIVE_UNKNOWN), now=10.0)
        batch.start_item("📚 일괄 2/2", "https://example.com/b")  # the next item has not sent bytes yet
        self.assertFalse(job_row(batch, now=10.0 + 5 * STALE_SECONDS).stale)
        batch.record_progress(_info(LIVE_UNKNOWN), now=400.0)
        self.assertTrue(job_row(batch, now=400.0 + STALE_SECONDS + 1).stale)  # then silence counts


class UrlKeyTest(unittest.TestCase):
    def test_a_malformed_url_is_not_an_error(self) -> None:
        table = JobTable(max_parallel=5)
        table.add("https://[abc/x")
        with self.assertRaises(JobRejected):
            table.add("https://[abc/x")  # the same text is the same download
        table.add("https://example.com／watch")


class JobTableTest(unittest.TestCase):
    def test_ids_increase_and_running_count(self) -> None:
        table = JobTable(max_parallel=3)
        first = table.add("https://example.com/a")
        second = table.add("https://example.com/b")
        self.assertEqual((first.id, second.id), (1, 2))
        self.assertEqual(table.running_count(), 2)
        self.assertEqual(table.tag_for(first), "[#1] ")
        first.finish("done", now=1.0)
        self.assertEqual(table.tag_for(second), "")  # alone again

    def test_over_the_cap_a_download_waits_instead_of_being_refused(self) -> None:
        table = JobTable(max_parallel=2)
        first = table.add("https://example.com/a")
        second = table.add("https://example.com/b")
        third = table.add("https://example.com/c")
        self.assertEqual([j.running for j in (first, second, third)], [True, True, False])
        self.assertTrue(third.queued and third.active)
        self.assertEqual((table.running_count(), table.queued_count(), table.active_count()), (2, 1, 3))

    def test_cap_frees_when_a_job_ends(self) -> None:
        table = JobTable(max_parallel=1)
        job = table.add("https://example.com/a")
        job.finish("done", now=1.0)
        self.assertTrue(table.add("https://example.com/b").running)  # nobody waits: starts at once

    def test_same_url_is_rejected_while_running_only(self) -> None:
        table = JobTable()
        job = table.add("https://example.com/watch?v=1")
        with self.assertRaises(JobRejected):
            table.add("https://example.com/watch?v=1")
        job.finish("failed", now=1.0)
        table.add("https://example.com/watch?v=1")  # retry after it ended

    def test_youtube_link_shapes_are_one_video(self) -> None:
        table = JobTable()
        table.add("https://www.youtube.com/live/J4lzQL2PluA?si=tracking")
        for same in (
            "https://youtu.be/J4lzQL2PluA",
            "https://www.youtube.com/watch?v=J4lzQL2PluA&t=30",
            "youtube.com/live/J4lzQL2PluA",
            "https://m.youtube.com/watch?v=J4lzQL2PluA",
        ):
            with self.assertRaises(JobRejected, msg=same):
                table.add(same)
        table.add("https://www.youtube.com/watch?v=otherVideo1")

    def test_other_sites_keep_the_v_and_list_parameters(self) -> None:
        table = JobTable()
        table.add("https://example.com/playlist?list=A")
        table.add("https://example.com/playlist?list=B")
        with self.assertRaises(JobRejected):
            table.add("https://example.com/playlist?list=A&utm_campaign=x")

    def test_site_specific_query_ids_are_different_media(self) -> None:
        table = JobTable(max_parallel=5)
        table.add("https://www.bilibili.com/video/BV1xx?p=2")
        table.add("https://www.bilibili.com/video/BV1xx?p=3")
        table.add("https://example.com/view?id=1")
        table.add("https://example.com/view?id=2")
        with self.assertRaises(JobRejected):  # only tracking parameters differ
            table.add("https://www.bilibili.com/video/BV1xx?p=2&utm_source=x&si=y")

    def test_finished_job_without_thread_is_not_alive(self) -> None:
        job = JobTable().add("https://example.com/a")
        self.assertTrue(job.alive)
        job.finish("failed", now=1.0)
        self.assertFalse(job.alive)

    def test_cancel(self) -> None:
        table = JobTable()
        first = table.add("https://example.com/a")
        second = table.add("https://example.com/b")
        self.assertTrue(table.cancel(first.id))
        self.assertTrue(first.cancel_event.is_set())
        self.assertFalse(second.cancel_event.is_set())
        self.assertFalse(table.cancel(first.id))  # already asked
        self.assertFalse(table.cancel(999))
        second.finish("done", now=1.0)
        self.assertFalse(table.cancel(second.id))  # not running

    def test_cancel_all_counts_only_new_requests(self) -> None:
        table = JobTable()
        table.add("https://example.com/a")
        table.add("https://example.com/b")
        self.assertEqual(table.cancel_all(), 2)
        self.assertEqual(table.cancel_all(), 0)

    def test_any_alive_follows_the_worker_thread(self) -> None:
        table = JobTable()
        job = table.add("https://example.com/a")
        self.assertTrue(table.any_alive())  # no thread yet: still running
        release = threading.Event()
        job.thread = threading.Thread(target=release.wait, daemon=True)
        job.thread.start()
        job.finish("cancelled", now=1.0)
        self.assertTrue(table.any_alive())  # state is final, thread still merging
        release.set()
        job.thread.join(timeout=2)
        self.assertFalse(table.any_alive())

    def test_clear_finished_keeps_running(self) -> None:
        table = JobTable()
        done = table.add("https://example.com/a")
        running = table.add("https://example.com/b")
        done.finish("done", now=1.0)
        table.clear_finished()
        self.assertEqual([j.id for j in table.jobs], [running.id])

    def test_promote_starts_the_oldest_waiting_jobs_first_as_slots_free(self) -> None:
        clock = iter(range(100, 200))
        table = JobTable(max_parallel=1, clock=lambda: float(next(clock)))
        first = table.add("https://example.com/a")
        second = table.add("https://example.com/b")
        third = table.add("https://example.com/c")
        self.assertEqual(table.promote(), [])  # no free slot
        first.finish("done", now=1.0)
        self.assertEqual(table.promote(), [second])
        self.assertTrue(second.running and third.queued)
        self.assertEqual(table.promote(), [])
        second.finish("failed", now=2.0)
        self.assertEqual(table.promote(), [third])

    def test_promote_restarts_the_clock_of_the_job(self) -> None:
        now = [10.0]
        table = JobTable(max_parallel=1, clock=lambda: now[0])
        first = table.add("https://example.com/a")
        waiting = table.add("https://example.com/b")
        now[0] = 500.0
        first.finish("done", now=now[0])
        table.promote()
        self.assertEqual(waiting.started_at, 500.0)
        self.assertEqual(job_row(waiting, now=505.0).values[5], "00:00:05")

    def test_promote_fills_every_slot_a_raised_cap_opens(self) -> None:
        table = JobTable(max_parallel=1)
        table.add("https://example.com/a")
        waiting = [table.add(f"https://example.com/{n}") for n in "bcd"]
        table.max_parallel = 3
        self.assertEqual(table.promote(), waiting[:2])
        self.assertTrue(waiting[2].queued)

    def test_a_new_download_waits_behind_the_queue_even_when_a_slot_is_free(self) -> None:
        table = JobTable(max_parallel=1)
        first = table.add("https://example.com/a")
        old = table.add("https://example.com/b")
        first.finish("done", now=1.0)  # a slot is free, promote() has not run yet
        newer = table.add("https://example.com/c")
        self.assertTrue(old.queued and newer.queued)
        self.assertEqual(table.promote(), [old])

    def test_queue_positions_count_only_waiting_jobs(self) -> None:
        table = JobTable(max_parallel=1)
        table.add("https://example.com/a")
        second = table.add("https://example.com/b")
        third = table.add("https://example.com/c")
        self.assertEqual(table.queue_positions(), {second.id: 1, third.id: 2})
        table.cancel(second.id)
        self.assertEqual(table.queue_positions(), {third.id: 1})

    def test_a_url_that_is_waiting_cannot_be_added_again(self) -> None:
        table = JobTable(max_parallel=1)
        table.add("https://example.com/a")
        waiting = table.add("https://www.youtube.com/watch?v=AAAAAAAAAAA")
        with self.assertRaises(JobRejected) as ctx:
            table.add("https://youtu.be/AAAAAAAAAAA?si=x")
        self.assertIn("대기열", str(ctx.exception))
        table.cancel(waiting.id)
        table.add("https://youtu.be/AAAAAAAAAAA")  # after it left the queue

    def test_the_queue_has_a_limit(self) -> None:
        table = JobTable(max_parallel=1)
        table.add("https://example.com/running")
        for n in range(MAX_QUEUED):
            table.add(f"https://example.com/{n}")
        with self.assertRaises(JobRejected) as ctx:
            table.add("https://example.com/one-too-many")
        self.assertIn(str(MAX_QUEUED), str(ctx.exception))

    def test_cancel_drops_a_waiting_job_at_once(self) -> None:
        table = JobTable(max_parallel=1)
        running = table.add("https://example.com/a")
        waiting = table.add("https://example.com/b")
        self.assertTrue(table.cancel(waiting.id))
        self.assertEqual((waiting.state, waiting.note, waiting.active, waiting.alive),
                         ("cancelled", "대기 중 취소", False, False))
        self.assertTrue(waiting.cancel_event.is_set())
        self.assertFalse(table.cancel(waiting.id))  # nothing left to cancel
        self.assertEqual(job_row(waiting, now=999.0).values[2:], ("취소 (대기 중 취소)", "-", "-", "00:00:00", "-"))
        self.assertTrue(running.running and not running.cancel_event.is_set())
        running.finish("done", now=1.0)
        self.assertEqual(table.promote(), [])  # the cancelled job is not started

    def test_cancel_all_cancels_waiting_jobs_too(self) -> None:
        table = JobTable(max_parallel=1)
        running = table.add("https://example.com/a")
        waiting = table.add("https://example.com/b")
        self.assertEqual(table.cancel_all(), 2)
        self.assertTrue(running.cancel_event.is_set())
        self.assertEqual(waiting.state, "cancelled")
        self.assertEqual(table.cancel_all(), 0)

    def test_clear_finished_and_trimming_keep_waiting_jobs(self) -> None:
        table = JobTable(max_parallel=1)
        running = table.add("https://example.com/running")
        waiting = table.add("https://example.com/waiting")
        table.clear_finished()
        self.assertEqual([j.id for j in table.jobs], [running.id, waiting.id])
        for n in range(30):  # many finished rows must not push a waiting job out
            job = table.add(f"https://example.com/done{n}")
            table.cancel(job.id)
        self.assertIn(waiting, table.jobs)

    def test_waiting_row(self) -> None:
        table = JobTable(max_parallel=1)
        table.add("https://example.com/a")
        waiting = table.add("https://example.com/b")
        self.assertEqual(
            job_row(waiting, now=50.0, queue_position=2).values,
            (str(waiting.id), "example.com/b", "대기 중 (2번째)", "대기열", "-", "-", "-"),
        )
        self.assertEqual(job_row(waiting, now=50.0).values[2], "대기 중")
        self.assertFalse(job_row(waiting, now=50.0 + 10 * STALE_SECONDS).stale)

    def test_old_finished_jobs_are_trimmed(self) -> None:
        table = JobTable(max_parallel=1)
        for n in range(30):
            table.add(f"https://example.com/{n}").finish("done", now=1.0)
        table.add("https://example.com/last")
        self.assertLessEqual(len(table.jobs), 21)
        self.assertEqual(table.jobs[-1].url, "https://example.com/last")


if __name__ == "__main__":
    unittest.main()
