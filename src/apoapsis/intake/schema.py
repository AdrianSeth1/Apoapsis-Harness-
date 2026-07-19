from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from apoapsis.specification.schema import StrictModel


class IntakeOperationStatus(StrEnum):
    """The durable lifecycle of one model-assisted new-task intake
    operation (ADR 0023). Mirrors the review-operation ledger's crash-safe
    discipline (ADR 0020/0021): RECORDED means nothing has been
    transmitted yet, RUNNING means the extraction call is (or was) in
    flight, and PENDING_SPECIFICATION_APPROVAL/FAILED are the two
    deterministic terminal outcomes of a completed extraction attempt --
    the task itself sits at SPEC_DRAFTED or FAILED respectively. AMBIGUOUS
    is reserved for explicit crash recovery only."""

    RECORDED = "recorded"
    RUNNING = "running"
    PENDING_SPECIFICATION_APPROVAL = "pending_specification_approval"
    FAILED = "failed"
    # A RUNNING operation whose owning process appears to have died before
    # reaching a terminal status. Terminal and inspectable, but never
    # automatically repeated: whether a model call was transmitted before
    # the process died is genuinely unknown.
    AMBIGUOUS = "ambiguous"


class IntakeOperationRecord(StrictModel):
    """The durable, authoritative record of one new-task intake operation.
    Carries everything needed to execute it -- a worker never needs
    anything but ``operation_id`` to reload the rest. ``request_text`` is
    preserved exactly, verbatim, as supplied by the caller; ``task_id`` is
    allocated once, at creation, using the same deterministic
    ``TASK-<hex>`` convention every other task-creation path uses."""

    operation_id: str = Field(pattern=r"^INOP-[A-Za-z0-9._-]+$")
    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    request_text: str = Field(min_length=1)
    request_sha256: str = Field(min_length=64, max_length=64)
    expected_task_version: int = Field(ge=1)
    provider_role: str = Field(min_length=1)
    status: IntakeOperationStatus
    created_at: datetime
    updated_at: datetime
    result_summary: str | None = None
    error: str | None = None
    audit_artifact_locations: list[str] = Field(default_factory=list)
    lease_owner_id: str | None = None
    lease_expires_at: datetime | None = None


__all__ = ["IntakeOperationRecord", "IntakeOperationStatus"]
