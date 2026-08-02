"""`plan slice reset` -- clearing one slice's ledger for a fresh attempt.

Re-running a slice was unreachable before this command. Two guards, each
correct alone, combined into a dead end: ``record_package`` refuses to
re-package anything past PACKAGED, and a derived task id is a deterministic
function of (plan, slice), so a second approval hit 'task already exists'
forever. A slice's first attempt was also its only attempt.

These tests pin both halves of the fix -- that a reset slice really is
runnable again, and that reset refuses the states where discarding the
ledger would strand a worktree or change what a later slice builds on.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apoapsis.architect.errors import (
    SliceExecutionNotFoundError,
    SliceResetError,
)
from apoapsis.architect.slice_service import reset_slice
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.workflow.engine import SQLiteTaskStore, TaskStoreError
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState
from tests.helpers import make_specification

W = WorkflowState
PLAN = "PLAN-TEST-0001"
SLICE = "SLICE-001"
TASK = "TASK-TEST-001"
SHA = "a" * 64

TO_HUMAN_REVIEW = (W.SPEC_DRAFTED, W.SPEC_APPROVED, W.HUMAN_REVIEW_REQUIRED)
TO_ROLLED_BACK = (W.SPEC_DRAFTED, W.SPEC_APPROVED, W.ROLLED_BACK)
TO_FAILED = (W.SPEC_DRAFTED, W.SPEC_APPROVED, W.FAILED)
TO_COMPLETE = (
    W.SPEC_DRAFTED,
    W.SPEC_APPROVED,
    W.REPOSITORY_ANALYZED,
    W.CONTEXT_COMPILED,
    W.ROUTED,
    W.IMPLEMENTING,
    W.PATCH_READY,
    W.VERIFYING,
    W.COMPLETE,
)


class PlanSliceResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "project"
        metadata = self.root / ".apoapsis"
        metadata.mkdir(parents=True)
        self.tasks = SQLiteTaskStore(metadata / "apoapsis.db")
        self.slices = PlanSliceExecutionStore(metadata / "plan-slice-executions.db")
        self.specification = make_specification(TASK)

    def _approved_slice(self, states: tuple[WorkflowState, ...]) -> None:
        """An approved slice whose derived task has reached ``states[-1]``."""

        record = self.tasks.create_task(self.specification)
        for state in states:
            record = self.tasks.transition(
                TASK, state, actor=WorkflowActor.SYSTEM, event_type="fixture"
            )
        self.slices.record_package(
            PLAN, SLICE, plan_version=5, package_sha256=SHA
        )
        self.slices.approve(
            PLAN,
            SLICE,
            expected_package_sha256=SHA,
            task_id=TASK,
            task_expected_version=record.version,
        )

    def test_stopped_slice_is_refused_and_sent_to_rollback_first(self) -> None:
        # HUMAN_REVIEW_REQUIRED usually still owns a worktree and a branch;
        # deleting the task that names them would orphan both.
        self._approved_slice(TO_HUMAN_REVIEW)

        with self.assertRaises(SliceResetError) as caught:
            reset_slice(self.root, self.tasks, self.slices, PLAN, SLICE)

        self.assertIn("apoapsis rollback", str(caught.exception))
        self.assertEqual(len(self.slices.list_for_plan(PLAN)), 1)
        self.assertEqual(self.tasks.get_task(TASK).state, W.HUMAN_REVIEW_REQUIRED)

    def test_rolled_back_slice_clears_both_ledger_rows(self) -> None:
        self._approved_slice(TO_ROLLED_BACK)

        result = reset_slice(self.root, self.tasks, self.slices, PLAN, SLICE)

        self.assertEqual(result["status"], "reset")
        self.assertEqual(result["deleted_task"]["task_id"], TASK)
        self.assertEqual(result["deleted_task"]["state_before_reset"], "ROLLED_BACK")
        self.assertEqual(self.slices.list_for_plan(PLAN), [])
        with self.assertRaises(Exception):
            self.tasks.get_task(TASK)

    def test_failed_slice_needs_no_override(self) -> None:
        self._approved_slice(TO_FAILED)

        result = reset_slice(self.root, self.tasks, self.slices, PLAN, SLICE)

        self.assertEqual(result["deleted_task"]["state_before_reset"], "FAILED")

    def test_reset_slice_can_be_packaged_and_approved_again(self) -> None:
        # The whole point: the deterministic task id becomes usable again,
        # and the new attempt starts with a clean history rather than
        # inheriting the previous run's events.
        self._approved_slice(TO_ROLLED_BACK)
        reset_slice(self.root, self.tasks, self.slices, PLAN, SLICE)

        recreated = self.tasks.create_task(self.specification)
        packaged = self.slices.record_package(
            PLAN, SLICE, plan_version=5, package_sha256="b" * 64
        )

        self.assertEqual(recreated.task_id, TASK)
        self.assertEqual(recreated.version, 1)
        self.assertEqual(
            [event.event_type for event in self.tasks.events(TASK)],
            ["task_created"],
        )
        self.assertEqual(packaged.status.value, "packaged")

    def test_completed_slice_is_refused_without_an_explicit_override(self) -> None:
        # A completed slice's branch is what later slices inherit as their
        # execution base, so discarding it silently would change what a
        # subsequent slice is built on top of.
        self._approved_slice(TO_COMPLETE)

        with self.assertRaises(SliceResetError) as caught:
            reset_slice(self.root, self.tasks, self.slices, PLAN, SLICE)
        self.assertIn("--allow-completed", str(caught.exception))

        result = reset_slice(
            self.root, self.tasks, self.slices, PLAN, SLICE, allow_completed=True
        )
        self.assertEqual(result["deleted_task"]["state_before_reset"], "COMPLETE")

    def test_delete_task_enforces_allowed_states_on_its_own(self) -> None:
        self._approved_slice(TO_HUMAN_REVIEW)

        with self.assertRaises(TaskStoreError) as caught:
            self.tasks.delete_task(TASK, allowed_states=(W.ROLLED_BACK,))

        self.assertIn("can only be deleted from", str(caught.exception))
        self.assertEqual(self.tasks.get_task(TASK).state, W.HUMAN_REVIEW_REQUIRED)

    def test_unknown_slice_raises_not_found(self) -> None:
        self._approved_slice(TO_ROLLED_BACK)

        with self.assertRaises(SliceExecutionNotFoundError):
            reset_slice(self.root, self.tasks, self.slices, PLAN, "SLICE-999")

    def test_reset_writes_nothing_outside_the_metadata_directory(self) -> None:
        self._approved_slice(TO_ROLLED_BACK)

        reset_slice(self.root, self.tasks, self.slices, PLAN, SLICE)

        self.assertEqual([item.name for item in self.root.iterdir()], [".apoapsis"])


if __name__ == "__main__":
    unittest.main()
