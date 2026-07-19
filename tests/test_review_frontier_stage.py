from __future__ import annotations

import json
import unittest
from pathlib import Path

from apoapsis.config import FrontierProviderConfig
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.reporting.report import TaskOutcome
from apoapsis.review.case import build_review_case
from apoapsis.review.errors import InvalidReviewActionError, ReviewError, WorktreeChangedError
from apoapsis.review.execution import execute_review_action
from apoapsis.review.schema import ReviewActionKind, ReviewOperationStatus, StopReasonKind
from apoapsis.workflow.states import WorkflowState
from tests.fakes import FakeModelProvider
from tests.test_agent_loop import action
from tests.test_vertical_slice import IMPLEMENTATION_PATCH, specification_response
from tests.test_review_execution import ReviewExecutionTestsBase, ProviderPricing


class FrontierStageTestsBase(ReviewExecutionTestsBase):
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

    def _frontier_coder(self) -> FrontierProviderConfig:
        return FrontierProviderConfig(
            base_url="https://frontier.invalid/v1", model="fake-frontier-stage-v1"
        )


class EligibilityTests(FrontierStageTestsBase):
    def test_not_eligible_when_frontier_unconfigured(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        self.assertNotIn(
            ReviewActionKind.AUTHORIZE_FRONTIER_STAGE, case.eligible_actions
        )

    def test_eligible_once_frontier_becomes_configured(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        # Frontier is configured fresh, checked against the *current*
        # config -- not whatever was true at the moment of the original
        # stop.
        config_with_frontier = self._agent_config(
            local_turns=3, frontier_coder=self._frontier_coder()
        )
        case = build_review_case(self.root, self.store, config_with_frontier, task_id)
        self.assertIn(
            ReviewActionKind.AUTHORIZE_FRONTIER_STAGE, case.eligible_actions
        )
        self.assertNotIn(
            ReviewActionKind.FRONTIER_CONTINUATION, case.eligible_actions
        )
        self.assertEqual(case.frontier_model, "fake-frontier-stage-v1")

    def test_not_eligible_once_a_frontier_stage_already_exists(self) -> None:
        config = self._agent_config(
            local_turns=3, frontier_coder=self._frontier_coder()
        )
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        fake = FakeModelProvider(
            [action("search_repository", query="x")]
        )
        frontier_provider = InstrumentedModelProvider(fake, ProviderPricing())
        execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.AUTHORIZE_FRONTIER_STAGE,
            operation_id="RVOP-STAGE-EXISTS-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
            frontier_coder_provider=frontier_provider,
        )
        new_case = build_review_case(self.root, self.store, config, task_id)
        self.assertEqual(new_case.stop_reason_kind, StopReasonKind.FRONTIER_AGENT_EXHAUSTED)
        self.assertNotIn(
            ReviewActionKind.AUTHORIZE_FRONTIER_STAGE, new_case.eligible_actions
        )
        self.assertIn(
            ReviewActionKind.FRONTIER_CONTINUATION, new_case.eligible_actions
        )


class FrontierStageExecutionTests(FrontierStageTestsBase):
    def test_unavailable_frontier_is_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        with self.assertRaises(InvalidReviewActionError):
            execute_review_action(
                self.root,
                self.store,
                self.operation_store,
                config,
                task_id=task_id,
                action=ReviewActionKind.AUTHORIZE_FRONTIER_STAGE,
                operation_id="RVOP-STAGE-UNAVAIL-1",
                expected_version=case.task_version,
                expected_worktree_fingerprint=case.worktree_fingerprint,
            )

    def test_stale_worktree_is_rejected(self) -> None:
        config = self._agent_config(
            local_turns=3, frontier_coder=self._frontier_coder()
        )
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        from apoapsis.review.execution import prepare_review_operation, run_review_operation

        prepare_review_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.AUTHORIZE_FRONTIER_STAGE,
            operation_id="RVOP-STAGE-STALE-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
        )
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
                operation_id="RVOP-STAGE-STALE-1",
            )
        self.assertEqual(
            self.operation_store.get("RVOP-STAGE-STALE-1").status,
            ReviewOperationStatus.FAILED,
        )
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )

    def test_duplicate_operation_id_rejected(self) -> None:
        config = self._agent_config(
            local_turns=3, frontier_coder=self._frontier_coder()
        )
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        from apoapsis.review.execution import prepare_review_operation

        prepare_review_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.AUTHORIZE_FRONTIER_STAGE,
            operation_id="RVOP-STAGE-DUP-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
        )
        with self.assertRaises(ReviewError):
            prepare_review_operation(
                self.root,
                self.store,
                self.operation_store,
                config,
                task_id=task_id,
                action=ReviewActionKind.AUTHORIZE_FRONTIER_STAGE,
                operation_id="RVOP-STAGE-DUP-2",
                expected_version=case.task_version,
                expected_worktree_fingerprint=case.worktree_fingerprint,
            )

    def test_successful_frontier_stage_writes_audit_package_and_completes(
        self,
    ) -> None:
        config = self._agent_config(
            local_turns=3, frontier_coder=self._frontier_coder(), frontier_turns=8
        )
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        self.assertIn(
            ReviewActionKind.AUTHORIZE_FRONTIER_STAGE, case.eligible_actions
        )

        frontier_outputs = [
            action("propose_patch", unified_diff=IMPLEMENTATION_PATCH),
            action("submit_for_verification"),
            action("inspect_diff"),
            action(
                "replace_text",
                path="src/download_service/downloader.py",
                old_text=(
                    '        mode = "ab" if offset else "wb"\n'
                    "        downloaded = offset"
                ),
                new_text=(
                    "        should_append = offset > 0 and "
                    "response.status_code == 206\n"
                    '        mode = "ab" if should_append else "wb"\n'
                    "        downloaded = offset if should_append else 0"
                ),
            ),
            action("run_check", command_name="download-tests"),
        ]
        fake = FakeModelProvider(
            frontier_outputs, model_name="fake-frontier-stage-v1"
        )
        frontier_provider = InstrumentedModelProvider(fake, ProviderPricing())

        record = execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.AUTHORIZE_FRONTIER_STAGE,
            operation_id="RVOP-STAGE-OK-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
            frontier_coder_provider=frontier_provider,
        )
        self.assertEqual(record.status, ReviewOperationStatus.SUCCEEDED)
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.COMPLETE
        )

        package_path = (
            self.root
            / ".apoapsis"
            / "tasks"
            / task_id
            / "review-frontier-stage-RVOP-STAGE-OK-1.json"
        )
        self.assertTrue(package_path.is_file())
        package = json.loads(package_path.read_text(encoding="utf-8"))
        self.assertEqual(package["frontier_model"], "fake-frontier-stage-v1")
        self.assertIn("local_session", package)
        self.assertGreaterEqual(len(package["local_session"]["turn_records"]), 3)
        self.assertIn("active_constraints", package)
        self.assertIn("normalized_failures", package)
        # Budget: the FRESH stage uses the full configured frontier budget,
        # never an accumulated continuation delta from an unrelated action.
        self.assertEqual(package["frontier_budget"]["max_turns"], 8)

        frontier_session_path = (
            self.root
            / ".apoapsis"
            / "tasks"
            / task_id
            / "frontier-agent-session.json"
        )
        self.assertTrue(frontier_session_path.is_file())

    def test_failed_frontier_stage_returns_to_human_review(self) -> None:
        config = self._agent_config(
            local_turns=3, frontier_coder=self._frontier_coder(), frontier_turns=2
        )
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        fake = FakeModelProvider(
            [
                action("search_repository", query="a"),
                action("search_repository", query="b"),
            ]
        )
        frontier_provider = InstrumentedModelProvider(fake, ProviderPricing())
        record = execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.AUTHORIZE_FRONTIER_STAGE,
            operation_id="RVOP-STAGE-FAIL-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
            frontier_coder_provider=frontier_provider,
        )
        self.assertEqual(record.status, ReviewOperationStatus.SUCCEEDED)
        self.assertIn("stopped", record.result_summary or "")
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )

        new_case = build_review_case(self.root, self.store, config, task_id)
        self.assertEqual(
            new_case.stop_reason_kind, StopReasonKind.FRONTIER_AGENT_EXHAUSTED
        )
        self.assertIn(
            ReviewActionKind.FRONTIER_CONTINUATION, new_case.eligible_actions
        )
        self.assertNotIn(
            ReviewActionKind.AUTHORIZE_FRONTIER_STAGE, new_case.eligible_actions
        )


if __name__ == "__main__":
    unittest.main()
