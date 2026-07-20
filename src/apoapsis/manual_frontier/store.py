from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from apoapsis.manual_frontier.errors import (
    ManualFrontierError,
    PreviewNotFoundError,
)
from apoapsis.manual_frontier.schema import (
    ManualFrontierPreviewRecord,
    ManualFrontierPreviewStatus,
)
from apoapsis.specification.schema import utc_now

_TABLE = "manual_frontier_previews"


class ManualFrontierPreviewStore:
    """Persistent ledger of imported-and-validated manual-frontier response
    previews (ADR 0031), in its own database
    (``.apoapsis/manual-frontier-previews.db``). Import creates a row here
    at ``PREVIEWED``; a distinct, explicit approval step (never bundled
    with import) atomically flips it to ``APPROVED`` -- the first of the
    two required approval steps before anything is ever applied to the
    worktree. A preview can never be approved twice, and a superseded
    preview (a fresh import for the same task) is never silently reused.
    """

    def __init__(self, database_path: str | Path, *, initialize: bool = True) -> None:
        self.database_path = Path(database_path)
        if initialize:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        elif not self.database_path.is_file():
            raise ManualFrontierError(
                f"manual-frontier preview database does not exist: {self.database_path}"
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
                CREATE TABLE IF NOT EXISTS manual_frontier_previews (
                    preview_id TEXT PRIMARY KEY,
                    package_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    task_version_at_import INTEGER NOT NULL,
                    worktree_fingerprint_at_import TEXT NOT NULL,
                    declared_model_name TEXT NOT NULL,
                    patch TEXT NOT NULL,
                    patch_sha256 TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    files_changed_json TEXT NOT NULL,
                    changed_lines INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    approved_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_manual_frontier_previews_task
                ON manual_frontier_previews(task_id);
                """
            )

    def create(self, record: ManualFrontierPreviewRecord) -> ManualFrontierPreviewRecord:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO manual_frontier_previews (
                        preview_id, package_id, task_id, task_version_at_import,
                        worktree_fingerprint_at_import, declared_model_name, patch,
                        patch_sha256, summary, files_changed_json, changed_lines,
                        status, created_at, approved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        record.preview_id,
                        record.package_id,
                        record.task_id,
                        record.task_version_at_import,
                        record.worktree_fingerprint_at_import,
                        record.declared_model_name,
                        record.patch,
                        record.patch_sha256,
                        record.summary,
                        json.dumps(record.files_changed),
                        record.changed_lines,
                        record.status.value,
                        record.created_at.isoformat(),
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ManualFrontierError(
                    f"preview already exists: {record.preview_id}"
                ) from exc
            except Exception:
                connection.rollback()
                raise
        return self.get(record.preview_id)

    def get(self, preview_id: str) -> ManualFrontierPreviewRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM manual_frontier_previews WHERE preview_id = ?",
                (preview_id,),
            ).fetchone()
        if row is None:
            raise PreviewNotFoundError(preview_id)
        return self._row_to_record(row)

    def list_for_task(self, task_id: str) -> list[ManualFrontierPreviewRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM manual_frontier_previews WHERE task_id = ? "
                "ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def mark_approved(
        self, preview_id: str, *, now: datetime | None = None
    ) -> ManualFrontierPreviewRecord:
        """Atomically flips a ``PREVIEWED`` row to ``APPROVED`` -- the first
        of the two required approval steps. Rejects a preview already
        approved, applied, or superseded, so a resubmitted approval request
        can never silently repeat."""

        moment = now if now is not None else utc_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE manual_frontier_previews
                SET status = ?, approved_at = ?
                WHERE preview_id = ? AND status = ?
                """,
                (
                    ManualFrontierPreviewStatus.APPROVED.value,
                    moment.isoformat(),
                    preview_id,
                    ManualFrontierPreviewStatus.PREVIEWED.value,
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM manual_frontier_previews WHERE preview_id = ?",
                    (preview_id,),
                ).fetchone()
                connection.rollback()
                if row is None:
                    raise PreviewNotFoundError(preview_id)
                raise ManualFrontierError(
                    f"preview {preview_id} cannot be approved from status "
                    f"{row['status']!r}"
                )
            connection.commit()
        return self.get(preview_id)

    def mark_applied(self, preview_id: str) -> ManualFrontierPreviewRecord:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE manual_frontier_previews SET status = ?
                WHERE preview_id = ? AND status = ?
                """,
                (
                    ManualFrontierPreviewStatus.APPLIED.value,
                    preview_id,
                    ManualFrontierPreviewStatus.APPROVED.value,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ManualFrontierError(
                    f"preview {preview_id} cannot be marked applied unless APPROVED"
                )
            connection.commit()
        return self.get(preview_id)

    def supersede_active_for_task(self, task_id: str) -> None:
        """Marks every non-terminal preview for ``task_id`` ``SUPERSEDED`` --
        called before creating a fresh import for the same task, so an
        older, unapplied preview can never be approved or applied after a
        newer one exists."""

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE manual_frontier_previews SET status = ?
                WHERE task_id = ? AND status IN (?, ?)
                """,
                (
                    ManualFrontierPreviewStatus.SUPERSEDED.value,
                    task_id,
                    ManualFrontierPreviewStatus.PREVIEWED.value,
                    ManualFrontierPreviewStatus.APPROVED.value,
                ),
            )
            connection.commit()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ManualFrontierPreviewRecord:
        return ManualFrontierPreviewRecord(
            preview_id=row["preview_id"],
            package_id=row["package_id"],
            task_id=row["task_id"],
            task_version_at_import=row["task_version_at_import"],
            worktree_fingerprint_at_import=row["worktree_fingerprint_at_import"],
            declared_model_name=row["declared_model_name"],
            patch=row["patch"],
            patch_sha256=row["patch_sha256"],
            summary=row["summary"],
            files_changed=json.loads(row["files_changed_json"]),
            changed_lines=row["changed_lines"],
            status=ManualFrontierPreviewStatus(row["status"]),
            created_at=row["created_at"],
            approved_at=row["approved_at"],
        )


__all__ = ["ManualFrontierPreviewStore"]
