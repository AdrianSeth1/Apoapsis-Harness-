from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from download_service import Downloader, JobStore


class FakeResponse:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self.body = body

    def iter_chunks(self) -> list[bytes]:
        return [self.body]


class FakeTransport:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.headers: dict[str, str] | None = None

    def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
        self.headers = headers
        return self.response


class VisibleResumableAcceptanceTests(unittest.TestCase):
    """Acceptance checks for resumable downloads: run through the
    "resumable-acceptance-check" verification command."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.destination = Path(self.temporary_directory.name) / "artifact.bin"
        self.url = "https://cdn.example.invalid/artifact.bin"

    def test_interrupted_download_resumes_from_persisted_offset(self) -> None:
        self.destination.write_bytes(b"partial-")
        jobs = JobStore()
        jobs.set_offset(self.url, 8)
        transport = FakeTransport(FakeResponse(206, b"continued-data"))
        downloader = Downloader(transport, jobs)

        downloaded = downloader.download(self.url, self.destination)

        self.assertEqual(transport.headers, {"Range": "bytes=8-"})
        self.assertEqual(
            self.destination.read_bytes(), b"partial-continued-data"
        )
        self.assertEqual(downloaded, 22)

    def test_range_ignoring_server_replaces_stale_partial_data(self) -> None:
        self.destination.write_bytes(b"outdated-chunk")
        jobs = JobStore()
        jobs.set_offset(self.url, 14)
        transport = FakeTransport(FakeResponse(200, b"brand-new-payload"))
        downloader = Downloader(transport, jobs)

        downloaded = downloader.download(self.url, self.destination)

        self.assertEqual(transport.headers, {"Range": "bytes=14-"})
        self.assertEqual(self.destination.read_bytes(), b"brand-new-payload")
        self.assertEqual(downloaded, 17)


if __name__ == "__main__":
    unittest.main()
