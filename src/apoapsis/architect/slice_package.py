from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from apoapsis.architect.audit import AuditArtifact, PlanAuditStore
from apoapsis.architect.errors import SlicePackagingError
from apoapsis.architect.schema import (
    ArchitecturePlan,
    ImplementationSlice,
    PlannerRequestPackage,
)
from apoapsis.discovery.errors import PackageIntegrityError, PackageNotFoundError
from apoapsis.discovery.frontier_package import load_package as load_frontier_package
from apoapsis.architect.slice_schema import (
    DependencyEvidence,
    PlanSliceExecutionPackage,
)
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.architect.validation import validate_plan
from apoapsis.config import ApoapsisConfig
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.execution.worktree import WorktreeError, WorktreeManager
from apoapsis.repository.fingerprint import compute_worktree_fingerprint
from apoapsis.repository.git import GitRepository
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.states import WorkflowState
from apoapsis.specification.schema import (
    AcceptanceCriterion,
    HardConstraint,
    SourceKind,
    TaskSpecification,
    TraceableStatement,
)


def _find_slice(plan: ArchitecturePlan, slice_id: str) -> ImplementationSlice:
    for item in plan.slices:
        if item.slice_id == slice_id:
            return item
    raise SlicePackagingError(f"plan has no slice {slice_id}")


def _approved_slice_fact(
    package_reference: str, field: str, text: str
) -> TraceableStatement:
    return TraceableStatement(
        text=text,
        source=SourceKind.APPROVED_DECISION,
        source_reference=f"{package_reference}/{field}",
    )


def _slice_contract_facts(
    *,
    package_reference: str,
    architecture_summary: str,
    work_brief: str,
    interface_contracts: list[str],
    exclusions: list[str],
    integration_assumptions: list[str],
    stop_conditions: list[str],
    suggested_paths: list[str],
    suggested_symbols: list[str],
) -> list[TraceableStatement]:
    """Preserve the approved execution contract in the derived task.

    These are model context, not new filesystem policy. Suggested paths and
    symbols remain explicitly advisory, while the work brief, exclusions,
    interfaces, assumptions, and stop conditions retain their approved source.
    """

    facts = [
        _approved_slice_fact(
            package_reference,
            "architecture-summary",
            f"Approved plan architecture summary:\n{architecture_summary}",
        ),
        _approved_slice_fact(
            package_reference,
            "work-brief",
            f"Approved slice work brief:\n{work_brief}",
        ),
    ]
    grouped = (
        ("interface-contracts", "Required interface contracts", interface_contracts),
        ("exclusions", "Explicit slice exclusions", exclusions),
        (
            "integration-assumptions",
            "Approved integration assumptions",
            integration_assumptions,
        ),
        ("stop-conditions", "Approved stop conditions", stop_conditions),
        (
            "suggested-paths",
            "Advisory suggested paths (not an allowlist)",
            suggested_paths,
        ),
        ("suggested-symbols", "Advisory suggested symbols", suggested_symbols),
    )
    for field, label, values in grouped:
        if values:
            facts.append(
                _approved_slice_fact(
                    package_reference,
                    field,
                    f"{label}:\n" + "\n".join(f"- {value}" for value in values),
                )
            )
    return facts


def enrich_specification_with_slice_package(
    specification: TaskSpecification,
    package: PlanSliceExecutionPackage,
) -> TaskSpecification:
    """Backfill approved slice context for tasks created by older packages.

    Existing audit artifacts are never rewritten. A continuation may enrich its
    in-memory model context from the exact immutable package the user approved.
    """

    package_reference = f"{package.plan_id}@v{package.plan_version}/{package.slice_id}"
    additions = _slice_contract_facts(
        package_reference=package_reference,
        architecture_summary=package.architecture_summary,
        work_brief=package.work_brief,
        interface_contracts=package.interface_contracts,
        exclusions=package.exclusions,
        integration_assumptions=package.integration_assumptions,
        stop_conditions=package.stop_conditions,
        suggested_paths=package.advisory_suggested_paths,
        suggested_symbols=package.advisory_suggested_symbols,
    )
    existing = {
        (item.source_reference, item.text) for item in specification.known_facts
    }
    known_facts = [
        *specification.known_facts,
        *(
            item
            for item in additions
            if (item.source_reference, item.text) not in existing
        ),
    ]
    verification_requirements = list(
        dict.fromkeys(
            [*specification.verification_requirements, *package.verification_commands]
        )
    )
    return specification.model_copy(
        update={
            "known_facts": known_facts,
            "verification_requirements": verification_requirements,
        }
    )


