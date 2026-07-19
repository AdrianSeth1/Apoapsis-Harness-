from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from apoapsis.execution.operation_errors import (
    ActiveExecutionOperationExistsError,
    DuplicateExecutionOperationError,
    ExecutionOperationAlreadyRunningError,
    ExecutionOperationError,
    ExecutionOperationNotFoundError,
)
from apoapsis.execution.operation_schema import (
    ExecutionOperationRecord,
    ExecutionOperationStatus,
)
from apoapsis.specification.schema import utc_now

_ACTIVE_STATUSES = (
    ExecutionOperationStatus.RECORDED.value,
    ExecutionOperationStatus.RUNNING.value,
)


class ExecutionOperationStore:
    """Persistent, idempotent ledger of post-approval task-execution
    operations (ADR 0024), structurally mirroring
    ``review.store.ReviewOperationStore`` and
    ``intake.store.IntakeOperationStore``.

    ``operation_id`` is a stable, caller-supplied idempotency key: creating
    the same ``operation_id`` twice is rejected outright
    (``DuplicateExecutionOperationError``), and only one RECORDED/RUNNING
    operation may exist per task at a time
    (``ActiveExecutionOperationExistsError``). An operation left ``RUNNING``
    can never be silently re-entered
    (``ExecutionOperationAlreadyRunningError``); explicit recovery
    (``execution.operation_recovery.recover_stale_execution_operations``) is
    the only path that ever moves a stuck ``RUNNING`` operation forward,
    into the terminal, inspectable ``AMBIGUOUS`` status -- never back into
    execution.
    """

    def __init__(self, database_path: str | Path, *, initialize: bool = True) -> None:
        self.database_path = Path(database_path)
        if initialize:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        elif not self.database_path.is_file():
            raise ExecutionOperationError(
                f"execution operation database does not exist: {self.database_path}"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=5.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_operations (
                    operation_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    expected_task_version INTEGER NOT NULL,
                    expected_repository_head TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_summary TEXT,
                    error TEXT,
                    report_path TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_execution_operations_task_status
                ON execution_operations(task_id, status);
                """
            )

    def create(
        self,
        operation_id: str,
        task_id: str,
        *,
        expected_task_version: int,
        expected_repository_head: str,
    ) -> ExecutionOperationRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT operation_id FROM execution_operations "
                "WHERE task_id = ? AND status IN (?, ?)",
                (task_id, *_ACTIVE_STATUSES),
            ).fetchone()
            if active is not None:
                connection.rollback()
                raise ActiveExecutionOperationExistsError(
                    f"task {task_id} already has an active execution "
                    f"operation {active['operation_id']}; wait for it to "
                    "finish or inspect it before submitting another"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO execution_operations (
                        operation_id, task_id, expected_task_version,
                        expected_repository_head, status, created_at,
                        updated_at, result_summary, error, report_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                    """,
                    (
                        operation_id,
                        task_id,
                        expected_task_version,
                        expected_repository_head,
                        ExecutionOperationStatus.RECORDED.value,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DuplicateExecutionOperationError(
                    f"operation already submitted: {operation_id}"
                ) from exc
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(operation_id)

    def get(self, operation_id: str) -> ExecutionOperationRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM execution_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise ExecutionOperationNotFoundError(operation_id)
        return self._row_to_record(row)

    def find_active_for_task(self, task_id: str) -> ExecutionOperationRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM execution_operations "
                "WHERE task_id = ? AND status IN (?, ?) "
                "ORDER BY created_at DESC LIMIT 1",
                (task_id, *_ACTIVE_STATUSES),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def list_active(self) -> list[ExecutionOperationRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM execution_operations WHERE status IN (?, ?) "
                "ORDER BY created_at ASC",
                _ACTIVE_STATUSES,
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def mark_running(self, operation_id: str) -> ExecutionOperationRecord:
        return self._transition(
            operation_id,
            from_statuses={ExecutionOperationStatus.RECORDED},
            to_status=ExecutionOperationStatus.RUNNING,
        )

    def mark_succeeded(
        self, operation_id: str, *, result_summary: str, report_path: str | None = None
    ) -> ExecutionOperationRecord:
        return self._transition(
            operation_id,
            from_statuses={ExecutionOperationStatus.RUNNING},
            to_status=ExecutionOperationStatus.SUCCEEDED,
            result_summary=result_summary,
            report_path=report_path,
        )

    def mark_failed(self, operation_id: str, *, error: str) -> ExecutionOperationRecord:
        return self._transition(
            operation_id,
            from_statuses={ExecutionOperationStatus.RUNNING},
            to_status=ExecutionOperationStatus.FAILED,
            error=error,
        )

    def mark_ambiguous(self, operation_id: str, *, note: str) -> ExecutionOperationRecord:
        """Explicit crash recovery only
        (``execution.operation_recovery``): moves a RUNNING operation
        whose owning process appears to have died into the terminal,
        inspectable ``AMBIGUOUS`` status. Never called as part of ordinary
        execution."""

        return self._transition(
            operation_id,
            from_statuses={ExecutionOperationStatus.RUNNING},
            to_status=ExecutionOperationStatus.AMBIGUOUS,
            error=note,
        )

    def _transition(
        self,
        operation_id: str,
        *,
        from_statuses: set[ExecutionOperationStatus],
        to_status: ExecutionOperationStatus,
        result_summary: str | None = None,
        error: str | None = None,
        report_path: str | None = None,
    ) -> ExecutionOperationRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM execution_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise ExecutionOperationNotFoundError(operation_id)
            current = ExecutionOperationStatus(row["status"])
            if current == ExecutionOperationStatus.RUNNING and to_status == (
                ExecutionOperationStatus.RUNNING
            ):
                raise ExecutionOperationAlreadyRunningError(
                    f"operation {operation_id} is already RUNNING; refusing "
                    "to start it again -- inspect it before creating a new "
                    "operation"
                )
            if current not in from_statuses:
                raise ExecutionOperationError(
                    f"operation {operation_id} cannot move to {to_status.value} "
                    f"from {current.value}"
                )
            connection.execute(
                """
                UPDATE execution_operations
                SET status = ?, updated_at = ?,
                    result_summary = COALESCE(?, result_summary),
                    error = COALESCE(?, error),
                    report_path = COALESCE(?, report_path)
                WHERE operation_id = ?
                """,
                (
                    to_status.value,
                    now.isoformat(),
                    result_summary,
                    error,
                    report_path,
                    operation_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(operation_id)

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ExecutionOperationRecord:
        return ExecutionOperationRecord(
            operation_id=row["operation_id"],
            task_id=row["task_id"],
            expected_task_version=row["expected_task_version"],
            expected_repository_head=row["expected_repository_head"],
            status=ExecutionOperationStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result_summary=row["result_summary"],
            error=row["error"],
            report_path=row["report_path"],
        )


__all__ = ["ExecutionOperationStore"]
