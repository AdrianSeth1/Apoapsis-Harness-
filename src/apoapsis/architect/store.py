from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from apoapsis.architect.errors import (
    ConcurrentPlanTransitionError,
    InvalidPlanTransitionError,
    PlanActionError,
    PlanNotFoundError,
    PlanStoreError,
)
from apoapsis.architect.schema import (
    ArchitecturePlan,
    PlanActor,
    PlanEvent,
    PlanRecord,
    PlanStatus,
    PlanValidationResult,
)
from apoapsis.specification.schema import utc_now


class SQLitePlanStore:
    """Persistent Architect Mode plan state with atomic, optimistic
    transitions -- deliberately mirrors ``workflow.engine.SQLiteTaskStore``'s
    schema and concurrency discipline exactly, in a separate database file
    so this milestone never touches the existing task store or its schema.
    """

    def __init__(self, database_path: str | Path, *, initialize: bool = True) -> None:
        self.database_path = Path(database_path)
        if initialize:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        elif not self.database_path.is_file():
            raise PlanStoreError(f"plan database does not exist: {self.database_path}")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=5.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    package_id TEXT NOT NULL,
                    idea_text TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    validation_json TEXT,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS plan_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    plan_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
                );

                CREATE INDEX IF NOT EXISTS idx_plan_events_plan_sequence
                ON plan_events(plan_id, sequence);
                """
            )

    def create_plan(
        self,
        plan_id: str,
        package_id: str,
        idea_text: str,
        plan: ArchitecturePlan,
        *,
        actor: PlanActor = PlanActor.SYSTEM,
    ) -> PlanRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO plans (
                        plan_id, package_id, idea_text, plan_json,
                        validation_json, status, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, ?, 1, ?, ?)
                    """,
                    (
                        plan_id,
                        package_id,
                        idea_text,
                        plan.model_dump_json(),
                        PlanStatus.PROPOSED.value,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise PlanStoreError(f"plan already exists: {plan_id}") from exc
            self._insert_event(
                connection,
                plan_id,
                event_type="plan_imported",
                from_status=None,
                to_status=PlanStatus.PROPOSED,
                actor=actor,
                payload={"package_id": package_id},
                created_at=now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_plan(plan_id)

    def get_plan(self, plan_id: str) -> PlanRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            raise PlanNotFoundError(plan_id)
        return self._row_to_plan(row)

    def list_plans(self, *, limit: int = 100) -> list[PlanRecord]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM plans ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_plan(row) for row in rows]

    def record_validation(
        self,
        plan_id: str,
        result: PlanValidationResult,
        *,
        expected_version: int | None = None,
    ) -> PlanRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, version FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                raise PlanNotFoundError(plan_id)
            source = PlanStatus(row["status"])
            version = int(row["version"])
            if expected_version is not None and version != expected_version:
                raise ConcurrentPlanTransitionError(
                    f"expected version {expected_version}, found {version}"
                )
            if source not in {PlanStatus.PROPOSED, PlanStatus.VALIDATED}:
                raise InvalidPlanTransitionError(
                    f"plan {plan_id} cannot be (re)validated from status "
                    f"{source.value}"
                )
            target = PlanStatus.VALIDATED if result.valid else PlanStatus.PROPOSED
            cursor = connection.execute(
                """
                UPDATE plans
                SET status = ?, validation_json = ?, version = version + 1,
                    updated_at = ?
                WHERE plan_id = ? AND version = ?
                """,
                (target.value, result.model_dump_json(), now.isoformat(), plan_id, version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentPlanTransitionError(
                    f"plan {plan_id} changed during validation"
                )
            self._insert_event(
                connection,
                plan_id,
                event_type="plan_validated",
                from_status=source,
                to_status=target,
                actor=PlanActor.SYSTEM,
                payload={
                    "valid": result.valid,
                    "finding_count": len(result.findings),
                },
                created_at=now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_plan(plan_id)

    def approve_plan(self, plan_id: str, *, expected_version: int) -> PlanRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, version, validation_json FROM plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise PlanNotFoundError(plan_id)
            source = PlanStatus(row["status"])
            version = int(row["version"])
            if version != expected_version:
                raise ConcurrentPlanTransitionError(
                    f"expected version {expected_version}, found {version}"
                )
            if source != PlanStatus.VALIDATED:
                raise PlanActionError(
                    f"plan approval requires VALIDATED, found {source.value}"
                )
            if row["validation_json"] is None:
                raise PlanActionError("plan has no recorded validation result")
            validation = PlanValidationResult.model_validate_json(row["validation_json"])
            if not validation.valid:
                raise PlanActionError("plan's last validation result was not valid")
            cursor = connection.execute(
                """
                UPDATE plans SET status = ?, version = version + 1, updated_at = ?
                WHERE plan_id = ? AND version = ?
                """,
                (PlanStatus.APPROVED.value, now.isoformat(), plan_id, version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentPlanTransitionError(
                    f"plan {plan_id} changed during approval"
                )
            self._insert_event(
                connection,
                plan_id,
                event_type="plan_approved",
                from_status=source,
                to_status=PlanStatus.APPROVED,
                actor=PlanActor.USER,
                payload={},
                created_at=now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_plan(plan_id)

    def mark_executed(
        self,
        plan_id: str,
        *,
        expected_version: int,
        final_commit: str,
        delivery_path: str,
    ) -> PlanRecord:
        """Record that every approved slice was integrated and delivered."""

        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, version FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                raise PlanNotFoundError(plan_id)
            source = PlanStatus(row["status"])
            version = int(row["version"])
            if version != expected_version:
                raise ConcurrentPlanTransitionError(
                    f"expected version {expected_version}, found {version}"
                )
            if source != PlanStatus.APPROVED:
                raise PlanActionError(
                    f"plan delivery requires APPROVED, found {source.value}"
                )
            cursor = connection.execute(
                "UPDATE plans SET status = ?, version = version + 1, updated_at = ? "
                "WHERE plan_id = ? AND version = ?",
                (PlanStatus.EXECUTED.value, now.isoformat(), plan_id, version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentPlanTransitionError(
                    f"plan {plan_id} changed during delivery"
                )
            self._insert_event(
                connection,
                plan_id,
                event_type="plan_delivery_prepared",
                from_status=source,
                to_status=PlanStatus.EXECUTED,
                actor=PlanActor.USER,
                payload={
                    "final_commit": final_commit,
                    "delivery_path": delivery_path,
                },
                created_at=now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_plan(plan_id)

    def create_revision(
        self,
        plan_id: str,
        package_id: str,
        idea_text: str,
        plan: ArchitecturePlan,
        *,
        expected_version: int,
        actor: PlanActor = PlanActor.USER,
    ) -> PlanRecord:
        """Record a new plan version. The prior version's content is never
        overwritten in place -- callers persist each version's immutable
        snapshot via ``PlanAuditStore`` before calling this; this method
        only advances the store's current-version pointer, exactly like
        ``SQLiteTaskStore.transition`` never deletes prior workflow events.
        """

        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, version FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                raise PlanNotFoundError(plan_id)
            source = PlanStatus(row["status"])
            version = int(row["version"])
            if version != expected_version:
                raise ConcurrentPlanTransitionError(
                    f"expected version {expected_version}, found {version}"
                )
            cursor = connection.execute(
                """
                UPDATE plans
                SET package_id = ?, idea_text = ?, plan_json = ?,
                    validation_json = NULL, status = ?, version = version + 1,
                    updated_at = ?
                WHERE plan_id = ? AND version = ?
                """,
                (
                    package_id,
                    idea_text,
                    plan.model_dump_json(),
                    PlanStatus.PROPOSED.value,
                    now.isoformat(),
                    plan_id,
                    version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentPlanTransitionError(
                    f"plan {plan_id} changed during revision"
                )
            self._insert_event(
                connection,
                plan_id,
                event_type="plan_revised",
                from_status=source,
                to_status=PlanStatus.PROPOSED,
                actor=actor,
                payload={"previous_version": version, "previous_status": source.value},
                created_at=now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_plan(plan_id)

    def events(self, plan_id: str) -> list[PlanEvent]:
        self.get_plan(plan_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM plan_events WHERE plan_id = ? ORDER BY sequence ASC",
                (plan_id,),
            ).fetchall()
        return [
            PlanEvent(
                event_id=row["event_id"],
                sequence=row["sequence"],
                plan_id=row["plan_id"],
                event_type=row["event_type"],
                from_status=(
                    PlanStatus(row["from_status"]) if row["from_status"] else None
                ),
                to_status=PlanStatus(row["to_status"]),
                actor=PlanActor(row["actor"]),
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        plan_id: str,
        *,
        event_type: str,
        from_status: PlanStatus | None,
        to_status: PlanStatus,
        actor: PlanActor,
        payload: dict[str, Any],
        created_at: Any,
    ) -> None:
        connection.execute(
            """
            INSERT INTO plan_events (
                event_id, plan_id, event_type, from_status, to_status,
                actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._new_event_id(),
                plan_id,
                event_type,
                from_status.value if from_status else None,
                to_status.value,
                actor.value,
                json.dumps(payload, sort_keys=True),
                created_at.isoformat(),
            ),
        )

    @staticmethod
    def _new_event_id() -> str:
        return f"EVT-{uuid.uuid4().hex}"

    @staticmethod
    def _row_to_plan(row: sqlite3.Row) -> PlanRecord:
        return PlanRecord(
            plan_id=row["plan_id"],
            package_id=row["package_id"],
            idea_text=row["idea_text"],
            plan=ArchitecturePlan.model_validate_json(row["plan_json"]),
            validation=(
                PlanValidationResult.model_validate_json(row["validation_json"])
                if row["validation_json"]
                else None
            ),
            status=PlanStatus(row["status"]),
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


__all__ = ["SQLitePlanStore"]
