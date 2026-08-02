from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from apoapsis.architect.errors import (
    ActiveSliceExecutionExistsError,
    SliceApprovalError,
    SliceExecutionNotFoundError,
    SlicePackagingError,
    SliceResetError,
)
from apoapsis.architect.slice_package import (
    build_plan_slice_execution_package,
    checkpoint_completed_prior_slices,
    dependency_evidence,
    write_plan_slice_execution_package,
)
from apoapsis.architect.slice_schema import (
    PlanSliceExecutionPackage,
    PlanSliceExecutionRecord,
    SliceExecutionStatus,
)
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.config import ApoapsisConfig
from apoapsis.execution.operation_service import execute_execution_operation
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.execution.worktree import WorktreeError, WorktreeManager
from apoapsis.repository.git import GitCommandError
from apoapsis.review.case import task_slug
from apoapsis.reporting.current_state import project_current_task_evidence
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState

_TASK_STATE_TO_SLICE_STATUS: dict[WorkflowState, SliceExecutionStatus] = {
    WorkflowState.COMPLETE: SliceExecutionStatus.COMPLETE,
    WorkflowState.HUMAN_REVIEW_REQUIRED: SliceExecutionStatus.HUMAN_REVIEW,
    WorkflowState.FAILED: SliceExecutionStatus.FAILED,
    WorkflowState.ROLLED_BACK: SliceExecutionStatus.FAILED,
}


def package_slice(
    project_root: str | Path,
    plan_store: SQLitePlanStore,
    slice_store: PlanSliceExecutionStore,
    task_store: SQLiteTaskStore,
    operation_store: ExecutionOperationStore,
    plan_id: str,
    slice_id: str,
    *,
    expected_plan_version: int,
    config: ApoapsisConfig,
):
    """Deterministically builds and durably records an immutable
    ``PlanSliceExecutionPackage`` -- no model call and no task creation.
    Completed earlier slices are first checkpointed on their isolated task
    branches so the package can authorize an exact inherited base commit;
    the user's checked-out branch is never moved or merged. Safe to call
    more than once before approval."""

    plan_record = plan_store.get_plan(plan_id)
    if plan_record.version != expected_plan_version:
        raise SlicePackagingError(
            f"expected plan version {expected_plan_version}, found "
            f"{plan_record.version}"
        )
    if plan_record.status.value != "approved":
        raise SlicePackagingError(
            f"plan {plan_id} must be APPROVED to package a slice, found "
            f"{plan_record.status.value}"
        )
    # Run every read-only provenance, plan, configuration, repository, and
    # dependency gate before checkpointing any completed task branch.
    build_plan_slice_execution_package(
        project_root,
        plan_store,
        slice_store,
        task_store,
        operation_store,
        plan_id,
        slice_id,
        expected_plan_version=expected_plan_version,
        config=config,
    )
    execution_base_commit, inherited_slice_ids = checkpoint_completed_prior_slices(
        project_root,
        plan_id,
        plan_record.plan,
        slice_id,
        task_store,
        slice_store,
    )

    package = build_plan_slice_execution_package(
        project_root,
        plan_store,
        slice_store,
        task_store,
        operation_store,
        plan_id,
        slice_id,
        expected_plan_version=expected_plan_version,
        config=config,
        execution_base_commit=execution_base_commit,
        inherited_slice_ids=inherited_slice_ids,
    )
    write_plan_slice_execution_package(project_root, package)
    slice_store.record_package(
        plan_id,
        slice_id,
        plan_version=expected_plan_version,
        package_sha256=package.package_sha256,
    )
    return package


def _other_slice_is_genuinely_active(
    task_store: SQLiteTaskStore,
    slice_store: PlanSliceExecutionStore,
    plan_id: str,
    slice_id: str,
) -> PlanSliceExecutionRecord | None:
    """Finds another slice of this plan that is genuinely still executing
    right now -- read live from its derived task's real workflow state,
    never from this store's own persisted ``status`` column.

    ``PlanSliceExecutionStore`` deliberately never writes anything past
    ``APPROVED`` for a slice (RUNNING/COMPLETE/HUMAN_REVIEW/FAILED are
    always a live projection, never a second copy of the truth -- see its
    own module docstring). A naive "no other row is APPROVED" check is
    therefore not merely stale but permanently wrong: once any slice of a
    plan is ever approved, its row stays ``APPROVED`` forever, which would
    block every other slice of that plan from ever being approved again,
    even long after the first slice's task genuinely finished. The
    "at most one slice per plan active at a time" invariant (ADR 0027) is
    about concurrency, not history, so it must be checked against live
    task state."""

    for record in slice_store.list_for_plan(plan_id):
        if record.slice_id == slice_id:
            continue
        if record.status != SliceExecutionStatus.APPROVED or record.task_id is None:
            continue
        task = task_store.get_task(record.task_id)
        if task.state not in _TASK_STATE_TO_SLICE_STATUS:
            return record
    return None


