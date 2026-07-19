from __future__ import annotations

import queue
import threading
from pathlib import Path

from apoapsis.config import ApoapsisConfig
from apoapsis.review.execution import run_review_operation
from apoapsis.review.recovery import recover_stale_operations
from apoapsis.review.store import ReviewOperationStore
from apoapsis.workflow.engine import SQLiteTaskStore, TaskStoreError


class ReviewWorker:
    """Runs authorized human-review operations on a background thread,
    outside any HTTP request (ADR 0020 Commit C2, hardened by ADR 0021).

    The queue carries only an ``operation_id`` -- ``run_review_operation``
    reloads the task, action, expected version/fingerprint, and authorized
    budget from the durable ``ReviewOperationRecord`` and freshly
    re-projects and re-checks the ``ReviewCase`` immediately before doing
    anything, so a delay between submission and execution (or a queued job
    surviving a restart) can never act on stale in-memory state.

    Submission (``prepare_review_operation``, called synchronously by the
    HTTP handler before ``submit()``) has already validated the request and
    durably recorded the operation as ``RECORDED`` before this worker ever
    sees it -- a browser disconnect after that point cannot cancel,
    duplicate, or repeat the operation; it just keeps running and its
    result is read back later by polling the operation id.

    At startup, this worker runs one explicit recovery pass
    (``review.recovery.recover_stale_operations``): any operation still
    ``RECORDED`` from before this process started (for example, its
    in-memory queue was lost when the previous process died) is safe to
    reclaim -- nothing was ever transmitted for it -- and is re-enqueued
    here. A ``RUNNING`` operation stale beyond the recovery module's
    expiry window is instead moved to the terminal, inspectable
    ``AMBIGUOUS`` status and never automatically repeated.
    """

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self._queue: queue.Queue[str] = queue.Queue()
        self._recover_at_startup()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, operation_id: str) -> None:
        self._queue.put(operation_id)

    def _recover_at_startup(self) -> None:
        try:
            task_store = SQLiteTaskStore(
                self.project_root / ".apoapsis" / "apoapsis.db", initialize=False
            )
            operation_store = ReviewOperationStore(
                self.project_root / ".apoapsis" / "review-operations.db"
            )
        except TaskStoreError:
            return  # a brand-new project has no task database yet
        report = recover_stale_operations(task_store, operation_store)
        for operation_id in report.reclaimed_operation_ids:
            self._queue.put(operation_id)

    def _run(self) -> None:
        while True:
            operation_id = self._queue.get()
            try:
                self._execute(operation_id)
            except Exception:
                # `run_review_operation` already records failure on the
                # operation itself; this worker loop must never die.
                pass

    def _execute(self, operation_id: str) -> None:
        config = ApoapsisConfig.from_toml(
            self.project_root / ".apoapsis" / "config.toml"
        )
        task_store = SQLiteTaskStore(self.project_root / ".apoapsis" / "apoapsis.db")
        operation_store = ReviewOperationStore(
            self.project_root / ".apoapsis" / "review-operations.db"
        )
        run_review_operation(
            self.project_root,
            task_store,
            operation_store,
            config,
            operation_id=operation_id,
        )


__all__ = ["ReviewWorker"]
