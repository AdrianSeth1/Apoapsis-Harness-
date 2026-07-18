from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel, utc_now


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    ERROR = "error"
    SKIPPED = "skipped"


class VerificationCommandResult(StrictModel):
    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    required: bool = True
    cwd: str
    status: VerificationStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime = Field(default_factory=utc_now)
    duration_seconds: float = Field(ge=0)


class VerificationResult(StrictModel):
    schema_version: str = "1.0"
    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    status: VerificationStatus
    commands: list[VerificationCommandResult] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime
    duration_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_aggregate_status(self) -> VerificationResult:
        required = [item for item in self.commands if item.required]
        if self.status == VerificationStatus.PASSED and any(
            item.status != VerificationStatus.PASSED for item in required
        ):
            raise ValueError("aggregate cannot pass when a required check failed")
        return self