def approve_slice(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    slice_store: PlanSliceExecutionStore,
    plan_id: str,
    slice_id: str,
    *,
    expected_package_sha256: str,
    approval_actor: WorkflowActor = WorkflowActor.USER,
    approval_event_type: str = "plan_slice_specification_approved",
    approval_context: dict[str, Any] | None = None,
) -> PlanSliceExecutionRecord:
    """Explicit human approval of exactly the package that was previewed:
    converts its ``derived_specification`` into a real task (the normal
    ``INTAKE -> SPEC_DRAFTED -> SPEC_APPROVED`` transitions, unchanged --
    no new workflow edge added), then records the slice as ``APPROVED``.
    Never starts execution -- that is the separate ``start_slice`` action,
    invoking the existing, unmodified D2 durable execution service."""

    root = Path(project_root).resolve()
    package = _load_latest_package(root, plan_id, slice_id, slice_store)
    if package.package_sha256 != expected_package_sha256:
        raise SliceApprovalError(
            f"slice {plan_id}/{slice_id}'s package no longer matches the "
            "expected hash; re-inspect before approving"
        )
    active = _other_slice_is_genuinely_active(task_store, slice_store, plan_id, slice_id)
    if active is not None:
        raise ActiveSliceExecutionExistsError(
            f"plan {plan_id} already has an active slice execution "
            f"({active.slice_id}); wait for it to finish or resolve it "
            "before approving another"
        )
    specification = package.derived_specification
    created = task_store.create_task(specification)
    drafted = task_store.transition(
        specification.task_id,
        WorkflowState.SPEC_DRAFTED,
        actor=WorkflowActor.SYSTEM,
        event_type="plan_slice_specification_drafted",
        payload={"plan_id": plan_id, "slice_id": slice_id},
        expected_version=created.version,
    )
    approved = task_store.transition(
        specification.task_id,
        WorkflowState.SPEC_APPROVED,
        actor=approval_actor,
        event_type=approval_event_type,
        payload={
            "plan_id": plan_id,
            "slice_id": slice_id,
            "package_id": package.package_id,
            "package_sha256": package.package_sha256,
            "execution_base_commit": (
                package.execution_base_commit or package.repository_head_commit
            ),
            "inherited_slice_ids": package.inherited_slice_ids,
            **(approval_context or {}),
        },
        expected_version=drafted.version,
    )
    return slice_store.approve(
        plan_id,
        slice_id,
        expected_package_sha256=expected_package_sha256,
        task_id=specification.task_id,
        task_expected_version=approved.version,
    )


def start_slice(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    slice_store: PlanSliceExecutionStore,
    operation_store: ExecutionOperationStore,
    plan_id: str,
    slice_id: str,
    config: ApoapsisConfig,
    *,
    operation_id: str | None = None,
):
    """Starts the approved slice's derived task through the existing,
    unmodified D2 durable execution service -- this function contains no
    routing, context, worktree, agent, patch, or verification logic of its
    own; it only looks up the derived task and hands off."""

    record = slice_store.get(plan_id, slice_id)
    if record.status != SliceExecutionStatus.APPROVED:
        raise SliceApprovalError(
            f"slice {plan_id}/{slice_id} must be APPROVED to start, found "
            f"{record.status.value}"
        )
    assert record.task_id is not None and record.task_expected_version is not None
    resolved_operation_id = operation_id or f"EXOP-{uuid.uuid4().hex[:24].upper()}"
    result = execute_execution_operation(
        project_root,
        task_store,
        operation_store,
        config,
        task_id=record.task_id,
        operation_id=resolved_operation_id,
        expected_version=record.task_expected_version,
    )
    slice_store.record_execution_operation(
        plan_id, slice_id, execution_operation_id=resolved_operation_id
    )
    return result


#: Task states ``reset_slice`` will discard without an explicit override.
#: ``ROLLED_BACK`` and ``FAILED`` are the two terminal states that leave
#: nothing a later slice can inherit. ``HUMAN_REVIEW_REQUIRED`` is
#: deliberately absent even though it is where a stopped slice usually
#: sits: its worktree and branch are typically still on disk, and deleting
#: the task that names them would orphan both. Run ``apoapsis rollback``
#: first -- that is what removes them -- and the task lands in
#: ``ROLLED_BACK``, which this list accepts.
_RESETTABLE_TASK_STATES: tuple[WorkflowState, ...] = (
    WorkflowState.ROLLED_BACK,
    WorkflowState.FAILED,
)


