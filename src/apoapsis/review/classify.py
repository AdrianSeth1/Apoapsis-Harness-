from __future__ import annotations

from apoapsis.review.schema import ReviewActionKind, StopReasonKind
from apoapsis.workflow.events import WorkflowEvent
from apoapsis.workflow.states import WorkflowState

_EVENT_TYPE_STOP_REASON: dict[str, StopReasonKind] = {
    "specification_not_approved": StopReasonKind.SPECIFICATION_NOT_APPROVED,
    "deterministic_route_requires_human": StopReasonKind.ROUTING_REQUIRES_HUMAN,
    "acceptance_coverage_incomplete": StopReasonKind.ACCEPTANCE_COVERAGE_INCOMPLETE,
    "review_verification_retry_incomplete": (
        StopReasonKind.ACCEPTANCE_COVERAGE_INCOMPLETE
    ),
    "review_verification_retry_failed": StopReasonKind.VERIFICATION_FAILED,
    "frontier_escalation_not_configured": (
        StopReasonKind.LOCAL_AGENT_ESCALATION_UNAVAILABLE
    ),
    "review_local_continuation_requires_human": (
        StopReasonKind.LOCAL_AGENT_ESCALATION_UNAVAILABLE
    ),
    "bounded_frontier_requires_human": StopReasonKind.FRONTIER_AGENT_EXHAUSTED,
    "review_frontier_continuation_requires_human": (
        StopReasonKind.FRONTIER_AGENT_EXHAUSTED
    ),
    "review_frontier_stage_requires_human": StopReasonKind.FRONTIER_AGENT_EXHAUSTED,
    "manual_frontier_apply_verification_failed": StopReasonKind.VERIFICATION_FAILED,
}

_BASE_ELIGIBLE_ACTIONS: dict[StopReasonKind, tuple[ReviewActionKind, ...]] = {
    StopReasonKind.SPECIFICATION_NOT_APPROVED: (
        ReviewActionKind.INSPECT_ONLY,
        ReviewActionKind.ABANDON,
    ),
    StopReasonKind.ROUTING_REQUIRES_HUMAN: (
        ReviewActionKind.INSPECT_ONLY,
        ReviewActionKind.ABANDON,
    ),
    StopReasonKind.ACCEPTANCE_COVERAGE_INCOMPLETE: (
        ReviewActionKind.INSPECT_ONLY,
        ReviewActionKind.ABANDON,
        ReviewActionKind.VERIFICATION_ONLY_RETRY,
    ),
    StopReasonKind.VERIFICATION_FAILED: (
        ReviewActionKind.INSPECT_ONLY,
        ReviewActionKind.ABANDON,
        ReviewActionKind.VERIFICATION_ONLY_RETRY,
        ReviewActionKind.MANUAL_FRONTIER_HANDOFF,
    ),
    StopReasonKind.LOCAL_AGENT_ESCALATION_UNAVAILABLE: (
        ReviewActionKind.INSPECT_ONLY,
        ReviewActionKind.ABANDON,
        ReviewActionKind.VERIFICATION_ONLY_RETRY,
        ReviewActionKind.LOCAL_CONTINUATION,
        ReviewActionKind.AUTHORIZE_FRONTIER_STAGE,
        ReviewActionKind.MANUAL_FRONTIER_HANDOFF,
    ),
    StopReasonKind.FRONTIER_AGENT_EXHAUSTED: (
        ReviewActionKind.INSPECT_ONLY,
        ReviewActionKind.ABANDON,
        ReviewActionKind.VERIFICATION_ONLY_RETRY,
        ReviewActionKind.FRONTIER_CONTINUATION,
        ReviewActionKind.MANUAL_FRONTIER_HANDOFF,
    ),
    StopReasonKind.UNKNOWN: (
        ReviewActionKind.INSPECT_ONLY,
        ReviewActionKind.ABANDON,
    ),
}


def classify_stop_reason(
    events: list[WorkflowEvent],
) -> tuple[StopReasonKind, WorkflowEvent | None]:
    """Classify the task's stop reason from its *newest* transition into
    HUMAN_REVIEW_REQUIRED only (ADR 0021).

    Returns ``(kind, event)``. The newest such event alone decides the
    classification: if its ``event_type`` is unrecognized, the result is
    ``StopReasonKind.UNKNOWN`` -- this function never keeps scanning past
    the newest matching event to find an older, recognized one. Falling
    back to stale history would misclassify a task whose most recent stop
    reason simply isn't in this module's lookup table yet (for example, a
    future event type), rather than failing closed.
    """

    for event in reversed(events):
        if event.to_state != WorkflowState.HUMAN_REVIEW_REQUIRED:
            continue
        kind = _EVENT_TYPE_STOP_REASON.get(event.event_type, StopReasonKind.UNKNOWN)
        return kind, event
    return StopReasonKind.UNKNOWN, None


def eligible_actions_for(
    kind: StopReasonKind,
    *,
    frontier_available: bool,
    continuations_used: int,
    max_continuations_per_task: int,
    frontier_stage_exists: bool = False,
    manual_frontier_rounds_used: int = 0,
    max_manual_frontier_rounds: int = 0,
) -> list[ReviewActionKind]:
    """The deterministic, harness-computed eligible-action set for a stop
    reason -- filtered by current frontier availability (checked fresh,
    not from the stale routing decision that originally stopped the task),
    the configured per-task continuation ceiling, and whether a frontier
    stage already exists for this task (ADR 0022): ``authorize_frontier_
    stage`` is only ever offered when one does not yet exist (it starts a
    *fresh* one); ``frontier_continuation`` implicitly requires one to
    already exist, since it only appears under
    ``StopReasonKind.FRONTIER_AGENT_EXHAUSTED``, which is only ever reached
    once a frontier session has actually run."""

    actions = list(
        _BASE_ELIGIBLE_ACTIONS.get(kind, _BASE_ELIGIBLE_ACTIONS[StopReasonKind.UNKNOWN])
    )
    if not frontier_available:
        actions = [
            item
            for item in actions
            if item
            not in {
                ReviewActionKind.FRONTIER_CONTINUATION,
                ReviewActionKind.AUTHORIZE_FRONTIER_STAGE,
            }
        ]
    if frontier_stage_exists and ReviewActionKind.AUTHORIZE_FRONTIER_STAGE in actions:
        actions.remove(ReviewActionKind.AUTHORIZE_FRONTIER_STAGE)
    if continuations_used >= max_continuations_per_task:
        actions = [
            item
            for item in actions
            if item
            not in {
                ReviewActionKind.LOCAL_CONTINUATION,
                ReviewActionKind.FRONTIER_CONTINUATION,
            }
        ]
    if manual_frontier_rounds_used >= max_manual_frontier_rounds:
        actions = [
            item for item in actions if item != ReviewActionKind.MANUAL_FRONTIER_HANDOFF
        ]
    return actions
