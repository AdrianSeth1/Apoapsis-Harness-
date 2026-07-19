from __future__ import annotations

from datetime import timedelta

from pydantic import Field

from apoapsis.execution.operation_schema import ExecutionOperationStatus
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.specification.schema import StrictModel, utc_now
from apoapsis.workflow.engine import SQLiteTaskStore, TaskNotFoundError
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState, transition_is_allowed

# Any of these states means the execution run already reached its own
# deterministic conclusion (or the task moved on some other way); nothing
# further needs to happen to the task itself -- only the operation record
# needs to become AMBIGUOUS.
_NO_FURTHER_ACTION_STATES = frozenset(
    {
        WorkflowState.COMPLETE,
        WorkflowState.FAILED,
        WorkflowState.HUMAN_REVIEW_REQUIRED,
        WorkflowState.ROLLED_BACK,
    }
)

DEFAULT_RUNNING_EXPIRY = timedelta(minutes=15)


class ExecutionRecoveryReport(StrictModel):
    """What one recovery pass actually did -- never speculative, only
    facts about rows this pass itself changed or found reclaimable."""

    reclaimed_operation_ids: list[str] = Field(default_factory=list)
    ambiguous_operation_ids: list[str] = Field(default_factory=list)
    tasks_returned_to_review: list[str] = Field(default_factory=list)


def recover_stale_execution_operations(
    task_store: SQLiteTaskStore,
    operation_store: ExecutionOperationStore,
    *,
    running_expiry: timedelta = DEFAULT_RUNNING_EXPIRY,
) -> ExecutionRecoveryReport:
    """Explicit crash recovery for the execution-operation ledger (ADR
    0024), structurally mirroring ``review.recovery
    .recover_stale_operations`` and ``intake.recovery
    .recover_stale_intake_operations``.

    ``RECORDED`` operations have never transmitted anything or created a
    worktree -- the very first thing ``run_execution_operation`` does is
    mark an operation ``RUNNING`` before any provider construction or
    other potentially failing setup. A ``RECORDED`` row found during a
    recovery scan is therefore always safe to reclaim.

    ``RUNNING`` operations are different: a provider call, patch
    application, or verification run may or may not have completed before
    the owning process died, so this function never touches a ``RUNNING``
    row that is still within ``running_expiry`` of its last update. Only
    once a ``RUNNING`` row has gone stale beyond ``running_expiry`` is it
    moved to the terminal, inspectable ``AMBIGUOUS`` status -- never
    automatically repeated, never silently resolved either way, and the
    task's worktree (if one was created) is never touched here -- only an
    explicit, separate ``abandon`` action ever cleans one up. If the
    task is stuck anywhere between ``SPEC_APPROVED`` and a terminal state,
    it is returned to ``HUMAN_REVIEW_REQUIRED`` through whichever
    permitted transition edge already exists from its current state (every
    intermediate state has one), with an event that makes no claim about
    whether the interrupted work succeeded or failed.
    """

    report = ExecutionRecoveryReport()
    now = utc_now()
    for record in operation_store.list_active():
        if record.status == ExecutionOperationStatus.RECORDED:
            report.reclaimed_operation_ids.append(record.operation_id)
            continue

        age = now - record.updated_at
        if age < running_expiry:
            continue

        operation_store.mark_ambiguous(
            record.operation_id,
            note=(
                f"no update for {age}; the process running this operation "
                "may have crashed. Whether execution completed before "
                "that happened is unknown -- this operation is not "
                "automatically repeated, and any worktree it created is "
                "left untouched."
            ),
        )
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
            event_type="execution_operation_recovery_requires_human",
            payload={
                "reason": (
                    f"execution operation {record.operation_id} was "
                    f"interrupted while the task was {task.state.value}; "
                    "its actual outcome is unknown and must be reviewed "
                    "manually"
                ),
                "operation_id": record.operation_id,
                "recovered_from_state": task.state.value,
            },
            expected_version=task.version,
        )
        report.tasks_returned_to_review.append(record.task_id)
    return report


__all__ = [
    "DEFAULT_RUNNING_EXPIRY",
    "ExecutionRecoveryReport",
    "recover_stale_execution_operations",
]
