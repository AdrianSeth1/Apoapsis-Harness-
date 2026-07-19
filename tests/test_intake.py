from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apoapsis.config import (
    ContextCompilerConfig,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ApoapsisConfig,
)
from apoapsis.intake.errors import (
    ActiveIntakeOperationExistsError,
    DuplicateIntakeOperationError,
    IntakeError,
)
from apoapsis.intake.execution import (
    execute_intake_operation,
    prepare_intake_operation,
    run_intake_operation,
)
from apoapsis.intake.recovery import recover_stale_intake_operations
from apoapsis.operations.lease import new_owner_id
from apoapsis.intake.schema import IntakeOperationStatus
from apoapsis.intake.store import IntakeOperationStore
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState
from tests.fakes import FakeModelProvider
from tests.helpers import force_operation_status
from tests.test_specification_correction import (
    _inject_task_id_into_every_json_response,
    _invalid_specification_null_hard_constraint_method,
    _invalid_specification_reworded_verbatim_source,
    _invalid_specification_unknown_catalog_mapping,
)
from tests.test_vertical_slice import COMPLETE_PATCH, REQUEST, specification_response


class IntakeTestsBase(unittest.TestCase):
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
        self.operation_store = IntakeOperationStore(
            self.root / ".apoapsis" / "intake-operations.db"
        )

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )

    def _config(self, *, acceptance_command_names: frozenset[str] = frozenset()) -> ApoapsisConfig:
        return ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1",
                    model="fake-coder-v1",
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
                        acceptance="download-tests" in acceptance_command_names,
                    )
                ]
            ),
        )

    def _audit_dir(self, task_id: str) -> Path:
        return self.root / ".apoapsis" / "tasks" / task_id

    def _provider(self, fake: FakeModelProvider) -> InstrumentedModelProvider:
        return InstrumentedModelProvider(fake)


class SuccessfulExtractionTests(IntakeTestsBase):
    def test_clean_first_response_reaches_spec_drafted(self) -> None:
        fake = FakeModelProvider([specification_response()])
        _inject_task_id_into_every_json_response(fake)
        record = execute_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            self._config(),
            request_text=REQUEST,
            operation_id="INOP-1",
            provider=self._provider(fake),
        )
        self.assertEqual(
            record.status, IntakeOperationStatus.PENDING_SPECIFICATION_APPROVAL
        )
        task = self.store.get_task(record.task_id)
        self.assertEqual(task.state, WorkflowState.SPEC_DRAFTED)
        # The task's specification is now the model's validated extraction
        # (its own, possibly reworded objective); the operation record is
        # the durable, verbatim home for the original request text.
        self.assertEqual(record.request_text, REQUEST)
        self.assertEqual(len(fake.invocations), 1)

        audit = self._audit_dir(record.task_id)
        self.assertTrue((audit / "call-001-context.json").is_file())
        self.assertTrue((audit / "call-001-request.json").is_file())
        self.assertTrue((audit / "call-001-response.json").is_file())
        self.assertTrue((audit / "call-001-telemetry.json").is_file())
        candidate = json.loads(
            (audit / "approved-specification-candidate.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(candidate["task_id"], record.task_id)

    def test_first_response_correction_reaches_spec_drafted(self) -> None:
        fake = FakeModelProvider(
            [
                _invalid_specification_null_hard_constraint_method(),
                specification_response(),
            ]
        )
        _inject_task_id_into_every_json_response(fake)
        record = execute_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            self._config(),
            request_text=REQUEST,
            operation_id="INOP-2",
            provider=self._provider(fake),
        )
        self.assertEqual(
            record.status, IntakeOperationStatus.PENDING_SPECIFICATION_APPROVAL
        )
        self.assertEqual(
            self.store.get_task(record.task_id).state, WorkflowState.SPEC_DRAFTED
        )
        self.assertEqual(len(fake.invocations), 2)

        audit = self._audit_dir(record.task_id)
        failure = json.loads(
            (audit / "specification-extraction-failure-001.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("verification_method", failure["error"])
        correction_request = json.loads(
            (audit / "call-002-request.json").read_text(encoding="utf-8")
        )
        self.assertIn("VALIDATION_ERRORS", correction_request["prompt"])
        self.assertTrue((audit / "call-002-telemetry.json").is_file())

    def test_approval_after_intake_uses_the_existing_unmodified_transition(
        self,
    ) -> None:
        fake = FakeModelProvider([specification_response()])
        _inject_task_id_into_every_json_response(fake)
        record = execute_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            self._config(),
            request_text=REQUEST,
            operation_id="INOP-APPROVE",
            provider=self._provider(fake),
        )
        drafted = self.store.get_task(record.task_id)
        approved = self.store.transition(
            record.task_id,
            WorkflowState.SPEC_APPROVED,
            actor=WorkflowActor.USER,
            event_type="specification_approved",
            expected_version=drafted.version,
        )
        self.assertEqual(approved.state, WorkflowState.SPEC_APPROVED)


class BoundedFailureTests(IntakeTestsBase):
    def test_double_failure_stops_deterministically_at_failed(self) -> None:
        fake = FakeModelProvider(
            [
                _invalid_specification_null_hard_constraint_method(),
                _invalid_specification_null_hard_constraint_method(),
                COMPLETE_PATCH,  # must never be consumed -- proves the retry ceiling
            ]
        )
        _inject_task_id_into_every_json_response(fake)
        record = execute_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            self._config(),
            request_text=REQUEST,
            operation_id="INOP-FAIL-1",
            provider=self._provider(fake),
        )
        self.assertEqual(record.status, IntakeOperationStatus.FAILED)
        self.assertIn("verification_method", record.error or "")
        self.assertEqual(
            self.store.get_task(record.task_id).state, WorkflowState.FAILED
        )
        self.assertEqual(len(fake.invocations), 2)

    def test_correction_still_enforces_verbatim_constraint_preservation(self) -> None:
        fake = FakeModelProvider(
            [
                _invalid_specification_null_hard_constraint_method(),
                _invalid_specification_reworded_verbatim_source(),
            ]
        )
        _inject_task_id_into_every_json_response(fake)
        record = execute_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            self._config(),
            request_text=REQUEST,
            operation_id="INOP-FAIL-2",
            provider=self._provider(fake),
        )
        self.assertEqual(record.status, IntakeOperationStatus.FAILED)
        self.assertIn("exact substring", record.error or "")

    def test_correction_still_enforces_acceptance_catalog_membership(self) -> None:
        fake = FakeModelProvider(
            [
                _invalid_specification_null_hard_constraint_method(),
                _invalid_specification_unknown_catalog_mapping(),
            ]
        )
        _inject_task_id_into_every_json_response(fake)
        record = execute_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            self._config(acceptance_command_names=frozenset({"download-tests"})),
            request_text=REQUEST,
            operation_id="INOP-FAIL-3",
            provider=self._provider(fake),
        )
        self.assertEqual(record.status, IntakeOperationStatus.FAILED)
        self.assertIn("acceptance-command", record.error or "")


