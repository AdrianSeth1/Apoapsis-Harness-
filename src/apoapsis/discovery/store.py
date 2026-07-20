from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from apoapsis.discovery.errors import (
    ConcurrentSessionTransitionError,
    DiscoveryError,
    InvalidTransitionError,
    SessionNotFoundError,
)
from apoapsis.discovery.schema import (
    ClarificationAnswer,
    ClarificationQuestion,
    DiscoverySessionRecord,
    DiscoveryStatus,
    IdeaBrief,
)
from apoapsis.specification.schema import utc_now


class SQLiteDiscoveryStore:
    """Persistent, optimistically-versioned discovery-session state (ADR
    0032) -- mirrors ``architect.store.SQLitePlanStore``'s concurrency
    discipline exactly, in its own database
    (``.apoapsis/discovery-sessions.db``). Every mutation checks both the
    caller-supplied ``expected_version`` and that the session's *current*
    status is a valid source for the requested transition; no field is
    ever set from anything a model claims about session state."""

    def __init__(self, database_path: str | Path, *, initialize: bool = True) -> None:
        self.database_path = Path(database_path)
        if initialize:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        elif not self.database_path.is_file():
            raise DiscoveryError(
                f"discovery session database does not exist: {self.database_path}"
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
                CREATE TABLE IF NOT EXISTS discovery_sessions (
                    session_id TEXT PRIMARY KEY,
                    idea_text TEXT NOT NULL,
                    local_questions_json TEXT NOT NULL,
                    local_answers_json TEXT NOT NULL,
                    idea_brief_json TEXT,
                    brief_approved INTEGER NOT NULL DEFAULT 0,
                    frontier_transport TEXT,
                    frontier_round INTEGER NOT NULL DEFAULT 0,
                    frontier_package_id TEXT,
                    frontier_questions_json TEXT NOT NULL,
                    frontier_answers_json TEXT NOT NULL,
                    plan_id TEXT,
                    status TEXT NOT NULL,
                    failure_reason TEXT,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def create_session(
        self, session_id: str, idea_text: str
    ) -> DiscoverySessionRecord:
        now = utc_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO discovery_sessions (
                        session_id, idea_text, local_questions_json,
                        local_answers_json, idea_brief_json, brief_approved,
                        frontier_transport, frontier_round, frontier_package_id,
                        frontier_questions_json, frontier_answers_json, plan_id,
                        status, failure_reason, version, created_at, updated_at
                    ) VALUES (?, ?, '[]', '[]', NULL, 0, NULL, 0, NULL, '[]', '[]',
                              NULL, ?, NULL, 1, ?, ?)
                    """,
                    (
                        session_id,
                        idea_text,
                        DiscoveryStatus.IDEA_ENTERED.value,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DiscoveryError(f"session already exists: {session_id}") from exc
            except Exception:
                connection.rollback()
                raise
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> DiscoverySessionRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM discovery_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionNotFoundError(session_id)
        return self._row_to_record(row)

    def list_sessions(self, *, limit: int = 100) -> list[DiscoverySessionRecord]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM discovery_sessions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def _transition(
        self,
        session_id: str,
        *,
        expected_version: int,
        allowed_sources: frozenset[DiscoveryStatus],
        target: DiscoveryStatus,
        assignments: dict[str, Any],
    ) -> DiscoverySessionRecord:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, version FROM discovery_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise SessionNotFoundError(session_id)
            source = DiscoveryStatus(row["status"])
            version = int(row["version"])
            if version != expected_version:
                raise ConcurrentSessionTransitionError(
                    f"expected version {expected_version}, found {version}"
                )
            if source not in allowed_sources:
                raise InvalidTransitionError(
                    f"session {session_id} cannot move to {target.value} from "
                    f"{source.value} (allowed sources: "
                    f"{sorted(item.value for item in allowed_sources)})"
                )
            set_clause = ", ".join(f"{key} = ?" for key in assignments)
            cursor = connection.execute(
                f"""
                UPDATE discovery_sessions
                SET status = ?, version = version + 1, updated_at = ?{
                    ", " + set_clause if set_clause else ""
                }
                WHERE session_id = ? AND version = ?
                """,
                (
                    target.value,
                    now.isoformat(),
                    *assignments.values(),
                    session_id,
                    version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentSessionTransitionError(
                    f"session {session_id} changed during transition"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_session(session_id)

    def record_local_questions(
        self,
        session_id: str,
        questions: list[ClarificationQuestion],
        *,
        expected_version: int,
    ) -> DiscoverySessionRecord:
        return self._transition(
            session_id,
            expected_version=expected_version,
            allowed_sources=frozenset({DiscoveryStatus.IDEA_ENTERED}),
            target=DiscoveryStatus.LOCAL_QUESTIONS_PROPOSED,
            assignments={
                "local_questions_json": json.dumps(
                    [item.model_dump(mode="json") for item in questions]
                )
            },
        )

    def record_local_answers(
        self,
        session_id: str,
        answers: list[ClarificationAnswer],
        *,
        expected_version: int,
    ) -> DiscoverySessionRecord:
        return self._transition(
            session_id,
            expected_version=expected_version,
            allowed_sources=frozenset({DiscoveryStatus.LOCAL_QUESTIONS_PROPOSED}),
            target=DiscoveryStatus.LOCAL_ANSWERS_RECORDED,
            assignments={
                "local_answers_json": json.dumps(
                    [item.model_dump(mode="json") for item in answers]
                )
            },
        )

    def record_idea_brief(
        self, session_id: str, brief: IdeaBrief, *, expected_version: int
    ) -> DiscoverySessionRecord:
        return self._transition(
            session_id,
            expected_version=expected_version,
            # Local clarification questions are optional ("may propose"),
            # so a brief may be proposed straight from IDEA_ENTERED too --
            # not only after a local Q&A round actually happened.
            allowed_sources=frozenset(
                {
                    DiscoveryStatus.IDEA_ENTERED,
                    DiscoveryStatus.LOCAL_ANSWERS_RECORDED,
                    DiscoveryStatus.BRIEF_PROPOSED,
                }
            ),
            target=DiscoveryStatus.BRIEF_PROPOSED,
            assignments={"idea_brief_json": brief.model_dump_json()},
        )

    def approve_idea_brief(
        self, session_id: str, *, expected_version: int
    ) -> DiscoverySessionRecord:
        return self._transition(
            session_id,
            expected_version=expected_version,
            allowed_sources=frozenset({DiscoveryStatus.BRIEF_PROPOSED}),
            target=DiscoveryStatus.BRIEF_APPROVED,
            assignments={"brief_approved": 1},
        )

    def record_frontier_package(
        self,
        session_id: str,
        package_id: str,
        transport: str,
        *,
        expected_version: int,
        next_round: int,
    ) -> DiscoverySessionRecord:
        return self._transition(
            session_id,
            expected_version=expected_version,
            allowed_sources=frozenset(
                {
                    DiscoveryStatus.BRIEF_APPROVED,
                    DiscoveryStatus.FRONTIER_ANSWERS_RECORDED,
                }
            ),
            target=DiscoveryStatus.FRONTIER_PACKAGE_EXPORTED,
            assignments={
                "frontier_transport": transport,
                "frontier_package_id": package_id,
                "frontier_round": next_round,
            },
        )

    def record_frontier_clarification(
        self,
        session_id: str,
        questions: list[ClarificationQuestion],
        *,
        expected_version: int,
    ) -> DiscoverySessionRecord:
        return self._transition(
            session_id,
            expected_version=expected_version,
            allowed_sources=frozenset({DiscoveryStatus.FRONTIER_PACKAGE_EXPORTED}),
            target=DiscoveryStatus.FRONTIER_CLARIFICATION_PROPOSED,
            assignments={
                "frontier_questions_json": json.dumps(
                    [item.model_dump(mode="json") for item in questions]
                )
            },
        )

    def record_frontier_answers(
        self,
        session_id: str,
        answers: list[ClarificationAnswer],
        *,
        expected_version: int,
    ) -> DiscoverySessionRecord:
        return self._transition(
            session_id,
            expected_version=expected_version,
            allowed_sources=frozenset(
                {DiscoveryStatus.FRONTIER_CLARIFICATION_PROPOSED}
            ),
            target=DiscoveryStatus.FRONTIER_ANSWERS_RECORDED,
            assignments={
                "frontier_answers_json": json.dumps(
                    [item.model_dump(mode="json") for item in answers]
                )
            },
        )

    def record_frontier_plan(
        self, session_id: str, plan_id: str, *, expected_version: int
    ) -> DiscoverySessionRecord:
        return self._transition(
            session_id,
            expected_version=expected_version,
            allowed_sources=frozenset({DiscoveryStatus.FRONTIER_PACKAGE_EXPORTED}),
            target=DiscoveryStatus.PLAN_IMPORTED,
            assignments={"plan_id": plan_id},
        )

    def mark_failed(
        self, session_id: str, reason: str, *, expected_version: int
    ) -> DiscoverySessionRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status FROM discovery_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionNotFoundError(session_id)
        current = DiscoveryStatus(row["status"])
        terminal = {DiscoveryStatus.PLAN_IMPORTED, DiscoveryStatus.FAILED}
        return self._transition(
            session_id,
            expected_version=expected_version,
            allowed_sources=frozenset({current}) - terminal,
            target=DiscoveryStatus.FAILED,
            assignments={"failure_reason": reason},
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DiscoverySessionRecord:
        return DiscoverySessionRecord(
            session_id=row["session_id"],
            idea_text=row["idea_text"],
            local_questions=[
                ClarificationQuestion.model_validate(item)
                for item in json.loads(row["local_questions_json"])
            ],
            local_answers=[
                ClarificationAnswer.model_validate(item)
                for item in json.loads(row["local_answers_json"])
            ],
            idea_brief=(
                IdeaBrief.model_validate_json(row["idea_brief_json"])
                if row["idea_brief_json"]
                else None
            ),
            brief_approved=bool(row["brief_approved"]),
            frontier_transport=row["frontier_transport"],
            frontier_round=row["frontier_round"],
            frontier_package_id=row["frontier_package_id"],
            frontier_questions=[
                ClarificationQuestion.model_validate(item)
                for item in json.loads(row["frontier_questions_json"])
            ],
            frontier_answers=[
                ClarificationAnswer.model_validate(item)
                for item in json.loads(row["frontier_answers_json"])
            ],
            plan_id=row["plan_id"],
            status=DiscoveryStatus(row["status"]),
            failure_reason=row["failure_reason"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


__all__ = ["SQLiteDiscoveryStore"]
