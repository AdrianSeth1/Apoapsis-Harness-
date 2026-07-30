from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from apoapsis.desktop.errors import RegistryStoreError
from apoapsis.desktop.schema import ProjectRecord


def default_registry_database_path() -> Path:
    """Resolves the application-owned user-data location for the project
    registry -- deliberately *not* inside any one project's `.apoapsis/`
    directory (that is per-project runtime state; the registry must survive
    and be readable independent of which project is currently open).

    No such application-owned, cross-project user-data directory exists
    anywhere else in this codebase yet (every existing SQLite store is
    rooted under a project's own `.apoapsis/`); this is a new convention
    introduced by ADR 0051, not a pre-existing one. Callers that want
    deterministic behavior (all tests) should pass an explicit
    `database_path` to `ProjectRegistryStore` instead of relying on this
    function."""

    import os

    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Apoapsis" / "registry.db"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "apoapsis" / "registry.db"
    return Path.home() / ".local" / "share" / "apoapsis" / "registry.db"


class ProjectRegistryStore:
    """Persistent "recent projects" list (ADR 0051, Phase 2). Stores only a
    canonical path and harmless display metadata -- never credentials,
    never repository contents. Mirrors `discovery.store.SQLiteDiscoveryStore`'s
    connection/migration discipline exactly."""

    def __init__(self, database_path: str | Path, *, initialize: bool = True) -> None:
        self.database_path = Path(database_path)
        if initialize:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        elif not self.database_path.is_file():
            raise RegistryStoreError(
                f"project registry database does not exist: {self.database_path}"
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
                CREATE TABLE IF NOT EXISTS projects (
                    canonical_path TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    last_opened_at TEXT NOT NULL,
                    initialized INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    def upsert(self, record: ProjectRecord) -> ProjectRecord:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO projects (
                        canonical_path, display_name, added_at,
                        last_opened_at, initialized
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(canonical_path) DO UPDATE SET
                        display_name = excluded.display_name,
                        last_opened_at = excluded.last_opened_at,
                        initialized = excluded.initialized
                    """,
                    (
                        record.canonical_path,
                        record.display_name,
                        record.added_at.isoformat(),
                        record.last_opened_at.isoformat(),
                        int(record.initialized),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get(record.canonical_path)  # type: ignore[return-value]

    def get(self, canonical_path: str) -> ProjectRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE canonical_path = ?",
                (canonical_path,),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def list_all(self) -> list[ProjectRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM projects ORDER BY last_opened_at DESC"
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def delete(self, canonical_path: str) -> bool:
        """Removes one entry from the registry. Never touches the actual
        project directory on disk -- "forget" is a registry-only action."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "DELETE FROM projects WHERE canonical_path = ?",
                    (canonical_path,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            canonical_path=row["canonical_path"],
            display_name=row["display_name"],
            added_at=row["added_at"],
            last_opened_at=row["last_opened_at"],
            initialized=bool(row["initialized"]),
        )


__all__ = ["ProjectRegistryStore", "default_registry_database_path"]
