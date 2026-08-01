from __future__ import annotations

import hashlib
import json
import queue
import sqlite3
import threading
import uuid
from contextlib import closing
from enum import StrEnum
from pathlib import Path
from typing import Callable

from pydantic import Field

from apoapsis.architect.errors import PlanActionError
from apoapsis.architect.schema import ArchitecturePlan, PlanStatus
from apoapsis.architect.slice_service import (
    approve_slice,
    package_slice,
    project_slice_status,
    start_slice,
)
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.config import ApoapsisConfig
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.repository.readiness import exclude_registered_plan_response_transfers
from apoapsis.specification.schema import StrictModel, utc_now
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.events import WorkflowActor


class PlanRunStatus(StrEnum):
    RECORDED = "recorded"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


class PlanRunRecord(StrictModel):
    run_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    expected_plan_version: int = Field(ge=1)
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    auto_advance: bool = True
    status: PlanRunStatus
    current_slice_id: str | None = None
    execution_operation_id: str | None = None
    completed_slice_ids: list[str] = Field(default_factory=list)
    detail: str = Field(min_length=1)
    created_at: str
    updated_at: str


class PlanRunStore:
    """Durable plan-level authorization and progress ledger.

    One row authorizes controller-produced, hash-bound packages for one exact
    approved plan version. It never grants a model transition authority.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS plan_runs (
                    run_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    expected_plan_version INTEGER NOT NULL,
                    config_sha256 TEXT NOT NULL,
                    auto_advance INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    current_slice_id TEXT,
                    execution_operation_id TEXT,
                    completed_slice_ids_json TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_plan_runs_plan_status
                ON plan_runs(plan_id, status);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=5.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def create(
        self,
        *,
        run_id: str,
        plan_id: str,
        expected_plan_version: int,
        config_sha256: str,
        auto_advance: bool,
    ) -> PlanRunRecord:
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT run_id FROM plan_runs WHERE plan_id = ? AND status IN (?, ?)",
                (plan_id, PlanRunStatus.RECORDED.value, PlanRunStatus.RUNNING.value),
            ).fetchone()
            if active is not None:
                connection.rollback()
                raise PlanActionError(
                    f"plan {plan_id} already has active run {active['run_id']}"
                )
            try:
                connection.execute(
                    "INSERT INTO plan_runs VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
                    (
                        run_id,
                        plan_id,
                        expected_plan_version,
                        config_sha256,
                        int(auto_advance),
                        PlanRunStatus.RECORDED.value,
                        "[]",
                        "Authorized and waiting for the controller.",
                        now,
                        now,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get(run_id)

    def get(self, run_id: str) -> PlanRunRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM plan_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise PlanActionError(f"plan run not found: {run_id}")
        return self._record(row)

    def latest_for_plan(self, plan_id: str) -> PlanRunRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM plan_runs WHERE plan_id = ? ORDER BY created_at DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def recorded(self) -> list[PlanRunRecord]:
        return self._with_status(PlanRunStatus.RECORDED)

    def running(self) -> list[PlanRunRecord]:
        return self._with_status(PlanRunStatus.RUNNING)

    def _with_status(self, status: PlanRunStatus) -> list[PlanRunRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM plan_runs WHERE status = ? ORDER BY created_at",
                (status.value,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def update(
        self,
        run_id: str,
        *,
        status: PlanRunStatus | None = None,
        current_slice_id: str | None = None,
        execution_operation_id: str | None = None,
        completed_slice_ids: list[str] | None = None,
        detail: str,
    ) -> PlanRunRecord:
        current = self.get(run_id)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE plan_runs SET status = ?, current_slice_id = ?,
                    execution_operation_id = ?, completed_slice_ids_json = ?,
                    detail = ?, updated_at = ? WHERE run_id = ?
                """,
                (
                    (status or current.status).value,
                    current_slice_id,
                    execution_operation_id,
                    json.dumps(
                        completed_slice_ids
                        if completed_slice_ids is not None
                        else current.completed_slice_ids
                    ),
                    detail,
                    utc_now().isoformat(),
                    run_id,
                ),
            )
        return self.get(run_id)

    @staticmethod
    def _record(row: sqlite3.Row) -> PlanRunRecord:
        return PlanRunRecord(
            run_id=row["run_id"],
            plan_id=row["plan_id"],
            expected_plan_version=row["expected_plan_version"],
            config_sha256=row["config_sha256"],
            auto_advance=bool(row["auto_advance"]),
            status=PlanRunStatus(row["status"]),
            current_slice_id=row["current_slice_id"],
            execution_operation_id=row["execution_operation_id"],
            completed_slice_ids=json.loads(row["completed_slice_ids_json"]),
            detail=row["detail"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def config_digest(config: ApoapsisConfig) -> str:
    body = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def dependency_order(plan: ArchitecturePlan) -> list[str]:
    by_id = {item.slice_id: item for item in plan.slices}
    pending = set(by_id)
    ordered: list[str] = []
    while pending:
        ready = sorted(
            item for item in pending if set(by_id[item].dependencies).issubset(ordered)
        )
        if not ready:
            return ordered + sorted(pending)
        ordered.extend(ready)
        pending.difference_update(ready)
    return ordered


def run_plan(
    project_root: str | Path,
    store: PlanRunStore,
    run_id: str,
    *,
    execute_slice: Callable[..., object] = start_slice,
    config_override: ApoapsisConfig | None = None,
) -> PlanRunRecord:
    root = Path(project_root).resolve()
    run = store.get(run_id)
    store.update(run_id, status=PlanRunStatus.RUNNING, detail="Checking the approved plan.")
    try:
        config = config_override or ApoapsisConfig.from_toml(
            root / ".apoapsis" / "config.toml"
        )
        if config_digest(config) != run.config_sha256:
            return store.update(
                run_id,
                status=PlanRunStatus.PAUSED,
                detail="Configuration changed after authorization; review and start again.",
            )
        plan_store = SQLitePlanStore(root / ".apoapsis" / "architect-plans.db")
        task_store = SQLiteTaskStore(root / ".apoapsis" / "apoapsis.db")
        slice_store = PlanSliceExecutionStore(
            root / ".apoapsis" / "plan-slice-executions.db"
        )
        operation_store = ExecutionOperationStore(
            root / ".apoapsis" / "execution-operations.db"
        )
        plan_record = plan_store.get_plan(run.plan_id)
        if (
            plan_record.version != run.expected_plan_version
            or plan_record.status != PlanStatus.APPROVED
        ):
            return store.update(
                run_id,
                status=PlanRunStatus.PAUSED,
                detail="The approved plan version changed; review and authorize it again.",
            )

        # Manual plan-response files are transfer material, not project source.
        # Recover an already-imported response before package_slice snapshots the
        # parent repository, using the same exact-audit-match rule as ordinary
        # execution preflight.
        exclude_registered_plan_response_transfers(root)

        completed = list(run.completed_slice_ids)
        for slice_id in dependency_order(plan_record.plan):
            status = project_slice_status(
                root,
                plan_store,
                slice_store,
                task_store,
                run.plan_id,
                slice_id,
                operation_store=operation_store,
            )
            state = status["status"]
            if state == "complete":
                if slice_id not in completed:
                    completed.append(slice_id)
                continue
            if state in {"running", "human_review", "failed", "superseded"}:
                return store.update(
                    run_id,
                    status=PlanRunStatus.PAUSED,
                    current_slice_id=slice_id,
                    completed_slice_ids=completed,
                    detail=f"Stopped at {slice_id}: {state.replace('_', ' ')}.",
                )
            if state == "ready_or_blocked":
                readiness = status.get("readiness") or {}
                if not readiness.get("ready"):
                    return store.update(
                        run_id,
                        status=PlanRunStatus.PAUSED,
                        current_slice_id=slice_id,
                        completed_slice_ids=completed,
                        detail=f"Stopped at {slice_id}: dependencies are not ready.",
                    )
            if state in {"ready_or_blocked", "packaged"}:
                # Rebuild even when a manual package already exists. Auto mode
                # authorizes the current configuration and repository state,
                # not whatever bytes an older preview happened to bind.
                package = package_slice(
                    root,
                    plan_store,
                    slice_store,
                    task_store,
                    operation_store,
                    run.plan_id,
                    slice_id,
                    expected_plan_version=run.expected_plan_version,
                    config=config,
                )
                approve_slice(
                    root,
                    task_store,
                    slice_store,
                    run.plan_id,
                    slice_id,
                    expected_package_sha256=package.package_sha256,
                    approval_actor=WorkflowActor.SYSTEM,
                    approval_event_type="plan_slice_auto_approved",
                    approval_context={"plan_run_id": run.run_id},
                )

            operation_id = f"EXOP-{uuid.uuid4().hex[:24].upper()}"
            store.update(
                run_id,
                current_slice_id=slice_id,
                execution_operation_id=operation_id,
                completed_slice_ids=completed,
                detail=f"Running {slice_id} through Apoapsis verification.",
            )
            execute_slice(
                root,
                task_store,
                slice_store,
                operation_store,
                run.plan_id,
                slice_id,
                config,
                operation_id=operation_id,
            )
            after = project_slice_status(
                root,
                plan_store,
                slice_store,
                task_store,
                run.plan_id,
                slice_id,
                operation_store=operation_store,
            )
            if after["status"] != "complete":
                return store.update(
                    run_id,
                    status=PlanRunStatus.PAUSED,
                    current_slice_id=slice_id,
                    execution_operation_id=operation_id,
                    completed_slice_ids=completed,
                    detail=(
                        f"Stopped at {slice_id}: verification produced "
                        f"{after['status'].replace('_', ' ')}."
                    ),
                )
            completed.append(slice_id)
            if not run.auto_advance:
                return store.update(
                    run_id,
                    status=PlanRunStatus.SUCCEEDED,
                    current_slice_id=slice_id,
                    execution_operation_id=operation_id,
                    completed_slice_ids=completed,
                    detail=f"{slice_id} completed. The next slice is ready when you are.",
                )

        return store.update(
            run_id,
            status=PlanRunStatus.SUCCEEDED,
            completed_slice_ids=completed,
            detail="Every slice completed. Final project preparation remains a separate action.",
        )
    except Exception as exc:  # noqa: BLE001 - durable operator-facing failure
        return store.update(
            run_id,
            status=PlanRunStatus.FAILED,
            current_slice_id=store.get(run_id).current_slice_id,
            execution_operation_id=store.get(run_id).execution_operation_id,
            detail=f"{type(exc).__name__}: {exc}",
        )


class PlanRunWorker:
    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.store = PlanRunStore(self.project_root / ".apoapsis" / "plan-runs.db")
        self._queue: queue.Queue[str | None] = queue.Queue()
        for item in self.store.running():
            self.store.update(
                item.run_id,
                status=PlanRunStatus.AMBIGUOUS,
                current_slice_id=item.current_slice_id,
                execution_operation_id=item.execution_operation_id,
                detail="The app stopped during this run. Inspect the current slice; it was not repeated.",
            )
        for item in self.store.recorded():
            self._queue.put(item.run_id)
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def submit(self, run_id: str) -> None:
        self._queue.put(run_id)

    def shutdown(self, timeout_seconds: float = 30.0) -> bool:
        self._queue.put(None)
        self._thread.join(timeout_seconds)
        return not self._thread.is_alive()

    def _work(self) -> None:
        while True:
            run_id = self._queue.get()
            try:
                if run_id is None:
                    return
                run_plan(self.project_root, self.store, run_id)
            finally:
                self._queue.task_done()


__all__ = [
    "PlanRunRecord",
    "PlanRunStatus",
    "PlanRunStore",
    "PlanRunWorker",
    "config_digest",
    "dependency_order",
    "run_plan",
]