def reset_slice(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    slice_store: PlanSliceExecutionStore,
    plan_id: str,
    slice_id: str,
    *,
    allow_completed: bool = False,
) -> dict[str, Any]:
    """Clears one slice's execution ledger so it can be packaged, approved,
    and run again from scratch -- no model call, nothing executed, and no
    file in the repository touched.

    Re-running a slice is not otherwise reachable. ``record_package``
    refuses to re-package anything past ``PACKAGED``, and a derived task id
    is a deterministic function of ``(plan, slice)``, so ``create_task``
    refuses a second approval with 'task already exists'. Together those
    two guards -- each correct on its own -- mean a slice's first attempt
    is also its only attempt, which is wrong for a harness whose whole
    purpose is measuring what a coding model does on a given slice.

    What this removes is the *ledger*, not the evidence. The slice's
    immutable package artifact stays in ``.apoapsis/plans/``, and the prior
    task's audit directory stays in ``.apoapsis/tasks/``; both are the
    record of what was authorized and what happened, and neither is a claim
    about what is authorized *now*. What it does remove is the pair of
    rows that assert an authorization the operator has explicitly retired.

    Refuses when the derived task is still live, and refuses a ``COMPLETE``
    task unless ``allow_completed`` is set: a completed slice's branch is
    what later slices inherit as their execution base, so discarding it
    silently would change what a subsequent slice is built on top of."""

    record = slice_store.get(plan_id, slice_id)
    deleted_task: dict[str, Any] | None = None

    if record.task_id is not None:
        task = task_store.get_task(record.task_id)
        if task.state == WorkflowState.COMPLETE and not allow_completed:
            raise SliceResetError(
                f"slice {plan_id}/{slice_id}'s task {record.task_id} is "
                "COMPLETE; later slices may already inherit its branch as "
                "their execution base. Re-run it only with an explicit "
                "override (--allow-completed)"
            )
        allowed = _RESETTABLE_TASK_STATES + (
            (WorkflowState.COMPLETE,) if allow_completed else ()
        )
        if task.state not in allowed:
            raise SliceResetError(
                f"slice {plan_id}/{slice_id}'s task {record.task_id} is "
                f"{task.state.value}, which is not a finished state this "
                "can safely discard. If it stopped and you want to abandon "
                f"it, run `apoapsis rollback {record.task_id}` first -- that "
                "removes its worktree and leaves it ROLLED_BACK"
            )
        removed = task_store.delete_task(record.task_id, allowed_states=allowed)
        deleted_task = {
            "task_id": removed.task_id,
            "state_before_reset": removed.state.value,
            "version_before_reset": removed.version,
        }

    discarded = slice_store.discard(plan_id, slice_id)
    return {
        "plan_id": plan_id,
        "slice_id": slice_id,
        "status": "reset",
        "discarded_record": discarded.model_dump(mode="json"),
        "deleted_task": deleted_task,
        "retained_artifacts": [
            f".apoapsis/plans/{plan_id}/slice-{slice_id}-package-*.json",
            *(
                [f".apoapsis/tasks/{deleted_task['task_id']}/"]
                if deleted_task is not None
                else []
            ),
        ],
        "next_action": (
            f"apoapsis plan slice package {plan_id} {slice_id} "
            f"--expected-plan-version <current plan version>"
        ),
    }


#: Terminal task states ``retry_slice`` will abandon on the operator's behalf
#: before clearing the ledger. A task here is finished; what remains is its
#: worktree, which retry removes exactly as `apoapsis rollback` would.
_ABANDONABLE_TASK_STATES: tuple[WorkflowState, ...] = (
    WorkflowState.HUMAN_REVIEW_REQUIRED,
    WorkflowState.FAILED,
)


