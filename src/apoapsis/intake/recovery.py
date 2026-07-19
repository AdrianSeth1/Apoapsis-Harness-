from __future__ import annotations

from datetime import timedelta

from pydantic import Field

from apoapsis.intake.schema import IntakeOperationStatus
from apoapsis.intake.store import IntakeOperationStore
from apoapsis.specification.schema import StrictModel, utc_now
from apoapsis.workflow.engine import SQLiteTaskStore, TaskNotFoundError
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState, transition_is_allowed

# Any state other than INTAKE means the extraction call already produced
# its real, deterministic outcome (SPEC_DRAFTED, FAILED) or the task moved
# on some other way; nothing further needs to happen to the task itself --
# only the operation record needs to become AMBIGUOUS.
_NO_FURTHER_ACTION_STATES = frozenset(
    {
        WorkflowState.SPEC_DRAFTED,
        WorkflowState.HUMAN_REVIEW_REQUIRED,
        WorkflowState.FAILED,
        WorkflowState.COMPLETE,
        WorkflowState.ROLLED_BACK,
    }
)

DEFAULT_RUNNING_EXPIRY = timedelta(minutes=15)


class IntakeRecoveryReport(StrictModel):
    """What one recovery pass actually did -- never speculative, only
    facts about rows this pass itself changed or found reclaimable."""

    reclaimed_operation_ids: list[str] = Field(default_factory=list)
    ambiguous_operation_ids: list[str] = Field(default_factory=list)
    tasks_returned_to_review: list[str] = Field(default_factory=list)


def recover_stale_intake_operations(
    task_store: SQLiteTaskStore,
    operation_store: IntakeOperationStore,
    *,
    running_expiry: timedelta = DEFAULT_RUNNING_EXPIRY,
) -> IntakeRecoveryReport:
    """Explicit crash recovery for the intake-operation ledger (ADR 0023),
    structurally mirroring ``review.recovery.recover_stale_operations``.

    ``RECORDED`` operations have never transmitted anything -- the very
    first thing ``run_intake_operation`` does is mark an operation
    ``RUNNING`` before any provider construction or other potentially
    failing setup. A ``RECORDED`` row found during a recovery scan is
    therefore always safe to reclaim: this function reports it, and the
    caller (``IntakeWorker`` at startup, or the CLI's ``intake recover``
    command) re-submits it for real execution.

    ``RUNNING`` operations are different: an extraction call may or may not
    have been transmitted before the owning process died, so this function
    never touches a ``RUNNING`` row that is still within ``running_expiry``
    of its last update. Only once a ``RUNNING`` row has gone stale beyond
    ``running_expiry`` is it moved to the terminal, inspectable
    ``AMBIGUOUS`` status -- never automatically repeated, never silently
    resolved either way. If the operation's task is still stuck at
    ``INTAKE`` (the crash happened before the task ever moved anywhere),
    it is returned to ``HUMAN_REVIEW_REQUIRED`` through the existing
    permitted transition edge, with an event that makes no claim about
    whether the interrupted extraction call succeeded or failed -- an
    operator can then inspect and abandon it through the existing,
    unmodified review machinery. A task that already reached
    ``SPEC_DRAFTED``/``FAILED`` before the crash (only the operation's own
    bookkeeping call never completed) is left exactly where it is; no
    outcome is inferred or retroactively granted.
    """

    report = IntakeRecoveryReport()
    now = utc_now()
    for record in operation_store.list_active():
        if record.status == IntakeOperationStatus.RECORDED:
            report.reclaimed_operation_ids.append(record.operation_id)
            continue

        age = now - record.updated_at
        if age < running_expiry:
            continue

        operation_store.mark_ambiguous(
            record.operation_id,
            note=(
                f"no update for {age}; the process running this operation "
                "may have crashed. Whether a model call was transmitted "
                "before that happened is unknown -- this operation is not "
                "automatically repeated."
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
            event_type="intake_operation_recovery_requires_human",
            payload={
                "reason": (
                    f"intake operation {record.operation_id} was interrupted "
                    f"while the task was {task.state.value}; its actual "
                    "outcome is unknown and must be reviewed manually"
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
    "IntakeRecoveryReport",
    "recover_stale_intake_operations",
]
