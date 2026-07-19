from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apoapsis.architect.schema import ArchitecturePlan, PlanRecord, PlanStatus
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.config import ApoapsisConfig
from apoapsis.doctor import run_doctor
from apoapsis.reporting.report import FinalTaskReport
from apoapsis.repository.git import GitCommandError, GitRepository
from apoapsis.review.case import build_review_case
from apoapsis.review.execution import prepare_review_operation
from apoapsis.review.schema import ReviewActionKind
from apoapsis.review.store import ReviewOperationStore
from apoapsis.review.worker import ReviewWorker
from apoapsis.workflow.engine import (
    SQLiteTaskStore,
    TaskNotFoundError,
    TaskRecord,
    TaskStoreError,
)
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState


class UIActionError(TaskStoreError):
    """Raised when the UI requests an unavailable deterministic action."""


def _slice_dependency_order(plan: ArchitecturePlan) -> list[str]:
    """A stable, deterministic topological order for rendering slices.

    Falls back to appending any slice a cycle prevented from being ordered
    (in original order) rather than raising -- a plan with a dependency
    cycle is still fully visible here, with the cycle itself reported by
    ``architect.validation.validate_plan``'s own findings, not silently
    hidden by this rendering helper.
    """

    slice_ids = [item.slice_id for item in plan.slices]
    known = set(slice_ids)
    indegree = {slice_id: 0 for slice_id in slice_ids}
    dependents: dict[str, list[str]] = {slice_id: [] for slice_id in slice_ids}
    for item in plan.slices:
        for dependency in item.dependencies:
            if dependency in known and dependency != item.slice_id:
                indegree[item.slice_id] += 1
                dependents[dependency].append(item.slice_id)

    ready = sorted(slice_id for slice_id in slice_ids if indegree[slice_id] == 0)
    remaining = dict(indegree)
    ordered: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for dependent in sorted(dependents[current]):
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)
        ready.sort()

    ordered_set = set(ordered)
    ordered.extend(slice_id for slice_id in slice_ids if slice_id not in ordered_set)
    return ordered


