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


class DownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.destination = Path(self.temporary_directory.name) / "payload.bin"
        self.url = "https://example.invalid/payload.bin"

    def test_new_download_preserves_existing_behavior(self) -> None:
        transport = FakeTransport(FakeResponse(200, b"complete"))
        downloader = Downloader(transport, JobStore())

        downloaded = downloader.download(self.url, self.destination)

        self.assertEqual(downloaded, 8)
        self.assertEqual(self.destination.read_bytes(), b"complete")
        self.assertEqual(transport.headers, {})

if __name__ == "__main__":
    unittest.main()
