from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from apoapsis.agent.session import AgentSessionResult
from apoapsis.config import (
    AgentLoopConfig,
    AgentRoute,
    CompletionPolicy,
    ContextCompilerConfig,
    ExecutionConfig,
    ExecutionMode,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ProviderPricing,
    ReviewConfig,
    ApoapsisConfig,
)
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.reporting.report import TaskOutcome
from apoapsis.review.case import build_review_case
from apoapsis.review.errors import (
    ContinuationCeilingExceededError,
    InvalidReviewActionError,
    ReviewError,
    WorktreeChangedError,
)
from apoapsis.review.execution import execute_review_action
from apoapsis.review.schema import ReviewActionKind, StopReasonKind
from apoapsis.operations.lease import new_owner_id
from apoapsis.review.store import ReviewOperationStore
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.states import WorkflowState
from apoapsis.workflow.vertical_slice import VerticalSliceRunner
from tests.fakes import FakeModelProvider
from tests.test_agent_loop import action, specification_with_risk
from tests.test_vertical_slice import (
    COMPLETE_PATCH,
    IMPLEMENTATION_PATCH,
    REQUEST,
    specification_response,
)


class ReviewExecutionTestsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "download-service"
        example = (
            Path(__file__).resolve().parents[1] / "examples" / "download-service"
        )
        shutil.copytree(example, self.root)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Apoapsis Tests")
        self._git("add", ".")
        self._git("commit", "-m", "controlled baseline")
        (self.root / ".apoapsis").mkdir()
        self.store = SQLiteTaskStore(self.root / ".apoapsis" / "apoapsis.db")
        self.operation_store = ReviewOperationStore(
            self.root / ".apoapsis" / "review-operations.db"
        )

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )

    def _verification(self, *, acceptance: bool = False) -> VerificationConfig:
        return VerificationConfig(
            commands=[
                VerificationCommand(
                    name="download-tests",
                    category="tests",
                    argv=[
                        sys.executable,
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        "tests",
                        "-v",
                    ],
                    timeout_seconds=30,
                    acceptance=acceptance,
                )
            ]
        )

    def _agent_config(
        self,
        *,
        route: AgentRoute = AgentRoute.AUTO,
        frontier_coder: FrontierProviderConfig | None = None,
        local_turns: int = 3,
        frontier_turns: int = 3,
        completion_policy: CompletionPolicy = CompletionPolicy.BASELINE,
        review: ReviewConfig | None = None,
        acceptance: bool = False,
    ) -> ApoapsisConfig:
        return ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1", model="fake-coder-v1"
                ),
                frontier_coder=frontier_coder,
            ),
            execution=ExecutionConfig(
                mode=ExecutionMode.AGENT,
                route=route,
                completion_policy=completion_policy,
                agent=AgentLoopConfig(
                    max_turns=local_turns,
                    max_patch_attempts=2,
                    max_verification_runs=2,
                    max_search_results=10,
                    max_read_lines=120,
                    max_observation_chars=20_000,
                ),
                frontier_agent=AgentLoopConfig(
                    max_turns=frontier_turns,
                    max_patch_attempts=2,
                    max_verification_runs=2,
                    max_search_results=10,
                    max_read_lines=120,
                    max_observation_chars=20_000,
                ),
            ),
            context=ContextCompilerConfig(
                max_files=10, max_excerpt_lines=200, max_total_chars=50_000
            ),
            patch=PatchPolicyConfig(max_changed_lines=100),
            verification=self._verification(acceptance=acceptance),
            review=review or ReviewConfig(),
        )

    def _one_shot_config(
        self,
        *,
        completion_policy: CompletionPolicy = CompletionPolicy.BASELINE,
        acceptance: bool = False,
    ) -> ApoapsisConfig:
        return ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1", model="fake-coder-v1"
                )
            ),
            execution=ExecutionConfig(completion_policy=completion_policy),
            context=ContextCompilerConfig(
                max_files=10, max_excerpt_lines=200, max_total_chars=50_000
            ),
            patch=PatchPolicyConfig(max_changed_lines=100),
            verification=self._verification(acceptance=acceptance),
        )

    @staticmethod
    def _inject_task_id(fake: FakeModelProvider) -> None:
        original_complete = fake.complete

        def complete(invocation):
            output = original_complete(invocation)
            if len(fake.invocations) == 1:
                task_id = invocation.prompt.split('task_id to "', 1)[1].split('"', 1)[
                    0
                ]
                raw = json.loads(output.content)
                raw["task_id"] = task_id
                return output.model_copy(update={"content": json.dumps(raw)})
            return output

        fake.complete = complete  # type: ignore[method-assign]

    def _run(
        self,
        outputs: list[str],
        config: ApoapsisConfig,
        *,
        request: str = REQUEST,
        approve=lambda specification: True,
        frontier_provider: InstrumentedModelProvider | None = None,
    ):
        fake = FakeModelProvider(outputs)
        self._inject_task_id(fake)
        provider = InstrumentedModelProvider(fake, ProviderPricing())
        return VerticalSliceRunner(
            self.root,
            self.store,
            provider,
            config,
            frontier_coder_provider=frontier_provider,
        ).run(request, approve=approve)

    def _agent_session(self, task_id: str, *, prefix: str = "") -> AgentSessionResult:
        path = (
            self.root
            / ".apoapsis"
            / "tasks"
            / task_id
            / f"{prefix}agent-session.json"
        )
        return AgentSessionResult.model_validate_json(path.read_text(encoding="utf-8"))


