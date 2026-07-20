from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from apoapsis.specification.schema import StrictModel


class DiscoveryOperationAction(StrEnum):
    """The three model-driven discovery steps that must never block an HTTP
    handler (ADR 0032/0033): the first two call the configured local model,
    the third calls the configured hosted frontier model."""

    LOCAL_QUESTIONS = "local_questions"
    IDEA_BRIEF = "idea_brief"
    FRONTIER_API_CALL = "frontier_api_call"


class DiscoveryOperationStatus(StrEnum):
    RECORDED = "recorded"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # A RUNNING operation whose owning process appears to have died before
    # reaching SUCCEEDED/FAILED. Terminal and inspectable, never
    # automatically repeated -- mirrors ReviewOperationStatus.AMBIGUOUS
    # (ADR 0021/0025) exactly.
    AMBIGUOUS = "ambiguous"


class DiscoveryOperationRecord(StrictModel):
    """The durable, authoritative record of one discovery model-call
    operation -- structurally mirrors ``ReviewOperationRecord``/
    ``IntakeOperationRecord``/``ExecutionOperationRecord`` exactly (same
    lease-owned, optimistically-versioned, crash-recoverable ledger
    pattern, ADR 0020/0021/0023/0024/0025)."""

    operation_id: str = Field(pattern=r"^DISCOP-[A-Za-z0-9._-]+$")
    session_id: str = Field(pattern=r"^DISC-[A-Za-z0-9._-]+$")
    action: DiscoveryOperationAction
    expected_session_version: int = Field(ge=1)
    # Only set for FRONTIER_API_CALL: the explicit per-call spend ceiling
    # authorized before this operation was recorded (ADR 0030's "shown
    # before separate authorization" discipline, applied here).
    authorized_max_spend_usd: float | None = Field(default=None, ge=0)
    package_id: str | None = None
    status: DiscoveryOperationStatus
    created_at: datetime
    updated_at: datetime
    result_summary: str | None = None
    error: str | None = None
    lease_owner_id: str | None = None
    lease_expires_at: datetime | None = None


__all__ = [
    "DiscoveryOperationAction",
    "DiscoveryOperationRecord",
    "DiscoveryOperationStatus",
]
