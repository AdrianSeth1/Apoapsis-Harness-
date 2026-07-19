from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from apoapsis.architect.errors import (
    ActiveSliceExecutionExistsError,
    ConcurrentSliceExecutionTransitionError,
    SliceApprovalError,
    SliceExecutionNotFoundError,
    SlicePackagingError,
)
from apoapsis.architect.slice_schema import PlanSliceExecutionRecord, SliceExecutionStatus
from apoapsis.specification.schema import utc_now

# The only two statuses this store ever writes itself -- everything past
# APPROVED (RUNNING/COMPLETE/HUMAN_REVIEW/FAILED) is a live projection
# computed from the derived task's own real state, never a second,
# independently-drifting copy of it. See ``slice_service.project_status``.
_OWNED_STATUSES = (SliceExecutionStatus.PACKAGED.value, SliceExecutionStatus.APPROVED.value)
_ACTIVE_STATUSES = (SliceExecutionStatus.APPROVED.value,)


class PlanSliceExecutionStore:
    """Persistent, optimistic-concurrency ledger of plan-slice execution
    records (ADR 0027), one row per ``(plan_id, slice_id)`` pair, in its
    own database file so this milestone never touches the plan or task
    stores' own schemas."""

    def __init__(self, database_path: str | Path, *, initialize: bool = True) -> None:
        self.database_path = Path(database_path)
        if initialize:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        elif not self.database_path.is_file():
            raise SliceExecutionNotFoundError(
                f"slice execution database does not exist: {self.database_path}"
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
                CREATE TABLE IF NOT EXISTS plan_slice_executions (
                    plan_id TEXT NOT NULL,
                    slice_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    package_sha256 TEXT,
                    task_id TEXT,
                    task_expected_version INTEGER,
                    execution_operation_id TEXT,
                    error TEXT,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (plan_id, slice_id)
                );

                CREATE INDEX IF NOT EXISTS idx_plan_slice_executions_plan
                ON plan_slice_executions(plan_id, status);
                """
            )

    def get(self, plan_id: str, slice_id: str) -> PlanSliceExecutionRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM plan_slice_executions WHERE plan_id = ? AND slice_id = ?",
                (plan_id, slice_id),
            ).fetchone()
        if row is None:
            raise SliceExecutionNotFoundError(f"{plan_id}/{slice_id}")
        return self._row_to_record(row)

    def list_for_plan(self, plan_id: str) -> list[PlanSliceExecutionRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM plan_slice_executions WHERE plan_id = ? "
                "ORDER BY slice_id ASC",
                (plan_id,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def record_package(
        self, plan_id: str, slice_id: str, *, plan_version: int, package_sha256: str
    ) -> PlanSliceExecutionRecord:
        """Insert a new record at ``PACKAGED``, or refresh an existing one
        still at ``PACKAGED`` with a new package hash/plan version -- never
        overwrites a record that has already been approved or executed."""

        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM plan_slice_executions "
                "WHERE plan_id = ? AND slice_id = ?",
                (plan_id, slice_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO plan_slice_executions (
                        plan_id, slice_id, plan_version, status,
                        package_sha256, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        plan_id,
                        slice_id,
                        plan_version,
                        SliceExecutionStatus.PACKAGED.value,
                        package_sha256,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            else:
                current_status = str(row["status"])
                if current_status != SliceExecutionStatus.PACKAGED.value:
                    connection.rollback()
                    raise SlicePackagingError(
                        f"slice {plan_id}/{slice_id} is already "
                        f"{current_status}; it cannot be re-packaged"
                    )
                connection.execute(
                    """
                    UPDATE plan_slice_executions
                    SET plan_version = ?, package_sha256 = ?, version = version + 1,
                        updated_at = ?
                    WHERE plan_id = ? AND slice_id = ?
                    """,
                    (plan_version, package_sha256, now.isoformat(), plan_id, slice_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(plan_id, slice_id)

    def approve(
        self,
        plan_id: str,
        slice_id: str,
        *,
        expected_package_sha256: str,
        task_id: str,
        task_expected_version: int,
    ) -> PlanSliceExecutionRecord:
        """Atomically transitions ``PACKAGED -> APPROVED`` and records the
        derived task's id/version -- but only if no *other* slice of the
        same plan is already ``APPROVED`` (a real execution has already
        been authorized and not yet resolved); only one slice per plan may
        be active at a time."""

        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, package_sha256, version FROM plan_slice_executions "
                "WHERE plan_id = ? AND slice_id = ?",
                (plan_id, slice_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise SliceExecutionNotFoundError(f"{plan_id}/{slice_id}")
            if str(row["status"]) != SliceExecutionStatus.PACKAGED.value:
                connection.rollback()
                raise SliceApprovalError(
                    f"slice {plan_id}/{slice_id} must be PACKAGED to "
                    f"approve, found {row['status']}"
                )
            if row["package_sha256"] != expected_package_sha256:
                connection.rollback()
                raise SliceApprovalError(
                    f"slice {plan_id}/{slice_id}'s package no longer "
                    "matches the expected hash; re-inspect before approving"
                )
            active = connection.execute(
                "SELECT slice_id FROM plan_slice_executions "
                "WHERE plan_id = ? AND status IN (%s) AND slice_id != ?"
                % ",".join("?" * len(_ACTIVE_STATUSES)),
                (plan_id, *_ACTIVE_STATUSES, slice_id),
            ).fetchone()
            if active is not None:
                connection.rollback()
                raise ActiveSliceExecutionExistsError(
                    f"plan {plan_id} already has an active slice execution "
                    f"({active['slice_id']}); wait for it to finish or "
                    "resolve it before approving another"
                )
            version = int(row["version"])
            cursor = connection.execute(
                """
                UPDATE plan_slice_executions
                SET status = ?, task_id = ?, task_expected_version = ?,
                    version = version + 1, updated_at = ?
                WHERE plan_id = ? AND slice_id = ? AND version = ?
                """,
                (
                    SliceExecutionStatus.APPROVED.value,
                    task_id,
                    task_expected_version,
                    now.isoformat(),
                    plan_id,
                    slice_id,
                    version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentSliceExecutionTransitionError(
                    f"slice {plan_id}/{slice_id} changed during approval"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(plan_id, slice_id)

    def record_execution_operation(
        self, plan_id: str, slice_id: str, *, execution_operation_id: str
    ) -> PlanSliceExecutionRecord:
        now = utc_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE plan_slice_executions
                    SET execution_operation_id = ?, version = version + 1,
                        updated_at = ?
                    WHERE plan_id = ? AND slice_id = ?
                    """,
                    (execution_operation_id, now.isoformat(), plan_id, slice_id),
                )
                if cursor.rowcount != 1:
                    raise SliceExecutionNotFoundError(f"{plan_id}/{slice_id}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get(plan_id, slice_id)

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> PlanSliceExecutionRecord:
        return PlanSliceExecutionRecord(
            plan_id=row["plan_id"],
            slice_id=row["slice_id"],
            plan_version=row["plan_version"],
            status=SliceExecutionStatus(row["status"]),
            package_sha256=row["package_sha256"],
            task_id=row["task_id"],
            task_expected_version=row["task_expected_version"],
            execution_operation_id=row["execution_operation_id"],
            error=row["error"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


__all__ = ["PlanSliceExecutionStore"]
