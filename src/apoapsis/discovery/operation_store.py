from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from apoapsis.discovery.errors import DiscoveryError
from apoapsis.discovery.operation_schema import (
    DiscoveryOperationAction,
    DiscoveryOperationRecord,
    DiscoveryOperationStatus,
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
    DiscoveryOperationStatus.RECORDED.value,
    DiscoveryOperationStatus.RUNNING.value,
)
_TABLE = "discovery_operations"


class ActiveDiscoveryOperationExistsError(DiscoveryError):
    """Raised when a session already has a RECORDED or RUNNING operation --
    only one active operation per session is ever permitted, mirroring
    ``review.errors.ActiveOperationExistsError`` (ADR 0021)."""


class DuplicateDiscoveryOperationError(DiscoveryError):
    """Raised when an operation_id has already been submitted."""


class DiscoveryOperationNotFoundError(DiscoveryError):
    """Raised when an operation_id is not present in the operation store."""


class DiscoveryOperationAlreadyRunningError(DiscoveryError):
    """Raised when an operation is already RUNNING -- fail closed rather
    than silently repeating a call that may already have been transmitted
    to a provider before an earlier process crashed."""


class DiscoveryOperationStore:
    """Persistent, idempotent ledger of discovery model-call operations
    (ADR 0032/0033) -- structurally identical to
    ``review.store.ReviewOperationStore`` (own database,
    ``.apoapsis/discovery-operations.db``): a caller-supplied
    ``operation_id`` can never be submitted twice, only one active
    operation may exist per session at a time, a ``RUNNING`` operation is
    owned by a unique renewed lease, and only explicit recovery ever moves
    a stale ``RUNNING`` row forward, into the terminal ``AMBIGUOUS``
    status.
    """

    def __init__(self, database_path: str | Path, *, initialize: bool = True) -> None:
        self.database_path = Path(database_path)
        if initialize:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        elif not self.database_path.is_file():
            raise DiscoveryError(
                f"discovery operation database does not exist: {self.database_path}"
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
                CREATE TABLE IF NOT EXISTS discovery_operations (
                    operation_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    expected_session_version INTEGER NOT NULL,
                    authorized_max_spend_usd REAL,
                    package_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_summary TEXT,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_discovery_operations_session_status
                ON discovery_operations(session_id, status);
                """
            )
            ensure_lease_columns(connection, _TABLE)

    def create(
        self,
        operation_id: str,
        session_id: str,
        action: DiscoveryOperationAction,
        *,
        expected_session_version: int,
        authorized_max_spend_usd: float | None = None,
        package_id: str | None = None,
    ) -> DiscoveryOperationRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT operation_id FROM discovery_operations "
                "WHERE session_id = ? AND status IN (?, ?)",
                (session_id, *_ACTIVE_STATUSES),
            ).fetchone()
            if active is not None:
                connection.rollback()
                raise ActiveDiscoveryOperationExistsError(
                    f"session {session_id} already has an active operation "
                    f"{active['operation_id']}; wait for it to finish or "
                    "inspect it before submitting another"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO discovery_operations (
                        operation_id, session_id, action, expected_session_version,
                        authorized_max_spend_usd, package_id, status, created_at,
                        updated_at, result_summary, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        operation_id,
                        session_id,
                        action.value,
                        expected_session_version,
                        authorized_max_spend_usd,
                        package_id,
                        DiscoveryOperationStatus.RECORDED.value,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DuplicateDiscoveryOperationError(
                    f"operation already submitted: {operation_id}"
                ) from exc
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(operation_id)

    def get(self, operation_id: str) -> DiscoveryOperationRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM discovery_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise DiscoveryOperationNotFoundError(operation_id)
        return self._row_to_record(row)

    def find_active_for_session(self, session_id: str) -> DiscoveryOperationRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM discovery_operations "
                "WHERE session_id = ? AND status IN (?, ?) "
                "ORDER BY created_at DESC LIMIT 1",
                (session_id, *_ACTIVE_STATUSES),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def list_active(self) -> list[DiscoveryOperationRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM discovery_operations WHERE status IN (?, ?) "
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
    ) -> DiscoveryOperationRecord:
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
                from_status=DiscoveryOperationStatus.RECORDED.value,
                to_status=DiscoveryOperationStatus.RUNNING.value,
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
                    running_status=DiscoveryOperationStatus.RUNNING.value,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return renewed

    def mark_succeeded(
        self, operation_id: str, *, owner_id: str, result_summary: str
    ) -> DiscoveryOperationRecord:
        return self._release(
            operation_id,
            owner_id=owner_id,
            to_status=DiscoveryOperationStatus.SUCCEEDED,
            extra_assignments={"result_summary": result_summary},
        )

    def mark_failed(
        self, operation_id: str, *, owner_id: str, error: str
    ) -> DiscoveryOperationRecord:
        return self._release(
            operation_id,
            owner_id=owner_id,
            to_status=DiscoveryOperationStatus.FAILED,
            extra_assignments={"error": error},
        )

    def mark_ambiguous(
        self, operation_id: str, *, note: str, now: datetime | None = None
    ) -> DiscoveryOperationRecord:
        moment = now if now is not None else utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            expired = expire_lease_to_ambiguous(
                connection,
                _TABLE,
                operation_id,
                running_status=DiscoveryOperationStatus.RUNNING.value,
                ambiguous_status=DiscoveryOperationStatus.AMBIGUOUS.value,
                now=moment,
                note=note,
            )
            if not expired:
                row = connection.execute(
                    "SELECT status, lease_expires_at FROM discovery_operations "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise DiscoveryOperationNotFoundError(operation_id)
                raise DiscoveryError(
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
        to_status: DiscoveryOperationStatus,
        extra_assignments: dict[str, object],
    ) -> DiscoveryOperationRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            released = release_lease(
                connection,
                _TABLE,
                operation_id,
                owner_id=owner_id,
                running_status=DiscoveryOperationStatus.RUNNING.value,
                to_status=to_status.value,
                now=now,
                extra_assignments=extra_assignments,
            )
            if not released:
                row = connection.execute(
                    "SELECT status, lease_owner_id FROM discovery_operations "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                connection.rollback()
                if row is None:
                    raise DiscoveryOperationNotFoundError(operation_id)
                if row["status"] == DiscoveryOperationStatus.RECORDED.value:
                    raise DiscoveryError(
                        f"operation {operation_id} cannot move to "
                        f"{to_status.value} from {row['status']!r}"
                    )
                raise LeaseLostError(
                    f"operation {operation_id} is no longer owned by {owner_id} "
                    f"(status={row['status']!r}, current owner="
                    f"{row['lease_owner_id']!r}); this worker's result cannot "
                    "be recorded"
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
            "SELECT status FROM discovery_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise DiscoveryOperationNotFoundError(operation_id)
        current = DiscoveryOperationStatus(row["status"])
        if current == DiscoveryOperationStatus.RUNNING:
            raise DiscoveryOperationAlreadyRunningError(
                f"operation {operation_id} is already RUNNING; refusing to "
                "start it again -- inspect it before creating a new operation"
            )
        raise DiscoveryError(
            f"operation {operation_id} cannot move to "
            f"{DiscoveryOperationStatus.RUNNING.value} from {current.value}"
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DiscoveryOperationRecord:
        return DiscoveryOperationRecord(
            operation_id=row["operation_id"],
            session_id=row["session_id"],
            action=DiscoveryOperationAction(row["action"]),
            expected_session_version=row["expected_session_version"],
            authorized_max_spend_usd=row["authorized_max_spend_usd"],
            package_id=row["package_id"],
            status=DiscoveryOperationStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result_summary=row["result_summary"],
            error=row["error"],
            lease_owner_id=row["lease_owner_id"],
            lease_expires_at=row["lease_expires_at"],
        )


__all__ = [
    "ActiveDiscoveryOperationExistsError",
    "DiscoveryOperationAlreadyRunningError",
    "DiscoveryOperationNotFoundError",
    "DiscoveryOperationStore",
    "DuplicateDiscoveryOperationError",
]
