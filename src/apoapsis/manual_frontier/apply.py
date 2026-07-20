from __future__ import annotations

from pathlib import Path

from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import ApoapsisConfig, CompletionPolicy
from apoapsis.manual_frontier.errors import (
    EligibilityError,
    PatchPolicyRejectedError,
    PreviewNotApprovedError,
    PreviewStaleError,
    TaskVersionMismatchError,
    WorktreeFingerprintMismatchError,
)
from apoapsis.manual_frontier.package import load_package, verify_package_integrity
from apoapsis.manual_frontier.schema import ManualFrontierPreviewStatus
from apoapsis.manual_frontier.store import ManualFrontierPreviewStore
from apoapsis.patches.apply import GitPatchApplier
from apoapsis.patches.parser import UnifiedDiffParser
from apoapsis.patches.validator import PatchPolicyValidator
from apoapsis.review.errors import ReviewError
from apoapsis.review.schema import ReviewCase
from apoapsis.verification.results import VerificationStatus
from apoapsis.verification.runner import VerificationRunner
from apoapsis.workflow.acceptance import (
    acceptance_coverage_satisfied,
    compute_acceptance_coverage,
)
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState


def execute_manual_frontier_apply(
    root: Path,
    task_store: SQLiteTaskStore,
    config: ApoapsisConfig,
    review_case: ReviewCase,
    expected_version: int,
    *,
    operation_id: str,
    preview_id: str | None,
) -> str:
    """Applies an operator-approved manual-frontier patch and runs
    verification (ADR 0031) -- dispatched from
    ``review.execution.run_review_operation`` exactly like every other
    review action, after that function has already re-projected a fresh
    ``ReviewCase`` and re-checked task version/eligibility/worktree
    fingerprint via ``_validate_operation_preconditions``. This function
    adds the manual-frontier-specific rechecks on top: the preview must
    exist, belong to this task, be explicitly ``APPROVED`` (the second of
    the two required approval steps -- the first, ``approve``, only ever
    records intent), and its own captured task version/worktree
    fingerprint/package hash must still match current state. Only the
    verification runner below ever decides completion; nothing in the
    imported response can claim it.
    """

    if preview_id is None:
        raise ReviewError("manual_frontier_handoff operation has no preview_id")
    assert review_case.worktree_path is not None
    task_id = review_case.task_id

    preview_store = ManualFrontierPreviewStore(
        root / ".apoapsis" / "manual-frontier-previews.db"
    )
    preview = preview_store.get(preview_id)
    if preview.task_id != task_id:
        raise EligibilityError(
            f"preview {preview_id} belongs to task {preview.task_id}, not {task_id}"
        )
    if preview.status != ManualFrontierPreviewStatus.APPROVED:
        raise PreviewNotApprovedError(
            f"preview {preview_id} is {preview.status.value}, not approved; "
            "run the approve step before apply"
        )
    if preview.task_version_at_import != review_case.task_version:
        raise TaskVersionMismatchError(
            f"preview {preview_id} was imported against task version "
            f"{preview.task_version_at_import}, but the task is now at "
            f"version {review_case.task_version}"
        )
    if preview.worktree_fingerprint_at_import != review_case.worktree_fingerprint:
        raise WorktreeFingerprintMismatchError(
            f"preview {preview_id} was imported against a different "
            "worktree fingerprint than the current one"
        )

    package = load_package(root, task_id, preview.package_id)
    if not verify_package_integrity(package):
        raise PreviewStaleError(
            f"package {preview.package_id} failed its own integrity check "
            "at apply time"
        )
    if package.worktree_fingerprint != review_case.worktree_fingerprint:
        raise WorktreeFingerprintMismatchError(
            f"package {preview.package_id} no longer matches the current "
            "worktree fingerprint"
        )

    parser = UnifiedDiffParser()
    parsed = parser.parse(preview.patch)
    validator = PatchPolicyValidator(config.patch)
    validation = validator.validate(parsed, review_case.worktree_path)
    audit = TaskAuditStore(root, task_id)
    audit.write_json(
        f"manual-frontier-apply-policy-{operation_id}.json",
        validation,
        kind="patch_policy",
    )
    if not validation.accepted:
        summary = "; ".join(item.message for item in validation.violations)
        raise PatchPolicyRejectedError(summary)

    started = task_store.transition(
        task_id,
        WorkflowState.IMPLEMENTING,
        actor=WorkflowActor.USER,
        event_type="manual_frontier_apply_started",
        payload={
            "reason": "human-approved manual subscription-frontier patch",
            "operation_id": operation_id,
            "preview_id": preview_id,
            "package_id": preview.package_id,
            "declared_model_name": preview.declared_model_name,
            "repair_round": package.repair_round,
        },
        expected_version=expected_version,
    )

    applier = GitPatchApplier()
    audit.write_text(
        f"manual-frontier-patch-{operation_id}.diff", parsed.raw, kind="manual_patch"
    )
    applier.apply(parsed, review_case.worktree_path)

    patch_ready = task_store.transition(
        task_id,
        WorkflowState.PATCH_READY,
        actor=WorkflowActor.SYSTEM,
        event_type="manual_frontier_patch_applied",
        payload={"operation_id": operation_id, "preview_id": preview_id},
        expected_version=started.version,
    )
    verifying = task_store.transition(
        task_id,
        WorkflowState.VERIFYING,
        actor=WorkflowActor.VERIFICATION_ENGINE,
        event_type="manual_frontier_verification_started",
        payload={"operation_id": operation_id},
        expected_version=patch_ready.version,
    )

    result = VerificationRunner(config.verification).run(
        task_id, review_case.worktree_path, attempt=1
    )
    audit.write_json(
        f"manual-frontier-verification-{operation_id}.json",
        result,
        kind="verification_result",
    )

    def _return_to_review(event_type: str, reason: str, *, expected_version: int) -> str:
        task_store.transition(
            task_id,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            actor=WorkflowActor.VERIFICATION_ENGINE,
            event_type=event_type,
            payload={
                "reason": reason,
                "operation_id": operation_id,
                "preview_id": preview_id,
            },
            expected_version=expected_version,
        )
        return reason

    if result.status != VerificationStatus.PASSED:
        return _return_to_review(
            "manual_frontier_apply_verification_failed",
            "configured verification failed after applying the manual-frontier patch",
            expected_version=verifying.version,
        )

    if config.execution.completion_policy == CompletionPolicy.STRICT:
        specification = task_store.get_task(task_id).specification
        command_results = {
            command.name: command.status
            for command in result.commands
            if command.status != VerificationStatus.SKIPPED
        }
        coverage = compute_acceptance_coverage(
            specification, config.verification.commands, command_results
        )
        if not acceptance_coverage_satisfied(coverage):
            return _return_to_review(
                "manual_frontier_apply_verification_failed",
                (
                    "configured verification passed but not every active "
                    "acceptance criterion is proven under the strict "
                    "completion policy"
                ),
                expected_version=verifying.version,
            )

    task_store.transition(
        task_id,
        WorkflowState.COMPLETE,
        actor=WorkflowActor.VERIFICATION_ENGINE,
        event_type="manual_frontier_verification_passed",
        payload={"operation_id": operation_id, "preview_id": preview_id},
        expected_version=verifying.version,
    )
    preview_store.mark_applied(preview_id)
    return "manual-frontier patch applied and verified; task marked COMPLETE"


__all__ = ["execute_manual_frontier_apply"]