def _discard_task_branch(root: Path, task_id: str) -> None:
    """Remove a derived task's worktree and branch, in whatever state the
    pair has been left in.

    Three states are reachable and all of them have to end with no
    worktree and no branch, because `WorktreeManager.create` refuses both:

    * both present -- an attempt that stopped without being abandoned;
    * neither present -- `apoapsis rollback --delete-branch`;
    * worktree gone, branch still there -- the review screen's abandon
      action, which does not delete the branch. `cleanup` cannot fix this
      one: it describes the worktree before doing anything, and that
      raises once the directory is gone, so the branch outlives every
      cleanup path that exists.

    Never raises. A retry that cannot tidy up should still clear the
    ledger; failing here would strand the slice in exactly the state this
    function exists to escape.
    """

    slug = task_slug(task_id)
    manager = WorktreeManager(root)
    try:
        manager.cleanup(slug, force=True, delete_branch=True)
        return
    except WorktreeError:
        pass

    branch = f"apoapsis/{slug.lower()}"
    try:
        manager.repository.run(["worktree", "prune"], check=False)
        exists = manager.repository.run(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        )
        if exists.returncode == 0:
            manager.repository.run(["branch", "-D", branch], check=False)
    except GitCommandError:
        pass


def retry_slice(
    project_root: str | Path,
    plan_store: SQLitePlanStore,
    slice_store: PlanSliceExecutionStore,
    task_store: SQLiteTaskStore,
    operation_store: ExecutionOperationStore,
    plan_id: str,
    slice_id: str,
    config: ApoapsisConfig,
    *,
    allow_completed: bool = False,
) -> dict[str, Any]:
    """One action for "this attempt is over, give the slice another one":
    abandon the finished task if it has not been abandoned already, clear
    the ledger, recompile the package, and approve it. Starting is still
    separate and still explicit -- this leaves the slice exactly where a
    freshly approved slice sits, and calls no model.

    The steps are not new; each is the existing, individually-guarded
    operation, in the only order that works. What is new is that the
    operator no longer has to know that order. Rolling back and resetting
    are genuinely different things -- one abandons a task, the other
    retires the authorization that named it -- and doing the first alone
    leaves a slice that looks recoverable and is not: `record_package`
    still refuses a slice past PACKAGED, and the derived task id is a
    function of (plan, slice), so re-approval still hits "task already
    exists". Nothing in the review screen said so, and the natural guess
    from an "Abandon & roll back" button is that abandoning is enough.

    Accepts a task that is already ROLLED_BACK, which is the state that
    button leaves behind, and treats it as the easy case: there is no
    worktree left to remove, so retry is a reset and a re-approval."""

    root = Path(project_root).resolve()
    plan_record = plan_store.get_plan(plan_id)
    abandoned = False

    try:
        record = slice_store.get(plan_id, slice_id)
    except SliceExecutionNotFoundError:
        record = None

    if record is not None and record.task_id is not None:
        task = task_store.get_task(record.task_id)
        if task.state in _ABANDONABLE_TASK_STATES:
            # Version-checked transition first, destructive cleanup second,
            # matching `review.execution._execute_abandon`: a stale caller
            # must fail its version check having deleted nothing.
            task_store.transition(
                record.task_id,
                WorkflowState.ROLLED_BACK,
                actor=WorkflowActor.USER,
                event_type="plan_slice_retry_abandoned",
                payload={
                    "reason": "operator retried this slice from its plan page",
                    "plan_id": plan_id,
                    "slice_id": slice_id,
                    "abandoned_state": task.state.value,
                },
                expected_version=task.version,
            )
            abandoned = True

    if record is not None and record.task_id is not None:
        # Unconditional, not gated on whether *this* call did the
        # abandoning. `review.execution._execute_abandon` -- the UI's
        # "Abandon & roll back" -- removes the worktree with
        # delete_branch=False, so a task arriving here already ROLLED_BACK
        # has no worktree left but still owns its branch. `cleanup` cannot
        # reach that branch, because it calls `describe` first and that
        # raises once the directory is gone. Left alone, the next start
        # fails at worktree creation with "branch already exists" -- after
        # a retry that reported success.
        _discard_task_branch(root, record.task_id)

    reset = reset_slice(
        root,
        task_store,
        slice_store,
        plan_id,
        slice_id,
        allow_completed=allow_completed,
    ) if record is not None else None

    package = package_slice(
        root,
        plan_store,
        slice_store,
        task_store,
        operation_store,
        plan_id,
        slice_id,
        expected_plan_version=plan_record.version,
        config=config,
    )
    approved = approve_slice(
        root,
        task_store,
        slice_store,
        plan_id,
        slice_id,
        expected_package_sha256=package.package_sha256,
        approval_event_type="plan_slice_specification_approved",
        approval_context={"retry_of_previous_attempt": True},
    )
    return {
        "plan_id": plan_id,
        "slice_id": slice_id,
        "status": "ready_to_start",
        "abandoned_previous_task": abandoned,
        "reset": reset,
        "package_id": package.package_id,
        "package_sha256": package.package_sha256,
        "record": approved.model_dump(mode="json"),
        "next_action": f"apoapsis plan slice start {plan_id} {slice_id}",
    }


