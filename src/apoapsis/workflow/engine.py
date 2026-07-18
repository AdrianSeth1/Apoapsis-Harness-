from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from apoapsis.specification.schema import StrictModel, TaskSpecification, utc_now
from apoapsis.workflow.events import WorkflowActor, WorkflowEvent
from apoapsis.workflow.states import WorkflowState, transition_is_allowed


class TaskStoreError(RuntimeError):
    """Base error for persisted workflow operations."""


class TaskNotFoundError(TaskStoreError):
    """Raised when a task identifier is not present in the store."""


class InvalidTransitionError(TaskStoreError):
    """Raised when a requested state edge is not in the transition table."""


class ConcurrentTransitionError(TaskStoreError):
    """Raised when optimistic task-version validation fails."""


class TaskRecord(StrictModel):
    task_id: str
    specification: TaskSpecification
    state: WorkflowState
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class SQLiteTaskStore:
    """Persistent task state with atomic, optimistic transitions."""

    def __init__(
        self, database_path: str | Path, *, initialize: bool = True
    ) -> None:
        self.database_path = Path(database_path)
        if initialize:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        elif not self.database_path.is_file():
            raise TaskStoreError(f"task database does not exist: {self.database_path}")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    specification_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_workflow_events_task_sequence
                ON workflow_events(task_id, sequence);
                """
            )

    def create_task(
        self,
        specification: TaskSpecification,
        *,
        actor: WorkflowActor = WorkflowActor.USER,
    ) -> TaskRecord:
        now = utc_now()
        event_id = self._new_event_id()
        specification_json = specification.model_dump_json()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, specification_json, state, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (
                    specification.task_id,
                    specification_json,
                    WorkflowState.INTAKE.value,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO workflow_events (
                    event_id, task_id, event_type, from_state, to_state,
                    actor, payload_json, created_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    specification.task_id,
                    "task_created",
                    WorkflowState.INTAKE.value,
                    actor.value,
                    "{}",
                    now.isoformat(),
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise TaskStoreError(
                f"task already exists: {specification.task_id}"
            ) from exc
        finally:
            connection.close()
        return self.get_task(specification.task_id)

    def get_task(self, task_id: str) -> TaskRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFoundError(task_id)
        return self._row_to_task(row)

    def list_tasks(self, *, limit: int = 100) -> list[TaskRecord]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def transition(
        self,
        task_id: str,
        target: WorkflowState,
        *,
        actor: WorkflowActor,
        event_type: str = "state_transition",
        payload: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> TaskRecord:
        if not event_type.strip():
            raise ValueError("event_type must not be empty")
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, version FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(task_id)
            source = WorkflowState(row["state"])
            version = int(row["version"])
            if expected_version is not None and version != expected_version:
                raise ConcurrentTransitionError(
                    f"expected version {expected_version}, found {version}"
                )
            if not transition_is_allowed(source, target):
                raise InvalidTransitionError(
                    f"transition {source.value} -> {target.value} is not allowed"
                )
            cursor = connection.execute(
                """
                UPDATE tasks
                SET state = ?, version = version + 1, updated_at = ?
                WHERE task_id = ? AND version = ?
                """,
                (target.value, now.isoformat(), task_id, version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentTransitionError(
                    f"task {task_id} changed during transition"
                )
            connection.execute(
                """
                INSERT INTO workflow_events (
                    event_id, task_id, event_type, from_state, to_state,
                    actor, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._new_event_id(),
                    task_id,
                    event_type,
                    source.value,
                    target.value,
                    actor.value,
                    json.dumps(payload or {}, sort_keys=True),
                    now.isoformat(),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_task(task_id)

    def update_specification(
        self,
        specification: TaskSpecification,
        *,
        actor: WorkflowActor,
        expected_version: int | None = None,
    ) -> TaskRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, version FROM tasks WHERE task_id = ?",
                (specification.task_id,),
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(specification.task_id)
            state = WorkflowState(row["state"])
            version = int(row["version"])
            if expected_version is not None and version != expected_version:
                raise ConcurrentTransitionError(
                    f"expected version {expected_version}, found {version}"
                )
            if state not in {
                WorkflowState.INTAKE,
                WorkflowState.SPEC_DRAFTED,
                WorkflowState.HUMAN_REVIEW_REQUIRED,
            }:
                raise InvalidTransitionError(
                    f"specification cannot be edited in {state.value}"
                )
            cursor = connection.execute(
                """
                UPDATE tasks
                SET specification_json = ?, version = version + 1,
                    updated_at = ?
                WHERE task_id = ? AND version = ?
                """,
                (
                    specification.model_dump_json(),
                    now.isoformat(),
                    specification.task_id,
                    version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentTransitionError(
                    f"task {specification.task_id} changed during update"
                )
            connection.execute(
                """
                INSERT INTO workflow_events (
                    event_id, task_id, event_type, from_state, to_state,
                    actor, payload_json, created_at
                ) VALUES (?, ?, 'specification_updated', ?, ?, ?, '{}', ?)
                """,
                (
                    self._new_event_id(),
                    specification.task_id,
                    state.value,
                    state.value,
                    actor.value,
                    now.isoformat(),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_task(specification.task_id)

    def events(self, task_id: str) -> list[WorkflowEvent]:
        self.get_task(task_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_events
                WHERE task_id = ? ORDER BY sequence ASC
                """,
                (task_id,),
            ).fetchall()
        return [
            WorkflowEvent(
                event_id=row["event_id"],
                sequence=row["sequence"],
                task_id=row["task_id"],
                event_type=row["event_type"],
                from_state=row["from_state"],
                to_state=row["to_state"],
                actor=row["actor"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    @staticmethod
    def _new_event_id() -> str:
        return f"EVT-{uuid.uuid4().hex}"

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            specification=TaskSpecification.model_validate_json(
                row["specification_json"]
            ),
            state=row["state"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
