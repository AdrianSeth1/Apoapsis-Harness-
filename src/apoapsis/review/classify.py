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
) -> tuple[StopReasonKind, str, str]:
    """Classify the most recent HUMAN_REVIEW_REQUIRED-causing event.

    Returns ``(kind, event_type, reason_text)``. Scans the event history
    from newest to oldest for the most recent event whose ``to_state`` is
    HUMAN_REVIEW_REQUIRED and whose ``event_type`` is recognized; falls
    back to ``StopReasonKind.UNKNOWN`` (only inspect/abandon eligible) if
    nothing recognized is found, rather than guessing at a broader
    capability set than the harness can actually justify.
    """

    for event in reversed(events):
        if event.to_state != WorkflowState.HUMAN_REVIEW_REQUIRED:
            continue
        kind = _EVENT_TYPE_STOP_REASON.get(event.event_type)
        if kind is None:
            continue
        reason = (
            event.payload.get("reason")
            if isinstance(event.payload, dict)
            else None
        )
        text = reason if isinstance(reason, str) and reason else event.event_type
        return kind, event.event_type, text
    return (
        StopReasonKind.UNKNOWN,
        "unknown",
        "no recognized stop reason was found in the task's event history",
    )


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
