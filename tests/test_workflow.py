from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apoapsis.specification.schema import SourceKind, TraceableStatement
from apoapsis.workflow.engine import (
    ConcurrentTransitionError,
    InvalidTransitionError,
    SQLiteTaskStore,
    TaskNotFoundError,
)
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState
from tests.helpers import make_specification


class SQLiteTaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Path(self.temporary_directory.name) / "state" / "apoapsis.db"
        self.store = SQLiteTaskStore(self.database)
        self.specification = make_specification()

    def test_task_and_events_survive_store_reopen(self) -> None:
        created = self.store.create_task(self.specification)
        drafted = self.store.transition(
            created.task_id,
            WorkflowState.SPEC_DRAFTED,
            actor=WorkflowActor.SYSTEM,
            event_type="spec_drafted",
            expected_version=1,
        )

        reopened = SQLiteTaskStore(self.database)
        persisted = reopened.get_task(created.task_id)
        events = reopened.events(created.task_id)

        self.assertEqual(drafted.version, 2)
        self.assertEqual(persisted.state, WorkflowState.SPEC_DRAFTED)
        self.assertEqual([event.event_type for event in events], [
            "task_created",
            "spec_drafted",
        ])
        self.assertEqual([event.sequence for event in events], sorted(
            event.sequence for event in events if event.sequence is not None
        ))

    def test_invalid_and_stale_transitions_do_not_mutate_state(self) -> None:
        self.store.create_task(self.specification)

        with self.assertRaises(InvalidTransitionError):
            self.store.transition(
                self.specification.task_id,
                WorkflowState.COMPLETE,
                actor=WorkflowActor.SYSTEM,
            )
        with self.assertRaises(ConcurrentTransitionError):
            self.store.transition(
                self.specification.task_id,
                WorkflowState.SPEC_DRAFTED,
                actor=WorkflowActor.SYSTEM,
                expected_version=999,
            )

        persisted = self.store.get_task(self.specification.task_id)
        self.assertEqual(persisted.state, WorkflowState.INTAKE)
        self.assertEqual(persisted.version, 1)
        self.assertEqual(len(self.store.events(self.specification.task_id)), 1)

    def test_specification_update_is_versioned_and_audited(self) -> None:
        created = self.store.create_task(self.specification)
        updated_specification = self.specification.model_copy(
            update={
                "objective": TraceableStatement(
                    text="Add safe resumable downloads.",
                    source=SourceKind.USER,
                    source_reference="message-2",
                )
            }
        )

        updated = self.store.update_specification(
            updated_specification,
            actor=WorkflowActor.USER,
            expected_version=created.version,
        )

        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.state, WorkflowState.INTAKE)
        self.assertEqual(
            updated.specification.objective.text,
            "Add safe resumable downloads.",
        )
        self.assertEqual(
            self.store.events(updated.task_id)[-1].event_type,
            "specification_updated",
        )

    def test_unknown_task_raises(self) -> None:
        with self.assertRaises(TaskNotFoundError):
            self.store.get_task("TASK-NOT-FOUND")


if __name__ == "__main__":
    unittest.main()

