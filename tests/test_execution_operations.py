from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from apoapsis.config import (
    AgentLoopConfig,
    AgentRoute,
    ContextCompilerConfig,
    ExecutionConfig,
    ExecutionMode,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ProviderPricing,
    ApoapsisConfig,
)
from apoapsis.execution.operation_errors import (
    ActiveExecutionOperationExistsError,
    DuplicateExecutionOperationError,
    ExecutionOperationError,
    StaleExecutionStartError,
)
from apoapsis.execution.operation_recovery import recover_stale_execution_operations
from apoapsis.execution.operation_schema import ExecutionOperationStatus
from apoapsis.execution.operation_service import (
    execute_execution_operation,
    prepare_execution_operation,
    run_execution_operation,
)
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.operations.lease import new_owner_id
from apoapsis.reporting.report import TaskOutcome
from apoapsis.specification.schema import TaskSpecification
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState
from tests.fakes import FakeModelProvider
from tests.helpers import force_operation_status
from tests.test_agent_loop import action
from tests.test_vertical_slice import (
    COMPLETE_PATCH,
    IMPLEMENTATION_PATCH,
    specification_response,
)


class ExecutionOperationTestsBase(unittest.TestCase):
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
        self.operation_store = ExecutionOperationStore(
            self.root / ".apoapsis" / "execution-operations.db"
        )

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )

    def _agent_config(
        self,
        *,
        route: AgentRoute = AgentRoute.AUTO,
        frontier_coder: FrontierProviderConfig | None = None,
        local_turns: int = 3,
        frontier_turns: int = 3,
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
            verification=VerificationConfig(
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
                    )
                ]
            ),
        )

    def _one_shot_config(self) -> ApoapsisConfig:
        return ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1", model="fake-coder-v1"
                )
            ),
            context=ContextCompilerConfig(
                max_files=10, max_excerpt_lines=200, max_total_chars=50_000
            ),
            patch=PatchPolicyConfig(max_changed_lines=100),
            verification=VerificationConfig(
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
                    )
                ]
            ),
        )

    def _build_specification(
        self, task_id: str, *, risk_level: str = "medium"
    ) -> TaskSpecification:
        payload = json.loads(specification_response())
        payload["task_id"] = task_id
        payload["risk_level"] = risk_level
        return TaskSpecification.model_validate(payload)

    def _create_approved_task(self, *, risk_level: str = "medium") -> tuple[str, int]:
        task_id = f"TASK-{uuid.uuid4().hex[:12].upper()}"
        specification = self._build_specification(task_id, risk_level=risk_level)
        self.store.create_task(specification)
        drafted = self.store.transition(
            task_id,
            WorkflowState.SPEC_DRAFTED,
            actor=WorkflowActor.SYSTEM,
            event_type="deterministic_specification_drafted",
        )
        approved = self.store.transition(
            task_id,
            WorkflowState.SPEC_APPROVED,
            actor=WorkflowActor.USER,
            event_type="specification_approved",
            expected_version=drafted.version,
        )
        return task_id, approved.version

    def _provider(self, outputs: list[str | Exception]) -> InstrumentedModelProvider:
        return InstrumentedModelProvider(
            FakeModelProvider(outputs), ProviderPricing()
        )


