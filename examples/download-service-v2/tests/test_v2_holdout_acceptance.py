from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

# Normally lives in `tests/`, two levels above `src/`; the held-out oracle
# mechanism also copies this file flat into a completed worktree's root
# (never into a `tests/` subdirectory there), one level above `src/` --
# handle both layouts explicitly rather than assuming either one.
_project_root = Path(__file__).resolve().parent
if _project_root.name == "tests":
    _project_root = _project_root.parent
sys.path.insert(0, str(_project_root / "src"))

from download_service_v2 import DownloadService, JobState, JobStore


class FakeResponse:
    def __init__(self, status_code: int, chunks: list[bytes]) -> None:
        self.status_code = status_code
        self.chunks = chunks

    def iter_chunks(self) -> list[bytes]:
        return list(self.chunks)


class ScriptedTransport:
    """Returns or raises each scripted outcome in order, one per `.get()`
    call. Withheld from every agent-visible fixture copy: adversarial,
    cross-slice integration cases that a per-slice dev/acceptance check does
    not exercise together."""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, str]] = []

    def get(self, url: str, *, headers: dict[str, str]):
        self.calls.append(headers)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class HoldOutCrossSliceAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.destination = Path(self.temporary_directory.name) / "artifact.bin"
        self.url = "https://cdn.example.invalid/artifact.bin"

    def test_existing_partial_data_with_range_honored_resumes_correctly(self) -> None:
        self.destination.write_bytes(b"partial-")
        jobs = JobStore()
        jobs.set_offset(self.url, 8)
        service = DownloadService(
            ScriptedTransport([FakeResponse(206, [b"continued"])]), jobs
        )

        downloaded = service.run(self.url, self.destination)

        self.assertEqual(self.destination.read_bytes(), b"partial-continued")
        self.assertEqual(downloaded, 17)
        self.assertEqual(jobs.get_record(self.url).state, JobState.COMPLETE)

    def test_existing_partial_data_with_range_ignored_replaces_it(self) -> None:
        self.destination.write_bytes(b"stale-data-here")
        jobs = JobStore()
        jobs.set_offset(self.url, 16)
        service = DownloadService(
            ScriptedTransport([FakeResponse(200, [b"fresh-full-response"])]), jobs
        )

        downloaded = service.run(self.url, self.destination)

        self.assertEqual(self.destination.read_bytes(), b"fresh-full-response")
        self.assertEqual(downloaded, 19)
        self.assertEqual(jobs.get_record(self.url).state, JobState.COMPLETE)

    def test_transient_failure_then_retry_succeeds_with_correct_content(self) -> None:
        jobs = JobStore()
        service = DownloadService(
            ScriptedTransport([ConnectionError("reset"), FakeResponse(200, [b"recovered"])]),
            jobs,
        )

        downloaded = service.run(self.url, self.destination)

        self.assertEqual(self.destination.read_bytes(), b"recovered")
        self.assertEqual(downloaded, 9)
        self.assertEqual(jobs.get_record(self.url).state, JobState.COMPLETE)

    def test_byte_accounting_is_correct_across_a_resume_and_a_retried_attempt(
        self,
    ) -> None:
        self.destination.write_bytes(b"head-")
        jobs = JobStore()
        jobs.set_offset(self.url, 5)
        service = DownloadService(
            ScriptedTransport(
                [ConnectionError("reset"), FakeResponse(206, [b"tail"])]
            ),
            jobs,
        )

        downloaded = service.run(self.url, self.destination)

        self.assertEqual(self.destination.read_bytes(), b"head-tail")
        self.assertEqual(downloaded, 9)
        record = jobs.get_record(self.url)
        self.assertEqual(record.offset, 9)
        self.assertEqual(record.transferred_bytes, 4)

    def test_progress_never_double_counts_bytes_across_multiple_chunks(self) -> None:
        jobs = JobStore()
        seen: list[tuple[int, int]] = []
        original_record_progress = jobs.record_progress
        jobs.record_progress = lambda url, offset, transferred: (  # type: ignore[method-assign]
            seen.append((offset, transferred)),
            original_record_progress(url, offset, transferred),
        )
        service = DownloadService(
            ScriptedTransport([FakeResponse(200, [b"abc", b"def", b"gh"])]), jobs
        )

        downloaded = service.run(self.url, self.destination)

        self.assertEqual(downloaded, 8)
        self.assertEqual(seen, [(3, 3), (6, 6), (8, 8)])
        self.assertEqual(jobs.get_record(self.url).transferred_bytes, 8)

    def test_matching_checksum_is_reported_complete(self) -> None:
        body = b"verified-payload"
        jobs = JobStore()
        jobs.set_expected_checksum(self.url, hashlib.sha256(body).hexdigest())
        service = DownloadService(ScriptedTransport([FakeResponse(200, [body])]), jobs)

        service.run(self.url, self.destination)

        self.assertEqual(jobs.get_record(self.url).state, JobState.COMPLETE)

    def test_mismatching_checksum_is_never_reported_complete(self) -> None:
        jobs = JobStore()
        jobs.set_expected_checksum(self.url, "0" * 64)
        service = DownloadService(
            ScriptedTransport([FakeResponse(200, [b"tampered-bytes"])]), jobs
        )

        with self.assertRaises(Exception):
            service.run(self.url, self.destination)

        # The corrupted file may remain on disk, but it must never be
        # reported as a completed, trustworthy download.
        self.assertNotEqual(jobs.get_record(self.url).state, JobState.COMPLETE)

    def test_corrupted_file_leaves_a_failed_state_with_a_reason(self) -> None:
        jobs = JobStore()
        jobs.set_expected_checksum(self.url, "0" * 64)
        service = DownloadService(
            ScriptedTransport([FakeResponse(200, [b"tampered-bytes"])]), jobs
        )

        with self.assertRaises(Exception):
            service.run(self.url, self.destination)

        record = jobs.get_record(self.url)
        self.assertEqual(record.state, JobState.FAILED)
        self.assertTrue(record.last_error)

    def test_exhausted_retries_leave_a_correct_failed_state(self) -> None:
        jobs = JobStore()
        service = DownloadService(
            ScriptedTransport([ConnectionError("a"), ConnectionError("b"), ConnectionError("c")]),
            jobs,
        )

        with self.assertRaises(ConnectionError):
            service.run(self.url, self.destination)

        record = jobs.get_record(self.url)
        self.assertEqual(record.state, JobState.FAILED)
        self.assertIn("ConnectionError", record.last_error or "")
        self.assertEqual(record.attempt_count, 1)

    def test_restart_recovery_resumes_from_persisted_offset_after_a_failed_run(
        self,
    ) -> None:
        jobs = JobStore()
        first_attempt = DownloadService(
            ScriptedTransport([ConnectionError("gone"), ConnectionError("gone"), ConnectionError("gone")]),
            jobs,
        )
        with self.assertRaises(ConnectionError):
            first_attempt.run(self.url, self.destination)
        self.assertEqual(jobs.get_record(self.url).state, JobState.FAILED)

        # Simulate a process restart: a brand-new `DownloadService` instance
        # over the same durable `JobStore`, with the transport now healthy.
        second_attempt = DownloadService(
            ScriptedTransport([FakeResponse(200, [b"recovered-after-restart"])]), jobs
        )
        downloaded = second_attempt.run(self.url, self.destination)

        self.assertEqual(downloaded, len(b"recovered-after-restart"))
        record = jobs.get_record(self.url)
        self.assertEqual(record.state, JobState.COMPLETE)
        self.assertEqual(record.attempt_count, 2)


if __name__ == "__main__":
    unittest.main()