class OperationLedgerTests(IntakeTestsBase):
    def test_duplicate_operation_id_rejected_once_terminal(self) -> None:
        fake = FakeModelProvider([specification_response()])
        _inject_task_id_into_every_json_response(fake)
        execute_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            self._config(),
            request_text=REQUEST,
            operation_id="INOP-DUP",
            provider=self._provider(fake),
        )
        with self.assertRaises(DuplicateIntakeOperationError):
            prepare_intake_operation(
                self.root,
                self.store,
                self.operation_store,
                request_text=REQUEST,
                operation_id="INOP-DUP",
            )

    def test_second_active_operation_for_same_task_rejected(self) -> None:
        self.operation_store.create(
            "INOP-A",
            "TASK-SHARED",
            REQUEST,
            request_sha256="0" * 64,
            expected_task_version=1,
            provider_role="FRONTIER_IMPLEMENTATION",
        )
        with self.assertRaises(ActiveIntakeOperationExistsError):
            self.operation_store.create(
                "INOP-B",
                "TASK-SHARED",
                REQUEST,
                request_sha256="0" * 64,
                expected_task_version=1,
                provider_role="FRONTIER_IMPLEMENTATION",
            )

    def test_different_tasks_may_each_have_an_active_operation(self) -> None:
        self.operation_store.create(
            "INOP-C",
            "TASK-ONE",
            REQUEST,
            request_sha256="0" * 64,
            expected_task_version=1,
            provider_role="FRONTIER_IMPLEMENTATION",
        )
        record = self.operation_store.create(
            "INOP-D",
            "TASK-TWO",
            REQUEST,
            request_sha256="0" * 64,
            expected_task_version=1,
            provider_role="FRONTIER_IMPLEMENTATION",
        )
        self.assertEqual(record.task_id, "TASK-TWO")

    def test_exact_request_text_preserved_verbatim(self) -> None:
        text = "Weird text with\ttabs, unicode é, and \"quotes\".\nSecond line."
        record = prepare_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            request_text=text,
            operation_id="INOP-VERBATIM",
        )
        self.assertEqual(record.request_text, text)
        self.assertEqual(
            self.store.get_task(record.task_id).specification.objective.text, text
        )


