from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from apoapsis.architect.errors import ArchitectError
from apoapsis.architect.slice_service import approve_slice, package_slice, start_slice
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import ApoapsisConfig
from apoapsis.evaluation.oracle import (
    HeldOutOracleDefinition,
    assert_oracle_withheld,
    run_held_out_oracle_against_worktree,
)
from apoapsis.evaluation.planning_schemas import (
    MonolithicConditionResult,
    PlannedConditionResult,
    PlannerProvenance,
    SliceAttemptResult,
)
from apoapsis.evaluation.schemas import EvalEvidenceKind, OracleStatus
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.execution.worktree import WorktreeManager
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.reporting.report import FinalTaskReport, TaskOutcome
from apoapsis.repository.git import GitRepository
from apoapsis.research.schemas import ResearchMode
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.states import WorkflowState
from apoapsis.workflow.vertical_slice import VerticalSliceRunner


class PlanningEvaluationError(ArchitectError):
    """Raised for a planning-comparison-framework-level problem (a
    dependency cycle in an approved plan, for example) -- never for a
    slice's own task-level outcome, which is always recorded as data, not
    raised."""


def _patch_attempts(fixture_root: Path, task_id: str) -> tuple[int, int]:
    """Counts real patch attempts and policy-rejected ones for one task,
    the same way the existing single-shot evaluation harness does (ADR
    0012) -- by reading the immutable per-attempt audit artifacts, never by
    asking a model."""

    audit_root = fixture_root / ".apoapsis" / "tasks" / task_id
    patch_files = list(audit_root.glob("patch-[0-9][0-9][0-9].diff"))
    rejections = 0
    for policy_path in audit_root.glob("patch-[0-9][0-9][0-9]-policy.json"):
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if policy.get("accepted") is False:
            rejections += 1
    return len(patch_files), rejections


def _read_report(fixture_root: Path, task_id: str) -> FinalTaskReport | None:
    report_path = fixture_root / ".apoapsis" / "tasks" / task_id / "report.json"
    if not report_path.is_file():
        return None
    return FinalTaskReport.model_validate_json(report_path.read_text(encoding="utf-8"))


def run_monolithic_condition(
    fixture_root: str | Path,
    config: ApoapsisConfig,
    provider: InstrumentedModelProvider,
    *,
    local_coder_provider: InstrumentedModelProvider | None = None,
    frontier_coder_provider: InstrumentedModelProvider | None = None,
    task_text: str,
    scenario_id: str,
    scenario_version: str,
    evidence_kind: EvalEvidenceKind = EvalEvidenceKind.DETERMINISTIC_FAKE,
    held_out_oracle: HeldOutOracleDefinition | None = None,
) -> MonolithicConditionResult:
    """The same task, attempted as a single request.

    Deliberately does not reuse `evaluation.harness.run_eval_lane`: every
    existing evaluation lane forces `CompletionPolicy.BASELINE` (ADR 0012)
    so historical false-success measurement stays comparable across runs.
    D4's scenario (ADR 0028) is deliberately per-criterion-scoped and needs
    `STRICT` for *both* conditions so a slice's isolated worktree is never
    blocked on an unrelated command it was never asked to satisfy -- an
    explicit, documented deviation, not a silent one. `config` is used
    exactly as given, with no lane overlay of any kind.
    """

    fixture_root = Path(fixture_root)
    if held_out_oracle is not None:
        assert_oracle_withheld(fixture_root, held_out_oracle)
    metadata = fixture_root / ".apoapsis"
    metadata.mkdir(parents=True, exist_ok=True)
    store = SQLiteTaskStore(metadata / "apoapsis.db")
    started = time.monotonic()
    report = VerticalSliceRunner(
        fixture_root,
        store,
        provider,
        config,
        local_coder_provider=local_coder_provider,
        frontier_coder_provider=frontier_coder_provider,
        research_mode=ResearchMode.OFF,
    ).run(task_text, approve=lambda specification: True)
    patch_attempts, unsafe_rejections = _patch_attempts(fixture_root, report.task_id)
    oracle_result = None
    if held_out_oracle is not None:
        from apoapsis.evaluation.oracle import run_held_out_oracle

        oracle_result = run_held_out_oracle(report, config, held_out_oracle)
        oracle_result = oracle_result.model_copy(
            update={
                "audit_artifact": f".apoapsis/tasks/{report.task_id}/held-out-oracle.json"
            }
        )
        TaskAuditStore(fixture_root, report.task_id).write_json(
            "held-out-oracle.json", oracle_result, kind="held_out_oracle_result"
        )
    return MonolithicConditionResult(
        scenario_id=scenario_id,
        scenario_version=scenario_version,
        report=report,
        patch_attempts=patch_attempts,
        unsafe_patch_rejections=unsafe_rejections,
        duration_seconds=time.monotonic() - started,
        held_out_oracle=oracle_result,
        evidence_kind=evidence_kind,
    )


