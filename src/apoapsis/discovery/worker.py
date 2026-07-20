from __future__ import annotations

import queue
import threading
from pathlib import Path

from apoapsis.architect.store import SQLitePlanStore
from apoapsis.config import ApoapsisConfig
from apoapsis.discovery.operation_recovery import recover_stale_discovery_operations
from apoapsis.discovery.operation_service import run_discovery_operation
from apoapsis.discovery.operation_store import DiscoveryOperationStore
from apoapsis.discovery.store import SQLiteDiscoveryStore


class DiscoveryWorker:
    """Runs discovery model-call operations on a background thread,
    outside any HTTP request -- structurally identical to
    ``intake.worker.IntakeWorker`` (ADR 0023), applied here to ADR 0032's
    local-clarification/idea-brief/frontier-API-call operations.

    The queue carries only an ``operation_id`` -- ``run_discovery_operation``
    reloads everything else from the durable ``DiscoveryOperationRecord``
    and freshly rechecks the session's current version immediately before
    doing anything.

    At startup, this worker runs one explicit recovery pass
    (``discovery.operation_recovery.recover_stale_discovery_operations``):
    any operation still ``RECORDED`` from before this process started is
    safe to reclaim -- nothing was ever transmitted for it -- and is
    re-enqueued here. A ``RUNNING`` operation stale beyond its lease is
    instead moved to the terminal, inspectable ``AMBIGUOUS`` status and
    never automatically repeated.
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
            operation_store = DiscoveryOperationStore(
                self.project_root / ".apoapsis" / "discovery-operations.db"
            )
        except Exception:
            return  # a brand-new project has no discovery database yet
        report = recover_stale_discovery_operations(operation_store)
        for operation_id in report.reclaimed_operation_ids:
            self._queue.put(operation_id)

    def _run(self) -> None:
        while True:
            operation_id = self._queue.get()
            try:
                self._execute(operation_id)
            except Exception:
                # `run_discovery_operation` already records failure on the
                # operation itself; this worker loop must never die.
                pass

    def _execute(self, operation_id: str) -> None:
        config = ApoapsisConfig.from_toml(
            self.project_root / ".apoapsis" / "config.toml"
        )
        discovery_store = SQLiteDiscoveryStore(
            self.project_root / ".apoapsis" / "discovery-sessions.db"
        )
        plan_store = SQLitePlanStore(self.project_root / ".apoapsis" / "architect-plans.db")
        operation_store = DiscoveryOperationStore(
            self.project_root / ".apoapsis" / "discovery-operations.db"
        )
        run_discovery_operation(
            self.project_root,
            discovery_store,
            plan_store,
            config,
            operation_store,
            operation_id=operation_id,
        )


__all__ = ["DiscoveryWorker"]
