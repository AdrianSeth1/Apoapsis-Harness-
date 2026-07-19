from __future__ import annotations

from pathlib import Path

from .downloader import Downloader
from .jobs import JobStore


class DownloadService:
    def __init__(self, transport: object, jobs: JobStore) -> None:
        self.jobs = jobs
        self.downloader = Downloader(transport)

    def run(self, url: str, destination: Path) -> int:
        return self.downloader.download(url, destination)
