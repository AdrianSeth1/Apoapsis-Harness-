from __future__ import annotations

from enum import StrEnum


class WorkflowState(StrEnum):
    INTAKE = "INTAKE"
    SPEC_DRAFTED = "SPEC_DRAFTED"
    SPEC_APPROVED = "SPEC_APPROVED"
    REPOSITORY_ANALYZED = "REPOSITORY_ANALYZED"
    CONTEXT_COMPILED = "CONTEXT_COMPILED"
    ROUTED = "ROUTED"
    IMPLEMENTING = "IMPLEMENTING"
    PATCH_READY = "PATCH_READY"
    VERIFYING = "VERIFYING"
    LOCAL_REPAIR = "LOCAL_REPAIR"
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.INTAKE: frozenset(
        {
            WorkflowState.SPEC_DRAFTED,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            WorkflowState.FAILED,
            WorkflowState.ROLLED_BACK,
        }
    ),
    WorkflowState.SPEC_DRAFTED: frozenset(
        {
            WorkflowState.SPEC_APPROVED,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            WorkflowState.FAILED,
            WorkflowState.ROLLED_BACK,
        }
    ),
    WorkflowState.SPEC_APPROVED: frozenset(
        {
            WorkflowState.REPOSITORY_ANALYZED,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            WorkflowState.FAILED,
            WorkflowState.ROLLED_BACK,
        }
    ),
    WorkflowState.REPOSITORY_ANALYZED: frozenset(
        {
            WorkflowState.CONTEXT_COMPILED,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            WorkflowState.FAILED,
            WorkflowState.ROLLED_BACK,
        }
    ),
    WorkflowState.CONTEXT_COMPILED: frozenset(
        {
            WorkflowState.ROUTED,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            WorkflowState.FAILED,
            WorkflowState.ROLLED_BACK,
        }
    ),
    WorkflowState.ROUTED: frozenset(
        {
            WorkflowState.IMPLEMENTING,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            WorkflowState.FAILED,
            WorkflowState.ROLLED_BACK,
        }
    ),
    WorkflowState.IMPLEMENTING: frozenset(
        {
            WorkflowState.PATCH_READY,
            WorkflowState.ESCALATION_REQUIRED,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            WorkflowState.FAILED,
            WorkflowState.ROLLED_BACK,
        }
    ),
    WorkflowState.PATCH_READY: frozenset(
        {
            WorkflowState.VERIFYING,
            WorkflowState.IMPLEMENTING,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            WorkflowState.FAILED,
            WorkflowState.ROLLED_BACK,
        }
    ),
    WorkflowState.VERIFYING: frozenset(
        {
            WorkflowState.COMPLETE,
            WorkflowState.LOCAL_REPAIR,
            WorkflowState.ESCALATION_REQUIRED,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            WorkflowState.FAILED,
            WorkflowState.ROLLED_BACK,
        }
    ),
    WorkflowState.LOCAL_REPAIR: frozenset(
        {
            WorkflowState.PATCH_READY,
            WorkflowState.ESCALATION_REQUIRED,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            WorkflowState.FAILED,
            WorkflowState.ROLLED_BACK,
        }
    ),
    WorkflowState.ESCALATION_REQUIRED: frozenset(
        {
            WorkflowState.IMPLEMENTING,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            WorkflowState.FAILED,
            WorkflowState.ROLLED_BACK,
        }
    ),
    WorkflowState.HUMAN_REVIEW_REQUIRED: frozenset(
        {
            WorkflowState.SPEC_DRAFTED,
            WorkflowState.SPEC_APPROVED,
            WorkflowState.IMPLEMENTING,
            WorkflowState.PATCH_READY,
            WorkflowState.VERIFYING,
            WorkflowState.FAILED,
            WorkflowState.ROLLED_BACK,
        }
    ),
    WorkflowState.COMPLETE: frozenset({WorkflowState.ROLLED_BACK}),
    WorkflowState.FAILED: frozenset(
        {
            WorkflowState.INTAKE,
            WorkflowState.IMPLEMENTING,
            WorkflowState.PATCH_READY,
            WorkflowState.ROLLED_BACK,
        }
    ),
    WorkflowState.ROLLED_BACK: frozenset(),
}


def transition_is_allowed(
    source: WorkflowState, target: WorkflowState
) -> bool:
    return target in ALLOWED_TRANSITIONS[source]

