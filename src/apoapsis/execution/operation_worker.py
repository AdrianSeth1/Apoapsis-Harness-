from __future__ import annotations

import queue
import threading
from pathlib import Path

from apoapsis.config import ApoapsisConfig
from apoapsis.execution.operation_recovery import recover_stale_execution_operations
from apoapsis.execution.operation_service import run_execution_operation
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.workflow.engine import SQLiteTaskStore, TaskStoreError


class ExecutionWorker:
    """Runs authorized post-approval task-execution operations on a
    background thread, outside any HTTP request (ADR 0024), structurally
    mirroring ``review.worker.ReviewWorker`` and
    ``intake.worker.IntakeWorker``.

    The queue carries only an ``operation_id`` -- ``run_execution_operation``
    reloads the task id, expected version, and expected repository HEAD
    from the durable ``ExecutionOperationRecord`` and freshly rechecks the
    task immediately before doing anything, so a delay between submission
    and execution (or a queued job surviving a restart) can never act on
    stale in-memory state.

    Submission (``prepare_execution_operation``, called synchronously by
    the HTTP handler before ``submit()``) has already durably recorded the
    operation as ``RECORDED`` before this worker ever sees it -- a browser
    disconnect after that point cannot cancel, duplicate, or repeat the
    operation; it just keeps running and its result is read back later by
    polling the operation id.

    At startup, this worker runs one explicit recovery pass
    (``execution.operation_recovery.recover_stale_execution_operations``):
    any operation still ``RECORDED`` from before this process started is
    safe to reclaim -- nothing was ever transmitted for it -- and is
    re-enqueued here. A ``RUNNING`` operation stale beyond the recovery
    module's expiry window is instead moved to the terminal, inspectable
    ``AMBIGUOUS`` status and never automatically repeated; any worktree it
    created is left untouched.
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
            operation_store = ExecutionOperationStore(
                self.project_root / ".apoapsis" / "execution-operations.db"
            )
        except TaskStoreError:
            return  # a brand-new project has no task database yet
        report = recover_stale_execution_operations(task_store, operation_store)
        for operation_id in report.reclaimed_operation_ids:
            self._queue.put(operation_id)

    def _run(self) -> None:
        while True:
            operation_id = self._queue.get()
            try:
                self._execute(operation_id)
            except Exception:
                # `run_execution_operation` already records failure on the
                # operation itself; this worker loop must never die.
                pass

    def _execute(self, operation_id: str) -> None:
        config = ApoapsisConfig.from_toml(
            self.project_root / ".apoapsis" / "config.toml"
        )
        task_store = SQLiteTaskStore(self.project_root / ".apoapsis" / "apoapsis.db")
        operation_store = ExecutionOperationStore(
            self.project_root / ".apoapsis" / "execution-operations.db"
        )
        run_execution_operation(
            self.project_root,
            task_store,
            operation_store,
            config,
            operation_id=operation_id,
        )


__all__ = ["ExecutionWorker"]
