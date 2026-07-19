from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
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
from apoapsis.operations.lease import (
    DEFAULT_LEASE_DURATION,
    LeaseLostError,
    claim_lease,
    ensure_lease_columns,
    expire_lease_to_ambiguous,
    release_lease,
    renew_lease as _renew_lease,
)
from apoapsis.specification.schema import utc_now

_ACTIVE_STATUSES = (
    ExecutionOperationStatus.RECORDED.value,
    ExecutionOperationStatus.RUNNING.value,
)
_TABLE = "execution_operations"


class ExecutionOperationStore:
    """Persistent, idempotent ledger of post-approval task-execution
    operations (ADR 0024, lease discipline hardened by ADR 0025),
    structurally mirroring ``review.store.ReviewOperationStore`` and
    ``intake.store.IntakeOperationStore``.

    ``operation_id`` is a stable, caller-supplied idempotency key: creating
    the same ``operation_id`` twice is rejected outright
    (``DuplicateExecutionOperationError``), and only one RECORDED/RUNNING
    operation may exist per task at a time
    (``ActiveExecutionOperationExistsError``). A ``RUNNING`` operation is
    owned by a unique lease (``operations.lease``): only the owning lease
    may mark it succeeded or failed (``LeaseLostError`` otherwise), and
    only an atomic expired-lease check may move it to the terminal,
    inspectable ``AMBIGUOUS`` status -- never back into execution.
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
            ensure_lease_columns(connection, _TABLE)

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

    def mark_running(
        self,
        operation_id: str,
        *,
        owner_id: str,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
        now: datetime | None = None,
    ) -> ExecutionOperationRecord:
        now = now if now is not None else utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            claimed = claim_lease(
                connection,
                _TABLE,
                operation_id,
                owner_id=owner_id,
                lease_duration=lease_duration,
                now=now,
                from_status=ExecutionOperationStatus.RECORDED.value,
                to_status=ExecutionOperationStatus.RUNNING.value,
            )
            if not claimed:
                self._raise_for_failed_claim(connection, operation_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(operation_id)

    def renew_lease(
        self,
        operation_id: str,
        *,
        owner_id: str,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
        now: datetime | None = None,
    ) -> bool:
        """Called by :class:`apoapsis.operations.lease.LeaseHeartbeat` on a
        fixed wall-clock interval while the operation is running. Returns
        ``False`` if the lease was lost (recovery already moved the row
        away, or somehow another owner holds it) -- the caller must stop
        treating its work as authoritative."""

        now = now if now is not None else utc_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                renewed = _renew_lease(
                    connection,
                    _TABLE,
                    operation_id,
                    owner_id=owner_id,
                    lease_duration=lease_duration,
                    now=now,
                    running_status=ExecutionOperationStatus.RUNNING.value,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return renewed

    def mark_succeeded(
        self,
        operation_id: str,
        *,
        owner_id: str,
        result_summary: str,
        report_path: str | None = None,
    ) -> ExecutionOperationRecord:
        extra: dict[str, object] = {"result_summary": result_summary}
        if report_path is not None:
            extra["report_path"] = report_path
        return self._release(
            operation_id,
            owner_id=owner_id,
            to_status=ExecutionOperationStatus.SUCCEEDED,
            extra_assignments=extra,
        )

    def mark_failed(
        self, operation_id: str, *, owner_id: str, error: str
    ) -> ExecutionOperationRecord:
        return self._release(
            operation_id,
            owner_id=owner_id,
            to_status=ExecutionOperationStatus.FAILED,
            extra_assignments={"error": error},
        )

    def mark_ambiguous(
        self, operation_id: str, *, note: str, now=None
    ) -> ExecutionOperationRecord:
        """Explicit crash recovery only
        (``execution.operation_recovery``): atomically moves a RUNNING
        operation whose lease has expired (or which never had one -- a
        legacy row) into the terminal, inspectable ``AMBIGUOUS`` status.
        Never called as part of ordinary execution. Raises
        ``ExecutionOperationError`` if the lease has not actually expired
        (a genuine race with a still-renewing owner) or the row is not
        currently RUNNING."""

        moment = now if now is not None else utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            expired = expire_lease_to_ambiguous(
                connection,
                _TABLE,
                operation_id,
                running_status=ExecutionOperationStatus.RUNNING.value,
                ambiguous_status=ExecutionOperationStatus.AMBIGUOUS.value,
                now=moment,
                note=note,
            )
            if not expired:
                row = connection.execute(
                    "SELECT status, lease_expires_at FROM execution_operations "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise ExecutionOperationNotFoundError(operation_id)
                raise ExecutionOperationError(
                    f"operation {operation_id} cannot be marked ambiguous: "
                    f"status={row['status']!r}, lease_expires_at="
                    f"{row['lease_expires_at']!r} is not yet expired as of "
                    f"{moment.isoformat()}"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(operation_id)

    def _release(
        self,
        operation_id: str,
        *,
        owner_id: str,
        to_status: ExecutionOperationStatus,
        extra_assignments: dict[str, object],
    ) -> ExecutionOperationRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            released = release_lease(
                connection,
                _TABLE,
                operation_id,
                owner_id=owner_id,
                running_status=ExecutionOperationStatus.RUNNING.value,
                to_status=to_status.value,
                now=now,
                extra_assignments=extra_assignments,
            )
            if not released:
                row = connection.execute(
                    "SELECT status, lease_owner_id FROM execution_operations "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                connection.rollback()
                if row is None:
                    raise ExecutionOperationNotFoundError(operation_id)
                if row["status"] == ExecutionOperationStatus.RECORDED.value:
                    # Never claimed by any owner at all -- an invalid
                    # lifecycle transition, not a lease race.
                    raise ExecutionOperationError(
                        f"operation {operation_id} cannot move to "
                        f"{to_status.value} from {row['status']!r}"
                    )
                raise LeaseLostError(
                    f"operation {operation_id} is no longer owned by {owner_id} "
                    f"(status={row['status']!r}, current owner="
                    f"{row['lease_owner_id']!r}); this worker's result cannot "
                    "be recorded -- the operation is already AMBIGUOUS or was "
                    "claimed by another owner"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(operation_id)

    def _raise_for_failed_claim(
        self, connection: sqlite3.Connection, operation_id: str
    ) -> None:
        row = connection.execute(
            "SELECT status FROM execution_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise ExecutionOperationNotFoundError(operation_id)
        current = ExecutionOperationStatus(row["status"])
        if current == ExecutionOperationStatus.RUNNING:
            raise ExecutionOperationAlreadyRunningError(
                f"operation {operation_id} is already RUNNING; refusing "
                "to start it again -- inspect it before creating a new "
                "operation"
            )
        raise ExecutionOperationError(
            f"operation {operation_id} cannot move to running from "
            f"{current.value}"
        )

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
            lease_owner_id=row["lease_owner_id"],
            lease_expires_at=row["lease_expires_at"],
        )


__all__ = ["ExecutionOperationStore"]
