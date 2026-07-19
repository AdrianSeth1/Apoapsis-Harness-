from __future__ import annotations

from pathlib import Path

from apoapsis.agent.session import AgentSessionResult
from apoapsis.config import ApoapsisConfig
from apoapsis.execution.worktree import WorktreeError, WorktreeManager
from apoapsis.repository.fingerprint import compute_worktree_fingerprint
from apoapsis.repository.git import GitRepository
from apoapsis.reporting.report import FinalTaskReport
from apoapsis.review.classify import classify_stop_reason, eligible_actions_for
from apoapsis.review.errors import ReviewCaseError
from apoapsis.review.schema import ReviewCase
from apoapsis.verification.failures import FailureNormalizer, NormalizedFailure
from apoapsis.verification.results import VerificationResult, VerificationStatus
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.states import WorkflowState

LOCAL_CONTINUATION_STARTED = "review_local_continuation_started"
FRONTIER_CONTINUATION_STARTED = "review_frontier_continuation_started"


def task_slug(task_id: str) -> str:
    return task_id.removeprefix("TASK-").lower()


def read_agent_session(
    task_directory: Path, prefix: str
) -> AgentSessionResult | None:
    path = task_directory / f"{prefix}agent-session.json"
    if not path.is_file():
        return None
    return AgentSessionResult.model_validate_json(path.read_text(encoding="utf-8"))


def _normalized_failures(
    verification_results: list[VerificationResult], worktree_path: str | None
) -> list[NormalizedFailure]:
    if worktree_path is None:
        return []
    normalizer = FailureNormalizer()
    failures: list[NormalizedFailure] = []
    for result in verification_results:
        if result.status == VerificationStatus.PASSED:
            continue
        try:
            _, failure = normalizer.extract(result, worktree_path)
        except ValueError:
            continue
        failures.append(failure)
    return failures


def continuation_additional_turns(events, event_type: str) -> int:
    total = 0
    for event in events:
        if event.event_type != event_type:
            continue
        budget = event.payload.get("authorized_budget") if isinstance(
            event.payload, dict
        ) else None
        if isinstance(budget, dict):
            total += int(budget.get("additional_turns", 0))
    return total