def project_slice_status(
    project_root: str | Path,
    plan_store: SQLitePlanStore,
    slice_store: PlanSliceExecutionStore,
    task_store: SQLiteTaskStore,
    plan_id: str,
    slice_id: str,
    *,
    operation_store: ExecutionOperationStore | None = None,
) -> dict[str, Any]:
    """Read-only status projection for one slice, computed entirely from
    persisted facts: the plan's own current version, this slice's own
    execution record (if any), dependency evidence (if not yet packaged),
    and -- once a derived task exists -- that task's own real, current
    workflow state. Never a second, independently-drifting copy of the
    task's status."""

    plan_record = plan_store.get_plan(plan_id)
    try:
        record = slice_store.get(plan_id, slice_id)
    except SliceExecutionNotFoundError:
        readiness = None
        slice_obj = next(
            (item for item in plan_record.plan.slices if item.slice_id == slice_id),
            None,
        )
        if slice_obj is not None and operation_store is not None:
            evidence = dependency_evidence(
                project_root,
                task_store,
                slice_store,
                operation_store,
                plan_id,
                slice_obj,
            )
            readiness = {
                "ready": all(item.satisfied for item in evidence),
                "dependency_evidence": [
                    item.model_dump(mode="json") for item in evidence
                ],
            }
        return {
            "plan_id": plan_id,
            "slice_id": slice_id,
            "status": "ready_or_blocked",
            "record": None,
            "readiness": readiness,
        }

    if record.status == SliceExecutionStatus.PACKAGED:
        if record.plan_version != plan_record.version:
            return {
                "plan_id": plan_id,
                "slice_id": slice_id,
                "status": SliceExecutionStatus.SUPERSEDED.value,
                "record": record.model_dump(mode="json"),
            }
        return {
            "plan_id": plan_id,
            "slice_id": slice_id,
            "status": SliceExecutionStatus.PACKAGED.value,
            "record": record.model_dump(mode="json"),
        }

    assert record.task_id is not None
    task = task_store.get_task(record.task_id)
    if task.state == WorkflowState.SPEC_APPROVED:
        status = SliceExecutionStatus.APPROVED
    else:
        status = _TASK_STATE_TO_SLICE_STATUS.get(task.state, SliceExecutionStatus.RUNNING)
    # `status`/`task_state` above were already computed from live workflow
    # state, never from `report.json`. `current_evidence` adds the matching
    # projection of *why* -- outcome, deciding stage, evidence integrity --
    # so the plan surface and the delivery record cannot disagree about a
    # slice that was repaired after its first stop (ADR 0072).
    evidence = project_current_task_evidence(
        project_root, task_store, record.task_id, record=task
    )
    return {
        "plan_id": plan_id,
        "slice_id": slice_id,
        "status": status.value,
        "record": record.model_dump(mode="json"),
        "task_state": task.state.value,
        "current_evidence": evidence.model_dump(mode="json"),
    }


def read_latest_slice_package(
    project_root: str | Path, plan_id: str, slice_id: str
) -> PlanSliceExecutionPackage | None:
    """Read-only: the most recently written package artifact for this
    slice, if any -- regardless of the slice's current status, since the
    artifact remains on disk permanently once written (an immutable audit
    record, never cleaned up). Returns ``None`` rather than raising if
    nothing has ever been packaged; callers that require one existing
    (approval) use ``_load_latest_package`` instead, which raises."""

    root = Path(project_root).resolve()
    plans_dir = root / ".apoapsis" / "plans" / plan_id
    candidates = sorted(
        plans_dir.glob(f"slice-{slice_id}-package-*.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        return None
    return PlanSliceExecutionPackage.model_validate_json(
        candidates[-1].read_text(encoding="utf-8")
    )


def _load_latest_package(
    root: Path, plan_id: str, slice_id: str, slice_store: PlanSliceExecutionStore
) -> PlanSliceExecutionPackage:
    record = slice_store.get(plan_id, slice_id)
    if record.status != SliceExecutionStatus.PACKAGED:
        raise SliceApprovalError(
            f"slice {plan_id}/{slice_id} must be PACKAGED to approve, "
            f"found {record.status.value}"
        )
    package = read_latest_slice_package(root, plan_id, slice_id)
    if package is None:
        raise SliceApprovalError(
            f"no package artifact found for slice {plan_id}/{slice_id}"
        )
    return package


__all__ = [
    "approve_slice",
    "package_slice",
    "project_slice_status",
    "read_latest_slice_package",
    "reset_slice",
    "retry_slice",
    "start_slice",
]
