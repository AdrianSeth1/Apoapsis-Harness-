from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from download_service_v2.jobs import JobState, JobStore


class JobsContractTests(unittest.TestCase):
    """Development checks for Slice A: durable job-record bookkeeping."""

    def setUp(self) -> None:
        self.jobs = JobStore()
        self.url = "https://cdn.example.invalid/artifact.bin"

    def test_new_job_starts_pending_with_zeroed_counters(self) -> None:
        record = self.jobs.get_record(self.url)
        self.assertEqual(record.offset, 0)
        self.assertEqual(record.attempt_count, 0)
        self.assertEqual(record.transferred_bytes, 0)
        self.assertEqual(record.state, JobState.PENDING)
        self.assertIsNone(record.expected_checksum)
        self.assertIsNone(record.last_error)

    def test_record_attempt_increments_and_returns_the_new_count(self) -> None:
        self.assertEqual(self.jobs.record_attempt(self.url), 1)
        self.assertEqual(self.jobs.record_attempt(self.url), 2)
        self.assertEqual(self.jobs.get_record(self.url).attempt_count, 2)

    def test_record_progress_updates_offset_transferred_bytes_and_state(self) -> None:
        self.jobs.record_progress(self.url, 100, 40)
        record = self.jobs.get_record(self.url)
        self.assertEqual(record.offset, 100)
        self.assertEqual(record.transferred_bytes, 40)
        self.assertEqual(record.state, JobState.IN_PROGRESS)

    def test_set_expected_checksum_is_recorded(self) -> None:
        self.jobs.set_expected_checksum(self.url, "abc123")
        self.assertEqual(self.jobs.get_record(self.url).expected_checksum, "abc123")

    def test_mark_state_complete_has_no_error(self) -> None:
        self.jobs.mark_state(self.url, JobState.COMPLETE)
        record = self.jobs.get_record(self.url)
        self.assertEqual(record.state, JobState.COMPLETE)
        self.assertIsNone(record.last_error)

    def test_mark_state_failed_records_the_error_reason(self) -> None:
        self.jobs.mark_state(self.url, JobState.FAILED, error="checksum mismatch")
        record = self.jobs.get_record(self.url)
        self.assertEqual(record.state, JobState.FAILED)
        self.assertEqual(record.last_error, "checksum mismatch")

    def test_existing_offset_api_is_preserved(self) -> None:
        # The pre-existing public surface (this scenario's own starting
        # point) must keep working unmodified once the new bookkeeping is
        # added.
        self.jobs.set_offset(self.url, 55)
        self.assertEqual(self.jobs.get_offset(self.url), 55)
        self.assertEqual(self.jobs.get_record(self.url).offset, 55)


if __name__ == "__main__":
    unittest.main()
