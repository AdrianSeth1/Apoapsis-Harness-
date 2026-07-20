from __future__ import annotations

import json
import unittest

from apoapsis.manual_frontier.approve import approve_manual_frontier_preview
from apoapsis.manual_frontier.errors import (
    EligibilityError,
    MalformedResponseError,
    PatchPolicyRejectedError,
    ResponseHashMismatchError,
    ResponseTooLargeError,
    TaskVersionMismatchError,
    WorktreeFingerprintMismatchError,
)
from apoapsis.manual_frontier.importer import import_manual_frontier_response
from apoapsis.manual_frontier.package import (
    build_manual_frontier_handoff_package,
    load_package,
    verify_package_integrity,
    write_handoff_artifacts,
)
from apoapsis.manual_frontier.schema import (
    ManualFrontierPreviewRecord,
    ManualFrontierPreviewStatus,
    ManualFrontierResponseEnvelope,
)
from apoapsis.manual_frontier.store import ManualFrontierPreviewStore
from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import ManualFrontierConfig
from apoapsis.review.case import build_review_case
from apoapsis.review.errors import ActiveOperationExistsError, InvalidReviewActionError
from apoapsis.review.execution import execute_review_action
from apoapsis.review.schema import ReviewActionKind, StopReasonKind
from apoapsis.workflow.states import WorkflowState
from apoapsis.reporting.report import TaskOutcome
from tests.test_agent_loop import action
from tests.test_review_execution import ReviewExecutionTestsBase
from tests.test_vertical_slice import specification_response


class _ManualFrontierTestBase(ReviewExecutionTestsBase):
    """Shared helper mirroring
    ``test_review_execution.LocalContinuationTests._escalate_locally``:
    drives a real local agent session until it exhausts its turn budget
    without ever calling verification, landing at ``HUMAN_REVIEW_REQUIRED``
    under ``LOCAL_AGENT_ESCALATION_UNAVAILABLE`` -- exactly the stop
    manual-frontier handoff is meant for."""

    def _escalate_locally(self, config) -> str:
        outputs = [
            specification_response(),
            action("search_repository", query="get_offset"),
            action("search_repository", query="downloader"),
            action("search_repository", query="jobs"),
        ]
        report = self._run(outputs, config)
        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)
        return report.task_id


