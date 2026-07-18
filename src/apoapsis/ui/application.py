from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apoapsis.config import ApoapsisConfig
from apoapsis.doctor import run_doctor
from apoapsis.reporting.report import FinalTaskReport
from apoapsis.repository.git import GitCommandError, GitRepository
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


class ApoapsisUIService:
    """Application boundary shared by the local UI and deterministic tests.

    The service exposes persisted Apoapsis facts and narrowly scoped workflow
    commands. It never calls a model provider and never grants browser code
    filesystem, shell, Git, verification, retry, or completion authority.
    """

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.metadata_root = self.project_root / ".apoapsis"

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
