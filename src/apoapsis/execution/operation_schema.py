from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from apoapsis.specification.schema import StrictModel


class ExecutionOperationStatus(StrEnum):
    """The durable lifecycle of one post-approval task-execution operation
    (ADR 0024). Mirrors the review/intake operation ledgers' crash-safe
    discipline: RECORDED means nothing has been transmitted yet, RUNNING
    means execution is (or was) in flight. SUCCEEDED means the operation
    itself ran to *any* deterministic conclusion -- the task may have
    reached COMPLETE, FAILED, or HUMAN_REVIEW_REQUIRED, all of which are
    legitimate, expected outcomes of ``VerticalSliceRunner``; only an
    unexpected exception (a crash before/during execution, not a normal
    task-level stop) marks the *operation* FAILED. AMBIGUOUS is reserved
    for explicit crash recovery only."""

    RECORDED = "recorded"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # A RUNNING operation whose owning process appears to have died before
    # reaching a terminal status. Terminal and inspectable, but never
    # automatically repeated: whether a provider call was transmitted
    # before the process died is genuinely unknown.
    AMBIGUOUS = "ambiguous"


class ExecutionOperationRecord(StrictModel):
    """The durable, authoritative record of one post-approval task-execution
    operation. Carries everything needed to execute it -- a worker never
    needs anything but ``operation_id`` to reload the rest."""

    operation_id: str = Field(pattern=r"^EXOP-[A-Za-z0-9._-]+$")
    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    expected_task_version: int = Field(ge=1)
    expected_repository_head: str = Field(min_length=1)
    status: ExecutionOperationStatus
    created_at: datetime
    updated_at: datetime
    result_summary: str | None = None
    error: str | None = None
    report_path: str | None = None
    lease_owner_id: str | None = None
    lease_expires_at: datetime | None = None


__all__ = ["ExecutionOperationRecord", "ExecutionOperationStatus"]
