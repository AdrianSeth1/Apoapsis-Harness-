from __future__ import annotations

from pathlib import Path

from apoapsis.agent.inspection import RepositoryInspector
from apoapsis.agent.session import AgentSessionResult
from apoapsis.config import ApoapsisConfig
from apoapsis.execution.worktree import WorktreeError, WorktreeManager
from apoapsis.repository.fingerprint import compute_worktree_fingerprint
from apoapsis.reporting.report import FinalTaskReport
from apoapsis.review.classify import classify_stop_reason, eligible_actions_for
from apoapsis.review.errors import ReviewCaseError
from apoapsis.review.schema import ReviewCase
from apoapsis.verification.failures import FailureNormalizer, NormalizedFailure
from apoapsis.verification.results import VerificationResult, VerificationStatus
from apoapsis.workflow.acceptance import AcceptanceCoverage
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.events import WorkflowEvent
from apoapsis.workflow.states import WorkflowState

LOCAL_CONTINUATION_STARTED = "review_local_continuation_started"
FRONTIER_CONTINUATION_STARTED = "review_frontier_continuation_started"

_VERIFICATION_RETRY_EVENT_TYPES = frozenset(
    {"review_verification_retry_incomplete", "review_verification_retry_failed"}
)
_LOCAL_SESSION_EVENT_TYPES = frozenset(
    {"frontier_escalation_not_configured", "review_local_continuation_requires_human"}
)
_FRONTIER_SESSION_EVENT_TYPES = frozenset(
    {"bounded_frontier_requires_human", "review_frontier_continuation_requires_human"}
)


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


def _event_reason_text(event: WorkflowEvent | None) -> str:
    if event is None:
        return "no recognized stop reason was found in the task's event history"
    reason = event.payload.get("reason") if isinstance(event.payload, dict) else None
    return reason if isinstance(reason, str) and reason else event.event_type


def _fresh_evidence(
    task_directory: Path,
    stop_event: WorkflowEvent | None,
    *,
    report_verification_results: list[VerificationResult],
    report_acceptance_coverage: list[AcceptanceCoverage],
    local_session: AgentSessionResult | None,
    frontier_session: AgentSessionResult | None,
) -> tuple[list[VerificationResult], list[AcceptanceCoverage]]:
    """Prefer the evidence behind the task's *current* stop over the
    original ``report.json`` snapshot, once a retry or continuation has
    actually produced newer evidence (ADR 0021) -- the report is only ever
    written once, at the first stop, and never updated afterward.

    Which source is authoritative is decided by the same newest-event
    classification `classify_stop_reason` already computed, so this stays
    consistent with `stop_reason_kind`/`stop_reason_text` rather than
    guessing independently at "freshness".
    """

    if stop_event is None:
        return report_verification_results, report_acceptance_coverage
    payload = stop_event.payload if isinstance(stop_event.payload, dict) else {}
    event_type = stop_event.event_type

    if event_type in _VERIFICATION_RETRY_EVENT_TYPES:
        verification_results = report_verification_results
        operation_id = payload.get("operation_id")
        if isinstance(operation_id, str):
            retry_path = task_directory / f"review-verification-retry-{operation_id}.json"
            if retry_path.is_file():
                verification_results = [
                    VerificationResult.model_validate_json(
                        retry_path.read_text(encoding="utf-8")
                    )
                ]
        coverage_payload = payload.get("coverage")
        acceptance_coverage = (
            [AcceptanceCoverage.model_validate(item) for item in coverage_payload]
            if isinstance(coverage_payload, list)
            else []
        )
        return verification_results, acceptance_coverage

    if event_type in _LOCAL_SESSION_EVENT_TYPES and local_session is not None:
        return local_session.verification_results, local_session.acceptance_coverage

    if event_type in _FRONTIER_SESSION_EVENT_TYPES and frontier_session is not None:
        return frontier_session.verification_results, frontier_session.acceptance_coverage

    return report_verification_results, report_acceptance_coverage


def build_review_case(
    project_root: str | Path,
    store: SQLiteTaskStore,
    config: ApoapsisConfig,
    task_id: str,
) -> ReviewCase:
    """Project a deterministic ``ReviewCase`` for a task currently stopped
    at HUMAN_REVIEW_REQUIRED (ADR 0020, hardened by ADR 0021). Raises
    ``ReviewCaseError`` if the task is not currently in that state -- a
    review case is only ever meaningful for an actual stop, never
    speculatively for any other workflow state. Every field is recomputed
    fresh from persisted state on every call; callers that need to
    guarantee nothing changed between two calls must compare the returned
    fingerprints/versions explicitly, never assume this function caches
    anything.
    """

    root = Path(project_root).resolve()
    record = store.get_task(task_id)
    if record.state != WorkflowState.HUMAN_REVIEW_REQUIRED:
        raise ReviewCaseError(
            f"task {task_id} is not at HUMAN_REVIEW_REQUIRED "
            f"(currently {record.state.value})"
        )
    events = store.events(task_id)
    kind, stop_event = classify_stop_reason(events)
    stop_event_type = stop_event.event_type if stop_event is not None else "unknown"
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
    # continuation or retry has run, the newest event's own payload (always
    # including a "reason") is the accurate, current text; the original
    # report's error would otherwise describe a stop reason that no longer
    # applies.
    if continuations_used == 0 and report and report.error:
        stop_reason_text = report.error
    else:
        stop_reason_text = _event_reason_text(stop_event)

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
        # The shared bounded inspection machinery (ADR 0017), not a plain
        # `git diff` -- so a reviewer sees exactly the same permitted
        # untracked text files (and binary/symlink path-only placeholders)
        # the worktree fingerprint above is already sensitive to, not only
        # tracked changes.
        inspector = RepositoryInspector(
            managed.path,
            max_search_results=1,
            max_read_lines=1,
            max_chars=config.context.max_total_chars,
        )
        diff_evidence = inspector.diff()
        current_diff = diff_evidence.content if diff_evidence is not None else ""
    except WorktreeError:
        pass

    local_session = read_agent_session(task_directory, "")
    frontier_session = read_agent_session(task_directory, "frontier-")

    verification_results, acceptance_coverage = _fresh_evidence(
        task_directory,
        stop_event,
        report_verification_results=report.verification_results if report else [],
        report_acceptance_coverage=report.acceptance_coverage if report else [],
        local_session=local_session,
        frontier_session=frontier_session,
    )
    normalized_failures = _normalized_failures(verification_results, worktree_path)
    models_used = (
        [f"{item.provider}/{item.model}" for item in report.models_used]
        if report
        else []
    )

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
        stop_event_type=stop_event_type,
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
