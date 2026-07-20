from __future__ import annotations

from datetime import datetime

from pydantic import Field

from apoapsis.discovery.errors import DiscoveryError
from apoapsis.discovery.operation_schema import DiscoveryOperationStatus
from apoapsis.discovery.operation_store import DiscoveryOperationStore
from apoapsis.specification.schema import StrictModel, utc_now


class DiscoveryRecoveryReport(StrictModel):
    """What one recovery pass actually did -- mirrors
    ``review.recovery.RecoveryReport`` (ADR 0021)."""

    reclaimed_operation_ids: list[str] = Field(default_factory=list)
    ambiguous_operation_ids: list[str] = Field(default_factory=list)


def recover_stale_discovery_operations(
    operation_store: DiscoveryOperationStore, *, now: datetime | None = None
) -> DiscoveryRecoveryReport:
    """Explicit crash recovery for the discovery-operation ledger, mirroring
    ``review.recovery.recover_stale_operations`` (ADR 0021/0025).

    Unlike review/intake/execution operations, a discovery operation never
    mutates session status until the underlying model call has already
    succeeded (``discovery.store.SQLiteDiscoveryStore`` is only ever
    written to at the end of ``propose_local_clarification_questions``/
    ``propose_idea_brief_step``/``run_frontier_planning_api_call``) -- so
    there is no separate "task stranded mid-transition" state to fix up
    here. A crashed operation simply leaves the session at whatever status
    it already had; the operator retries with a fresh operation from the
    session's current, unchanged state.
    """

    report = DiscoveryRecoveryReport()
    moment = now if now is not None else utc_now()
    for record in operation_store.list_active():
        if record.status == DiscoveryOperationStatus.RECORDED:
            report.reclaimed_operation_ids.append(record.operation_id)
            continue

        if record.lease_expires_at is not None and record.lease_expires_at >= moment:
            continue  # a healthy operation, still renewing its own lease

        try:
            operation_store.mark_ambiguous(
                record.operation_id,
                note=(
                    "this operation's lease expired without renewal; the "
                    "process running it may have crashed. Whether a model "
                    "call was transmitted before that happened is unknown "
                    "-- this operation is not automatically repeated."
                ),
                now=moment,
            )
        except DiscoveryError:
            continue  # lost the race; the owner renewed in the meantime
        report.ambiguous_operation_ids.append(record.operation_id)
    return report


__all__ = ["DiscoveryRecoveryReport", "recover_stale_discovery_operations"]