def _topological_slice_order(plan) -> list[str]:
    by_id = {item.slice_id: item for item in plan.slices}
    order: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(slice_id: str) -> None:
        if slice_id in visited:
            return
        if slice_id in visiting:
            raise PlanningEvaluationError(
                f"dependency cycle detected at slice {slice_id!r}"
            )
        visiting.add(slice_id)
        for dependency_id in by_id[slice_id].dependencies:
            visit(dependency_id)
        visiting.discard(slice_id)
        visited.add(slice_id)
        order.append(slice_id)

    for slice_id in by_id:
        visit(slice_id)
    return order


def _commit_and_merge_slice(project_root: Path, task_id: str) -> None:
    """Mirrors exactly what a human is required to do before a dependent
    slice can be packaged (ADR 0027): commit the completed worktree's
    changes, then merge its branch into the shared base. Apoapsis itself
    never does this for a live product user -- this evaluation-only driver
    performs it deterministically, purely so a fixed comparison run can
    advance without a human clicking through each slice by hand."""

    slug = task_id.removeprefix("TASK-").lower()
    managed = WorktreeManager(project_root).describe(slug)
    worktree_repository = GitRepository(managed.path)
    if worktree_repository.run(["status", "--porcelain"]).stdout.strip():
        worktree_repository.run(["add", "-A"])
        worktree_repository.run(
            ["commit", "-m", f"evaluation: {task_id} slice work"]
        )
    GitRepository(project_root).run(["merge", "--ff-only", managed.branch])


