from __future__ import annotations

import datetime
import unittest
from pathlib import Path
from unittest.mock import patch

from apoapsis.reporting.report import TaskOutcome
from apoapsis.review.case import build_review_case
from apoapsis.review.errors import (
    ActiveOperationExistsError,
    ReviewError,
    WorktreeChangedError,
)
from apoapsis.review.execution import (
    execute_review_action,
    prepare_review_operation,
    run_review_operation,
)
from apoapsis.operations.lease import new_owner_id
from apoapsis.review.recovery import recover_stale_operations
from apoapsis.review.schema import (
    ReviewActionKind,
    ReviewOperationStatus,
    StopReasonKind,
)
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState
from tests.test_agent_loop import action
from tests.test_vertical_slice import specification_response
from tests.test_review_execution import ReviewExecutionTestsBase


class QueueDelayAndMismatchTests(ReviewExecutionTestsBase):
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

    def test_worktree_changed_between_prepare_and_run_is_rejected(self) -> None:
        """Simulates a worker-queue delay: the worktree fingerprint shown
        at prepare time no longer matches by the time the (fresh)
        precondition recheck runs inside run_review_operation."""

        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        prepare_review_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.VERIFICATION_ONLY_RETRY,
            operation_id="RVOP-QUEUE-DELAY-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
        )
        # The worktree changes AFTER prepare (queued) but BEFORE the worker
        # actually dequeues and runs it.
        assert case.worktree_path is not None
        (Path(case.worktree_path) / "surprise.txt").write_text(
            "changed after prepare\n", encoding="utf-8"
        )

        with self.assertRaises(WorktreeChangedError):
            run_review_operation(
                self.root,
                self.store,
                self.operation_store,
                config,
                operation_id="RVOP-QUEUE-DELAY-1",
            )
        record = self.operation_store.get("RVOP-QUEUE-DELAY-1")
        self.assertEqual(record.status, ReviewOperationStatus.FAILED)
        # No verification ran and the task never moved.
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )

    def test_task_version_changed_between_prepare_and_run_is_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        prepare_review_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.ABANDON,
            operation_id="RVOP-VERSION-DELAY-1",
            expected_version=case.task_version,
        )
        # Something else edits the specification (a legitimate, unrelated
        # concurrent write) between prepare and run, bumping the version.
        record = self.store.get_task(task_id)
        self.store.update_specification(
            record.specification, actor=WorkflowActor.SYSTEM
        )

        with self.assertRaises(ReviewError):
            run_review_operation(
                self.root,
                self.store,
                self.operation_store,
                config,
                operation_id="RVOP-VERSION-DELAY-1",
            )
        self.assertEqual(
            self.operation_store.get("RVOP-VERSION-DELAY-1").status,
            ReviewOperationStatus.FAILED,
        )
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )

    def test_stale_abandon_never_deletes_worktree_before_version_check(self) -> None:
        """Regression test for the fixed ordering bug: a stale operation
        must fail its version check before any destructive worktree
        cleanup, never after."""

        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        assert case.worktree_path is not None
        worktree_path = Path(case.worktree_path)
        self.assertTrue(worktree_path.is_dir())

        prepare_review_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.ABANDON,
            operation_id="RVOP-STALE-ABANDON-1",
            expected_version=case.task_version,
        )
        record = self.store.get_task(task_id)
        self.store.update_specification(
            record.specification, actor=WorkflowActor.SYSTEM
        )

        with self.assertRaises(ReviewError):
            run_review_operation(
                self.root,
                self.store,
                self.operation_store,
                config,
                operation_id="RVOP-STALE-ABANDON-1",
            )
        self.assertTrue(
            worktree_path.is_dir(),
            "a stale abandon must never delete the worktree before its "
            "version check succeeds",
        )
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )

    def test_simultaneous_operations_on_one_task_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        prepare_review_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.VERIFICATION_ONLY_RETRY,
            operation_id="RVOP-SIMUL-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
        )
        with self.assertRaises(ActiveOperationExistsError):
            prepare_review_operation(
                self.root,
                self.store,
                self.operation_store,
                config,
                task_id=task_id,
                action=ReviewActionKind.ABANDON,
                operation_id="RVOP-SIMUL-2",
                expected_version=case.task_version,
            )

    def test_provider_construction_failure_reaches_failed_not_stuck_recorded(
        self,
    ) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        prepare_review_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.LOCAL_CONTINUATION,
            operation_id="RVOP-PROVIDER-FAIL-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
            additional_turns=3,
        )
        with patch(
            "apoapsis.review.execution._build_provider",
            side_effect=RuntimeError("provider misconfigured"),
        ):
            with self.assertRaises(RuntimeError):
                run_review_operation(
                    self.root,
                    self.store,
                    self.operation_store,
                    config,
                    operation_id="RVOP-PROVIDER-FAIL-1",
                )
        record = self.operation_store.get("RVOP-PROVIDER-FAIL-1")
        self.assertEqual(record.status, ReviewOperationStatus.FAILED)
        self.assertIn("provider misconfigured", record.error or "")

    def test_unknown_newest_stop_event_only_offers_inspect_and_abandon(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        # Force an unrecognized newest HUMAN_REVIEW_REQUIRED event directly
        # via the store, simulating a future/unrecognized event type.
        record = self.store.get_task(task_id)
        self.store.transition(
            task_id,
            WorkflowState.IMPLEMENTING,
            actor=WorkflowActor.SYSTEM,
            event_type="test_only_transition",
            expected_version=record.version,
        )
        implementing = self.store.get_task(task_id)
        self.store.transition(
            task_id,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            actor=WorkflowActor.SYSTEM,
            event_type="some_future_unrecognized_event",
            expected_version=implementing.version,
        )
        case = build_review_case(self.root, self.store, config, task_id)
        self.assertEqual(case.stop_reason_kind, StopReasonKind.UNKNOWN)
        self.assertEqual(
            set(case.eligible_actions),
            {ReviewActionKind.INSPECT_ONLY, ReviewActionKind.ABANDON},
        )

    def test_untracked_text_file_appears_in_current_diff(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        assert case.worktree_path is not None
        (Path(case.worktree_path) / "untracked_note.txt").write_text(
            "hello from an untracked file\n", encoding="utf-8"
        )
        refreshed = build_review_case(self.root, self.store, config, task_id)
        assert refreshed.current_diff is not None
        self.assertIn("untracked_note.txt", refreshed.current_diff)
        self.assertIn("hello from an untracked file", refreshed.current_diff)


class FreshEvidenceTests(ReviewExecutionTestsBase):
    def test_verification_retry_surfaces_fresh_results_not_the_stale_report(
        self,
    ) -> None:
        """The original agent-mode escalation stop's report.json was
        written with empty verification_results (the local agent only
        searched, never verified). A verification-only retry afterward
        produces real, fresh results that ReviewCase must surface --
        never the stale, empty original snapshot."""

        config = self._agent_config(local_turns=3)
        outputs = [
            specification_response(),
            action("search_repository", query="get_offset"),
            action("search_repository", query="downloader"),
            action("search_repository", query="jobs"),
        ]
        report = self._run(outputs, config)
        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)
        self.assertEqual(report.verification_results, [])
        task_id = report.task_id

        case = build_review_case(self.root, self.store, config, task_id)
        self.assertEqual(case.verification_results, [])

        record = execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.VERIFICATION_ONLY_RETRY,
            operation_id="RVOP-FRESH-EVIDENCE-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
        )
        self.assertEqual(record.status, ReviewOperationStatus.SUCCEEDED)
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )

        refreshed = build_review_case(self.root, self.store, config, task_id)
        self.assertEqual(len(refreshed.verification_results), 1)
        self.assertNotEqual(refreshed.verification_results[0].status.value, "skipped")
        self.assertTrue(
            any(
                command.name == "download-tests"
                for command in refreshed.verification_results[0].commands
            )
        )