def _exact_constraints(
    plan: ArchitecturePlan, slice_obj: ImplementationSlice
) -> list[HardConstraint]:
    """Copies the exact ``HardConstraint`` objects a slice inherits from the
    plan's own records -- never re-derived or reworded. Fails closed if a
    referenced ID cannot be recovered exactly (should not happen against a
    plan that passed validation, but this is the last gate before that
    content becomes a real task's specification, so it is checked again
    here rather than trusted)."""

    by_id = {item.id: item for item in plan.hard_constraints}
    constraints: list[HardConstraint] = []
    for constraint_id in slice_obj.inherited_constraint_ids:
        constraint = by_id.get(constraint_id)
        if constraint is None:
            raise SlicePackagingError(
                f"slice {slice_obj.slice_id} references hard constraint "
                f"{constraint_id}, which cannot be recovered exactly from "
                "the approved plan"
            )
        constraints.append(constraint)
    return constraints


def _exact_criteria(
    plan: ArchitecturePlan, slice_obj: ImplementationSlice
) -> list[AcceptanceCriterion]:
    by_id = {item.id: item for item in plan.acceptance_criteria}
    criteria: list[AcceptanceCriterion] = []
    for criterion_id in slice_obj.acceptance_criterion_ids:
        criterion = by_id.get(criterion_id)
        if criterion is None:
            raise SlicePackagingError(
                f"slice {slice_obj.slice_id} references acceptance "
                f"criterion {criterion_id}, which cannot be recovered "
                "exactly from the approved plan"
            )
        criteria.append(criterion)
    return criteria


def _relevant_decisions(plan: ArchitecturePlan) -> list:
    # A slice does not name which decisions apply to it (ADR 0019's schema
    # has no such field); every decision on the plan is architecture-wide
    # context, so all are carried through for human/model visibility.
    return list(plan.decisions)


def checkpoint_completed_prior_slices(
    project_root: str | Path,
    plan_id: str,
    plan: ArchitecturePlan,
    current_slice_id: str,
    task_store: SQLiteTaskStore,
    slice_store: PlanSliceExecutionStore,
    *,
    include_current: bool = False,
) -> tuple[str | None, list[str]]:
    """Checkpoint completed slices in this plan and return the newest tip.

    Checkpoints are ordinary commits on Apoapsis-owned task branches. The
    project's checked-out branch is never moved or merged. Because a plan
    permits only one active slice, each newly packaged slice can start from
    the latest completed predecessor and naturally accumulate all prior
    slice work.
    """

    root = Path(project_root).resolve()
    repository = GitRepository(root)
    inherited: list[tuple[str, str]] = []
    found_current = any(item.slice_id == current_slice_id for item in plan.slices)
    if not found_current:
        raise SlicePackagingError(f"plan has no slice {current_slice_id}")
    for slice_obj in plan.slices:
        if slice_obj.slice_id == current_slice_id and not include_current:
            continue
        try:
            record = slice_store.get(plan_id, slice_obj.slice_id)
        except Exception:
            continue
        if record.task_id is None:
            continue
        task = task_store.get_task(record.task_id)
        if task.state != WorkflowState.COMPLETE:
            continue
        slug = record.task_id.removeprefix("TASK-").lower()
        try:
            managed = WorktreeManager(root).describe(slug)
        except WorktreeError as exc:
            raise SlicePackagingError(
                f"completed prior slice {slice_obj.slice_id} has no usable "
                "worktree to inherit"
            ) from exc
        status = repository.run(
            ["status", "--porcelain=v1"], cwd=managed.path
        ).stdout
        if status.strip():
            repository.run(["add", "-A"], cwd=managed.path)
            hooks_dir = root / ".apoapsis" / "runtime" / "empty-git-hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            repository.run(
                [
                    "-c",
                    "user.name=Apoapsis Harness",
                    "-c",
                    "user.email=apoapsis@localhost",
                    "-c",
                    f"core.hooksPath={hooks_dir}",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "-m",
                    f"apoapsis: checkpoint {plan_id}/{slice_obj.slice_id}",
                ],
                cwd=managed.path,
            )
        tip = repository.run(["rev-parse", "HEAD"], cwd=managed.path).stdout.strip()
        inherited.append((slice_obj.slice_id, tip))
    if not inherited:
        return None, []
    candidates = []
    for _slice_id, candidate in inherited:
        if all(
            repository.run(
                ["merge-base", "--is-ancestor", commit, candidate], check=False
            ).returncode
            == 0
            for _other_id, commit in inherited
        ):
            candidates.append(candidate)
    if not candidates:
        raise SlicePackagingError(
            "completed slice branches diverge; Apoapsis will not guess an "
            "integration order or merge conflicts automatically"
        )
    latest_commit = candidates[0]
    return latest_commit, [slice_id for slice_id, _commit in inherited]


