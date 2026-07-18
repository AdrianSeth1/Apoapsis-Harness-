from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import Field

from apoapsis.specification.schema import StrictModel


class ResearchCacheEntry(StrictModel):
    cache_key: str
    category: str
    created_at: datetime
    expires_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchCache:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_cache (
                    cache_key TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_cache_category
                ON research_cache(category);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def key(category: str, components: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"category": category, "components": components},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, cache_key: str) -> Any | None:
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value_json, expires_at FROM research_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at <= now:
                connection.execute(
                    "DELETE FROM research_cache WHERE cache_key = ?", (cache_key,)
                )
                connection.commit()
                return None
            return json.loads(row["value_json"])

    def set(
        self,
        cache_key: str,
        category: str,
        value: Any,
        *,
        ttl_hours: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(hours=ttl_hours)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO research_cache (
                    cache_key, category, value_json, metadata_json,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    category = excluded.category,
                    value_json = excluded.value_json,
                    metadata_json = excluded.metadata_json,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    cache_key,
                    category,
                    json.dumps(value, sort_keys=True),
                    json.dumps(metadata or {}, sort_keys=True),
                    created_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            connection.commit()

    def inspect(self) -> list[ResearchCacheEntry]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT cache_key, category, metadata_json, created_at, expires_at
                FROM research_cache ORDER BY created_at DESC
                """
            ).fetchall()
        return [
            ResearchCacheEntry(
                cache_key=row["cache_key"],
                category=row["category"],
                metadata=json.loads(row["metadata_json"]),
                created_at=row["created_at"],
                expires_at=row["expires_at"],
            )
            for row in rows
        ]

    def clear(self, *, category: str | None = None) -> int:
        with closing(self._connect()) as connection:
            if category is None:
                cursor = connection.execute("DELETE FROM research_cache")
            else:
                cursor = connection.execute(
                    "DELETE FROM research_cache WHERE category = ?", (category,)
                )
            connection.commit()
            return cursor.rowcount