class QueueDelayAndFailureTests(IntakeTestsBase):
    def test_provider_construction_failure_reaches_failed_not_stuck_recorded(
        self,
    ) -> None:
        prepare_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            request_text=REQUEST,
            operation_id="INOP-BUILD-FAIL",
        )
        with patch(
            "apoapsis.intake.execution._build_provider",
            side_effect=RuntimeError("bad provider config"),
        ):
            with self.assertRaises(RuntimeError):
                run_intake_operation(
                    self.root,
                    self.store,
                    self.operation_store,
                    self._config(),
                    operation_id="INOP-BUILD-FAIL",
                )
        record = self.operation_store.get("INOP-BUILD-FAIL")
        self.assertEqual(record.status, IntakeOperationStatus.FAILED)
        self.assertIn("bad provider config", record.error or "")

    def test_task_mutated_between_prepare_and_run_is_rejected(self) -> None:
        record = prepare_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            request_text=REQUEST,
            operation_id="INOP-STALE",
        )
        # Simulate a queue-delay race: something else moves the task before
        # the worker actually dequeues and runs this operation.
        self.store.transition(
            record.task_id,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            actor=WorkflowActor.USER,
            event_type="manual_test_mutation",
            expected_version=record.expected_task_version,
        )
        fake = FakeModelProvider([specification_response()])
        with self.assertRaises(IntakeError):
            run_intake_operation(
                self.root,
                self.store,
                self.operation_store,
                self._config(),
                operation_id="INOP-STALE",
                provider=self._provider(fake),
            )
        self.assertEqual(len(fake.invocations), 0)
        self.assertEqual(
            self.operation_store.get("INOP-STALE").status,
            IntakeOperationStatus.FAILED,
        )


class RecoveryTests(IntakeTestsBase):
    def test_recorded_operation_is_reclaimed_never_run_twice_by_recovery_alone(
        self,
    ) -> None:
        prepare_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            request_text=REQUEST,
            operation_id="INOP-RECLAIM",
        )
        report = recover_stale_intake_operations(self.store, self.operation_store)
        self.assertEqual(report.reclaimed_operation_ids, ["INOP-RECLAIM"])
        self.assertEqual(
            self.operation_store.get("INOP-RECLAIM").status,
            IntakeOperationStatus.RECORDED,
        )

    def test_crash_while_still_at_intake_returns_task_to_review(self) -> None:
        record = prepare_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            request_text=REQUEST,
            operation_id="INOP-CRASH-1",
        )
        self.operation_store.mark_running(
            "INOP-CRASH-1",
            owner_id=new_owner_id(),
            lease_duration=datetime.timedelta(seconds=-1),
        )
        report = recover_stale_intake_operations(self.store, self.operation_store)
        self.assertEqual(report.ambiguous_operation_ids, ["INOP-CRASH-1"])
        self.assertEqual(report.tasks_returned_to_review, [record.task_id])
        task = self.store.get_task(record.task_id)
        self.assertEqual(task.state, WorkflowState.HUMAN_REVIEW_REQUIRED)
        self.assertEqual(
            self.operation_store.get("INOP-CRASH-1").status,
            IntakeOperationStatus.AMBIGUOUS,
        )

        from apoapsis.review.case import build_review_case
        from apoapsis.review.schema import ReviewActionKind, StopReasonKind

        case = build_review_case(self.root, self.store, self._config(), record.task_id)
        self.assertEqual(case.stop_reason_kind, StopReasonKind.UNKNOWN)
        self.assertEqual(
            set(case.eligible_actions),
            {ReviewActionKind.INSPECT_ONLY, ReviewActionKind.ABANDON},
        )

    def test_crash_after_reaching_spec_drafted_marks_ambiguous_only(self) -> None:
        fake = FakeModelProvider([specification_response()])
        _inject_task_id_into_every_json_response(fake)
        record = execute_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            self._config(),
            request_text=REQUEST,
            operation_id="INOP-CRASH-2",
            provider=self._provider(fake),
        )
        self.assertEqual(
            self.store.get_task(record.task_id).state, WorkflowState.SPEC_DRAFTED
        )
        # Simulate the bookkeeping call itself crashing right after the real
        # work (the task transition) already landed.
        force_operation_status(
            self.operation_store.database_path,
            "intake_operations",
            "INOP-CRASH-2",
            status=IntakeOperationStatus.RUNNING.value,
        )
        report = recover_stale_intake_operations(self.store, self.operation_store)
        self.assertEqual(report.ambiguous_operation_ids, ["INOP-CRASH-2"])
        self.assertEqual(report.tasks_returned_to_review, [])
        self.assertEqual(
            self.store.get_task(record.task_id).state, WorkflowState.SPEC_DRAFTED
        )

    def test_recent_running_operation_is_left_alone(self) -> None:
        prepare_intake_operation(
            self.root,
            self.store,
            self.operation_store,
            request_text=REQUEST,
            operation_id="INOP-RECENT",
        )
        self.operation_store.mark_running("INOP-RECENT", owner_id=new_owner_id())
        report = recover_stale_intake_operations(self.store, self.operation_store)
        self.assertEqual(report.reclaimed_operation_ids, [])
        self.assertEqual(report.ambiguous_operation_ids, [])
        self.assertEqual(
            self.operation_store.get("INOP-RECENT").status,
            IntakeOperationStatus.RUNNING,
        )


if __name__ == "__main__":
    unittest.main()
