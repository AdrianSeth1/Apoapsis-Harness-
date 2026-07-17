class JobStore:
    """Small in-memory stand-in for persisted download progress."""

    def __init__(self) -> None:
        self._offsets: dict[str, int] = {}

    def get_offset(self, url: str) -> int:
        return self._offsets.get(url, 0)

    def set_offset(self, url: str, offset: int) -> None:
        self._offsets[url] = offset