def run_planned_condition(
    project_root: str | Path,
    plan_store: SQLitePlanStore,
    slice_store: PlanSliceExecutionStore,
    task_store: SQLiteTaskStore,
    operation_store: ExecutionOperationStore,
    plan_id: str,
    *,
    expected_plan_version: int,
    config: ApoapsisConfig,
    planner: PlannerProvenance,
    scenario_id: str,
    scenario_version: str,
    evidence_kind: EvalEvidenceKind = EvalEvidenceKind.DETERMINISTIC_FAKE,
    held_out_oracle: HeldOutOracleDefinition | None = None,
) -> PlannedConditionResult:
    """Advances an already-approved, fixed plan's slices strictly in
    dependency order, one at a time, through the exact, unmodified D3a
    `package_slice`/`approve_slice`/`start_slice` functions -- this
    function contains no execution, routing, or completion logic of its
    own, only orchestration. Auto-advance across slices exists only here,
    inside this evaluation-only module, gated by the caller having already
    approved a fixed plan; it is never reachable from `apoapsis plan
    slice ...` or the Plans UI, which remain one-slice-at-a-time with no
    scheduler (ADR 0027/0028).

    Stops the moment one slice fails to reach `COMPLETE` -- no auto-repair,
    no auto-advance past a stuck slice. Only merges a slice's branch into
    the shared base *after* it reaches `COMPLETE`, so a dependent slice's
    packaging can prove the merge happened via the exact same git-ancestry
    check ADR 0027 already requires for a live product user.

    `start_slice` (unchanged from D3a) always builds its own providers from
    `config.models` when none are injected -- exactly like the CLI/UI's own
    "Start coding" path. This function accepts no provider parameters for
    the same reason: live callers (D4b) rely on that real construction, and
    deterministic tests (D4a) inject fake providers the same way
    `tests/test_architect_slice.py` already does, by patching
    `apoapsis.execution.operation_service._build_providers` around the
    call, never by threading a parallel provider-injection path through
    this orchestration function.
    """

    root = Path(project_root).resolve()
    if held_out_oracle is not None:
        assert_oracle_withheld(root, held_out_oracle)
    started = time.monotonic()
    plan_record = plan_store.get_plan(plan_id)
    order = _topological_slice_order(plan_record.plan)
    dependencies_by_id = {
        item.slice_id: list(item.dependencies) for item in plan_record.plan.slices
    }

    attempts: list[SliceAttemptResult] = []
    stopped_at: str | None = None
    for slice_id in order:
        if stopped_at is not None:
            attempts.append(
                SliceAttemptResult(
                    slice_id=slice_id,
                    dependencies=dependencies_by_id[slice_id],
                    attempted=False,
                    skip_reason=f"plan stopped advancing at slice {stopped_at!r}",
                )
            )
            continue

        if held_out_oracle is not None:
            assert_oracle_withheld(root, held_out_oracle)
        slice_started = time.monotonic()
        package = package_slice(
            root,
            plan_store,
            slice_store,
            task_store,
            operation_store,
            plan_id,
            slice_id,
            expected_plan_version=expected_plan_version,
            config=config,
        )
        record = approve_slice(
            root,
            task_store,
            slice_store,
            plan_id,
            slice_id,
            expected_package_sha256=package.package_sha256,
        )
        operation_id = f"EXOP-{uuid.uuid4().hex[:24].upper()}"
        start_slice(
            root,
            task_store,
            slice_store,
            operation_store,
            plan_id,
            slice_id,
            config,
            operation_id=operation_id,
        )
        assert record.task_id is not None
        task = task_store.get_task(record.task_id)
        report = _read_report(root, record.task_id)
        patch_attempts, unsafe_rejections = _patch_attempts(root, record.task_id)
        attempts.append(
            SliceAttemptResult(
                slice_id=slice_id,
                dependencies=dependencies_by_id[slice_id],
                attempted=True,
                report=report,
                patch_attempts=patch_attempts,
                unsafe_patch_rejections=unsafe_rejections,
                duration_seconds=time.monotonic() - slice_started,
            )
        )
        if task.state != WorkflowState.COMPLETE:
            stopped_at = slice_id
            continue
        _commit_and_merge_slice(root, record.task_id)

    all_complete = stopped_at is None and bool(attempts) and all(
        item.attempted and item.report is not None and item.report.outcome == TaskOutcome.COMPLETE
        for item in attempts
    )
    oracle_result = None
    integration_failure = False
    if all_complete and held_out_oracle is not None:
        oracle_result = run_held_out_oracle_against_worktree(
            root,
            config,
            held_out_oracle,
            task_id=f"TASK-PLANNED-{plan_id.removeprefix('PLAN-')}",
        )
        integration_failure = oracle_result.status == OracleStatus.FAILED

    return PlannedConditionResult(
        scenario_id=scenario_id,
        scenario_version=scenario_version,
        planner=planner,
        slices=attempts,
        all_slices_complete=all_complete,
        stopped_at_slice_id=stopped_at,
        merged_repository_path=str(root) if all_complete else None,
        held_out_oracle=oracle_result,
        integration_failure=integration_failure,
        duration_seconds=time.monotonic() - started,
        evidence_kind=evidence_kind,
    )


__all__ = [
    "PlanningEvaluationError",
    "run_monolithic_condition",
    "run_planned_condition",
]
