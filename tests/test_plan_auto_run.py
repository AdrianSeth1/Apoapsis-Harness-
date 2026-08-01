from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from apoapsis.architect.auto_run import (
    PlanRunStatus,
    PlanRunStore,
    PlanRunWorker,
    config_digest,
    run_plan,
)
from apoapsis.ui.application import ApoapsisUIService
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState
from tests.test_architect_slice import PlanSliceExecutionTestsBase


class PlanRunStoreTests(unittest.TestCase):
    def test_only_one_active_run_per_plan_and_running_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = PlanRunStore(Path(temporary) / "runs.db")
            created = store.create(
                run_id="PLANRUN-1",
                plan_id="PLAN-1",
                expected_plan_version=3,
                config_sha256="a" * 64,
                auto_advance=True,
            )
            self.assertEqual(created.status, PlanRunStatus.RECORDED)
            with self.assertRaisesRegex(Exception, "active run"):
                store.create(
                    run_id="PLANRUN-2",
                    plan_id="PLAN-1",
                    expected_plan_version=3,
                    config_sha256="a" * 64,
                    auto_advance=True,
                )
            running = store.update(
                created.run_id,
                status=PlanRunStatus.RUNNING,
                current_slice_id="SLICE-1",
                detail="running",
            )
            reopened = PlanRunStore(Path(temporary) / "runs.db")
            self.assertEqual(reopened.get(created.run_id), running)

    def test_idle_worker_shuts_down_without_surviving_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            worker = PlanRunWorker(temporary)
            self.assertTrue(worker.shutdown(timeout_seconds=5.0))


class PlanAutoRunTests(PlanSliceExecutionTestsBase):
    def _complete_without_model(
        self,
        _root,
        task_store,
        slice_store,
        _operation_store,
        plan_id,
        slice_id,
        _config,
        *,
        operation_id,
    ):
        from apoapsis.architect.slice_service import read_latest_slice_package
        from apoapsis.execution.worktree import WorktreeManager

        record = slice_store.get(plan_id, slice_id)
        package = read_latest_slice_package(_root, plan_id, slice_id)
        assert package is not None
        WorktreeManager(_root).create(
            record.task_id.removeprefix("TASK-").lower(),
            base_ref=package.execution_base_commit or package.repository_head_commit,
        )
        task = task_store.get_task(record.task_id)
        for state in (
            WorkflowState.REPOSITORY_ANALYZED,
            WorkflowState.CONTEXT_COMPILED,
            WorkflowState.ROUTED,
            WorkflowState.IMPLEMENTING,
            WorkflowState.PATCH_READY,
            WorkflowState.VERIFYING,
            WorkflowState.COMPLETE,
        ):
            task = task_store.transition(
                task.task_id,
                state,
                actor=WorkflowActor.SYSTEM,
                event_type=f"test_{state.value.lower()}",
                payload={},
                expected_version=task.version,
            )
        return Mock(operation_id=operation_id)

    def test_auto_mode_packages_approves_executes_and_advances(self) -> None:
        from tests.architect_helpers import make_slice

        first = make_slice(slice_id="SLICE-1")
        second = make_slice(slice_id="SLICE-2", dependencies=["SLICE-1"])
        plan, config = self._approved_plan(slices=[first, second])
        store = PlanRunStore(self.root / ".apoapsis" / "plan-runs.db")
        run = store.create(
            run_id="PLANRUN-AUTO",
            plan_id=plan.plan_id,
            expected_plan_version=plan.version,
            config_sha256=config_digest(config),
            auto_advance=True,
        )
        result = run_plan(
            self.root,
            store,
            run.run_id,
            execute_slice=self._complete_without_model,
            config_override=config,
        )
        self.assertEqual(result.status, PlanRunStatus.SUCCEEDED)
        self.assertEqual(result.completed_slice_ids, ["SLICE-1", "SLICE-2"])
        first_record = self.slice_store.get(plan.plan_id, "SLICE-1")
        approval = next(
            event
            for event in self.task_store.events(first_record.task_id)
            if event.event_type == "plan_slice_auto_approved"
        )
        self.assertEqual(approval.actor, WorkflowActor.SYSTEM)
        self.assertEqual(approval.payload["plan_run_id"], run.run_id)

    def test_manual_mode_stops_after_one_complete_slice(self) -> None:
        from tests.architect_helpers import make_slice

        first = make_slice(slice_id="SLICE-1")
        second = make_slice(slice_id="SLICE-2", dependencies=["SLICE-1"])
        plan, config = self._approved_plan(slices=[first, second])
        store = PlanRunStore(self.root / ".apoapsis" / "plan-runs.db")
        run = store.create(
            run_id="PLANRUN-NEXT",
            plan_id=plan.plan_id,
            expected_plan_version=plan.version,
            config_sha256=config_digest(config),
            auto_advance=False,
        )
        result = run_plan(
            self.root,
            store,
            run.run_id,
            execute_slice=self._complete_without_model,
            config_override=config,
        )
        self.assertEqual(result.status, PlanRunStatus.SUCCEEDED)
        self.assertEqual(result.completed_slice_ids, ["SLICE-1"])

    def test_configuration_drift_stops_before_packaging(self) -> None:
        plan, config = self._approved_plan()
        store = PlanRunStore(self.root / ".apoapsis" / "plan-runs.db")
        run = store.create(
            run_id="PLANRUN-DRIFT",
            plan_id=plan.plan_id,
            expected_plan_version=plan.version,
            config_sha256=config_digest(config),
            auto_advance=True,
        )
        drifted = config.model_copy(
            update={"patch": config.patch.model_copy(update={"max_files": 21})}
        )
        result = run_plan(self.root, store, run.run_id, config_override=drifted)
        self.assertEqual(result.status, PlanRunStatus.PAUSED)
        self.assertIn("Configuration changed", result.detail)
        self.assertEqual(self.slice_store.list_for_plan(plan.plan_id), [])

    def test_service_records_one_version_bound_authorization(self) -> None:
        plan, config = self._approved_plan()
        service = ApoapsisUIService(self.root)
        worker = Mock()
        with (
            patch.object(service, "_plan_run_worker_instance", return_value=worker),
            patch.object(service, "_config", return_value=config),
        ):
            result = service.start_plan_run(
                plan.plan_id,
                expected_plan_version=plan.version,
                auto_advance=True,
                run_id="PLANRUN-SERVICE",
            )
        self.assertEqual(result["status"], "recorded")
        self.assertTrue(result["auto_advance"])
        worker.submit.assert_called_once_with("PLANRUN-SERVICE")


if __name__ == "__main__":
    unittest.main()
