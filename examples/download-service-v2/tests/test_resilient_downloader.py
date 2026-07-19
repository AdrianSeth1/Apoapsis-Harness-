from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from download_service_v2 import Downloader


class FakeClock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class FakeResponse:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self.body = body

    def iter_chunks(self) -> list[bytes]:
        return [self.body]


class ScriptedTransport:
    """Returns or raises each scripted outcome in order, one per `.get()`
    call -- deterministic, no real network, no real sleeping."""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, str]] = []

    def get(self, url: str, *, headers: dict[str, str]):
        self.calls.append(headers)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ResilientDownloaderTests(unittest.TestCase):
    """Development checks for Slice B: resumable, retrying, progress-
    reporting downloads."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.destination = Path(self.temporary_directory.name) / "artifact.bin"
        self.url = "https://cdn.example.invalid/artifact.bin"
        self.clock = FakeClock()

    def test_fresh_download_sends_no_range_header(self) -> None:
        transport = ScriptedTransport([FakeResponse(200, b"payload")])
        downloader = Downloader(transport, sleep=self.clock.sleep)

        downloaded = downloader.download(self.url, self.destination)

        self.assertEqual(downloaded, 7)
        self.assertEqual(transport.calls, [{}])
        self.assertEqual(self.destination.read_bytes(), b"payload")

    def test_resumed_download_sends_range_and_appends(self) -> None:
        self.destination.write_bytes(b"partial-")
        transport = ScriptedTransport([FakeResponse(206, b"continued")])
        downloader = Downloader(transport, sleep=self.clock.sleep)

        downloaded = downloader.download(self.url, self.destination, resume_offset=8)

        self.assertEqual(transport.calls, [{"Range": "bytes=8-"}])
        self.assertEqual(self.destination.read_bytes(), b"partial-continued")
        self.assertEqual(downloaded, 17)

    def test_range_ignoring_server_replaces_stale_partial_data(self) -> None:
        self.destination.write_bytes(b"outdated-chunk")
        transport = ScriptedTransport([FakeResponse(200, b"brand-new-payload")])
        downloader = Downloader(transport, sleep=self.clock.sleep)

        downloaded = downloader.download(self.url, self.destination, resume_offset=14)

        self.assertEqual(transport.calls, [{"Range": "bytes=14-"}])
        self.assertEqual(self.destination.read_bytes(), b"brand-new-payload")
        self.assertEqual(downloaded, 17)

    def test_transient_failure_retries_with_backoff_then_succeeds(self) -> None:
        transport = ScriptedTransport(
            [ConnectionError("reset"), ConnectionError("reset"), FakeResponse(200, b"ok")]
        )
        downloader = Downloader(
            transport, sleep=self.clock.sleep, max_attempts=3, backoff_base_seconds=1.0
        )

        downloaded = downloader.download(self.url, self.destination)

        self.assertEqual(downloaded, 2)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(self.clock.slept, [1.0, 2.0])

    def test_exhausting_retries_raises_the_last_error(self) -> None:
        transport = ScriptedTransport([ConnectionError("first"), ConnectionError("second")])
        downloader = Downloader(transport, sleep=self.clock.sleep, max_attempts=2)

        with self.assertRaises(ConnectionError):
            downloader.download(self.url, self.destination)
        self.assertEqual(len(transport.calls), 2)

    def test_progress_callback_reports_bytes_transferred_this_call(self) -> None:
        transport = ScriptedTransport([FakeResponse(200, b"abcdef")])
        downloader = Downloader(transport, sleep=self.clock.sleep)
        seen: list[tuple[int, int]] = []

        downloader.download(
            self.url,
            self.destination,
            on_progress=lambda offset, transferred: seen.append((offset, transferred)),
        )

        self.assertEqual(seen, [(6, 6)])


if __name__ == "__main__":
    unittest.main()
