"""The single harness-owned projection of a task's *current* outcome and
verification evidence (ADR 0072).

``report.json`` is written exactly once, by ``_finalize_report`` at the
task's original stop, and is never updated afterwards. That is deliberate:
audit history is append-only, and the original stop is a fact worth keeping
verbatim. The defect this module exists to fix is that several surfaces
read that one-time snapshot as if it described the task *now*.

Crisis Atlas (``PLAN-E1B90639E58D``, 2026-07-29) is the live case. Its final
slice stopped at ``human_review_required`` with a failed verification, was
repaired by a hash-bound manual-frontier patch, re-verified, and reached a
persisted ``COMPLETE``. ``architect.delivery`` nevertheless serialized the
pre-repair snapshot into ``delivery.json`` and into the whole-project
frontier handoff, so the delivered record contradicted the workflow state
that had authorized the delivery in the first place.

The fix is not to rewrite ``report.json``. It is to compute current state
once, here, from three sources the harness owns outright:

1. persisted task state (``SQLiteTaskStore.get_task``) -- the only authority
   on what the outcome *is*;
2. the append-only event history -- the only authority on *which* stage
   produced the evidence behind that outcome; and
3. the immutable operation artifacts each stage wrote -- the only authority
   on *what that evidence says*.

Every consumer that labels a task outcome uses this projection: the Report
page, review-case construction, finished-plan delivery, the whole-project
frontier handoff, and the UI/CLI task summaries.

Fail-closed rule
----------------

When the event history says a newer evidence generation exists but its
artifact is missing or malformed, this module reports
``EvidenceIntegrity.MISSING``/``MALFORMED`` with **empty** verification
results. It never falls back to the older ``report.json``. Substituting an
old pass for unreadable new evidence is precisely how a stale green result
survives, and it is worse than an obviously unproven one: delivery can
refuse an unproven task, but it cannot detect a plausible lie.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, ValidationError

from apoapsis.agent.session import AgentSessionResult
from apoapsis.reporting.report import FinalTaskReport, TaskOutcome
from apoapsis.specification.schema import StrictModel, TaskSpecification
from apoapsis.verification.results import (
    VerificationResult,
    VerificationStatus,
)
from apoapsis.verification.runner import VerificationCommand
from apoapsis.workflow.acceptance import AcceptanceCoverage, compute_acceptance_coverage
from apoapsis.workflow.engine import SQLiteTaskStore, TaskRecord
from apoapsis.workflow.events import WorkflowEvent
from apoapsis.workflow.states import WorkflowState


class EvidenceGeneration(StrEnum):
    """Which artifact family holds the evidence behind the current state.

    Names the *source*, not the stage: ``evidence_event_type`` already
    records which transition produced it, and several different stages
    write into the same file. ``ORIGINAL_REPORT`` means nothing has
    superseded the first stop, so ``report.json`` is current and reading
    it is correct.
    """

    NONE = "none"
    ORIGINAL_REPORT = "original_report"
    VERIFICATION_RETRY = "verification_retry"
    MANUAL_FRONTIER_APPLY = "manual_frontier_apply"
    LOCAL_STAGE_SESSION = "local_stage_session"
    FRONTIER_STAGE_SESSION = "frontier_stage_session"


class EvidenceIntegrity(StrEnum):
    """Whether the artifacts the chosen generation promised were readable.

    Anything other than ``INTACT`` means the projection is reporting less
    than the task may actually have proven. That is the intended direction
    of error.
    """

    INTACT = "intact"
    MISSING = "missing"
    MALFORMED = "malformed"


# Terminal, outcome-bearing workflow states. A task in any other state is
# mid-flight: it has no current outcome to project, and claiming one would
# be a guess about a run still in progress.
_OUTCOME_FOR_STATE: dict[WorkflowState, TaskOutcome] = {
    WorkflowState.COMPLETE: TaskOutcome.COMPLETE,
    WorkflowState.FAILED: TaskOutcome.FAILED,
    WorkflowState.HUMAN_REVIEW_REQUIRED: TaskOutcome.HUMAN_REVIEW_REQUIRED,
}

# Every event that can be the decisive one for a terminal state, mapped to
# the artifact family holding its evidence. Unmapped decisive events fail
# closed rather than defaulting to the original report -- the same
# newest-event-wins discipline ADR 0021 applied to stop classification.
#
# The original run's own transitions resolve to `ORIGINAL_REPORT` because
# `_finalize_report` writes `report.json` at that exact moment: the report
# is only "stale" relative to stages that ran *after* it, and there are
# none yet. The two stop events that instead resolve to a session file are
# the agent-loop stops, where `report.verification_results` aggregates
# every stage's runs (`_record_agent_result` extends the list); a reviewer
# looking at the current stop needs that stage's own narrower evidence,
# which is exactly what `review.case` already did before this module
# existed.
_DECISIVE_EVENT_GENERATION: dict[str, EvidenceGeneration] = {
    # -- original one-shot / repair run ---------------------------------
    "verification_passed": EvidenceGeneration.ORIGINAL_REPORT,
    "repair_verification_passed": EvidenceGeneration.ORIGINAL_REPORT,
    "verification_failed": EvidenceGeneration.ORIGINAL_REPORT,
    "repair_budget_exhausted": EvidenceGeneration.ORIGINAL_REPORT,
    "acceptance_coverage_incomplete": EvidenceGeneration.ORIGINAL_REPORT,
    "specification_not_approved": EvidenceGeneration.ORIGINAL_REPORT,
    "deterministic_route_requires_human": EvidenceGeneration.ORIGINAL_REPORT,
    "vertical_slice_failed": EvidenceGeneration.ORIGINAL_REPORT,
    "review_local_stage_start_failed": EvidenceGeneration.ORIGINAL_REPORT,
    "review_frontier_run_start_failed": EvidenceGeneration.ORIGINAL_REPORT,
    # -- original bounded-agent / sandbox / frontier stages -------------
    "local_agent_verification_passed": EvidenceGeneration.ORIGINAL_REPORT,
    "local_power_sandbox_verification_passed": EvidenceGeneration.ORIGINAL_REPORT,
    "frontier_agent_verification_passed": EvidenceGeneration.ORIGINAL_REPORT,
    "frontier_escalation_not_configured": EvidenceGeneration.LOCAL_STAGE_SESSION,
    "bounded_frontier_requires_human": EvidenceGeneration.FRONTIER_STAGE_SESSION,
    # -- post-report review stages --------------------------------------
    "review_verification_retry_passed": EvidenceGeneration.VERIFICATION_RETRY,
    "review_verification_retry_failed": EvidenceGeneration.VERIFICATION_RETRY,
    "review_verification_retry_incomplete": EvidenceGeneration.VERIFICATION_RETRY,
    "manual_frontier_verification_passed": EvidenceGeneration.MANUAL_FRONTIER_APPLY,
    "manual_frontier_apply_verification_failed": (
        EvidenceGeneration.MANUAL_FRONTIER_APPLY
    ),
    "review_local_continuation_requires_human": (
        EvidenceGeneration.LOCAL_STAGE_SESSION
    ),
    "review_frontier_continuation_requires_human": (
        EvidenceGeneration.FRONTIER_STAGE_SESSION
    ),
    "review_frontier_stage_requires_human": EvidenceGeneration.FRONTIER_STAGE_SESSION,
    # Written identically by `_execute_continuation` (local *or* frontier)
    # and `_execute_frontier_stage`, so the completion event alone cannot
    # say which session file holds its evidence. Resolved from the newest
    # preceding *started* event instead; see `_resolve_stage_generation`.
    "review_continuation_verification_passed": EvidenceGeneration.NONE,
}

_STAGE_STARTED_GENERATION: dict[str, EvidenceGeneration] = {
    "review_local_continuation_started": EvidenceGeneration.LOCAL_STAGE_SESSION,
    "review_frontier_continuation_started": EvidenceGeneration.FRONTIER_STAGE_SESSION,
    "review_frontier_stage_started": EvidenceGeneration.FRONTIER_STAGE_SESSION,
}

_VERIFICATION_ARTIFACT_PREFIX: dict[EvidenceGeneration, str] = {
    EvidenceGeneration.VERIFICATION_RETRY: "review-verification-retry-",
    EvidenceGeneration.MANUAL_FRONTIER_APPLY: "manual-frontier-verification-",
}


class CurrentTaskEvidence(StrictModel):
    """Current outcome and evidence for one task, with its provenance.

    ``outcome`` comes from persisted workflow state, never from
    ``report.json``. ``original_report_outcome`` preserves what the first
    stop said, so a consumer can show both without either overwriting the
    other.
    """

    schema_version: str = "1.0"
    task_id: str
    task_version: int = Field(ge=0)
    workflow_state: WorkflowState
    outcome: TaskOutcome | None
    original_report_outcome: TaskOutcome | None
    supersedes_original_report: bool
    evidence_generation: EvidenceGeneration
    evidence_event_type: str | None
    evidence_event_sequence: int | None = Field(default=None, ge=1)
    evidence_sources: list[str] = Field(default_factory=list)
    evidence_integrity: EvidenceIntegrity
    evidence_integrity_detail: str | None = None
    verification_results: list[VerificationResult] = Field(default_factory=list)
    acceptance_coverage: list[AcceptanceCoverage] = Field(default_factory=list)
    reason: str

    @property
    def evidence_is_intact(self) -> bool:
        return self.evidence_integrity == EvidenceIntegrity.INTACT

    @property
    def current_verification_status(self) -> VerificationStatus | None:
        """Status of the run that decided the current outcome.

        The last element, matching the `[-1]` convention already used for
        this field in `agent.session`, `evaluation.report`, and
        `architect.delivery`. ``None`` when there is no readable evidence
        at all -- which a caller must not read as "passed".
        """

        if not self.verification_results:
            return None
        return self.verification_results[-1].status

    @property
    def is_verified_complete(self) -> bool:
        """The gate every delivery-shaped consumer should use.

        True only when the task is persistently COMPLETE *and* the evidence
        behind that completion was actually readable *and* that evidence
        records a pass. A COMPLETE task whose newer artifact went missing
        fails this deliberately: the harness cannot show what proved it.
        """

        return (
            self.outcome == TaskOutcome.COMPLETE
            and self.evidence_is_intact
            and self.current_verification_status == VerificationStatus.PASSED
        )

    def command_results(self) -> list[dict[str, object]]:
        """Per-command name/status/exit_code from the deciding run.

        `verification_results` holds aggregate runs; the per-command detail
        consumers display lives on each run's nested `.commands`.
        """

        if not self.verification_results:
            return []
        return [
            {
                "name": item.name,
                "status": item.status.value,
                "exit_code": item.exit_code,
            }
            for item in self.verification_results[-1].commands
        ]


def _read_report(task_directory: Path) -> tuple[FinalTaskReport | None, str | None]:
    path = task_directory / "report.json"
    if not path.is_file():
        return None, "report.json is not present"
    try:
        return (
            FinalTaskReport.model_validate_json(path.read_text(encoding="utf-8")),
            None,
        )
    except (OSError, ValidationError, ValueError) as exc:
        return None, f"report.json could not be validated as a FinalTaskReport: {exc}"


def _read_verification_artifact(
    path: Path,
) -> tuple[VerificationResult | None, EvidenceIntegrity, str | None]:
    if not path.is_file():
        return (
            None,
            EvidenceIntegrity.MISSING,
            f"{path.name} was recorded by the event history but is not present",
        )
    try:
        return (
            VerificationResult.model_validate_json(path.read_text(encoding="utf-8")),
            EvidenceIntegrity.INTACT,
            None,
        )
    except (OSError, ValidationError, ValueError) as exc:
        return (
            None,
            EvidenceIntegrity.MALFORMED,
            f"{path.name} could not be validated as a VerificationResult: {exc}",
        )


def _read_session_artifact(
    path: Path,
) -> tuple[AgentSessionResult | None, EvidenceIntegrity, str | None]:
    if not path.is_file():
        return (
            None,
            EvidenceIntegrity.MISSING,
            f"{path.name} was recorded by the event history but is not present",
        )
    try:
        return (
            AgentSessionResult.model_validate_json(path.read_text(encoding="utf-8")),
            EvidenceIntegrity.INTACT,
            None,
        )
    except (OSError, ValidationError, ValueError) as exc:
        return (
            None,
            EvidenceIntegrity.MALFORMED,
            f"{path.name} could not be validated as an AgentSessionResult: {exc}",
        )


def _session_paths(
    task_directory: Path, generation: EvidenceGeneration
) -> list[Path]:
    """Candidate session files for a stage generation, in preference order.

    A local stage may have run under either the strict bounded loop
    (``agent-session.json``) or the Local Power sandbox
    (``local-power-session.json``); only one exists, and which one is a
    configuration fact, not evidence. Both are offered so that neither
    counts as missing evidence when the other is present. The strict
    loop's record is preferred when both somehow exist, matching
    `review.case.read_local_stage_session`.
    """

    if generation == EvidenceGeneration.LOCAL_STAGE_SESSION:
        return [
            task_directory / "agent-session.json",
            task_directory / "local-power-session.json",
        ]
    return [task_directory / "frontier-agent-session.json"]


def coverage_from_verification_result(
    specification: TaskSpecification, result: VerificationResult
) -> list[AcceptanceCoverage]:
    """Recompute acceptance coverage from an immutable verification result.

    Deliberately does not consult live configuration. Each
    ``VerificationCommandResult`` already carries the ``required`` and
    ``acceptance`` flags that were in force when it ran (ADR 0018), so
    coverage for a historical run can be projected from the run itself.
    Reading today's ``config.verification.commands`` instead would let an
    edit made after the fact silently change what a past run is said to
    have proven.

    ``SKIPPED`` commands are dropped before the status map is built, for
    the same reason `compute_acceptance_coverage` requires it: a command
    that was skipped never executed, and "never executed" and "executed and
    failed" are different evidentiary states.
    """

    configured = [
        VerificationCommand(
            name=item.name,
            category=item.category,
            argv=item.argv,
            required=item.required,
            acceptance=item.acceptance,
        )
        for item in result.commands
    ]
    command_results = {
        item.name: item.status
        for item in result.commands
        if item.status != VerificationStatus.SKIPPED
    }
    return compute_acceptance_coverage(specification, configured, command_results)


def _coverage_from_event_payload(event: WorkflowEvent) -> list[AcceptanceCoverage] | None:
    """Coverage carried on a stop event's own payload, when present.

    ``review_verification_retry_incomplete`` and the STRICT rejection path
    of ``manual_frontier_apply_verification_failed`` both serialize the
    exact coverage that caused the stop, so it is read back rather than
    recomputed. Returns ``None`` when the payload carries none, which is
    not an error -- most events do not.
    """

    payload = event.payload if isinstance(event.payload, dict) else {}
    raw = payload.get("coverage")
    if not isinstance(raw, list):
        return None
    try:
        return [AcceptanceCoverage.model_validate(item) for item in raw]
    except (ValidationError, ValueError):
        return None


def _decisive_event(
    events: list[WorkflowEvent], state: WorkflowState
) -> WorkflowEvent | None:
    """The newest event that moved the task into its current state.

    Same newest-event-wins discipline as `review.classify
    .classify_stop_reason` (ADR 0021): scanning past it to find an older
    recognized event would describe a stage that has since been superseded.
    """

    for event in reversed(events):
        if event.to_state == state:
            return event
    return None


def _resolve_stage_generation(
    events: list[WorkflowEvent], decisive: WorkflowEvent
) -> EvidenceGeneration:
    """Which stage a ``review_continuation_verification_passed`` came from.

    That one event type is written by the local continuation, the frontier
    continuation, and the fresh frontier stage alike, so the completion
    event alone cannot say which session file holds its evidence. The
    newest *started* event at or before it can. Returns ``NONE`` when no
    started event precedes the completion, which the caller treats as
    unlocatable evidence rather than guessing at a default.
    """

    index = len(events)
    for position, event in enumerate(events):
        if event is decisive:
            index = position
            break
    for event in reversed(events[:index]):
        generation = _STAGE_STARTED_GENERATION.get(event.event_type)
        if generation is not None:
            return generation
    return EvidenceGeneration.NONE


def _event_reason(event: WorkflowEvent | None) -> str | None:
    if event is None:
        return None
    payload = event.payload if isinstance(event.payload, dict) else {}
    for key in ("reason", "stop_reason"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def project_current_task_evidence(
    project_root: str | Path,
    store: SQLiteTaskStore,
    task_id: str,
    *,
    record: TaskRecord | None = None,
    events: list[WorkflowEvent] | None = None,
) -> CurrentTaskEvidence:
    """Project one task's current outcome, verification, and coverage.

    ``record``/``events`` may be supplied by a caller that has already read
    them (``review.case`` has), purely to avoid a redundant store round
    trip. Passing a *stale* pair would defeat the point of this function,
    so callers that care about concurrency must read them immediately
    before calling and compare ``task_version`` afterwards.
    """

    root = Path(project_root).resolve()
    record = record if record is not None else store.get_task(task_id)
    events = events if events is not None else store.events(task_id)
    task_directory = root / ".apoapsis" / "tasks" / task_id

    report, report_problem = _read_report(task_directory)
    original_outcome = report.outcome if report is not None else None
    outcome = _OUTCOME_FOR_STATE.get(record.state)

    def _build(
        *,
        generation: EvidenceGeneration,
        decisive: WorkflowEvent | None,
        sources: list[str],
        integrity: EvidenceIntegrity,
        detail: str | None,
        verification_results: list[VerificationResult],
        acceptance_coverage: list[AcceptanceCoverage],
        reason: str,
    ) -> CurrentTaskEvidence:
        return CurrentTaskEvidence(
            task_id=record.task_id,
            task_version=record.version,
            workflow_state=record.state,
            outcome=outcome,
            original_report_outcome=original_outcome,
            supersedes_original_report=(
                generation
                not in {EvidenceGeneration.NONE, EvidenceGeneration.ORIGINAL_REPORT}
            ),
            evidence_generation=generation,
            evidence_event_type=(
                decisive.event_type if decisive is not None else None
            ),
            evidence_event_sequence=(
                decisive.sequence if decisive is not None else None
            ),
            evidence_sources=sources,
            evidence_integrity=integrity,
            evidence_integrity_detail=detail,
            verification_results=verification_results,
            acceptance_coverage=acceptance_coverage,
            reason=reason,
        )

    if outcome is None:
        # Either mid-flight or ROLLED_BACK, neither of which `TaskOutcome`
        # can express. There is no current outcome to report, and inventing
        # one from the last finished stage would describe a run that has
        # since moved on. `evidence_integrity` stays INTACT because nothing
        # was promised and nothing is missing -- callers gate on
        # `is_verified_complete`, which a null outcome already fails.
        return _build(
            generation=EvidenceGeneration.NONE,
            decisive=None,
            sources=[],
            integrity=EvidenceIntegrity.INTACT,
            detail=None,
            verification_results=[],
            acceptance_coverage=[],
            reason=(
                f"task is at {record.state.value}, which carries no terminal "
                "task outcome"
            ),
        )

    decisive = _decisive_event(events, record.state)
    decisive_type = decisive.event_type if decisive is not None else None

    generation = EvidenceGeneration.NONE
    if decisive_type is not None and decisive_type in _DECISIVE_EVENT_GENERATION:
        generation = _DECISIVE_EVENT_GENERATION[decisive_type]
        if generation == EvidenceGeneration.NONE:
            assert decisive is not None
            generation = _resolve_stage_generation(events, decisive)

    if generation == EvidenceGeneration.NONE:
        # An unrecognized decisive event. Failing closed here rather than
        # defaulting to the original report is the same reasoning ADR 0021
        # applied to stop classification: an event type this table has not
        # been taught about is not evidence that the first stop still
        # stands, and a future stage must not inherit an old pass by
        # default.
        detail = (
            f"decisive event {decisive_type!r} is not mapped to an evidence "
            "generation; current verification evidence cannot be located"
            if decisive_type is not None
            else (
                f"task is {record.state.value} but no event recorded that "
                "transition; current verification evidence cannot be located"
            )
        )
        return _build(
            generation=EvidenceGeneration.NONE,
            decisive=decisive,
            sources=[],
            integrity=EvidenceIntegrity.MISSING,
            detail=detail,
            verification_results=[],
            acceptance_coverage=[],
            reason=_event_reason(decisive) or detail,
        )

    if generation == EvidenceGeneration.ORIGINAL_REPORT:
        if report is None:
            return _build(
                generation=generation,
                decisive=decisive,
                sources=[],
                integrity=(
                    EvidenceIntegrity.MISSING
                    if report_problem == "report.json is not present"
                    else EvidenceIntegrity.MALFORMED
                ),
                detail=report_problem,
                verification_results=[],
                acceptance_coverage=[],
                reason=_event_reason(decisive) or (report_problem or "unknown"),
            )
        relative = (task_directory / "report.json").relative_to(root).as_posix()
        return _build(
            generation=generation,
            decisive=decisive,
            sources=[relative],
            integrity=EvidenceIntegrity.INTACT,
            detail=None,
            verification_results=report.verification_results,
            acceptance_coverage=report.acceptance_coverage,
            reason=(
                report.error
                or _event_reason(decisive)
                or f"task reached {outcome.value} on its original run"
            ),
        )

    assert decisive is not None
    payload = decisive.payload if isinstance(decisive.payload, dict) else {}

    if generation in {
        EvidenceGeneration.VERIFICATION_RETRY,
        EvidenceGeneration.MANUAL_FRONTIER_APPLY,
    }:
        operation_id = payload.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            detail = (
                f"{decisive.event_type} carries no operation_id, so the "
                "verification artifact it produced cannot be identified"
            )
            return _build(
                generation=generation,
                decisive=decisive,
                sources=[],
                integrity=EvidenceIntegrity.MISSING,
                detail=detail,
                verification_results=[],
                acceptance_coverage=[],
                reason=_event_reason(decisive) or detail,
            )
        path = (
            task_directory
            / f"{_VERIFICATION_ARTIFACT_PREFIX[generation]}{operation_id}.json"
        )
        result, integrity, detail = _read_verification_artifact(path)
        if result is None:
            return _build(
                generation=generation,
                decisive=decisive,
                sources=[],
                integrity=integrity,
                detail=detail,
                verification_results=[],
                acceptance_coverage=[],
                reason=_event_reason(decisive) or (detail or "unknown"),
            )
        coverage = _coverage_from_event_payload(decisive)
        if coverage is None:
            coverage = coverage_from_verification_result(record.specification, result)
        return _build(
            generation=generation,
            decisive=decisive,
            sources=[path.relative_to(root).as_posix()],
            integrity=EvidenceIntegrity.INTACT,
            detail=None,
            verification_results=[result],
            acceptance_coverage=coverage,
            reason=(
                _event_reason(decisive)
                or f"task reached {outcome.value} via {generation.value}"
            ),
        )

    candidates = _session_paths(task_directory, generation)
    present = [path for path in candidates if path.is_file()]
    if not present:
        names = " or ".join(path.name for path in candidates)
        detail = (
            f"{decisive.event_type} recorded a {generation.value} stage, but "
            f"{names} is not present"
        )
        return _build(
            generation=generation,
            decisive=decisive,
            sources=[],
            integrity=EvidenceIntegrity.MISSING,
            detail=detail,
            verification_results=[],
            acceptance_coverage=[],
            reason=_event_reason(decisive) or detail,
        )
    session, integrity, detail = _read_session_artifact(present[0])
    if session is None:
        return _build(
            generation=generation,
            decisive=decisive,
            sources=[],
            integrity=integrity,
            detail=detail,
            verification_results=[],
            acceptance_coverage=[],
            reason=_event_reason(decisive) or (detail or "unknown"),
        )
    return _build(
        generation=generation,
        decisive=decisive,
        sources=[present[0].relative_to(root).as_posix()],
        integrity=EvidenceIntegrity.INTACT,
        detail=None,
        verification_results=session.verification_results,
        acceptance_coverage=session.acceptance_coverage,
        reason=(
            _event_reason(decisive)
            or session.stop_reason
            or f"task reached {outcome.value} via {generation.value}"
        ),
    )


__all__ = [
    "CurrentTaskEvidence",
    "EvidenceGeneration",
    "EvidenceIntegrity",
    "coverage_from_verification_result",
    "project_current_task_evidence",
]
