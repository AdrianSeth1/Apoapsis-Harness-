from __future__ import annotations

from pathlib import Path

from apoapsis.agent.inspection import RepositoryInspector
from apoapsis.agent.power_session import LocalPowerReviewPackage
from apoapsis.agent.session import AgentSessionResult
from apoapsis.config import ApoapsisConfig, effective_config_for_specification
from apoapsis.execution.worktree import WorktreeError, WorktreeManager
from apoapsis.repository.fingerprint import compute_worktree_fingerprint
from apoapsis.reporting.current_state import project_current_task_evidence
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
# One manual-frontier "repair round" is consumed each time an applied
# manual-subscription patch fails verification and the task returns to
# HUMAN_REVIEW_REQUIRED still eligible for another handoff (ADR 0031) --
# never for a successful apply, since a successful apply reaches COMPLETE
# and there is nothing left to repair.
MANUAL_FRONTIER_ROUND_CONSUMED_EVENT = "manual_frontier_apply_verification_failed"


# Which stop event corresponds to which evidence artifact is no longer
# decided here. `reporting.current_state` owns that mapping for every
# consumer that labels a task outcome (ADR 0072), so the Report page,
# review case, delivery record, and frontier handoff cannot drift apart
# by each maintaining its own private table.


def task_slug(task_id: str) -> str:
    return task_id.removeprefix("TASK-").lower()


def read_agent_session(
    task_directory: Path, prefix: str
) -> AgentSessionResult | None:
    path = task_directory / f"{prefix}agent-session.json"
    if not path.is_file():
        return None
    return AgentSessionResult.model_validate_json(path.read_text(encoding="utf-8"))


def read_local_power_session(task_directory: Path) -> AgentSessionResult | None:
    """Read a Local Power Sandbox stage's session result (ADR 0059).

    The sandbox loop writes ``local-power-session.json``, not the strict
    loop's ``{prefix}agent-session.json``, so ``read_agent_session`` cannot
    see it. Both persist the same ``AgentSessionResult``; only the filename
    differs. Callers deciding whether a task has a prior local stage at all
    must consult both -- a task executed under local power has no
    ``agent-session.json``, and treating that absence as "no session ever
    ran" is how a resumable stage came to look like a nonexistent one.
    """

    path = task_directory / "local-power-session.json"
    if not path.is_file():
        return None
    return AgentSessionResult.model_validate_json(path.read_text(encoding="utf-8"))


def read_local_stage_session(
    task_directory: Path,
) -> tuple[AgentSessionResult | None, bool]:
    """Return ``(session, is_local_power)`` for whichever local stage ran.

    The single question most callers actually want answered is "did a local
    coding stage already run against this task", and the answer must not
    depend on which local loop the project happened to be configured for.
    Prefers the strict loop's record when both somehow exist, since only
    that one can be resumed by ``BoundedAgentSession``.
    """

    strict = read_agent_session(task_directory, "")
    if strict is not None:
        return strict, False
    return read_local_power_session(task_directory), True


def read_local_power_review_package(
    task_directory: Path,
) -> LocalPowerReviewPackage | None:
    """Read the sandbox stage's review package, if the stage wrote one.

    Carries the shell-command and rejection records an
    ``AgentSessionResult`` does not, which a resumed sandbox session needs
    in order to continue spending the same shell budget rather than a
    fresh one.
    """

    path = task_directory / "local-power-review-package.json"
    if not path.is_file():
        return None
    return LocalPowerReviewPackage.model_validate_json(
        path.read_text(encoding="utf-8")
    )


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
    config = effective_config_for_specification(config, record.specification)
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

    local_session, _ = read_local_stage_session(task_directory)
    frontier_session = read_agent_session(task_directory, "frontier-")

    # One shared projection, not a locally reimplemented notion of
    # "freshness" (ADR 0072). `record`/`events` are handed over rather than
    # re-read so this case and its evidence describe the same instant; the
    # projection's own `task_version` is asserted below to keep that
    # promise checkable rather than assumed.
    evidence = project_current_task_evidence(
        root, store, task_id, record=record, events=events
    )
    assert evidence.task_version == record.version
    verification_results = evidence.verification_results
    acceptance_coverage = evidence.acceptance_coverage
    # The projection resolves the current stop text the same way it
    # resolves the current evidence: `report.error` while the original stop
    # still stands, the deciding event's own reason once a retry,
    # continuation, or manual-frontier round (ADR 0031) has superseded it.
    # A live browser pass first caught the old divergence here: a failed
    # manual-frontier apply correctly reclassified `stop_reason_kind` to
    # `VERIFICATION_FAILED` while `stop_reason_text` still showed the
    # original escalation message.
    stop_reason_text = evidence.reason
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
    frontier_model = (
        config.models.frontier_coder.model if frontier_available else None
    )
    frontier_stage_exists = frontier_session is not None
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

    manual_frontier_rounds_used = sum(
        1
        for event in events
        if event.event_type == MANUAL_FRONTIER_ROUND_CONSUMED_EVENT
    )
    max_manual_frontier_rounds = config.manual_frontier.max_repair_rounds

    eligible_actions = eligible_actions_for(
        kind,
        frontier_available=frontier_available,
        continuations_used=continuations_used,
        max_continuations_per_task=config.review.max_continuations_per_task,
        frontier_stage_exists=frontier_stage_exists,
        manual_frontier_rounds_used=manual_frontier_rounds_used,
        max_manual_frontier_rounds=max_manual_frontier_rounds,
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
        frontier_model=frontier_model,
        frontier_stage_exists=frontier_stage_exists,
        continuations_used=continuations_used,
        max_continuations_per_task=config.review.max_continuations_per_task,
        max_additional_turns_per_continuation=(
            config.review.max_additional_turns_per_continuation
        ),
        manual_frontier_rounds_used=manual_frontier_rounds_used,
        max_manual_frontier_rounds=max_manual_frontier_rounds,
        eligible_actions=eligible_actions,
        audit_artifact_locations=audit_artifact_locations,
    )


__all__ = [
    "build_review_case",
    "task_slug",
    "read_agent_session",
    "read_local_power_session",
    "read_local_power_review_package",
    "read_local_stage_session",
    "continuation_additional_turns",
    "LOCAL_CONTINUATION_STARTED",
    "FRONTIER_CONTINUATION_STARTED",
    "MANUAL_FRONTIER_ROUND_CONSUMED_EVENT",
]
