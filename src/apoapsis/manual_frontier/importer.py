from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from pydantic import ValidationError

from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import ApoapsisConfig
from apoapsis.manual_frontier.errors import (
    EligibilityError,
    MalformedResponseError,
    PatchParseError,
    PatchPolicyRejectedError,
    ResponseHashMismatchError,
    ResponseTooLargeError,
    TaskVersionMismatchError,
    WorktreeFingerprintMismatchError,
)
from apoapsis.manual_frontier.package import load_package
from apoapsis.manual_frontier.schema import (
    ManualFrontierPreviewRecord,
    ManualFrontierPreviewStatus,
    ManualFrontierResponseEnvelope,
)
from apoapsis.manual_frontier.store import ManualFrontierPreviewStore
from apoapsis.patches.parser import UnifiedDiffError, UnifiedDiffParser
from apoapsis.patches.validator import PatchPolicyValidator
from apoapsis.review.case import build_review_case
from apoapsis.review.errors import ReviewCaseError
from apoapsis.review.schema import ReviewActionKind
from apoapsis.review.store import ReviewOperationStore
from apoapsis.specification.schema import utc_now
from apoapsis.workflow.engine import SQLiteTaskStore


def import_manual_frontier_response(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    preview_store: ManualFrontierPreviewStore,
    review_operation_store: ReviewOperationStore,
    config: ApoapsisConfig,
    *,
    task_id: str,
    package_id: str,
    response_bytes: bytes,
    declared_model_name: str,
    preview_id: str | None = None,
) -> ManualFrontierPreviewRecord:
    """Import and validate one pasted response, creating an immutable
    preview -- never applying anything (ADR 0031). Every check below runs
    fresh, every time, and fails closed on the first violation:

    1. the task must currently be at ``HUMAN_REVIEW_REQUIRED`` with
       ``MANUAL_FRONTIER_HANDOFF`` in its eligible actions (recomputed
       fresh, including the configured repair-round ceiling);
    2. no other review operation may already be active for this task
       (active-operation conflict);
    3. the referenced package must exist, pass its own integrity check,
       and its recorded task version/worktree fingerprint must match the
       task's *current* version/fingerprint exactly (a stale package
       requires a fresh export, never a forced import);
    4. the raw response must not exceed the configured byte ceiling,
       checked before any JSON parsing;
    5. the response must parse as JSON and validate against the strict,
       ``extra="forbid"`` envelope schema, and must echo back this exact
       package's id/hash/task id/task version;
    6. the patch must parse as a well-formed unified diff and pass
       deterministic repository patch policy.

    Nothing here touches the worktree or mutates task state.
    """

    root = Path(project_root).resolve()
    declared_model_name = declared_model_name.strip()
    if not declared_model_name:
        raise MalformedResponseError(
            "a declared model name is required -- manual subscription "
            "model identity is operator-declared provenance and is never "
            "inferred or defaulted"
        )

    try:
        review_case = build_review_case(root, task_store, config, task_id)
    except ReviewCaseError as exc:
        raise EligibilityError(str(exc)) from exc
    if ReviewActionKind.MANUAL_FRONTIER_HANDOFF not in review_case.eligible_actions:
        raise EligibilityError(
            f"manual_frontier_handoff is not currently eligible for {task_id} "
            f"(eligible actions: "
            f"{[item.value for item in review_case.eligible_actions]})"
        )
    if review_operation_store.find_active_for_task(task_id) is not None:
        raise EligibilityError(
            f"task {task_id} already has an active review operation; wait "
            "for it to finish or inspect it before importing a response"
        )

    package = load_package(root, task_id, package_id)
    if package.task_id != task_id:
        raise TaskVersionMismatchError(
            f"package {package_id} belongs to task {package.task_id}, not {task_id}"
        )
    if package.task_version != review_case.task_version:
        raise TaskVersionMismatchError(
            f"package {package_id} was exported for task version "
            f"{package.task_version}, but the task is now at version "
            f"{review_case.task_version}; export a fresh package"
        )
    if package.worktree_fingerprint != review_case.worktree_fingerprint:
        raise WorktreeFingerprintMismatchError(
            f"package {package_id} was exported for worktree fingerprint "
            f"{package.worktree_fingerprint}, but the worktree fingerprint "
            f"is now {review_case.worktree_fingerprint}; export a fresh package"
        )

    if len(response_bytes) > config.manual_frontier.max_response_bytes:
        raise ResponseTooLargeError(
            f"response is {len(response_bytes)} bytes; maximum is "
            f"{config.manual_frontier.max_response_bytes}"
        )
    try:
        raw_text = response_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedResponseError(f"response is not valid UTF-8: {exc}") from exc
    try:
        raw_payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise MalformedResponseError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(raw_payload, dict):
        raise MalformedResponseError("response must be a single JSON object")
    try:
        envelope = ManualFrontierResponseEnvelope.model_validate(raw_payload)
    except ValidationError as exc:
        raise MalformedResponseError(f"response failed schema validation: {exc}") from exc

    if envelope.package_id != package_id:
        raise ResponseHashMismatchError(
            f"response package_id {envelope.package_id!r} does not match "
            f"the imported package {package_id!r}"
        )
    if envelope.package_sha256 != package.package_sha256:
        raise ResponseHashMismatchError(
            "response package_sha256 does not match the package's own hash "
            "-- the response was not produced for this exact package"
        )
    if envelope.task_id != task_id:
        raise ResponseHashMismatchError(
            f"response task_id {envelope.task_id!r} does not match {task_id!r}"
        )
    if envelope.task_version != review_case.task_version:
        raise TaskVersionMismatchError(
            f"response task_version {envelope.task_version} does not match "
            f"the current task version {review_case.task_version}"
        )

    patch_bytes = envelope.patch.encode("utf-8")
    if len(patch_bytes) > config.manual_frontier.max_patch_bytes:
        raise ResponseTooLargeError(
            f"patch is {len(patch_bytes)} bytes; maximum is "
            f"{config.manual_frontier.max_patch_bytes}"
        )
    try:
        parsed = UnifiedDiffParser().parse(envelope.patch)
    except UnifiedDiffError as exc:
        raise PatchParseError(str(exc)) from exc

    assert review_case.worktree_path is not None
    validation = PatchPolicyValidator(config.patch).validate(
        parsed, review_case.worktree_path
    )
    if not validation.accepted:
        summary = "; ".join(item.message for item in validation.violations)
        raise PatchPolicyRejectedError(summary)

    preview_store.supersede_active_for_task(task_id)
    resolved_preview_id = preview_id or f"MFPV-{uuid.uuid4().hex}"
    record = ManualFrontierPreviewRecord(
        preview_id=resolved_preview_id,
        package_id=package_id,
        task_id=task_id,
        task_version_at_import=review_case.task_version,
        worktree_fingerprint_at_import=review_case.worktree_fingerprint,
        declared_model_name=declared_model_name,
        patch=parsed.raw,
        patch_sha256=hashlib.sha256(parsed.raw.encode("utf-8")).hexdigest(),
        summary=envelope.summary,
        files_changed=sorted(parsed.paths),
        changed_lines=parsed.changed_lines,
        status=ManualFrontierPreviewStatus.PREVIEWED,
        created_at=utc_now(),
    )
    created = preview_store.create(record)

    audit = TaskAuditStore(root, task_id)
    audit.write_json(
        f"manual-frontier-preview-{created.preview_id}.json",
        created,
        kind="manual_frontier_preview",
    )
    audit.write_json(
        f"manual-frontier-response-{created.preview_id}.json",
        envelope,
        kind="manual_frontier_response_envelope",
    )
    audit.write_json(
        f"manual-frontier-patch-policy-{created.preview_id}.json",
        validation,
        kind="patch_policy",
    )
    return created


__all__ = ["import_manual_frontier_response"]
