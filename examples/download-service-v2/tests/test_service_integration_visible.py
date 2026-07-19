from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from download_service_v2 import DownloadService, JobState, JobStore


class FakeResponse:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self.body = body

    def iter_chunks(self) -> list[bytes]:
        return [self.body]


class FakeTransport:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def get(self, url: str, *, headers: dict[str, str]):
        return self.response


class ServiceIntegrationVisibleTests(unittest.TestCase):
    """Model-visible integration acceptance checks for Slice C: run through
    the "v2-service-tests" verification command."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.destination = Path(self.temporary_directory.name) / "artifact.bin"
        self.url = "https://cdn.example.invalid/artifact.bin"

    def test_matching_checksum_completes_and_persists_job_state(self) -> None:
        body = b"hello world"
        jobs = JobStore()
        jobs.set_expected_checksum(self.url, hashlib.sha256(body).hexdigest())
        service = DownloadService(FakeTransport(FakeResponse(200, body)), jobs)

        downloaded = service.run(self.url, self.destination)

        self.assertEqual(downloaded, len(body))
        record = jobs.get_record(self.url)
        self.assertEqual(record.state, JobState.COMPLETE)
        self.assertIsNone(record.last_error)
        self.assertEqual(record.attempt_count, 1)

    def test_mismatched_checksum_fails_and_records_the_reason(self) -> None:
        jobs = JobStore()
        jobs.set_expected_checksum(self.url, "0" * 64)
        service = DownloadService(FakeTransport(FakeResponse(200, b"corrupted")), jobs)

        with self.assertRaises(Exception):
            service.run(self.url, self.destination)

        record = jobs.get_record(self.url)
        self.assertEqual(record.state, JobState.FAILED)
        self.assertIsNotNone(record.last_error)

    def test_resumes_from_persisted_offset(self) -> None:
        self.destination.write_bytes(b"partial-")
        jobs = JobStore()
        jobs.set_offset(self.url, 8)
        service = DownloadService(FakeTransport(FakeResponse(206, b"continued")), jobs)

        downloaded = service.run(self.url, self.destination)

        self.assertEqual(self.destination.read_bytes(), b"partial-continued")
        self.assertEqual(downloaded, 17)
        self.assertEqual(jobs.get_record(self.url).state, JobState.COMPLETE)


if __name__ == "__main__":
    unittest.main()
