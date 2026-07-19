from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from apoapsis.intake.errors import (
    ActiveIntakeOperationExistsError,
    DuplicateIntakeOperationError,
    IntakeError,
    IntakeOperationAlreadyRunningError,
    IntakeOperationNotFoundError,
)
from apoapsis.intake.schema import IntakeOperationRecord, IntakeOperationStatus
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
    IntakeOperationStatus.RECORDED.value,
    IntakeOperationStatus.RUNNING.value,
)
_TABLE = "intake_operations"


class IntakeOperationStore:
    """Persistent, idempotent ledger of new-task intake operations (ADR
    0023, lease discipline hardened by ADR 0025), structurally mirroring
    ``review.store.ReviewOperationStore``.

    ``operation_id`` is a stable, caller-supplied idempotency key: creating
    the same ``operation_id`` twice is rejected outright
    (``DuplicateIntakeOperationError``), and only one RECORDED/RUNNING
    operation may exist per task at a time
    (``ActiveIntakeOperationExistsError``). A ``RUNNING`` operation is
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
            ensure_lease_columns(connection, _TABLE)

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

    def mark_running(
        self,
        operation_id: str,
        *,
        owner_id: str,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
        now: datetime | None = None,
    ) -> IntakeOperationRecord:
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
                from_status=IntakeOperationStatus.RECORDED.value,
                to_status=IntakeOperationStatus.RUNNING.value,
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
                    running_status=IntakeOperationStatus.RUNNING.value,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return renewed

    def mark_pending_approval(
        self,
        operation_id: str,
        *,
        owner_id: str,
        result_summary: str,
        audit_artifact_locations: list[str],
    ) -> IntakeOperationRecord:
        return self._release(
            operation_id,
            owner_id=owner_id,
            to_status=IntakeOperationStatus.PENDING_SPECIFICATION_APPROVAL,
            extra_assignments={
                "result_summary": result_summary,
                "audit_artifact_locations_json": json.dumps(audit_artifact_locations),
            },
        )

    def mark_failed(
        self,
        operation_id: str,
        *,
        owner_id: str,
        error: str,
        audit_artifact_locations: list[str] | None = None,
    ) -> IntakeOperationRecord:
        extra: dict[str, object] = {"error": error}
        if audit_artifact_locations is not None:
            extra["audit_artifact_locations_json"] = json.dumps(
                audit_artifact_locations
            )
        return self._release(
            operation_id,
            owner_id=owner_id,
            to_status=IntakeOperationStatus.FAILED,
            extra_assignments=extra,
        )

    def mark_ambiguous(
        self, operation_id: str, *, note: str, now=None
    ) -> IntakeOperationRecord:
        """Explicit crash recovery only (``intake.recovery``): atomically
        moves a RUNNING operation whose lease has expired (or which never
        had one -- a legacy row) into the terminal, inspectable
        ``AMBIGUOUS`` status. Never called as part of ordinary execution."""

        moment = now if now is not None else utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            expired = expire_lease_to_ambiguous(
                connection,
                _TABLE,
                operation_id,
                running_status=IntakeOperationStatus.RUNNING.value,
                ambiguous_status=IntakeOperationStatus.AMBIGUOUS.value,
                now=moment,
                note=note,
            )
            if not expired:
                row = connection.execute(
                    "SELECT status, lease_expires_at FROM intake_operations "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise IntakeOperationNotFoundError(operation_id)
                raise IntakeError(
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
        to_status: IntakeOperationStatus,
        extra_assignments: dict[str, object],
    ) -> IntakeOperationRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            released = release_lease(
                connection,
                _TABLE,
                operation_id,
                owner_id=owner_id,
                running_status=IntakeOperationStatus.RUNNING.value,
                to_status=to_status.value,
                now=now,
                extra_assignments=extra_assignments,
            )
            if not released:
                row = connection.execute(
                    "SELECT status, lease_owner_id FROM intake_operations "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                connection.rollback()
                if row is None:
                    raise IntakeOperationNotFoundError(operation_id)
                if row["status"] == IntakeOperationStatus.RECORDED.value:
                    # Never claimed by any owner at all -- an invalid
                    # lifecycle transition, not a lease race.
                    raise IntakeError(
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
            "SELECT status FROM intake_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise IntakeOperationNotFoundError(operation_id)
        current = IntakeOperationStatus(row["status"])
        if current == IntakeOperationStatus.RUNNING:
            raise IntakeOperationAlreadyRunningError(
                f"operation {operation_id} is already RUNNING; refusing to "
                "start it again -- inspect it before creating a new operation"
            )
        raise IntakeError(
            f"operation {operation_id} cannot move to running from "
            f"{current.value}"
        )

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
            lease_owner_id=row["lease_owner_id"],
            lease_expires_at=row["lease_expires_at"],
        )


__all__ = ["IntakeOperationStore"]