def _dependency_evidence(
    project_root: Path,
    task_store: SQLiteTaskStore,
    slice_store: PlanSliceExecutionStore,
    operation_store: ExecutionOperationStore,
    plan_id: str,
    slice_obj: ImplementationSlice,
) -> list[DependencyEvidence]:
    """Deterministically proves that every dependency has a completed,
    inheritable task worktree. Packaging checkpoints those branches before
    building the immutable package, so dependent execution can use the
    recorded branch tip without requiring a merge into the user's branch.

    The dependency's *current* status is read from its derived task's own
    real, current workflow state -- never from this store's own persisted
    ``status`` field, which (by design, see ``slice_store``) only ever
    holds ``PACKAGED``/``APPROVED`` and would otherwise look permanently
    stale once a task starts actually running."""

    evidence: list[DependencyEvidence] = []
    repository = GitRepository(project_root)
    for dependency_slice_id in slice_obj.dependencies:
        try:
            dependency_record = slice_store.get(plan_id, dependency_slice_id)
        except Exception:
            evidence.append(
                DependencyEvidence(
                    slice_id=dependency_slice_id,
                    satisfied=False,
                    reason=(
                        "dependency slice has not been packaged or "
                        "executed yet"
                    ),
                )
            )
            continue
        if dependency_record.task_id is None:
            evidence.append(
                DependencyEvidence(
                    slice_id=dependency_slice_id,
                    satisfied=False,
                    reason="dependency slice has not been approved yet",
                )
            )
            continue
        dependency_task = task_store.get_task(dependency_record.task_id)
        if dependency_task.state != WorkflowState.COMPLETE:
            evidence.append(
                DependencyEvidence(
                    slice_id=dependency_slice_id,
                    satisfied=False,
                    reason=(
                        f"dependency task state is "
                        f"{dependency_task.state.value!r}, not COMPLETE"
                    ),
                    dependency_task_id=dependency_record.task_id,
                )
            )
            continue
        slug = dependency_record.task_id.removeprefix("TASK-").lower()
        try:
            managed = WorktreeManager(project_root).describe(slug)
        except WorktreeError:
            evidence.append(
                DependencyEvidence(
                    slice_id=dependency_slice_id,
                    satisfied=False,
                    reason=(
                        "dependency's worktree no longer exists; its "
                        "changes cannot be proven merged"
                    ),
                    dependency_task_id=dependency_record.task_id,
                )
            )
            continue
        worktree_tip = repository.run(
            ["rev-parse", managed.branch], check=False
        ).stdout.strip()
        if not worktree_tip:
            evidence.append(
                DependencyEvidence(
                    slice_id=dependency_slice_id,
                    satisfied=False,
                    reason=(
                        "dependency's worktree branch has no resolvable commit"
                    ),
                    dependency_task_id=dependency_record.task_id,
                    dependency_branch=managed.branch,
                )
            )
            continue
        evidence.append(
            DependencyEvidence(
                slice_id=dependency_slice_id,
                satisfied=True,
                reason=(
                    "dependency complete with an inheritable isolated-branch tip"
                ),
                dependency_task_id=dependency_record.task_id,
                dependency_branch=managed.branch,
                dependency_commit=worktree_tip,
            )
        )
    return evidence


def dependency_evidence(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    slice_store: PlanSliceExecutionStore,
    operation_store: ExecutionOperationStore,
    plan_id: str,
    slice_obj: ImplementationSlice,
) -> list[DependencyEvidence]:
    """Public read-only projection of the exact dependency proof packaging
    already uses.  UI status rendering may call this, but it cannot satisfy,
    override, or mutate a dependency itself."""

    return _dependency_evidence(
        Path(project_root).resolve(),
        task_store,
        slice_store,
        operation_store,
        plan_id,
        slice_obj,
    )


