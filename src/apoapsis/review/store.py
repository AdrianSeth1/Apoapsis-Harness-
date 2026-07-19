from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from apoapsis.operations.lease import (
    DEFAULT_LEASE_DURATION,
    LeaseLostError,
    claim_lease,
    ensure_lease_columns,
    expire_lease_to_ambiguous,
    release_lease,
    renew_lease as _renew_lease,
)
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
_TABLE = "review_operations"


class ReviewOperationStore:
    """Persistent, idempotent ledger of human-review operations (ADR 0020,
    hardened by ADR 0021, lease discipline hardened by ADR 0025).

    ``operation_id`` is a stable, caller-supplied idempotency key: creating
    the same ``operation_id`` twice is rejected outright
    (``DuplicateOperationError``), and only one ``RECORDED``/``RUNNING``
    operation may exist per task at a time (``ActiveOperationExistsError``,
    checked atomically inside the same transaction as the insert). A
    ``RUNNING`` operation is owned by a unique lease (``operations.lease``):
    only the owning lease may mark it succeeded or failed
    (``LeaseLostError`` otherwise), and only an atomic expired-lease check
    may move it to the terminal, inspectable ``AMBIGUOUS`` status -- only
    explicit recovery ever moves it forward, never back into execution.
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
            ensure_lease_columns(connection, _TABLE)

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

    def mark_running(
        self,
        operation_id: str,
        *,
        owner_id: str,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
        now: datetime | None = None,
    ) -> ReviewOperationRecord:
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
                from_status=ReviewOperationStatus.RECORDED.value,
                to_status=ReviewOperationStatus.RUNNING.value,
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
                    running_status=ReviewOperationStatus.RUNNING.value,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return renewed

    def mark_succeeded(
        self, operation_id: str, *, owner_id: str, result_summary: str
    ) -> ReviewOperationRecord:
        return self._release(
            operation_id,
            owner_id=owner_id,
            to_status=ReviewOperationStatus.SUCCEEDED,
            extra_assignments={"result_summary": result_summary},
        )

    def mark_failed(
        self, operation_id: str, *, owner_id: str, error: str
    ) -> ReviewOperationRecord:
        return self._release(
            operation_id,
            owner_id=owner_id,
            to_status=ReviewOperationStatus.FAILED,
            extra_assignments={"error": error},
        )

    def mark_ambiguous(
        self, operation_id: str, *, note: str, now=None
    ) -> ReviewOperationRecord:
        """Explicit crash recovery only (``review.recovery``): atomically
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
                running_status=ReviewOperationStatus.RUNNING.value,
                ambiguous_status=ReviewOperationStatus.AMBIGUOUS.value,
                now=moment,
                note=note,
            )
            if not expired:
                row = connection.execute(
                    "SELECT status, lease_expires_at FROM review_operations "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise OperationNotFoundError(operation_id)
                raise ReviewError(
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
        to_status: ReviewOperationStatus,
        extra_assignments: dict[str, object],
    ) -> ReviewOperationRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            released = release_lease(
                connection,
                _TABLE,
                operation_id,
                owner_id=owner_id,
                running_status=ReviewOperationStatus.RUNNING.value,
                to_status=to_status.value,
                now=now,
                extra_assignments=extra_assignments,
            )
            if not released:
                row = connection.execute(
                    "SELECT status, lease_owner_id FROM review_operations "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                connection.rollback()
                if row is None:
                    raise OperationNotFoundError(operation_id)
                if row["status"] == ReviewOperationStatus.RECORDED.value:
                    # Never claimed by any owner at all -- an invalid
                    # lifecycle transition, not a lease race.
                    raise ReviewError(
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
            "SELECT status FROM review_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise OperationNotFoundError(operation_id)
        current = ReviewOperationStatus(row["status"])
        if current == ReviewOperationStatus.RUNNING:
            raise OperationAlreadyRunningError(
                f"operation {operation_id} is already RUNNING; refusing to "
                "start it again -- inspect it before creating a new operation"
            )
        raise ReviewError(
            f"operation {operation_id} cannot move to {ReviewOperationStatus.RUNNING.value} "
            f"from {current.value}"
        )

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
            lease_owner_id=row["lease_owner_id"],
            lease_expires_at=row["lease_expires_at"],
        )


__all__ = ["ReviewOperationStore"]
