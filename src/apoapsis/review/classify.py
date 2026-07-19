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
    ),
    StopReasonKind.LOCAL_AGENT_ESCALATION_UNAVAILABLE: (
        ReviewActionKind.INSPECT_ONLY,
        ReviewActionKind.ABANDON,
        ReviewActionKind.VERIFICATION_ONLY_RETRY,
        ReviewActionKind.LOCAL_CONTINUATION,
    ),
    StopReasonKind.FRONTIER_AGENT_EXHAUSTED: (
        ReviewActionKind.INSPECT_ONLY,
        ReviewActionKind.ABANDON,
        ReviewActionKind.VERIFICATION_ONLY_RETRY,
        ReviewActionKind.FRONTIER_CONTINUATION,
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
) -> list[ReviewActionKind]:
    """The deterministic, harness-computed eligible-action set for a stop
    reason -- filtered by current frontier availability (checked fresh,
    not from the stale routing decision that originally stopped the task)
    and the configured per-task continuation ceiling."""

    actions = list(
        _BASE_ELIGIBLE_ACTIONS.get(kind, _BASE_ELIGIBLE_ACTIONS[StopReasonKind.UNKNOWN])
    )
    if not frontier_available and ReviewActionKind.FRONTIER_CONTINUATION in actions:
        actions.remove(ReviewActionKind.FRONTIER_CONTINUATION)
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
    return actions
