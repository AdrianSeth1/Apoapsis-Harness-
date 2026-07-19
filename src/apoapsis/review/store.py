from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from apoapsis.review.errors import (
    ActiveOperationExistsError,
    DuplicateOperationError,
    OperationAlreadyRunningError,
    OperationNotFoundError,
    ReviewError,
)
from apoapsis.review.schema import (
    ContinuationBudget,
    ReviewActionKind,
    ReviewOperationRecord,
    ReviewOperationStatus,
)
from apoapsis.specification.schema import utc_now

_ACTIVE_STATUSES = (
    ReviewOperationStatus.RECORDED.value,
    ReviewOperationStatus.RUNNING.value,
)


class ReviewOperationStore:
    """Persistent, idempotent ledger of human-review operations (ADR 0020,
    hardened by ADR 0021).

    ``operation_id`` is a stable, caller-supplied idempotency key: creating
    the same ``operation_id`` twice is rejected outright
    (``DuplicateOperationError``), and only one RECORDED/RUNNING operation
    may exist per task at a time (``ActiveOperationExistsError``). An
    operation left ``RUNNING`` -- for example because the process crashed
    after a provider request had possibly already been transmitted -- can
    never be silently re-entered (``OperationAlreadyRunningError``); explicit
    recovery (``review.recovery.recover_stale_operations``) is the only path
    that ever moves a stuck ``RUNNING`` operation forward, into the terminal,
    inspectable ``AMBIGUOUS`` status -- never back into execution.
    """

    def __init__(self, database_path: str | Path, *, initialize: bool = True) -> None:
        self.database_path = Path(database_path)
        if initialize:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        elif not self.database_path.is_file():
            raise ReviewError(
                f"review operation database does not exist: {self.database_path}"
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
                CREATE TABLE IF NOT EXISTS review_operations (
                    operation_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    expected_task_version INTEGER NOT NULL,
                    expected_worktree_fingerprint TEXT,
                    authorized_budget_json TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_summary TEXT,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_review_operations_task_status
                ON review_operations(task_id, status);
                """
            )

    def create(
        self,
        operation_id: str,
        task_id: str,
        action: ReviewActionKind,
        *,
        expected_task_version: int,
        expected_worktree_fingerprint: str | None = None,
        authorized_budget: ContinuationBudget | None = None,
    ) -> ReviewOperationRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT operation_id FROM review_operations "
                "WHERE task_id = ? AND status IN (?, ?)",
                (task_id, *_ACTIVE_STATUSES),
            ).fetchone()
            if active is not None:
                connection.rollback()
                raise ActiveOperationExistsError(
                    f"task {task_id} already has an active operation "
                    f"{active['operation_id']}; wait for it to finish or "
                    "inspect it before submitting another"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO review_operations (
                        operation_id, task_id, action, expected_task_version,
                        expected_worktree_fingerprint, authorized_budget_json,
                        status, created_at, updated_at, result_summary, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        operation_id,
                        task_id,
                        action.value,
                        expected_task_version,
                        expected_worktree_fingerprint,
                        (
                            authorized_budget.model_dump_json()
                            if authorized_budget is not None
                            else None
                        ),
                        ReviewOperationStatus.RECORDED.value,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DuplicateOperationError(
                    f"operation already submitted: {operation_id}"
                ) from exc
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(operation_id)

    def get(self, operation_id: str) -> ReviewOperationRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM review_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise OperationNotFoundError(operation_id)
        return self._row_to_record(row)

    def find_active_for_task(self, task_id: str) -> ReviewOperationRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM review_operations "
                "WHERE task_id = ? AND status IN (?, ?) "
                "ORDER BY created_at DESC LIMIT 1",
                (task_id, *_ACTIVE_STATUSES),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def list_active(self) -> list[ReviewOperationRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM review_operations WHERE status IN (?, ?) "
                "ORDER BY created_at ASC",
                _ACTIVE_STATUSES,
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def mark_running(self, operation_id: str) -> ReviewOperationRecord:
        return self._transition(
            operation_id,
            from_statuses={ReviewOperationStatus.RECORDED},
            to_status=ReviewOperationStatus.RUNNING,
        )

    def mark_succeeded(
        self, operation_id: str, *, result_summary: str
    ) -> ReviewOperationRecord:
        return self._transition(
            operation_id,
            from_statuses={ReviewOperationStatus.RUNNING},
            to_status=ReviewOperationStatus.SUCCEEDED,
            result_summary=result_summary,
        )

    def mark_failed(self, operation_id: str, *, error: str) -> ReviewOperationRecord:
        return self._transition(
            operation_id,
            from_statuses={ReviewOperationStatus.RUNNING},
            to_status=ReviewOperationStatus.FAILED,
            error=error,
        )

    def mark_ambiguous(self, operation_id: str, *, note: str) -> ReviewOperationRecord:
        """Explicit crash recovery only (ADR 0021, ``review.recovery``):
        moves a RUNNING operation whose owning process appears to have died
        into the terminal, inspectable ``AMBIGUOUS`` status. Never called
        as part of ordinary execution."""

        return self._transition(
            operation_id,
            from_statuses={ReviewOperationStatus.RUNNING},
            to_status=ReviewOperationStatus.AMBIGUOUS,
            error=note,
        )

    def _transition(
        self,
        operation_id: str,
        *,
        from_statuses: set[ReviewOperationStatus],
        to_status: ReviewOperationStatus,
        result_summary: str | None = None,
        error: str | None = None,
    ) -> ReviewOperationRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM review_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise OperationNotFoundError(operation_id)
            current = ReviewOperationStatus(row["status"])
            if current == ReviewOperationStatus.RUNNING and to_status == (
                ReviewOperationStatus.RUNNING
            ):
                raise OperationAlreadyRunningError(
                    f"operation {operation_id} is already RUNNING; refusing to "
                    "start it again -- inspect it before creating a new operation"
                )
            if current not in from_statuses:
                raise ReviewError(
                    f"operation {operation_id} cannot move to {to_status.value} "
                    f"from {current.value}"
                )
            connection.execute(
                """
                UPDATE review_operations
                SET status = ?, updated_at = ?, result_summary = COALESCE(?, result_summary),
                    error = COALESCE(?, error)
                WHERE operation_id = ?
                """,
                (to_status.value, now.isoformat(), result_summary, error, operation_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(operation_id)

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ReviewOperationRecord:
        return ReviewOperationRecord(
            operation_id=row["operation_id"],
            task_id=row["task_id"],
            action=ReviewActionKind(row["action"]),
            expected_task_version=row["expected_task_version"],
            expected_worktree_fingerprint=row["expected_worktree_fingerprint"],
            authorized_budget=(
                ContinuationBudget.model_validate_json(row["authorized_budget_json"])
                if row["authorized_budget_json"]
                else None
            ),
            status=ReviewOperationStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result_summary=row["result_summary"],
            error=row["error"],
        )


__all__ = ["ReviewOperationStore"]