class SpecificationNotApprovedTests(ReviewExecutionTestsBase):
    def test_review_case_and_abandon_with_no_worktree(self) -> None:
        config = self._one_shot_config()
        report = self._run(
            [specification_response()], config, approve=lambda specification: False
        )
        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)

        case = build_review_case(self.root, self.store, config, report.task_id)
        self.assertEqual(case.stop_reason_kind, StopReasonKind.SPECIFICATION_NOT_APPROVED)
        self.assertEqual(
            set(case.eligible_actions),
            {ReviewActionKind.INSPECT_ONLY, ReviewActionKind.ABANDON},
        )
        self.assertFalse(case.worktree_exists)

        record = execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=report.task_id,
            action=ReviewActionKind.ABANDON,
            operation_id="RVOP-1",
            expected_version=case.task_version,
        )
        self.assertEqual(record.status.value, "succeeded")
        self.assertEqual(
            self.store.get_task(report.task_id).state, WorkflowState.ROLLED_BACK
        )


class RoutingRequiresHumanTests(ReviewExecutionTestsBase):
    def test_critical_risk_stops_before_any_worktree(self) -> None:
        config = self._agent_config()
        report = self._run(
            [specification_with_risk("critical")], config
        )
        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)

        case = build_review_case(self.root, self.store, config, report.task_id)
        self.assertEqual(case.stop_reason_kind, StopReasonKind.ROUTING_REQUIRES_HUMAN)
        self.assertEqual(
            set(case.eligible_actions),
            {ReviewActionKind.INSPECT_ONLY, ReviewActionKind.ABANDON},
        )
        self.assertFalse(case.worktree_exists)

        execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=report.task_id,
            action=ReviewActionKind.ABANDON,
            operation_id="RVOP-1",
            expected_version=case.task_version,
        )
        self.assertEqual(
            self.store.get_task(report.task_id).state, WorkflowState.ROLLED_BACK
        )


class AcceptanceCoverageOneShotTests(ReviewExecutionTestsBase):
    def test_incomplete_then_verification_only_retry_stays_incomplete(self) -> None:
        config = self._one_shot_config(completion_policy=CompletionPolicy.STRICT)
        report = self._run([specification_response(), COMPLETE_PATCH], config)
        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)

        case = build_review_case(self.root, self.store, config, report.task_id)
        self.assertEqual(
            case.stop_reason_kind, StopReasonKind.ACCEPTANCE_COVERAGE_INCOMPLETE
        )
        self.assertIn(ReviewActionKind.VERIFICATION_ONLY_RETRY, case.eligible_actions)

        # Retrying with the same (still unmapped) configuration stays incomplete.
        execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=report.task_id,
            action=ReviewActionKind.VERIFICATION_ONLY_RETRY,
            operation_id="RVOP-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
        )
        retried_case = build_review_case(self.root, self.store, config, report.task_id)
        self.assertEqual(
            retried_case.stop_reason_kind, StopReasonKind.ACCEPTANCE_COVERAGE_INCOMPLETE
        )