class ApoapsisUIService:
    """Application boundary shared by the local UI and deterministic tests.

    The service exposes persisted Apoapsis facts and narrowly scoped workflow
    commands. It never calls a model provider and never grants browser code
    filesystem, shell, Git, verification, retry, or completion authority.
    """

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.metadata_root = self.project_root / ".apoapsis"
        self._review_worker: ReviewWorker | None = None

    def overview(self) -> dict[str, Any]:
        config = self._config()
        repository: dict[str, Any]
        try:
            repository = GitRepository(self.project_root).snapshot().model_dump(
                mode="json"
            )
        except (GitCommandError, OSError) as exc:
            repository = {
                "root": str(self.project_root),
                "error": str(exc),
                "branch": None,
                "head_commit": None,
                "is_clean": None,
                "changed_files": [],
            }

        tasks = [self._task_summary(item) for item in self._tasks()]
        task_counts: dict[str, int] = {}
        for task in tasks:
            state = str(task["state"])
            task_counts[state] = task_counts.get(state, 0) + 1

        return {
            "product": "Apoapsis",
            "project": {
                "name": self.project_root.name,
                "root": str(self.project_root),
                "initialized": config is not None,
            },
            "repository": repository,
            "tasks": tasks,
            "task_counts": task_counts,
            "models": self._models(config),
            "execution": self._execution(config),
            "last_model_lifecycle": self._read_json(
                self.metadata_root / "runtime" / "last-model-lifecycle.json"
            ),
            "evaluation_runs": len(self.evaluations()["runs"]),
        }

    def task_detail(self, task_id: str) -> dict[str, Any]:
        store = self._require_store()
        record = store.get_task(task_id)
        report = self._report(record.task_id)
        task_directory = self.metadata_root / "tasks" / record.task_id
        artifacts: list[str] = []
        if task_directory.is_dir():
            artifacts = [
                str(path.relative_to(self.project_root)).replace("\\", "/")
                for path in sorted(task_directory.rglob("*"))
                if path.is_file()
            ]
        return {
            "task": record.model_dump(mode="json"),
            "events": [
                event.model_dump(mode="json") for event in store.events(record.task_id)
            ],
            "report": report,
            "artifacts": artifacts,
            "available_actions": self._available_actions(record),
        }

    def approve_specification(
        self, task_id: str, *, expected_version: int
    ) -> dict[str, Any]:
        store = self._require_store()
        current = store.get_task(task_id)
        if current.state != WorkflowState.SPEC_DRAFTED:
            raise UIActionError(
                "specification approval requires SPEC_DRAFTED, found "
                f"{current.state.value}"
            )
        approved = store.transition(
            current.task_id,
            WorkflowState.SPEC_APPROVED,
            actor=WorkflowActor.USER,
            event_type="specification_approved",
            expected_version=expected_version,
        )
        return {
            "task": approved.model_dump(mode="json"),
            "events": [
                event.model_dump(mode="json")
                for event in store.events(approved.task_id)
            ],
            "available_actions": self._available_actions(approved),
        }

    def plans(self) -> dict[str, Any]:
        store = self._plan_store()
        records = [] if store is None else store.list_plans(limit=100)
        return {"plans": [self._plan_summary(item) for item in records]}

    def plan_detail(self, plan_id: str) -> dict[str, Any]:
        store = self._require_plan_store()
        record = store.get_plan(plan_id)
        plan_directory = self.metadata_root / "plans" / record.plan_id
        artifacts: list[str] = []
        if plan_directory.is_dir():
            artifacts = [
                str(path.relative_to(self.project_root)).replace("\\", "/")
                for path in sorted(plan_directory.rglob("*"))
                if path.is_file()
            ]
        return {
            "plan": record.model_dump(mode="json"),
            "events": [
                event.model_dump(mode="json") for event in store.events(record.plan_id)
            ],
            "artifacts": artifacts,
            "dependency_order": _slice_dependency_order(record.plan),
            "available_actions": self._plan_available_actions(record),
        }

    def approve_plan(self, plan_id: str, *, expected_version: int) -> dict[str, Any]:
        store = self._require_plan_store()
        approved = store.approve_plan(plan_id, expected_version=expected_version)
        return {
            "plan": approved.model_dump(mode="json"),
            "events": [
                event.model_dump(mode="json")
                for event in store.events(approved.plan_id)
            ],
            "available_actions": self._plan_available_actions(approved),
        }

    def review_cases(self) -> dict[str, Any]:
        store = self._store()
        config = self._config()
        if store is None or config is None:
            return {"cases": []}
        cases = [
            build_review_case(
                self.project_root, store, config, record.task_id
            ).model_dump(mode="json")
            for record in store.list_tasks(limit=200)
            if record.state == WorkflowState.HUMAN_REVIEW_REQUIRED
        ]
        return {"cases": cases}

    def review_case_detail(self, task_id: str) -> dict[str, Any]:
        store = self._require_store()
        config = self._config()
        if config is None:
            raise TaskStoreError(
                "Apoapsis is not initialized; run 'apoapsis init' first"
            )
        return build_review_case(
            self.project_root, store, config, task_id
        ).model_dump(mode="json")

    def submit_review_operation(
        self,
        task_id: str,
        *,
        action: str,
        operation_id: str,
        expected_version: int,
        expected_worktree_fingerprint: str | None = None,
        additional_turns: int | None = None,
    ) -> dict[str, Any]:
        """Validate and durably record a review operation, then hand it to
        the background worker -- this method itself never calls a model or
        runs a command; only ``ReviewWorker`` (on its own thread) does."""

        store = self._require_store()
        config = self._config()
        if config is None:
            raise TaskStoreError(
                "Apoapsis is not initialized; run 'apoapsis init' first"
            )
        operation_store = self._review_operation_store()
        action_kind = ReviewActionKind(action)
        review_case, budget = prepare_review_operation(
            self.project_root,
            store,
            operation_store,
            config,
            task_id=task_id,
            action=action_kind,
            operation_id=operation_id,
            expected_version=expected_version,
            expected_worktree_fingerprint=expected_worktree_fingerprint,
            additional_turns=additional_turns,
        )
        self._worker().submit(
            review_case,
            action=action_kind,
            operation_id=operation_id,
            expected_version=expected_version,
            budget=budget,
        )
        return operation_store.get(operation_id).model_dump(mode="json")

    def review_operation_status(self, operation_id: str) -> dict[str, Any]:
        return self._review_operation_store().get(operation_id).model_dump(
            mode="json"
        )

    def _review_operation_store(self) -> ReviewOperationStore:
        return ReviewOperationStore(self.metadata_root / "review-operations.db")

    def _worker(self) -> ReviewWorker:
        if self._review_worker is None:
            self._review_worker = ReviewWorker(self.project_root)
        return self._review_worker

    def doctor(self) -> dict[str, Any]:
        """Run the existing explicit diagnostic command without provider probes."""

        return run_doctor(self.project_root, probe_providers=False).model_dump(
            mode="json"
        )

    def evaluations(self) -> dict[str, Any]:
        evaluation_root = self.project_root / ".apoapsis-eval"
        runs: list[dict[str, Any]] = []
        if evaluation_root.is_dir():
            paths = sorted(
                evaluation_root.rglob("comparison.json"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for path in paths[:25]:
                payload = self._read_json(path)
                if payload is None:
                    continue
                runs.append(
                    {
                        "artifact": str(path.relative_to(self.project_root)).replace(
                            "\\", "/"
                        ),
                        "comparison": payload,
                    }
                )
        return {"runs": runs, "measured": bool(runs)}

    def _config(self) -> ApoapsisConfig | None:
        path = self.metadata_root / "config.toml"
        if not path.is_file():
            return None
        return ApoapsisConfig.from_toml(path)

    def _tasks(self) -> list[TaskRecord]:
        store = self._store()
        return [] if store is None else store.list_tasks(limit=100)

    def _store(self) -> SQLiteTaskStore | None:
        path = self.metadata_root / "apoapsis.db"
        if not path.is_file():
            return None
        return SQLiteTaskStore(path, initialize=False)

    def _require_store(self) -> SQLiteTaskStore:
        store = self._store()
        if store is None:
            raise TaskStoreError("Apoapsis is not initialized; run 'apoapsis init' first")
        return store

    def _plan_store(self) -> SQLitePlanStore | None:
        path = self.metadata_root / "architect-plans.db"
        if not path.is_file():
            return None
        return SQLitePlanStore(path, initialize=False)

    def _require_plan_store(self) -> SQLitePlanStore:
        store = self._plan_store()
        if store is None:
            raise TaskStoreError("Apoapsis is not initialized; run 'apoapsis init' first")
        return store

    @staticmethod
    def _plan_summary(record: PlanRecord) -> dict[str, Any]:
        return {
            "plan_id": record.plan_id,
            "idea_text": record.idea_text,
            "architecture_summary": record.plan.architecture_summary,
            "status": record.status.value,
            "version": record.version,
            "updated_at": record.updated_at.isoformat(),
            "slice_count": len(record.plan.slices),
        }

    @staticmethod
    def _plan_available_actions(record: PlanRecord) -> list[str]:
        if record.status == PlanStatus.VALIDATED:
            return ["approve_plan"]
        return []

    def _task_summary(self, record: TaskRecord) -> dict[str, Any]:
        report = self._report(record.task_id)
        return {
            "task_id": record.task_id,
            "objective": record.specification.objective.text,
            "state": record.state.value,
            "version": record.version,
            "updated_at": record.updated_at.isoformat(),
            "constraint_count": len(record.specification.active_hard_constraints),
            "acceptance_count": len(record.specification.acceptance_criteria),
            "outcome": None if report is None else report.get("outcome"),
        }

    def _report(self, task_id: str) -> dict[str, Any] | None:
        path = self.metadata_root / "tasks" / task_id / "report.json"
        if not path.is_file():
            return None
        try:
            report = FinalTaskReport.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {
                "outcome": "unreadable",
                "error": "report.json could not be validated as a FinalTaskReport",
            }
        return report.model_dump(mode="json")

    @staticmethod
    def _models(config: ApoapsisConfig | None) -> list[dict[str, Any]]:
        if config is None:
            return []
        models: list[dict[str, Any]] = []
        for role in ("frontier", "local_coder", "frontier_coder", "local_research"):
            item = getattr(config.models, role)
            if item is None:
                models.append({"role": role, "configured": False})
                continue
            models.append(
                {
                    "role": role,
                    "configured": True,
                    "provider": item.provider,
                    "model": item.model,
                    "base_url": item.base_url,
                    "context_window_tokens": item.context_window_tokens,
                }
            )
        return models

    @staticmethod
    def _execution(config: ApoapsisConfig | None) -> dict[str, Any] | None:
        if config is None:
            return None
        return {
            "mode": config.execution.mode.value,
            "route": config.execution.route.value,
            "completion_policy": config.execution.completion_policy.value,
            "verification_backend": config.verification.backend.backend.value,
            "max_turns": config.execution.agent.max_turns,
            "max_patch_attempts": config.execution.agent.max_patch_attempts,
            "max_verification_runs": config.execution.agent.max_verification_runs,
        }

    @staticmethod
    def _available_actions(record: TaskRecord) -> list[str]:
        if record.state == WorkflowState.SPEC_DRAFTED:
            return ["approve_specification"]
        return []

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None


__all__ = ["ApoapsisUIService", "UIActionError", "TaskNotFoundError"]
