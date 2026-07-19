from __future__ import annotations

from datetime import datetime

from pydantic import Field

from apoapsis.review.errors import ReviewError
from apoapsis.review.schema import ReviewOperationStatus
from apoapsis.review.store import ReviewOperationStore
from apoapsis.specification.schema import StrictModel, utc_now
from apoapsis.workflow.engine import SQLiteTaskStore, TaskNotFoundError
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState, transition_is_allowed

_NO_FURTHER_ACTION_STATES = frozenset(
    {
        WorkflowState.COMPLETE,
        WorkflowState.FAILED,
        WorkflowState.HUMAN_REVIEW_REQUIRED,
        WorkflowState.ROLLED_BACK,
    }
)


class RecoveryReport(StrictModel):
    """What one recovery pass actually did -- never speculative, only
    facts about rows this pass itself changed or found reclaimable."""

    reclaimed_operation_ids: list[str] = Field(default_factory=list)
    ambiguous_operation_ids: list[str] = Field(default_factory=list)
    tasks_returned_to_review: list[str] = Field(default_factory=list)


def recover_stale_operations(
    task_store: SQLiteTaskStore,
    operation_store: ReviewOperationStore,
    *,
    now: datetime | None = None,
) -> RecoveryReport:
    """Explicit crash recovery for the review-operation ledger (ADR 0021,
    lease discipline hardened by ADR 0025).

    ``RECORDED`` operations have never transmitted anything -- the very
    first thing ``run_review_operation`` does is mark an operation
    ``RUNNING`` before any provider construction or other potentially
    failing setup. A ``RECORDED`` row found during a recovery scan is
    therefore always safe to reclaim: this function reports it, and the
    caller (``ReviewWorker`` at startup, or the CLI's ``review recover``
    command) re-submits it for real execution.

    ``RUNNING`` operations are different: a provider call may or may not
    have been transmitted before the owning process died, so this function
    never touches a ``RUNNING`` row whose lease has not actually expired --
    a healthy continuation still being renewed by its own heartbeat is
    left alone regardless of how long it has been running, even across
    many former 15-minute windows. Only once a lease has genuinely expired
    (checked atomically, never by reading a timestamp and guessing) is it
    moved to the terminal, inspectable ``AMBIGUOUS`` status -- never
    automatically repeated, never silently resolved either way. If the
    operation's task is stuck outside ``HUMAN_REVIEW_REQUIRED`` and outside
    any terminal workflow state, it is returned to
    ``HUMAN_REVIEW_REQUIRED`` through an existing permitted transition
    edge, with an event that makes no claim about whether the interrupted
    model call succeeded or failed.
    """

    report = RecoveryReport()
    moment = now if now is not None else utc_now()
    for record in operation_store.list_active():
        if record.status == ReviewOperationStatus.RECORDED:
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
        except ReviewError:
            continue  # lost the race; the owner renewed in the meantime
        report.ambiguous_operation_ids.append(record.operation_id)

        try:
            task = task_store.get_task(record.task_id)
        except TaskNotFoundError:
            continue
        if task.state in _NO_FURTHER_ACTION_STATES:
            continue
        if not transition_is_allowed(task.state, WorkflowState.HUMAN_REVIEW_REQUIRED):
            continue
        task_store.transition(
            record.task_id,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            actor=WorkflowActor.SYSTEM,
            event_type="review_operation_recovery_requires_human",
            payload={
                "reason": (
                    f"operation {record.operation_id} was interrupted while "
                    f"the task was {task.state.value}; its actual outcome "
                    "is unknown and must be reviewed manually"
                ),
                "operation_id": record.operation_id,
                "recovered_from_state": task.state.value,
            },
            expected_version=task.version,
        )
        report.tasks_returned_to_review.append(record.task_id)
    return report


__all__ = ["RecoveryReport", "recover_stale_operations"]