class RecoveryTests(ReviewExecutionTestsBase):
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

    def test_recorded_operation_is_reclaimed_never_run_twice_by_recovery_alone(
        self,
    ) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        prepare_review_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.ABANDON,
            operation_id="RVOP-RECORDED-CRASH-1",
            expected_version=case.task_version,
        )
        report = recover_stale_operations(self.store, self.operation_store)
        self.assertIn("RVOP-RECORDED-CRASH-1", report.reclaimed_operation_ids)
        # Recovery itself never executes anything -- the operation is still
        # exactly RECORDED, just reported as safe to reclaim.
        self.assertEqual(
            self.operation_store.get("RVOP-RECORDED-CRASH-1").status,
            ReviewOperationStatus.RECORDED,
        )
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )

    def test_crash_while_still_at_human_review_required_marks_ambiguous_only(
        self,
    ) -> None:
        """'Crash before' any workflow transition: the operation was
        RUNNING but the task never left HUMAN_REVIEW_REQUIRED, so recovery
        only needs to mark the operation AMBIGUOUS -- no task transition is
        needed or made."""

        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        prepare_review_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.VERIFICATION_ONLY_RETRY,
            operation_id="RVOP-CRASH-BEFORE-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
        )
        self.operation_store.mark_running(
            "RVOP-CRASH-BEFORE-1",
            owner_id=new_owner_id(),
            lease_duration=datetime.timedelta(seconds=-1),
        )

        report = recover_stale_operations(self.store, self.operation_store)
        self.assertIn("RVOP-CRASH-BEFORE-1", report.ambiguous_operation_ids)
        self.assertEqual(report.tasks_returned_to_review, [])
        self.assertEqual(
            self.operation_store.get("RVOP-CRASH-BEFORE-1").status,
            ReviewOperationStatus.AMBIGUOUS,
        )
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )

    def test_crash_after_leaving_human_review_required_returns_task_to_review(
        self,
    ) -> None:
        """'Crash after' a workflow transition: the task moved to
        IMPLEMENTING (a continuation started) but the operation never
        reached a terminal status, so recovery must return the task to
        HUMAN_REVIEW_REQUIRED without claiming any outcome."""

        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        prepare_review_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.LOCAL_CONTINUATION,
            operation_id="RVOP-CRASH-AFTER-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
            additional_turns=3,
        )
        self.operation_store.mark_running(
            "RVOP-CRASH-AFTER-1",
            owner_id=new_owner_id(),
            lease_duration=datetime.timedelta(seconds=-1),
        )
        # Simulate the crash happening exactly after the workflow
        # transition to IMPLEMENTING but before the session finished.
        self.store.transition(
            task_id,
            WorkflowState.IMPLEMENTING,
            actor=WorkflowActor.USER,
            event_type="review_local_continuation_started",
            payload={
                "reason": "human-authorized continuation",
                "operation_id": "RVOP-CRASH-AFTER-1",
                "authorized_budget": {"additional_turns": 3},
            },
            expected_version=case.task_version,
        )

        report = recover_stale_operations(self.store, self.operation_store)
        self.assertIn("RVOP-CRASH-AFTER-1", report.ambiguous_operation_ids)
        self.assertIn(task_id, report.tasks_returned_to_review)
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )
        events = self.store.events(task_id)
        self.assertEqual(
            events[-1].event_type, "review_operation_recovery_requires_human"
        )
        self.assertEqual(
            events[-1].payload.get("recovered_from_state"), "IMPLEMENTING"
        )
        # The recovered case must not claim the interrupted continuation
        # succeeded or failed -- it must classify as UNKNOWN (an unrecognized
        # newest event to the stop-reason classifier) so only inspect/abandon
        # are offered until a human actually looks at it.
        recovered_case = build_review_case(self.root, self.store, config, task_id)
        self.assertEqual(recovered_case.stop_reason_kind, StopReasonKind.UNKNOWN)

    def test_recent_running_operation_is_left_alone(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        prepare_review_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.VERIFICATION_ONLY_RETRY,
            operation_id="RVOP-STILL-FRESH-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
        )
        self.operation_store.mark_running(
            "RVOP-STILL-FRESH-1",
            owner_id=new_owner_id(),
            lease_duration=datetime.timedelta(hours=1),
        )

        report = recover_stale_operations(self.store, self.operation_store)
        self.assertEqual(report.ambiguous_operation_ids, [])
        self.assertEqual(
            self.operation_store.get("RVOP-STILL-FRESH-1").status,
            ReviewOperationStatus.RUNNING,
        )


if __name__ == "__main__":
    unittest.main()
