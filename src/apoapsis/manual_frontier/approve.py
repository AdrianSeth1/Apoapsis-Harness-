from __future__ import annotations

from pathlib import Path

from apoapsis.config import ApoapsisConfig
from apoapsis.manual_frontier.errors import (
    EligibilityError,
    PreviewStaleError,
    TaskVersionMismatchError,
)
from apoapsis.manual_frontier.schema import (
    ManualFrontierPreviewRecord,
    ManualFrontierPreviewStatus,
)
from apoapsis.manual_frontier.store import ManualFrontierPreviewStore
from apoapsis.review.case import build_review_case
from apoapsis.review.errors import ReviewCaseError
from apoapsis.review.schema import ReviewActionKind
from apoapsis.workflow.engine import SQLiteTaskStore


def approve_manual_frontier_preview(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    preview_store: ManualFrontierPreviewStore,
    config: ApoapsisConfig,
    *,
    task_id: str,
    preview_id: str,
    expected_task_version: int,
) -> ManualFrontierPreviewRecord:
    """The first of two required, explicit user-approval steps (ADR 0031):
    records that the user has reviewed the previewed patch and intends to
    apply it. Never mutates the worktree or task state -- only
    ``manual_frontier.apply.execute_manual_frontier_apply`` (invoked
    through the normal review-operation machinery, ADR 0020/0021) does
    that, and it independently re-checks everything here again immediately
    before applying, never trusting that nothing changed between the two
    steps.
    """

    root = Path(project_root).resolve()
    preview = preview_store.get(preview_id)
    if preview.task_id != task_id:
        raise EligibilityError(
            f"preview {preview_id} belongs to task {preview.task_id}, not {task_id}"
        )
    if preview.status != ManualFrontierPreviewStatus.PREVIEWED:
        raise PreviewStaleError(
            f"preview {preview_id} is {preview.status.value}, not previewed; "
            "import a fresh response if you need to approve a different patch"
        )
    try:
        review_case = build_review_case(root, task_store, config, task_id)
    except ReviewCaseError as exc:
        raise EligibilityError(str(exc)) from exc
    if review_case.task_version != expected_task_version:
        raise TaskVersionMismatchError(
            f"expected task version {expected_task_version}, found "
            f"{review_case.task_version}"
        )
    if preview.task_version_at_import != review_case.task_version:
        raise TaskVersionMismatchError(
            f"preview {preview_id} was imported against task version "
            f"{preview.task_version_at_import}, but the task is now at "
            f"version {review_case.task_version}; import a fresh response"
        )
    if preview.worktree_fingerprint_at_import != review_case.worktree_fingerprint:
        raise PreviewStaleError(
            f"preview {preview_id} was imported against a different "
            "worktree fingerprint than the current one; import a fresh "
            "response"
        )
    if ReviewActionKind.MANUAL_FRONTIER_HANDOFF not in review_case.eligible_actions:
        raise EligibilityError(
            f"manual_frontier_handoff is no longer eligible for {task_id}"
        )
    return preview_store.mark_approved(preview_id)


__all__ = ["approve_manual_frontier_preview"]