class SuccessfulExecutionTests(ExecutionOperationTestsBase):
    def test_local_agent_completes_via_execute_approved_task(self) -> None:
        task_id, version = self._create_approved_task()
        config = self._agent_config(route=AgentRoute.LOCAL_ONLY, local_turns=8)
        provider = self._provider(
            [
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
        )
        record = execute_execution_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            operation_id="EXOP-LOCAL-1",
            expected_version=version,
            local_coder_provider=provider,
        )
        self.assertEqual(record.status, ExecutionOperationStatus.SUCCEEDED)
        self.assertIn("complete", record.result_summary or "")
        self.assertEqual(self.store.get_task(task_id).state, WorkflowState.COMPLETE)
        report = json.loads(
            (self.root / record.report_path).read_text(encoding="utf-8")
        )
        self.assertEqual(report["outcome"], "complete")

    def test_one_shot_completes_via_execute_approved_task(self) -> None:
        task_id, version = self._create_approved_task()
        config = self._one_shot_config()
        provider = self._provider([COMPLETE_PATCH])
        record = execute_execution_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            operation_id="EXOP-ONESHOT-1",
            expected_version=version,
            provider=provider,
        )
        self.assertEqual(record.status, ExecutionOperationStatus.SUCCEEDED)
        self.assertEqual(self.store.get_task(task_id).state, WorkflowState.COMPLETE)

    def test_local_then_frontier_escalation_completes(self) -> None:
        task_id, version = self._create_approved_task()
        frontier_config = FrontierProviderConfig(
            base_url="https://frontier.invalid/v1", model="fake-frontier-v1"
        )
        config = self._agent_config(
            route=AgentRoute.LOCAL_THEN_FRONTIER,
            frontier_coder=frontier_config,
            local_turns=1,
            frontier_turns=8,
        )
        local_outputs = [action("search_repository", query="downloader")]
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
        with patch(
            "apoapsis.execution.operation_service._build_providers"
        ) as build_providers:
            build_providers.return_value = (
                self._provider([]),
                self._provider(local_outputs),
                self._provider(frontier_outputs),
            )
            record = execute_execution_operation(
                self.root,
                self.store,
                self.operation_store,
                config,
                task_id=task_id,
                operation_id="EXOP-ESCALATE-1",
                expected_version=version,
            )
        self.assertEqual(record.status, ExecutionOperationStatus.SUCCEEDED)
        self.assertEqual(self.store.get_task(task_id).state, WorkflowState.COMPLETE)
        self.assertTrue(
            (self.root / ".apoapsis" / "tasks" / task_id / "frontier-escalation-package.json").is_file()
        )

    def test_critical_risk_route_requires_human_before_any_worktree(self) -> None:
        task_id, version = self._create_approved_task(risk_level="critical")
        config = self._agent_config(route=AgentRoute.AUTO)
        record = execute_execution_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            operation_id="EXOP-CRITICAL-1",
            expected_version=version,
        )
        self.assertEqual(record.status, ExecutionOperationStatus.SUCCEEDED)
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )
        self.assertFalse(
            (self.root / ".apoapsis" / "worktrees").exists()
            and any((self.root / ".apoapsis" / "worktrees").iterdir())
        )

    def test_local_agent_exhausted_no_frontier_requires_human(self) -> None:
        task_id, version = self._create_approved_task()
        config = self._agent_config(route=AgentRoute.LOCAL_ONLY, local_turns=1)
        provider = self._provider([action("search_repository", query="x")])
        record = execute_execution_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            operation_id="EXOP-EXHAUST-1",
            expected_version=version,
            local_coder_provider=provider,
        )
        self.assertEqual(record.status, ExecutionOperationStatus.SUCCEEDED)
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )


