from __future__ import annotations

from pathlib import Path


class Downloader:
    def __init__(self, transport: object) -> None:
        self.transport = transport

    def download(self, url: str, destination: Path) -> int:
        response = self.transport.get(url, headers={})
        destination.parent.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_chunks():
                handle.write(chunk)
                downloaded += len(chunk)
        return downloaded
