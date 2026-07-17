from pathlib import Path

from .jobs import JobStore


class Downloader:
    def __init__(self, transport: object, jobs: JobStore) -> None:
        self.transport = transport
        self.jobs = jobs

    def download(self, url: str, destination: Path) -> int:
        response = self.transport.get(url, headers={})
        destination.parent.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_chunks():
                handle.write(chunk)
                downloaded += len(chunk)
                self.jobs.set_offset(url, downloaded)
        return downloaded