class OperationLedgerTests(ExecutionOperationTestsBase):
    def test_duplicate_operation_id_rejected_once_terminal(self) -> None:
        task_id, version = self._create_approved_task()
        config = self._one_shot_config()
        execute_execution_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            operation_id="EXOP-DUP",
            expected_version=version,
            provider=self._provider([COMPLETE_PATCH]),
        )
        # The task has since moved on, so a resubmission through the full
        # `prepare_execution_operation` orchestration would (correctly)
        # fail its version/state check first -- this exercises the
        # operation store's own terminal-duplicate-id guarantee directly,
        # the same way `test_second_active_operation_for_same_task_rejected`
        # exercises its active-operation guarantee.
        with self.assertRaises(DuplicateExecutionOperationError):
            self.operation_store.create(
                "EXOP-DUP",
                task_id,
                expected_task_version=version,
                expected_repository_head="a" * 40,
            )

    def test_second_active_operation_for_same_task_rejected(self) -> None:
        self.operation_store.create(
            "EXOP-A",
            "TASK-SHARED",
            expected_task_version=1,
            expected_repository_head="a" * 40,
        )
        with self.assertRaises(ActiveExecutionOperationExistsError):
            self.operation_store.create(
                "EXOP-B",
                "TASK-SHARED",
                expected_task_version=1,
                expected_repository_head="a" * 40,
            )

    def test_stale_task_version_rejected_at_prepare(self) -> None:
        task_id, version = self._create_approved_task()
        with self.assertRaises(StaleExecutionStartError):
            prepare_execution_operation(
                self.root,
                self.store,
                self.operation_store,
                task_id=task_id,
                operation_id="EXOP-STALE-VERSION",
                expected_version=version + 1,
                config=self._one_shot_config(),
            )

    def test_task_not_at_spec_approved_is_rejected(self) -> None:
        task_id = f"TASK-{uuid.uuid4().hex[:12].upper()}"
        specification = self._build_specification(task_id)
        created = self.store.create_task(specification)
        with self.assertRaises(ExecutionOperationError):
            prepare_execution_operation(
                self.root,
                self.store,
                self.operation_store,
                task_id=task_id,
                operation_id="EXOP-WRONG-STATE",
                expected_version=created.version,
                config=self._one_shot_config(),
            )

    def test_repository_head_changed_between_prepare_and_run_is_rejected(self) -> None:
        task_id, version = self._create_approved_task()
        config = self._one_shot_config()
        prepare_execution_operation(
            self.root,
            self.store,
            self.operation_store,
            task_id=task_id,
            operation_id="EXOP-STALE-HEAD",
            expected_version=version,
            config=config,
        )
        (self.root / "surprise.txt").write_text("changed after prepare\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "unexpected upstream change")
        with self.assertRaises(StaleExecutionStartError):
            run_execution_operation(
                self.root,
                self.store,
                self.operation_store,
                config,
                operation_id="EXOP-STALE-HEAD",
            )
        self.assertEqual(
            self.operation_store.get("EXOP-STALE-HEAD").status,
            ExecutionOperationStatus.FAILED,
        )
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.SPEC_APPROVED
        )

    def test_provider_construction_failure_reaches_failed_not_stuck_recorded(
        self,
    ) -> None:
        task_id, version = self._create_approved_task()
        config = self._one_shot_config()
        prepare_execution_operation(
            self.root,
            self.store,
            self.operation_store,
            task_id=task_id,
            operation_id="EXOP-BUILD-FAIL",
            expected_version=version,
            config=config,
        )
        with patch(
            "apoapsis.execution.operation_service._build_providers",
            side_effect=RuntimeError("bad provider config"),
        ):
            with self.assertRaises(RuntimeError):
                run_execution_operation(
                    self.root,
                    self.store,
                    self.operation_store,
                    config,
                    operation_id="EXOP-BUILD-FAIL",
                )
        record = self.operation_store.get("EXOP-BUILD-FAIL")
        self.assertEqual(record.status, ExecutionOperationStatus.FAILED)
        self.assertIn("bad provider config", record.error or "")


