from __future__ import annotations

from apoapsis.audit.store import AuditArtifact, TaskAuditStore
from apoapsis.config import AgentLoopConfig
from apoapsis.review.schema import (
    ContinuationBudget,
    ReviewActionKind,
    ReviewCase,
    ReviewContinuationPackage,
)
from apoapsis.specification.schema import TaskSpecification


def build_continuation_package(
    review_case: ReviewCase,
    specification: TaskSpecification,
    *,
    operation_id: str,
    action: ReviewActionKind,
    authorized_budget: ContinuationBudget,
    effective_agent_budget: AgentLoopConfig,
    verification_catalog: list[str],
) -> ReviewContinuationPackage:
    """Build the immutable record of everything a resumed model call is
    about to see, before any such call happens (ADR 0020). Requires the
    review case to have a worktree (diff, fingerprint, repository HEAD) --
    only LOCAL_CONTINUATION and FRONTIER_CONTINUATION ever call this, and
    both are only ever eligible when a worktree already exists."""

    assert review_case.current_diff is not None
    assert review_case.worktree_fingerprint is not None
    assert review_case.repository_head_commit is not None
    prior_turn_count = (
        review_case.consumed_frontier_turns
        if action == ReviewActionKind.FRONTIER_CONTINUATION
        else review_case.consumed_local_turns
    )
    return ReviewContinuationPackage(
        operation_id=operation_id,
        task_id=review_case.task_id,
        action=action,
        specification=specification,
        active_constraints=review_case.active_hard_constraints,
        current_diff=review_case.current_diff,
        stop_reason_kind=review_case.stop_reason_kind,
        stop_reason_text=review_case.stop_reason_text,
        prior_turn_count=prior_turn_count,
        normalized_failures=review_case.normalized_failures,
        verification_catalog=verification_catalog,
        authorized_budget=authorized_budget,
        effective_agent_budget=effective_agent_budget,
        worktree_fingerprint=review_case.worktree_fingerprint,
        repository_head_commit=review_case.repository_head_commit,
    )


def write_continuation_package(
    audit: TaskAuditStore, package: ReviewContinuationPackage
) -> AuditArtifact:
    return audit.write_json(
        f"review-continuation-{package.operation_id}.json",
        package,
        kind="review_continuation_package",
    )


__all__ = ["build_continuation_package", "write_continuation_package"]