def _sha256_canonical(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_originating_package_repository_root(root: Path, package_id: str) -> str:
    """Loads and integrity-checks the plan's originating request package,
    regardless of which of the two planning entry points produced it, and
    returns the repository root it was built against.

    A plan reaching Architect Mode's ``ArchitecturePlan`` shape can come
    from either ``apoapsis plan export``/``plan import`` (an on-disk
    ``PlannerRequestPackage`` under ``.apoapsis/plan-packages/<package_id>``)
    or the discovery-to-frontier-planning handoff (ADR 0032, an on-disk
    ``FrontierPlanningRequestPackage`` under
    ``.apoapsis/discovery-planning-packages/<package_id>``, with its own
    ``package_sha256`` self-consistency check). The plan record itself only
    stores ``package_id``, not which of the two produced it, so the two
    package id formats (``PKG-`` vs ``FPKG-``) are what disambiguate which
    directory and schema to load against -- both are validated exactly as
    strictly as before, just via whichever loader actually matches how the
    package was written."""

    if package_id.startswith("FPKG-"):
        try:
            frontier_package = load_frontier_package(root, package_id)
        except PackageNotFoundError as exc:
            raise SlicePackagingError(
                f"no exported request package found for {package_id}; "
                "this plan's provenance cannot be verified"
            ) from exc
        except PackageIntegrityError as exc:
            raise SlicePackagingError(str(exc)) from exc
        return frontier_package.repository.root

    package_path = root / ".apoapsis" / "plan-packages" / package_id / "request-package.json"
    if not package_path.is_file():
        raise SlicePackagingError(
            f"no exported request package found for {package_id}; "
            "this plan's provenance cannot be verified"
        )
    plan_package = PlannerRequestPackage.model_validate_json(
        package_path.read_text(encoding="utf-8")
    )
    return plan_package.repository.root


def build_plan_slice_execution_package(
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
    execution_base_commit: str | None = None,
    inherited_slice_ids: list[str] | None = None,
) -> PlanSliceExecutionPackage:
    """Deterministically compiles exactly what approving this slice would
    authorize -- no model call and no task creation. The service checkpoints
    completed predecessor worktrees before calling this builder.
    Requires the plan to be ``APPROVED`` at exactly ``expected_plan_
    version``, revalidates it against the *current* configured
    constraints/criteria/verification-command catalog (never trusting a
    stale validation result), verifies the plan's originating request
    package (Architect Mode's own export, or the discovery-flow's frontier
    planning package) still exists and was built against this same
    repository, and proves every dependency slice via git ancestry before
    ever copying the slice's exact inherited hard constraints and
    acceptance criteria into a derived ``TaskSpecification``."""

    root = Path(project_root).resolve()
    record = plan_store.get_plan(plan_id)
    if record.version != expected_plan_version:
        raise SlicePackagingError(
            f"expected plan version {expected_plan_version}, found "
            f"{record.version}"
        )
    if record.status.value != "approved":
        raise SlicePackagingError(
            f"plan {plan_id} must be APPROVED to package a slice, found "
            f"{record.status.value}"
        )

    originating_repository_root = _load_originating_package_repository_root(
        root, record.package_id
    )
    current_snapshot = GitRepository(root).snapshot()
    if originating_repository_root != current_snapshot.root:
        raise SlicePackagingError(
            "this plan's originating request package was built against a "
            "different repository root; re-plan or re-validate before "
            "packaging a slice"
        )

    configured_names = {command.name for command in config.verification.commands}
    findings = validate_plan(
        record.plan,
        configured_verification_commands=configured_names,
        ceilings=config.architect.ceilings,
    )
    if any(item.severity.value == "error" for item in findings):
        raise SlicePackagingError(
            "plan fails revalidation against current configuration "
            f"({len(findings)} finding(s)); re-validate and re-approve "
            "before packaging a slice"
        )

    slice_obj = _find_slice(record.plan, slice_id)
    dependency_evidence = _dependency_evidence(
        root, task_store, slice_store, operation_store, plan_id, slice_obj
    )
    unsatisfied = [item for item in dependency_evidence if not item.satisfied]
    if unsatisfied:
        reasons = "; ".join(f"{item.slice_id}: {item.reason}" for item in unsatisfied)
        raise SlicePackagingError(
            f"slice {slice_id} has unsatisfied dependencies: {reasons}"
        )

    inherited_constraints = _exact_constraints(record.plan, slice_obj)
    inherited_criteria = _exact_criteria(record.plan, slice_obj)
    fingerprint = compute_worktree_fingerprint(root)

    # Deterministic, not random: repackaging the same (plan, slice, plan
    # version) before approval always proposes the same not-yet-created
    # task id, so the whole package -- including this -- reproduces the
    # same hash given unchanged inputs. A different plan version (a real
    # revision) gets a genuinely different id, never colliding with a
    # stale, superseded package's.
    derived_task_id = "TASK-" + hashlib.sha256(
        f"{plan_id}:{slice_id}:{expected_plan_version}".encode("utf-8")
    ).hexdigest()[:24].upper()
    package_reference = f"{plan_id}@v{expected_plan_version}/{slice_id}"
    derived_specification = TaskSpecification(
        task_id=derived_task_id,
        objective=TraceableStatement(
            text=slice_obj.objective,
            source=SourceKind.APPROVED_DECISION,
            source_reference=package_reference,
        ),
        acceptance_criteria=inherited_criteria,
        hard_constraints=inherited_constraints,
        known_facts=_slice_contract_facts(
            package_reference=package_reference,
            architecture_summary=record.plan.architecture_summary,
            work_brief=slice_obj.work_brief,
            interface_contracts=list(slice_obj.interface_contracts),
            exclusions=list(slice_obj.exclusions),
            integration_assumptions=list(slice_obj.integration_assumptions),
            stop_conditions=list(slice_obj.stop_conditions),
            suggested_paths=list(slice_obj.suggested_paths),
            suggested_symbols=list(slice_obj.suggested_symbols),
        ),
        verification_requirements=list(slice_obj.verification_commands),
        risk_level=slice_obj.risk_level,
    )

    package_id = f"SXP-{uuid.uuid4().hex[:12].upper()}"
    package = PlanSliceExecutionPackage(
        package_id=package_id,
        plan_id=plan_id,
        plan_version=expected_plan_version,
        plan_package_id=record.package_id,
        slice_id=slice_id,
        idea_text=record.idea_text,
        architecture_summary=record.plan.architecture_summary,
        relevant_decisions=_relevant_decisions(record.plan),
        interface_contracts=list(slice_obj.interface_contracts),
        objective=slice_obj.objective,
        exclusions=list(slice_obj.exclusions),
        inherited_hard_constraints=inherited_constraints,
        acceptance_criteria=inherited_criteria,
        verification_commands=list(slice_obj.verification_commands),
        dependency_evidence=dependency_evidence,
        integration_assumptions=list(slice_obj.integration_assumptions),
        risk_level=slice_obj.risk_level,
        stop_conditions=list(slice_obj.stop_conditions),
        local_model_fit_rationale=slice_obj.local_model_fit_rationale,
        work_brief=slice_obj.work_brief,
        advisory_suggested_paths=list(slice_obj.suggested_paths),
        advisory_suggested_symbols=list(slice_obj.suggested_symbols),
        advisory_context_seeds=list(slice_obj.context_seeds),
        repository_root=str(root),
        repository_head_commit=fingerprint.head_commit,
        repository_fingerprint=fingerprint.digest,
        execution_base_commit=execution_base_commit or fingerprint.head_commit,
        inherited_slice_ids=list(inherited_slice_ids or []),
        derived_specification=derived_specification,
    )
    # ``package_id`` is excluded too, for the same reason ADR 0026's
    # ``ExecutionAuthorizationPackage`` excludes ``operation_id``: it is a
    # fresh identifier for this specific packaging attempt, not content
    # someone is authorizing -- excluding it lets repeated packaging of
    # the same (plan, slice, plan version) reproduce the same hash.
    package_sha256 = _sha256_canonical(
        package.model_dump(
            mode="json", exclude={"package_sha256", "generated_at", "package_id"}
        )
    )
    return package.model_copy(update={"package_sha256": package_sha256})


def write_plan_slice_execution_package(
    project_root: str | Path, package: PlanSliceExecutionPackage
) -> AuditArtifact:
    audit = PlanAuditStore(project_root, package.plan_id)
    return audit.write_json(
        f"slice-{package.slice_id}-package-{package.package_id}.json",
        package,
        kind="plan_slice_execution_package",
    )


__all__ = [
    "dependency_evidence",
    "build_plan_slice_execution_package",
    "write_plan_slice_execution_package",
]