class RecoveryTests(ExecutionOperationTestsBase):
    def test_recorded_operation_is_reclaimed_never_run_twice_by_recovery_alone(
        self,
    ) -> None:
        task_id, version = self._create_approved_task()
        prepare_execution_operation(
            self.root,
            self.store,
            self.operation_store,
            task_id=task_id,
            operation_id="EXOP-RECLAIM",
            expected_version=version,
            config=self._one_shot_config(),
        )
        report = recover_stale_execution_operations(self.store, self.operation_store)
        self.assertEqual(report.reclaimed_operation_ids, ["EXOP-RECLAIM"])
        self.assertEqual(
            self.operation_store.get("EXOP-RECLAIM").status,
            ExecutionOperationStatus.RECORDED,
        )

    def test_crash_mid_execution_returns_task_to_review_with_worktree_preserved(
        self,
    ) -> None:
        task_id, version = self._create_approved_task()
        prepare_execution_operation(
            self.root,
            self.store,
            self.operation_store,
            task_id=task_id,
            operation_id="EXOP-CRASH-1",
            expected_version=version,
            config=self._one_shot_config(),
        )
        # Simulate a crash mid-execution: the task reached REPOSITORY_ANALYZED
        # (partway through `_run_from_approved`) but the operation itself
        # never reached a terminal status.
        self.store.transition(
            task_id,
            WorkflowState.REPOSITORY_ANALYZED,
            actor=WorkflowActor.SYSTEM,
            event_type="repository_analyzed",
            expected_version=version,
        )
        self.operation_store.mark_running(
            "EXOP-CRASH-1",
            owner_id=new_owner_id(),
            lease_duration=datetime.timedelta(seconds=-1),
        )
        report = recover_stale_execution_operations(self.store, self.operation_store)
        self.assertEqual(report.ambiguous_operation_ids, ["EXOP-CRASH-1"])
        self.assertEqual(report.tasks_returned_to_review, [task_id])
        self.assertEqual(
            self.store.get_task(task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )
        self.assertEqual(
            self.operation_store.get("EXOP-CRASH-1").status,
            ExecutionOperationStatus.AMBIGUOUS,
        )

        from apoapsis.review.case import build_review_case
        from apoapsis.review.schema import ReviewActionKind, StopReasonKind

        case = build_review_case(
            self.root, self.store, self._one_shot_config(), task_id
        )
        self.assertEqual(case.stop_reason_kind, StopReasonKind.UNKNOWN)
        self.assertEqual(
            set(case.eligible_actions),
            {ReviewActionKind.INSPECT_ONLY, ReviewActionKind.ABANDON},
        )

    def test_crash_after_terminal_state_marks_ambiguous_only(self) -> None:
        task_id, version = self._create_approved_task()
        config = self._one_shot_config()
        record = execute_execution_operation(
            self.root,
            self.store,
            self.operation_store,
            config,
            task_id=task_id,
            operation_id="EXOP-CRASH-2",
            expected_version=version,
            provider=self._provider([COMPLETE_PATCH]),
        )
        self.assertEqual(self.store.get_task(task_id).state, WorkflowState.COMPLETE)
        # Simulate the operation's own bookkeeping crashing right after the
        # real work (the task transition to COMPLETE) already landed.
        force_operation_status(
            self.operation_store.database_path,
            "execution_operations",
            "EXOP-CRASH-2",
            status=ExecutionOperationStatus.RUNNING.value,
        )
        report = recover_stale_execution_operations(self.store, self.operation_store)
        self.assertEqual(report.ambiguous_operation_ids, ["EXOP-CRASH-2"])
        self.assertEqual(report.tasks_returned_to_review, [])
        self.assertEqual(self.store.get_task(task_id).state, WorkflowState.COMPLETE)

    def test_recent_running_operation_is_left_alone(self) -> None:
        task_id, version = self._create_approved_task()
        prepare_execution_operation(
            self.root,
            self.store,
            self.operation_store,
            task_id=task_id,
            operation_id="EXOP-RECENT",
            expected_version=version,
            config=self._one_shot_config(),
        )
        self.operation_store.mark_running("EXOP-RECENT", owner_id=new_owner_id())
        report = recover_stale_execution_operations(self.store, self.operation_store)
        self.assertEqual(report.reclaimed_operation_ids, [])
        self.assertEqual(report.ambiguous_operation_ids, [])
        self.assertEqual(
            self.operation_store.get("EXOP-RECENT").status,
            ExecutionOperationStatus.RUNNING,
        )


if __name__ == "__main__":
    unittest.main()
