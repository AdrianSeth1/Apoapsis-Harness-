from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class JobState(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class JobRecord:
    offset: int = 0
    expected_checksum: str | None = None


class JobStore:
    """In-memory stand-in for persisted download-job bookkeeping."""

    def __init__(self) -> None:
        self._records: dict[str, JobRecord] = {}

    def get_offset(self, url: str) -> int:
        return self._records.get(url, JobRecord()).offset

    def set_offset(self, url: str, offset: int) -> None:
        self._records.setdefault(url, JobRecord()).offset = offset