def _envelope_bytes(package, *, patch: str, task_version: int | None = None, **overrides) -> bytes:
    payload = {
        "package_id": package.package_id,
        "package_sha256": package.package_sha256,
        "task_id": package.task_id,
        "task_version": task_version if task_version is not None else package.task_version,
        "patch": patch,
        "summary": "fixed it",
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


class ManualFrontierPackageTests(_ManualFrontierTestBase):
    def test_package_hash_is_deterministic_and_excludes_identifiers(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        specification = self.store.get_task(task_id).specification

        first = build_manual_frontier_handoff_package(
            case, specification, config.verification.commands, package_id="MFH-fixed"
        )
        second = build_manual_frontier_handoff_package(
            case, specification, config.verification.commands, package_id="MFH-different"
        )
        self.assertEqual(first.package_sha256, second.package_sha256)
        self.assertTrue(verify_package_integrity(first))

    def test_tampered_package_fails_integrity_check(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        specification = self.store.get_task(task_id).specification
        package = build_manual_frontier_handoff_package(
            case, specification, config.verification.commands
        )
        tampered = package.model_copy(update={"current_diff": "tampered"})
        self.assertFalse(verify_package_integrity(tampered))

    def test_response_schema_forbids_extra_fields(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        specification = self.store.get_task(task_id).specification
        package = build_manual_frontier_handoff_package(
            case, specification, config.verification.commands
        )
        payload = json.loads(
            _envelope_bytes(package, patch="diff --git a/x b/x\n")
        )
        payload["status"] = "complete"  # a model cannot claim completion
        with self.assertRaises(Exception):
            ManualFrontierResponseEnvelope.model_validate(payload)

    def test_markdown_and_json_artifacts_are_written(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        specification = self.store.get_task(task_id).specification
        package = build_manual_frontier_handoff_package(
            case, specification, config.verification.commands
        )
        audit = TaskAuditStore(self.root, task_id)
        json_artifact, markdown_artifact = write_handoff_artifacts(audit, package)
        self.assertTrue((self.root / json_artifact.path).is_file())
        self.assertTrue((self.root / markdown_artifact.path).is_file())
        markdown = (self.root / markdown_artifact.path).read_text(encoding="utf-8")
        self.assertIn(package.package_id, markdown)
        self.assertIn(package.package_sha256, markdown)
        reloaded = load_package(self.root, task_id, package.package_id)
        self.assertEqual(reloaded.package_sha256, package.package_sha256)


class ManualFrontierPreviewStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = ManualFrontierPreviewStore(
            Path(self.temporary_directory.name) / "previews.db"
        )

    def _record(self, **overrides):
        from apoapsis.specification.schema import utc_now

        values = dict(
            preview_id="MFPV-1",
            package_id="MFH-1",
            task_id="TASK-1",
            task_version_at_import=1,
            worktree_fingerprint_at_import="fp-1",
            declared_model_name="claude-opus-4.6-web",
            patch="diff --git a/x b/x\n",
            patch_sha256="0" * 64,
            summary="",
            files_changed=["x"],
            changed_lines=1,
            status=ManualFrontierPreviewStatus.PREVIEWED,
            created_at=utc_now(),
        )
        values.update(overrides)
        return ManualFrontierPreviewRecord(**values)

    def test_approve_then_apply_transitions(self) -> None:
        self.store.create(self._record())
        approved = self.store.mark_approved("MFPV-1")
        self.assertEqual(approved.status, ManualFrontierPreviewStatus.APPROVED)
        applied = self.store.mark_applied("MFPV-1")
        self.assertEqual(applied.status, ManualFrontierPreviewStatus.APPLIED)

    def test_double_approve_rejected(self) -> None:
        self.store.create(self._record())
        self.store.mark_approved("MFPV-1")
        with self.assertRaises(Exception):
            self.store.mark_approved("MFPV-1")

    def test_supersede_marks_previewed_and_approved_only(self) -> None:
        self.store.create(self._record())
        self.store.supersede_active_for_task("TASK-1")
        record = self.store.get("MFPV-1")
        self.assertEqual(record.status, ManualFrontierPreviewStatus.SUPERSEDED)


class ManualFrontierImporterTests(_ManualFrontierTestBase):
    def _export(self, config, task_id):
        case = build_review_case(self.root, self.store, config, task_id)
        specification = self.store.get_task(task_id).specification
        package = build_manual_frontier_handoff_package(
            case, specification, config.verification.commands
        )
        write_handoff_artifacts(TaskAuditStore(self.root, task_id), package)
        return package

    def _stores(self, config):
        preview_store = ManualFrontierPreviewStore(
            self.root / ".apoapsis" / "manual-frontier-previews.db"
        )
        return preview_store

    def test_successful_import_creates_previewed_record(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        package = self._export(config, task_id)
        preview_store = self._stores(config)

        preview = import_manual_frontier_response(
            self.root,
            self.store,
            preview_store,
            self.operation_store,
            config,
            task_id=task_id,
            package_id=package.package_id,
            response_bytes=_envelope_bytes(
                package, patch="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
            ),
            declared_model_name="claude-opus-4.6-web",
            preview_id="MFPV-OK",
        )
        self.assertEqual(preview.status, ManualFrontierPreviewStatus.PREVIEWED)
        self.assertEqual(preview.declared_model_name, "claude-opus-4.6-web")

    def test_stale_task_version_is_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        package = self._export(config, task_id)
        preview_store = self._stores(config)

        with self.assertRaises(TaskVersionMismatchError):
            import_manual_frontier_response(
                self.root,
                self.store,
                preview_store,
                self.operation_store,
                config,
                task_id=task_id,
                package_id=package.package_id,
                response_bytes=_envelope_bytes(
                    package, patch="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
                    task_version=package.task_version + 1,
                ),
                declared_model_name="claude-opus-4.6-web",
                preview_id="MFPV-STALE-VERSION",
            )

    def test_stale_worktree_fingerprint_is_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        package = self._export(config, task_id)
        preview_store = self._stores(config)

        # Mutate the worktree after export -- the fingerprint changes, but
        # the (still task-version-valid) package now refers to a stale one.
        case = build_review_case(self.root, self.store, config, task_id)
        assert case.worktree_path is not None
        from pathlib import Path

        (Path(case.worktree_path) / "untracked-drift.txt").write_text(
            "drift", encoding="utf-8"
        )

        with self.assertRaises(WorktreeFingerprintMismatchError):
            import_manual_frontier_response(
                self.root,
                self.store,
                preview_store,
                self.operation_store,
                config,
                task_id=task_id,
                package_id=package.package_id,
                response_bytes=_envelope_bytes(
                    package, patch="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
                ),
                declared_model_name="claude-opus-4.6-web",
                preview_id="MFPV-STALE-FP",
            )

    def test_malformed_json_is_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        package = self._export(config, task_id)
        preview_store = self._stores(config)

        with self.assertRaises(MalformedResponseError):
            import_manual_frontier_response(
                self.root,
                self.store,
                preview_store,
                self.operation_store,
                config,
                task_id=task_id,
                package_id=package.package_id,
                response_bytes=b"not json at all {{{",
                declared_model_name="claude-opus-4.6-web",
                preview_id="MFPV-MALFORMED",
            )

    def test_extra_field_in_response_is_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        package = self._export(config, task_id)
        preview_store = self._stores(config)

        with self.assertRaises(MalformedResponseError):
            import_manual_frontier_response(
                self.root,
                self.store,
                preview_store,
                self.operation_store,
                config,
                task_id=task_id,
                package_id=package.package_id,
                response_bytes=_envelope_bytes(
                    package,
                    patch="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
                    status="complete",
                ),
                declared_model_name="claude-opus-4.6-web",
                preview_id="MFPV-EXTRA",
            )

    def test_response_too_large_is_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        config = config.model_copy(
            update={"manual_frontier": ManualFrontierConfig(max_response_bytes=1000)}
        )
        task_id = self._escalate_locally(config)
        package = self._export(config, task_id)
        preview_store = self._stores(config)

        oversized_summary = "x" * 5000
        with self.assertRaises(ResponseTooLargeError):
            import_manual_frontier_response(
                self.root,
                self.store,
                preview_store,
                self.operation_store,
                config,
                task_id=task_id,
                package_id=package.package_id,
                response_bytes=_envelope_bytes(
                    package, patch="diff --git a/x b/x\n", summary=oversized_summary
                ),
                declared_model_name="claude-opus-4.6-web",
                preview_id="MFPV-TOO-LARGE",
            )

    def test_hash_mismatch_is_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        package = self._export(config, task_id)
        preview_store = self._stores(config)

        with self.assertRaises(ResponseHashMismatchError):
            import_manual_frontier_response(
                self.root,
                self.store,
                preview_store,
                self.operation_store,
                config,
                task_id=task_id,
                package_id=package.package_id,
                response_bytes=_envelope_bytes(
                    package,
                    patch="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
                    package_sha256="0" * 64,
                ),
                declared_model_name="claude-opus-4.6-web",
                preview_id="MFPV-HASH-MISMATCH",
            )

    def test_malformed_patch_is_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        package = self._export(config, task_id)
        preview_store = self._stores(config)

        with self.assertRaises(Exception):
            import_manual_frontier_response(
                self.root,
                self.store,
                preview_store,
                self.operation_store,
                config,
                task_id=task_id,
                package_id=package.package_id,
                response_bytes=_envelope_bytes(package, patch="this is not a diff"),
                declared_model_name="claude-opus-4.6-web",
                preview_id="MFPV-BAD-PATCH",
            )

    def test_patch_policy_rejection_for_oversized_patch(self) -> None:
        config = self._agent_config(local_turns=3)
        config = config.model_copy(update={"patch": config.patch.model_copy(update={"max_changed_lines": 1})})
        task_id = self._escalate_locally(config)
        package = self._export(config, task_id)
        preview_store = self._stores(config)

        big_patch = (
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n"
            "+++ b/README.md\n"
            "@@ -1,2 +1,4 @@\n"
            " line one\n"
            "+added one\n"
            "+added two\n"
            " line two\n"
        )
        with self.assertRaises(PatchPolicyRejectedError):
            import_manual_frontier_response(
                self.root,
                self.store,
                preview_store,
                self.operation_store,
                config,
                task_id=task_id,
                package_id=package.package_id,
                response_bytes=_envelope_bytes(package, patch=big_patch),
                declared_model_name="claude-opus-4.6-web",
                preview_id="MFPV-POLICY",
            )

    def test_no_longer_eligible_after_task_moves_on_rejects_import(self) -> None:
        """A stale/replayed response against a task that already left
        HUMAN_REVIEW_REQUIRED must be rejected, not silently reapplied."""

        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        package = self._export(config, task_id)
        preview_store = self._stores(config)
        record = self.store.get_task(task_id)
        from apoapsis.workflow.events import WorkflowActor

        self.store.transition(
            task_id,
            WorkflowState.ROLLED_BACK,
            actor=WorkflowActor.USER,
            event_type="test_abandon",
            expected_version=record.version,
        )

        with self.assertRaises(EligibilityError):
            import_manual_frontier_response(
                self.root,
                self.store,
                preview_store,
                self.operation_store,
                config,
                task_id=task_id,
                package_id=package.package_id,
                response_bytes=_envelope_bytes(
                    package, patch="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
                ),
                declared_model_name="claude-opus-4.6-web",
                preview_id="MFPV-REPLAY",
            )


class ManualFrontierApplyTests(_ManualFrontierTestBase):
    def _prepare_approved_preview(self, config, task_id, patch: str, *, preview_id: str):
        preview_store = ManualFrontierPreviewStore(
            self.root / ".apoapsis" / "manual-frontier-previews.db"
        )
        case = build_review_case(self.root, self.store, config, task_id)
        specification = self.store.get_task(task_id).specification
        package = build_manual_frontier_handoff_package(
            case, specification, config.verification.commands
        )
        write_handoff_artifacts(TaskAuditStore(self.root, task_id), package)
        preview = import_manual_frontier_response(
            self.root,
            self.store,
            preview_store,
            self.operation_store,
            config,
            task_id=task_id,
            package_id=package.package_id,
            response_bytes=_envelope_bytes(package, patch=patch),
            declared_model_name="claude-opus-4.6-web",
            preview_id=preview_id,
        )
        approved = approve_manual_frontier_preview(
            self.root,
            self.store,
            preview_store,
            config,
            task_id=task_id,
            preview_id=preview.preview_id,
            expected_task_version=case.task_version,
        )
        return preview_store, approved, case

    def test_successful_apply_is_verifier_owned_completion(self) -> None:
        from tests.test_vertical_slice import COMPLETE_PATCH

        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        preview_store, approved, case = self._prepare_approved_preview(
            config, task_id, COMPLETE_PATCH, preview_id="MFPV-COMPLETE"
        )

        record = execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.MANUAL_FRONTIER_HANDOFF,
            operation_id="RVOP-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
            manual_frontier_preview_id=approved.preview_id,
        )
        self.assertEqual(record.status.value, "succeeded")
        task = self.store.get_task(task_id)
        self.assertEqual(task.state, WorkflowState.COMPLETE)
        applied_preview = preview_store.get(approved.preview_id)
        self.assertEqual(applied_preview.status, ManualFrontierPreviewStatus.APPLIED)

    def test_failing_verification_returns_to_human_review_and_stays_eligible(self) -> None:
        from tests.test_vertical_slice import IMPLEMENTATION_PATCH

        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        # IMPLEMENTATION_PATCH alone does not make the fixture's held-back
        # test suite pass -- a real, deterministic verification failure.
        preview_store, approved, case = self._prepare_approved_preview(
            config, task_id, IMPLEMENTATION_PATCH, preview_id="MFPV-FAIL-1"
        )

        record = execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.MANUAL_FRONTIER_HANDOFF,
            operation_id="RVOP-FAIL-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
            manual_frontier_preview_id=approved.preview_id,
        )
        self.assertEqual(record.status.value, "succeeded")
        task = self.store.get_task(task_id)
        self.assertEqual(task.state, WorkflowState.HUMAN_REVIEW_REQUIRED)

        new_case = build_review_case(self.root, self.store, config, task_id)
        self.assertEqual(new_case.manual_frontier_rounds_used, 1)
        self.assertIn(ReviewActionKind.MANUAL_FRONTIER_HANDOFF, new_case.eligible_actions)
        # A live browser pass first caught this: stop_reason_kind correctly
        # reclassified to VERIFICATION_FAILED, but stop_reason_text still
        # showed the *original* escalation message from the never-updated
        # report.json, because only local/frontier continuation events
        # were ever counted as "state advanced since the report" -- never
        # a manual-frontier apply round.
        self.assertEqual(new_case.stop_reason_kind, StopReasonKind.VERIFICATION_FAILED)
        self.assertNotIn("agent turn budget exhausted", new_case.stop_reason_text)
        self.assertIn("manual-frontier patch", new_case.stop_reason_text)
        # The same staleness gap applied to verification_results/
        # acceptance_coverage: the UI must show the verification that just
        # ran against the applied patch, not an empty/original snapshot.
        self.assertEqual(len(new_case.verification_results), 1)
        self.assertTrue(new_case.verification_results[0].commands)

    def test_repair_ceiling_removes_eligibility(self) -> None:
        from tests.test_vertical_slice import IMPLEMENTATION_PATCH

        config = self._agent_config(local_turns=3)
        config = config.model_copy(
            update={"manual_frontier": ManualFrontierConfig(max_repair_rounds=1)}
        )
        task_id = self._escalate_locally(config)

        for round_number in range(1):
            preview_store, approved, case = self._prepare_approved_preview(
                config, task_id, IMPLEMENTATION_PATCH, preview_id=f"MFPV-CEIL-{round_number}"
            )
            execute_review_action(
                self.root,
                self.store,
                self.operation_store,
                config,
                task_id=task_id,
                action=ReviewActionKind.MANUAL_FRONTIER_HANDOFF,
                operation_id=f"RVOP-CEIL-{round_number}",
                expected_version=case.task_version,
                expected_worktree_fingerprint=case.worktree_fingerprint,
                manual_frontier_preview_id=approved.preview_id,
            )
            # git-apply of the same patch a second time would fail, so
            # reset the worktree between rounds to isolate the ceiling
            # behavior from patch re-application mechanics.
            self._git_reset_worktree(task_id)

        final_case = build_review_case(self.root, self.store, config, task_id)
        self.assertEqual(final_case.manual_frontier_rounds_used, 1)
        self.assertNotIn(
            ReviewActionKind.MANUAL_FRONTIER_HANDOFF, final_case.eligible_actions
        )

    def _git_reset_worktree(self, task_id: str) -> None:
        import subprocess

        case = build_review_case(self.root, self.store, config=self._agent_config(), task_id=task_id)
        if case.worktree_path is None:
            return
        subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=case.worktree_path,
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=case.worktree_path,
            check=False,
            capture_output=True,
        )

    def test_concurrent_active_operation_is_rejected(self) -> None:
        from tests.test_vertical_slice import COMPLETE_PATCH

        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        preview_store, approved, case = self._prepare_approved_preview(
            config, task_id, COMPLETE_PATCH, preview_id="MFPV-CONCURRENT"
        )
        # Simulate an already-active operation for this task (e.g. a
        # verification-only retry queued moments earlier).
        self.operation_store.create(
            "RVOP-BLOCKER",
            task_id,
            ReviewActionKind.VERIFICATION_ONLY_RETRY,
            expected_task_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
        )
        with self.assertRaises(ActiveOperationExistsError):
            execute_review_action(
                self.root,
                self.store,
                self.operation_store,
                config,
                task_id=task_id,
                action=ReviewActionKind.MANUAL_FRONTIER_HANDOFF,
                operation_id="RVOP-BLOCKED",
                expected_version=case.task_version,
                expected_worktree_fingerprint=case.worktree_fingerprint,
                manual_frontier_preview_id=approved.preview_id,
            )

    def test_not_eligible_when_not_yet_approved(self) -> None:
        from tests.test_vertical_slice import COMPLETE_PATCH

        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        preview_store = ManualFrontierPreviewStore(
            self.root / ".apoapsis" / "manual-frontier-previews.db"
        )
        case = build_review_case(self.root, self.store, config, task_id)
        specification = self.store.get_task(task_id).specification
        package = build_manual_frontier_handoff_package(
            case, specification, config.verification.commands
        )
        write_handoff_artifacts(TaskAuditStore(self.root, task_id), package)
        preview = import_manual_frontier_response(
            self.root,
            self.store,
            preview_store,
            self.operation_store,
            config,
            task_id=task_id,
            package_id=package.package_id,
            response_bytes=_envelope_bytes(package, patch=COMPLETE_PATCH),
            declared_model_name="claude-opus-4.6-web",
            preview_id="MFPV-UNAPPROVED",
        )
        # No approval step was taken -- apply must fail rather than apply
        # an unapproved patch.
        with self.assertRaises(Exception):
            execute_review_action(
                self.root,
                self.store,
                self.operation_store,
                config,
                task_id=task_id,
                action=ReviewActionKind.MANUAL_FRONTIER_HANDOFF,
                operation_id="RVOP-UNAPPROVED",
                expected_version=case.task_version,
                expected_worktree_fingerprint=case.worktree_fingerprint,
                manual_frontier_preview_id=preview.preview_id,
            )


if __name__ == "__main__":
    unittest.main()