class LocalContinuationTests(ReviewExecutionTestsBase):
    def _escalate_locally(self, config: ApoapsisConfig) -> str:
        outputs = [
            specification_response(),
            action("search_repository", query="get_offset"),
            action("search_repository", query="downloader"),
            action("search_repository", query="jobs"),
        ]
        report = self._run(outputs, config)
        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)
        return report.task_id

    def test_frontier_not_configured_scenario_eligible_actions(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)

        case = build_review_case(self.root, self.store, config, task_id)
        self.assertEqual(
            case.stop_reason_kind, StopReasonKind.LOCAL_AGENT_ESCALATION_UNAVAILABLE
        )
        self.assertEqual(
            set(case.eligible_actions),
            {
                ReviewActionKind.INSPECT_ONLY,
                ReviewActionKind.ABANDON,
                ReviewActionKind.VERIFICATION_ONLY_RETRY,
                ReviewActionKind.LOCAL_CONTINUATION,
                # Manual subscription-frontier handoff (ADR 0031) never
                # requires the automated API frontier to be configured.
                ReviewActionKind.MANUAL_FRONTIER_HANDOFF,
            },
        )
        self.assertTrue(case.worktree_exists)
        self.assertEqual(case.consumed_local_turns, 3)

    def test_frontier_continuation_rejected_when_not_configured(self) -> None:
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
                action=ReviewActionKind.FRONTIER_CONTINUATION,
                operation_id="RVOP-FRONTIER-1",
                expected_version=case.task_version,
                expected_worktree_fingerprint=case.worktree_fingerprint,
                additional_turns=5,
            )

    def test_successful_local_continuation_preserves_counters_and_completes(
        self,
    ) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        continuation_outputs = [
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
        fake2 = FakeModelProvider(continuation_outputs)
        provider2 = InstrumentedModelProvider(fake2, ProviderPricing())

        record = execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.LOCAL_CONTINUATION,
            operation_id="RVOP-LOCAL-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
            additional_turns=5,
            local_coder_provider=provider2,
        )
        self.assertEqual(record.status.value, "succeeded")
        self.assertIn("completed", record.result_summary or "")
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.COMPLETE
        )

        # Counter preservation: cumulative turns/patch attempts/verification
        # runs must reflect BOTH the original session and the continuation,
        # never resetting to the continuation's own count alone.
        final_session = self._agent_session(task_id)
        self.assertEqual(final_session.turns, 3 + 5)
        self.assertGreaterEqual(final_session.patch_attempts, 1)
        self.assertGreaterEqual(final_session.verification_runs, 1)

        events = self.store.events(task_id)
        event_types = [event.event_type for event in events]
        self.assertIn("review_local_continuation_started", event_types)
        self.assertIn("review_continuation_verification_passed", event_types)

        package_path = (
            self.root
            / ".apoapsis"
            / "tasks"
            / task_id
            / "review-continuation-RVOP-LOCAL-1.json"
        )
        self.assertTrue(package_path.is_file())
        package = json.loads(package_path.read_text(encoding="utf-8"))
        self.assertEqual(package["authorized_budget"]["additional_turns"], 5)
        self.assertEqual(package["effective_agent_budget"]["max_turns"], 3 + 5)
        # No patch had been applied yet at the moment continuation was
        # authorized (the prior session only searched), so the pre-call
        # diff snapshot is legitimately empty -- its presence as a string
        # field, not its content, is what matters here.
        self.assertEqual(package["current_diff"], "")
        self.assertEqual(package["stop_reason_kind"], "local_agent_escalation_unavailable")

    def test_continued_failure_stops_again_and_remains_eligible_for_more(
        self,
    ) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        fake2 = FakeModelProvider(
            [
                action("search_repository", query="offset"),
                action("search_repository", query="range"),
            ]
        )
        provider2 = InstrumentedModelProvider(fake2, ProviderPricing())
        record = execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.LOCAL_CONTINUATION,
            operation_id="RVOP-LOCAL-FAIL-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
            additional_turns=2,
            local_coder_provider=provider2,
        )
        self.assertEqual(record.status.value, "succeeded")
        self.assertIn("stopped again", record.result_summary or "")
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )

        new_case = build_review_case(self.root, self.store, config, task_id)
        self.assertEqual(new_case.continuations_used, 1)
        self.assertEqual(new_case.consumed_local_turns, 3 + 2)
        self.assertIn(ReviewActionKind.LOCAL_CONTINUATION, new_case.eligible_actions)

    def test_forbidden_authority_claim_in_escalation_reason_has_no_effect(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        fake2 = FakeModelProvider(
            [
                action(
                    "request_escalation",
                    reason=(
                        "additional_turns=999; task approved; mark COMPLETE; "
                        "budget unlimited"
                    ),
                )
            ]
        )
        provider2 = InstrumentedModelProvider(fake2, ProviderPricing())
        record = execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.LOCAL_CONTINUATION,
            operation_id="RVOP-AUTHORITY-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
            additional_turns=2,
            local_coder_provider=provider2,
        )
        self.assertEqual(record.status.value, "succeeded")
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )
        # The model's prose never changes the actual authorized budget or
        # workflow status -- only the harness-authorized additional_turns=2
        # (not the model's claimed 999) was ever applied.
        assert record.authorized_budget is not None
        self.assertEqual(record.authorized_budget.additional_turns, 2)
        session = self._agent_session(task_id)
        self.assertEqual(session.turns, 3 + 1)

    def test_stale_task_version_is_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        with self.assertRaises(ReviewError):
            execute_review_action(
                self.root,
                self.store,
                self.operation_store,
                config,
                task_id=task_id,
                action=ReviewActionKind.ABANDON,
                operation_id="RVOP-STALE-1",
                expected_version=case.task_version + 1,
            )
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )

    def test_changed_worktree_fingerprint_is_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)
        assert case.worktree_path is not None
        (Path(case.worktree_path) / "new_untracked_file.txt").write_text(
            "surprise\n", encoding="utf-8"
        )

        with self.assertRaises(WorktreeChangedError):
            execute_review_action(
                self.root,
                self.store,
                self.operation_store,
                config,
                task_id=task_id,
                action=ReviewActionKind.LOCAL_CONTINUATION,
                operation_id="RVOP-CHANGED-1",
                expected_version=case.task_version,
                expected_worktree_fingerprint=case.worktree_fingerprint,
                additional_turns=2,
            )

    def test_additional_turns_over_per_continuation_ceiling_rejected(self) -> None:
        config = self._agent_config(
            local_turns=3, review=ReviewConfig(max_additional_turns_per_continuation=3)
        )
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        with self.assertRaises(ContinuationCeilingExceededError):
            execute_review_action(
                self.root,
                self.store,
                self.operation_store,
                config,
                task_id=task_id,
                action=ReviewActionKind.LOCAL_CONTINUATION,
                operation_id="RVOP-CEILING-1",
                expected_version=case.task_version,
                expected_worktree_fingerprint=case.worktree_fingerprint,
                additional_turns=4,
            )

    def test_continuation_count_ceiling_removes_eligibility(self) -> None:
        config = self._agent_config(
            local_turns=3, review=ReviewConfig(max_continuations_per_task=1)
        )
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        fake2 = FakeModelProvider([action("search_repository", query="x")])
        provider2 = InstrumentedModelProvider(fake2, ProviderPricing())
        execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.LOCAL_CONTINUATION,
            operation_id="RVOP-ONLY-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
            additional_turns=1,
            local_coder_provider=provider2,
        )
        new_case = build_review_case(self.root, self.store, config, task_id)
        self.assertNotIn(ReviewActionKind.LOCAL_CONTINUATION, new_case.eligible_actions)
        with self.assertRaises(InvalidReviewActionError):
            execute_review_action(
                self.root,
                self.store,
                self.operation_store,
                config,
                task_id=task_id,
                action=ReviewActionKind.LOCAL_CONTINUATION,
                operation_id="RVOP-ONLY-2",
                expected_version=new_case.task_version,
                expected_worktree_fingerprint=new_case.worktree_fingerprint,
                additional_turns=1,
            )

    def test_duplicate_operation_id_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            action=ReviewActionKind.INSPECT_ONLY,
            operation_id="RVOP-DUP-1",
            expected_version=case.task_version,
        )
        with self.assertRaises(ReviewError):
            execute_review_action(
                self.root,
                self.store,
                self.operation_store,
                config,
                task_id=task_id,
                action=ReviewActionKind.INSPECT_ONLY,
                operation_id="RVOP-DUP-1",
                expected_version=case.task_version,
            )

    def test_crash_ambiguous_running_operation_fails_closed(self) -> None:
        config = self._agent_config(local_turns=3)
        task_id = self._escalate_locally(config)
        case = build_review_case(self.root, self.store, config, task_id)

        # Simulate a process that created the operation, marked it RUNNING,
        # then crashed before finishing -- a caller retrying the exact same
        # operation_id must never silently re-enter it.
        self.operation_store.create(
            "RVOP-CRASH-1",
            task_id,
            ReviewActionKind.LOCAL_CONTINUATION,
            expected_task_version=case.task_version,
        )
        self.operation_store.mark_running("RVOP-CRASH-1", owner_id=new_owner_id())

        with self.assertRaises(ReviewError):
            execute_review_action(
                self.root,
                self.store,
                self.operation_store,
                config,
                task_id=task_id,
                action=ReviewActionKind.LOCAL_CONTINUATION,
                operation_id="RVOP-CRASH-1",
                expected_version=case.task_version,
                expected_worktree_fingerprint=case.worktree_fingerprint,
                additional_turns=2,
            )
        # The task itself never moved -- the fail-closed rejection happened
        # before any model call or workflow transition.
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )


