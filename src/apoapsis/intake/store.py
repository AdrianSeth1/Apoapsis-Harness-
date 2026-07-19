from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from apoapsis.intake.errors import (
    ActiveIntakeOperationExistsError,
    DuplicateIntakeOperationError,
    IntakeError,
    IntakeOperationAlreadyRunningError,
    IntakeOperationNotFoundError,
)
from apoapsis.intake.schema import IntakeOperationRecord, IntakeOperationStatus
from apoapsis.specification.schema import utc_now

_ACTIVE_STATUSES = (
    IntakeOperationStatus.RECORDED.value,
    IntakeOperationStatus.RUNNING.value,
)


class IntakeOperationStore:
    """Persistent, idempotent ledger of new-task intake operations (ADR
    0023), structurally mirroring ``review.store.ReviewOperationStore``.

    ``operation_id`` is a stable, caller-supplied idempotency key: creating
    the same ``operation_id`` twice is rejected outright
    (``DuplicateIntakeOperationError``), and only one RECORDED/RUNNING
    operation may exist per task at a time
    (``ActiveIntakeOperationExistsError``). An operation left ``RUNNING``
    can never be silently re-entered (``IntakeOperationAlreadyRunningError``);
    explicit recovery (``intake.recovery.recover_stale_intake_operations``)
    is the only path that ever moves a stuck ``RUNNING`` operation forward,
    into the terminal, inspectable ``AMBIGUOUS`` status -- never back into
    execution.
    """

    def __init__(self, database_path: str | Path, *, initialize: bool = True) -> None:
        self.database_path = Path(database_path)
        if initialize:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        elif not self.database_path.is_file():
            raise IntakeError(
                f"intake operation database does not exist: {self.database_path}"
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
                CREATE TABLE IF NOT EXISTS intake_operations (
                    operation_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    request_text TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    expected_task_version INTEGER NOT NULL,
                    provider_role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_summary TEXT,
                    error TEXT,
                    audit_artifact_locations_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_intake_operations_task_status
                ON intake_operations(task_id, status);
                """
            )

    def create(
        self,
        operation_id: str,
        task_id: str,
        request_text: str,
        *,
        request_sha256: str,
        expected_task_version: int,
        provider_role: str,
    ) -> IntakeOperationRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT operation_id FROM intake_operations "
                "WHERE task_id = ? AND status IN (?, ?)",
                (task_id, *_ACTIVE_STATUSES),
            ).fetchone()
            if active is not None:
                connection.rollback()
                raise ActiveIntakeOperationExistsError(
                    f"task {task_id} already has an active intake operation "
                    f"{active['operation_id']}; wait for it to finish or "
                    "inspect it before submitting another"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO intake_operations (
                        operation_id, task_id, request_text, request_sha256,
                        expected_task_version, provider_role, status,
                        created_at, updated_at, result_summary, error,
                        audit_artifact_locations_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                    """,
                    (
                        operation_id,
                        task_id,
                        request_text,
                        request_sha256,
                        expected_task_version,
                        provider_role,
                        IntakeOperationStatus.RECORDED.value,
                        now.isoformat(),
                        now.isoformat(),
                        json.dumps([]),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DuplicateIntakeOperationError(
                    f"operation already submitted: {operation_id}"
                ) from exc
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(operation_id)

    def get(self, operation_id: str) -> IntakeOperationRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM intake_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise IntakeOperationNotFoundError(operation_id)
        return self._row_to_record(row)

    def find_active_for_task(self, task_id: str) -> IntakeOperationRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM intake_operations "
                "WHERE task_id = ? AND status IN (?, ?) "
                "ORDER BY created_at DESC LIMIT 1",
                (task_id, *_ACTIVE_STATUSES),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def list_active(self) -> list[IntakeOperationRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM intake_operations WHERE status IN (?, ?) "
                "ORDER BY created_at ASC",
                _ACTIVE_STATUSES,
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def mark_running(self, operation_id: str) -> IntakeOperationRecord:
        return self._transition(
            operation_id,
            from_statuses={IntakeOperationStatus.RECORDED},
            to_status=IntakeOperationStatus.RUNNING,
        )

    def mark_pending_approval(
        self,
        operation_id: str,
        *,
        result_summary: str,
        audit_artifact_locations: list[str],
    ) -> IntakeOperationRecord:
        return self._transition(
            operation_id,
            from_statuses={IntakeOperationStatus.RUNNING},
            to_status=IntakeOperationStatus.PENDING_SPECIFICATION_APPROVAL,
            result_summary=result_summary,
            audit_artifact_locations=audit_artifact_locations,
        )

    def mark_failed(
        self,
        operation_id: str,
        *,
        error: str,
        audit_artifact_locations: list[str] | None = None,
    ) -> IntakeOperationRecord:
        return self._transition(
            operation_id,
            from_statuses={IntakeOperationStatus.RUNNING},
            to_status=IntakeOperationStatus.FAILED,
            error=error,
            audit_artifact_locations=audit_artifact_locations,
        )

    def mark_ambiguous(self, operation_id: str, *, note: str) -> IntakeOperationRecord:
        """Explicit crash recovery only (``intake.recovery``): moves a
        RUNNING operation whose owning process appears to have died into
        the terminal, inspectable ``AMBIGUOUS`` status. Never called as
        part of ordinary execution."""

        return self._transition(
            operation_id,
            from_statuses={IntakeOperationStatus.RUNNING},
            to_status=IntakeOperationStatus.AMBIGUOUS,
            error=note,
        )

    def _transition(
        self,
        operation_id: str,
        *,
        from_statuses: set[IntakeOperationStatus],
        to_status: IntakeOperationStatus,
        result_summary: str | None = None,
        error: str | None = None,
        audit_artifact_locations: list[str] | None = None,
    ) -> IntakeOperationRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM intake_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise IntakeOperationNotFoundError(operation_id)
            current = IntakeOperationStatus(row["status"])
            if current == IntakeOperationStatus.RUNNING and to_status == (
                IntakeOperationStatus.RUNNING
            ):
                raise IntakeOperationAlreadyRunningError(
                    f"operation {operation_id} is already RUNNING; refusing to "
                    "start it again -- inspect it before creating a new operation"
                )
            if current not in from_statuses:
                raise IntakeError(
                    f"operation {operation_id} cannot move to {to_status.value} "
                    f"from {current.value}"
                )
            artifacts_json = (
                json.dumps(audit_artifact_locations)
                if audit_artifact_locations is not None
                else None
            )
            connection.execute(
                """
                UPDATE intake_operations
                SET status = ?, updated_at = ?,
                    result_summary = COALESCE(?, result_summary),
                    error = COALESCE(?, error),
                    audit_artifact_locations_json = COALESCE(
                        ?, audit_artifact_locations_json
                    )
                WHERE operation_id = ?
                """,
                (
                    to_status.value,
                    now.isoformat(),
                    result_summary,
                    error,
                    artifacts_json,
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
    def _row_to_record(row: sqlite3.Row) -> IntakeOperationRecord:
        return IntakeOperationRecord(
            operation_id=row["operation_id"],
            task_id=row["task_id"],
            request_text=row["request_text"],
            request_sha256=row["request_sha256"],
            expected_task_version=row["expected_task_version"],
            provider_role=row["provider_role"],
            status=IntakeOperationStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result_summary=row["result_summary"],
            error=row["error"],
            audit_artifact_locations=json.loads(row["audit_artifact_locations_json"]),
        )


__all__ = ["IntakeOperationStore"]
