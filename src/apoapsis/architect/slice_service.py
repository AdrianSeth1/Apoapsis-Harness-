from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from apoapsis.architect.errors import SliceApprovalError, SliceExecutionNotFoundError
from apoapsis.architect.slice_package import (
    build_plan_slice_execution_package,
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
    ``PlanSliceExecutionPackage`` -- no model call, no task creation, no
    repository mutation. Safe to call more than once before approval; a
    fresh package simply replaces the prior one at ``PACKAGED``."""

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
    )
    write_plan_slice_execution_package(project_root, package)
    slice_store.record_package(
        plan_id,
        slice_id,
        plan_version=expected_plan_version,
        package_sha256=package.package_sha256,
    )
    return package


def approve_slice(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    slice_store: PlanSliceExecutionStore,
    plan_id: str,
    slice_id: str,
    *,
    expected_package_sha256: str,
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
        actor=WorkflowActor.USER,
        event_type="plan_slice_specification_approved",
        payload={
            "plan_id": plan_id,
            "slice_id": slice_id,
            "package_id": package.package_id,
            "package_sha256": package.package_sha256,
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


def project_slice_status(
    project_root: str | Path,
    plan_store: SQLitePlanStore,
    slice_store: PlanSliceExecutionStore,
    task_store: SQLiteTaskStore,
    plan_id: str,
    slice_id: str,
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
        return {
            "plan_id": plan_id,
            "slice_id": slice_id,
            "status": "ready_or_blocked",
            "record": None,
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
    return {
        "plan_id": plan_id,
        "slice_id": slice_id,
        "status": status.value,
        "record": record.model_dump(mode="json"),
        "task_state": task.state.value,
    }


def _load_latest_package(
    root: Path, plan_id: str, slice_id: str, slice_store: PlanSliceExecutionStore
) -> PlanSliceExecutionPackage:
    record = slice_store.get(plan_id, slice_id)
    if record.status != SliceExecutionStatus.PACKAGED:
        raise SliceApprovalError(
            f"slice {plan_id}/{slice_id} must be PACKAGED to approve, "
            f"found {record.status.value}"
        )
    plans_dir = root / ".apoapsis" / "plans" / plan_id
    candidates = sorted(
        plans_dir.glob(f"slice-{slice_id}-package-*.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        raise SliceApprovalError(
            f"no package artifact found for slice {plan_id}/{slice_id}"
        )
    return PlanSliceExecutionPackage.model_validate_json(
        candidates[-1].read_text(encoding="utf-8")
    )


__all__ = [
    "approve_slice",
    "package_slice",
    "project_slice_status",
    "start_slice",
]