class FrontierContinuationTests(ReviewExecutionTestsBase):
    def test_frontier_agent_exhausted_then_continuation_completes(self) -> None:
        frontier_coder = FrontierProviderConfig(
            base_url="https://frontier.invalid/v1", model="fake-frontier-v1"
        )
        config = self._agent_config(
            route=AgentRoute.FRONTIER_ONLY,
            frontier_coder=frontier_coder,
            frontier_turns=3,
        )
        frontier_fake = FakeModelProvider(
            [
                action("search_repository", query="get_offset"),
                action("search_repository", query="downloader"),
                action("search_repository", query="jobs"),
            ]
        )
        frontier_provider = InstrumentedModelProvider(frontier_fake, ProviderPricing())
        report = self._run(
            [specification_response()], config, frontier_provider=frontier_provider
        )
        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)

        case = build_review_case(self.root, self.store, config, report.task_id)
        self.assertEqual(
            case.stop_reason_kind, StopReasonKind.FRONTIER_AGENT_EXHAUSTED
        )
        self.assertIn(ReviewActionKind.FRONTIER_CONTINUATION, case.eligible_actions)
        self.assertNotIn(ReviewActionKind.LOCAL_CONTINUATION, case.eligible_actions)

        continuation_outputs = [
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
        fake2 = FakeModelProvider(continuation_outputs)
        provider2 = InstrumentedModelProvider(fake2, ProviderPricing())
        record = execute_review_action(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=report.task_id,
            action=ReviewActionKind.FRONTIER_CONTINUATION,
            operation_id="RVOP-FRONTIER-OK-1",
            expected_version=case.task_version,
            expected_worktree_fingerprint=case.worktree_fingerprint,
            additional_turns=5,
            frontier_coder_provider=provider2,
        )
        self.assertEqual(record.status.value, "succeeded")
        self.assertEqual(
            self.store.get_task(report.task_id).state, WorkflowState.COMPLETE
        )
        session = self._agent_session(report.task_id, prefix="frontier-")
        self.assertEqual(session.turns, 3 + 5)


if __name__ == "__main__":
    unittest.main()