def build_review_case(
    project_root: str | Path,
    store: SQLiteTaskStore,
    config: ApoapsisConfig,
    task_id: str,
) -> ReviewCase:
    """Project a deterministic ``ReviewCase`` for a task currently stopped
    at HUMAN_REVIEW_REQUIRED (ADR 0020). Raises ``ReviewCaseError`` if the
    task is not currently in that state -- a review case is only ever
    meaningful for an actual stop, never speculatively for any other
    workflow state."""

    root = Path(project_root).resolve()
    record = store.get_task(task_id)
    if record.state != WorkflowState.HUMAN_REVIEW_REQUIRED:
        raise ReviewCaseError(
            f"task {task_id} is not at HUMAN_REVIEW_REQUIRED "
            f"(currently {record.state.value})"
        )
    events = store.events(task_id)
    kind, event_type, fallback_text = classify_stop_reason(events)
    continuations_used = sum(
        1
        for event in events
        if event.event_type in {LOCAL_CONTINUATION_STARTED, FRONTIER_CONTINUATION_STARTED}
    )

    task_directory = root / ".apoapsis" / "tasks" / task_id
    report: FinalTaskReport | None = None
    report_path = task_directory / "report.json"
    if report_path.is_file():
        report = FinalTaskReport.model_validate_json(
            report_path.read_text(encoding="utf-8")
        )
    # `report.json` is a snapshot of the *original* stop only -- once a
    # continuation has run, its own event payload (always including a
    # "reason") is the accurate, current text; the original report's error
    # would otherwise describe a stop reason that no longer applies.
    if continuations_used == 0 and report and report.error:
        stop_reason_text = report.error
    else:
        stop_reason_text = fallback_text

    worktree_path: str | None = None
    worktree_exists = False
    worktree_fingerprint: str | None = None
    repository_head_commit: str | None = None
    current_diff: str | None = None
    try:
        managed = WorktreeManager(root).describe(task_slug(task_id))
        worktree_path = managed.path
        worktree_exists = True
        fingerprint = compute_worktree_fingerprint(managed.path)
        worktree_fingerprint = fingerprint.digest
        repository_head_commit = fingerprint.head_commit
        current_diff = GitRepository(managed.path).run(
            ["diff", "--no-ext-diff", "--unified=3", "HEAD"]
        ).stdout
    except WorktreeError:
        pass

    verification_results = report.verification_results if report else []
    acceptance_coverage = report.acceptance_coverage if report else []
    normalized_failures = _normalized_failures(verification_results, worktree_path)
    models_used = (
        [f"{item.provider}/{item.model}" for item in report.models_used]
        if report
        else []
    )

    local_session = read_agent_session(task_directory, "")
    frontier_session = read_agent_session(task_directory, "frontier-")

    local_additional = continuation_additional_turns(
        events, LOCAL_CONTINUATION_STARTED
    )
    frontier_additional = continuation_additional_turns(
        events, FRONTIER_CONTINUATION_STARTED
    )
    configured_local_budget = None
    if config.execution.agent is not None:
        base = config.execution.agent
        configured_local_budget = base.model_copy(
            update={
                "max_turns": base.max_turns + local_additional,
                "max_patch_attempts": base.max_patch_attempts + local_additional,
                "max_verification_runs": (
                    base.max_verification_runs + local_additional
                ),
            }
        )
    configured_frontier_budget = None
    frontier_available = config.models.frontier_coder is not None
    if frontier_available:
        base = config.execution.frontier_agent
        configured_frontier_budget = base.model_copy(
            update={
                "max_turns": base.max_turns + frontier_additional,
                "max_patch_attempts": base.max_patch_attempts + frontier_additional,
                "max_verification_runs": (
                    base.max_verification_runs + frontier_additional
                ),
            }
        )

    eligible_actions = eligible_actions_for(
        kind,
        frontier_available=frontier_available,
        continuations_used=continuations_used,
        max_continuations_per_task=config.review.max_continuations_per_task,
    )

    audit_artifact_locations: list[str] = []
    if task_directory.is_dir():
        audit_artifact_locations = sorted(
            str(path.relative_to(root)).replace("\\", "/")
            for path in task_directory.rglob("*")
            if path.is_file()
        )

    return ReviewCase(
        task_id=record.task_id,
        task_version=record.version,
        workflow_state=record.state,
        stop_reason_kind=kind,
        stop_reason_text=stop_reason_text,
        stop_event_type=event_type,
        objective_text=record.specification.objective.text,
        worktree_path=worktree_path,
        worktree_exists=worktree_exists,
        worktree_fingerprint=worktree_fingerprint,
        repository_head_commit=repository_head_commit,
        active_hard_constraints=record.specification.active_hard_constraints,
        current_diff=current_diff,
        verification_results=verification_results,
        acceptance_coverage=acceptance_coverage,
        normalized_failures=normalized_failures,
        models_used=models_used,
        consumed_local_turns=local_session.turns if local_session else 0,
        consumed_local_patch_attempts=(
            local_session.patch_attempts if local_session else 0
        ),
        consumed_local_verification_runs=(
            local_session.verification_runs if local_session else 0
        ),
        configured_local_budget=configured_local_budget,
        consumed_frontier_turns=frontier_session.turns if frontier_session else 0,
        consumed_frontier_patch_attempts=(
            frontier_session.patch_attempts if frontier_session else 0
        ),
        consumed_frontier_verification_runs=(
            frontier_session.verification_runs if frontier_session else 0
        ),
        configured_frontier_budget=configured_frontier_budget,
        frontier_available=frontier_available,
        continuations_used=continuations_used,
        max_continuations_per_task=config.review.max_continuations_per_task,
        max_additional_turns_per_continuation=(
            config.review.max_additional_turns_per_continuation
        ),
        eligible_actions=eligible_actions,
        audit_artifact_locations=audit_artifact_locations,
    )


__all__ = [
    "build_review_case",
    "task_slug",
    "read_agent_session",
    "continuation_additional_turns",
    "LOCAL_CONTINUATION_STARTED",
    "FRONTIER_CONTINUATION_STARTED",
]
