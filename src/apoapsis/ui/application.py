from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apoapsis.architect.schema import ArchitecturePlan, PlanRecord, PlanStatus
from apoapsis.architect.slice_service import (
    approve_slice,
    package_slice,
    project_slice_status,
    read_latest_slice_package,
)
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import ApoapsisConfig, ExecutionMode
from apoapsis.discovery.api import (
    FrontierPlanningApiNotConfiguredError,
    preview_frontier_planning_api_call,
)
from apoapsis.discovery.frontier_package import (
    FrontierPlanningRequestPackage,
    load_package as load_frontier_planning_package,
)
from apoapsis.discovery.manual import (
    import_manual_frontier_planning_response as import_discovery_manual_response_fn,
)
from apoapsis.discovery.operation_schema import DiscoveryOperationAction
from apoapsis.discovery.operation_service import prepare_discovery_operation
from apoapsis.discovery.operation_store import DiscoveryOperationStore
from apoapsis.discovery.schema import ClarificationAnswer
from apoapsis.discovery.service import (
    approve_idea_brief_step as approve_discovery_idea_brief_fn,
    export_frontier_planning_package,
    record_frontier_answers as record_discovery_frontier_answers_fn,
    record_local_answers as record_discovery_local_answers_fn,
    start_session as start_discovery_session_fn,
)
from apoapsis.discovery.store import SQLiteDiscoveryStore
from apoapsis.discovery.worker import DiscoveryWorker
from apoapsis.doctor import run_doctor
from apoapsis.execution.authorization import build_execution_authorization_package
from apoapsis.execution.operation_errors import ExecutionAuthorizationDriftError
from apoapsis.execution.operation_service import prepare_execution_operation
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.execution.operation_worker import ExecutionWorker
from apoapsis.intake.execution import prepare_intake_operation
from apoapsis.intake.store import IntakeOperationStore
from apoapsis.intake.worker import IntakeWorker
from apoapsis.manual_frontier.approve import (
    approve_manual_frontier_preview as approve_manual_frontier_preview_fn,
)
from apoapsis.manual_frontier.importer import (
    import_manual_frontier_response as import_manual_frontier_response_fn,
)
from apoapsis.manual_frontier.package import (
    build_manual_frontier_handoff_package,
    load_package as load_manual_frontier_package,
    write_handoff_artifacts,
)
from apoapsis.manual_frontier.store import ManualFrontierPreviewStore
from apoapsis.reporting.report import FinalTaskReport
from apoapsis.repository.git import GitCommandError, GitRepository
from apoapsis.review.case import build_review_case
from apoapsis.review.execution import prepare_review_operation
from apoapsis.review.schema import ReviewActionKind
from apoapsis.review.store import ReviewOperationStore
from apoapsis.review.worker import ReviewWorker
from apoapsis.agent.session import AgentTurnRecord
from apoapsis.workflow.engine import (
    SQLiteTaskStore,
    TaskNotFoundError,
    TaskRecord,
    TaskStoreError,
)
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.routing import select_agent_route
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
        self._intake_worker: IntakeWorker | None = None
        self._execution_worker: ExecutionWorker | None = None
        self._discovery_worker: DiscoveryWorker | None = None

    def start_background_workers(self) -> None:
        """Eagerly construct all three operation workers, running each
        one's startup recovery pass immediately (ADR 0025) -- rather than
        waiting for whatever operation type happens to be submitted
        first. ``create_ui_server()`` calls this once, right after
        construction, so a stranded ``RECORDED`` operation from a crashed
        previous process is reclaimed and queued the moment ``apoapsis
        ui`` starts, not only when an unrelated new submission happens to
        lazily construct that operation type's worker for the first time
        (which also closes a duplicate-enqueue window: recovery's
        startup scan can no longer race a submission that is preparing
        its own, not-yet-enqueued operation on the same worker's very
        first construction).

        Idempotent and safe to call more than once; only the first call
        for each operation type actually constructs anything.
        """

        self._worker()
        self._intake_worker_instance()
        self._execution_worker_instance()
        self._discovery_worker_instance()

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
        active_operation = None
        config = self._config()
        if config is not None:
            active_record = self._execution_operation_store().find_active_for_task(
                record.task_id
            )
            if active_record is not None:
                active_operation = active_record.model_dump(mode="json")
        return {
            "task": record.model_dump(mode="json"),
            "events": [
                event.model_dump(mode="json") for event in store.events(record.task_id)
            ],
            "report": report,
            "artifacts": artifacts,
            "available_actions": self._available_actions(record),
            "execution_preview": (
                self._execution_preview(record, config) if config is not None else None
            ),
            "active_execution_operation": active_operation,
            "recent_agent_turns": self._recent_agent_turns(task_directory),
        }

    def submit_execution_operation(
        self,
        task_id: str,
        *,
        operation_id: str,
        expected_version: int,
        expected_authorization_sha256: str,
    ) -> dict[str, Any]:
        """Validate and durably record an execution operation, then hand it
        to the background worker -- this method itself never calls a model,
        creates a worktree, or runs a command; only ``ExecutionWorker`` (on
        its own thread) does.

        ``expected_authorization_sha256`` must match the package hash the
        preview showed (ADR 0026): the confirmation authorizes exactly
        what was previewed, not whatever the task/specification/repository/
        configuration happen to be by the time the confirmation arrives.
        Rejected before ``prepare_execution_operation`` -- and therefore
        before any audit write, worktree mutation, or provider
        construction -- if the task, specification, repository state, or
        execution configuration changed since the preview was rendered."""

        store = self._require_store()
        config = self._config()
        if config is None:
            raise TaskStoreError(
                "Apoapsis is not initialized; run 'apoapsis init' first"
            )
        task = store.get_task(task_id)
        current_package = build_execution_authorization_package(
            self.project_root,
            operation_id=operation_id,
            task_id=task_id,
            task_version=task.version,
            specification=task.specification,
            config=config,
        )
        if current_package.package_sha256 != expected_authorization_sha256:
            raise ExecutionAuthorizationDriftError(
                "the previewed authorization no longer matches the current "
                "task, specification, repository state, or execution "
                "configuration -- reload the task page and try again"
            )
        operation_store = self._execution_operation_store()
        prepare_execution_operation(
            self.project_root,
            store,
            operation_store,
            task_id=task_id,
            operation_id=operation_id,
            expected_version=expected_version,
            config=config,
        )
        self._execution_worker_instance().submit(operation_id)
        return operation_store.get(operation_id).model_dump(mode="json")

    def execution_operation_status(self, operation_id: str) -> dict[str, Any]:
        return self._execution_operation_store().get(operation_id).model_dump(
            mode="json"
        )

    def _execution_operation_store(self) -> ExecutionOperationStore:
        return ExecutionOperationStore(self.metadata_root / "execution-operations.db")

    def _execution_worker_instance(self) -> ExecutionWorker:
        if self._execution_worker is None:
            self._execution_worker = ExecutionWorker(self.project_root)
        return self._execution_worker

    def _execution_preview(
        self, record: TaskRecord, config: ApoapsisConfig
    ) -> dict[str, Any] | None:
        """A read-only, deterministic preview of what starting execution
        would do -- computed with the exact same ``select_agent_route()``
        the real execution service uses, never a separate guess. Only
        meaningful once a route can actually be decided (the specification
        already exists), so it is always returned regardless of task
        state; the UI only offers to start execution at ``SPEC_APPROVED``.

        Also builds the exact ``ExecutionAuthorizationPackage`` (ADR 0026)
        a real submission would authorize, using a placeholder
        ``operation_id`` (a real one is chosen client-side only once the
        user actually confirms) -- ``package_sha256`` excludes
        ``operation_id`` from its input, so this preview's hash is
        reproducible against the real operation_id at submission time as
        long as nothing about the task, specification, repository state,
        or configuration has changed. Never writes anything -- purely a
        read, unlike ``prepare_execution_operation``, which builds the
        same package again and persists it."""

        frontier_available = config.models.frontier_coder is not None
        routing_decision = None
        if config.execution.mode == ExecutionMode.AGENT:
            routing_decision = select_agent_route(
                record.specification, config.execution, frontier_available=frontier_available
            )
        local_model = (
            config.models.local_coder.model
            if config.models.local_coder is not None
            else config.models.frontier.model
        )
        authorization_package = build_execution_authorization_package(
            self.project_root,
            operation_id="EXOP-PREVIEW",
            task_id=record.task_id,
            task_version=record.version,
            specification=record.specification,
            config=config,
        )
        return {
            "execution_mode": config.execution.mode.value,
            "authorization_sha256": authorization_package.package_sha256,
            "authority_rules": authorization_package.authority_rules,
            "predicted_route": (
                routing_decision.route.value if routing_decision is not None else None
            ),
            "predicted_route_reason": (
                routing_decision.reason if routing_decision is not None else None
            ),
            "completion_policy": config.execution.completion_policy.value,
            "verification_backend": config.verification.backend.backend.value,
            "verification_commands": [
                item.name for item in config.verification.commands
            ],
            "local_model": local_model,
            "frontier_model": (
                config.models.frontier_coder.model
                if config.models.frontier_coder is not None
                else None
            ),
            "frontier_available": frontier_available,
            "local_budget": config.execution.agent.model_dump(mode="json"),
            "frontier_budget": config.execution.frontier_agent.model_dump(mode="json"),
        }

    # A session always runs every local turn (if any) before ever
    # escalating to a frontier turn -- never interleaved -- so this is a
    # genuine execution-order priority, not an alphabetical accident (the
    # previous ``(stage, turn)`` sort put "frontier" before "local"
    # alphabetically, showing an escalated session's turns backwards).
    _STAGE_EXECUTION_ORDER = {"local": 0, "frontier": 1}

    @classmethod
    def _recent_agent_turns(
        cls, task_directory: Path, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        if not task_directory.is_dir():
            return []
        turns: list[dict[str, Any]] = []
        for path in task_directory.glob("*agent-turn-*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            try:
                record = AgentTurnRecord.model_validate(payload)
            except ValueError:
                continue
            turns.append(
                {
                    "stage": "frontier" if path.name.startswith("frontier-") else "local",
                    **record.model_dump(mode="json", exclude={"observation_ledger"}),
                }
            )
        turns.sort(
            key=lambda item: (
                cls._STAGE_EXECUTION_ORDER.get(item["stage"], 2),
                item["turn"],
            )
        )
        return turns[-limit:]

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
            "slices": self._plan_slice_statuses(record),
        }

    def plan_slice_detail(self, plan_id: str, slice_id: str) -> dict[str, Any]:
        """Everything the app needs to let a person select, inspect,
        package, and approve exactly one plan slice -- read-only, no
        model call, no repository mutation of its own (ADR 0027)."""

        plan_store = self._require_plan_store()
        task_store = self._require_store()
        plan_record = plan_store.get_plan(plan_id)
        slice_obj = next(
            (item for item in plan_record.plan.slices if item.slice_id == slice_id),
            None,
        )
        if slice_obj is None:
            raise TaskStoreError(f"plan {plan_id} has no slice {slice_id}")
        status = project_slice_status(
            self.project_root,
            plan_store,
            self._plan_slice_store(),
            task_store,
            plan_id,
            slice_id,
        )
        package = read_latest_slice_package(self.project_root, plan_id, slice_id)
        task = None
        record = status.get("record")
        if record and record.get("task_id"):
            try:
                task = self.task_detail(record["task_id"])
            except TaskNotFoundError:
                task = None
        return {
            "plan_id": plan_id,
            "slice_id": slice_id,
            "plan_version": plan_record.version,
            "slice": slice_obj.model_dump(mode="json"),
            "status": status,
            "package": package.model_dump(mode="json") if package is not None else None,
            "task": task,
        }

    def package_plan_slice(
        self, plan_id: str, slice_id: str, *, expected_plan_version: int
    ) -> dict[str, Any]:
        """Deterministically compiles and durably records an immutable
        execution package for one slice -- no model call, no task created
        yet (ADR 0027)."""

        config = self._config()
        if config is None:
            raise TaskStoreError(
                "Apoapsis is not initialized; run 'apoapsis init' first"
            )
        package = package_slice(
            self.project_root,
            self._require_plan_store(),
            self._plan_slice_store(),
            self._require_store(),
            self._execution_operation_store(),
            plan_id,
            slice_id,
            expected_plan_version=expected_plan_version,
            config=config,
        )
        return package.model_dump(mode="json")

    def approve_plan_slice(
        self, plan_id: str, slice_id: str, *, expected_package_sha256: str
    ) -> dict[str, Any]:
        """Approves exactly the previewed package: creates and approves
        the derived task through the normal specification-approval
        transitions, but never starts it -- starting is a separate,
        explicit action on the resulting task's own control room (ADR
        0027; the exact same durable execution service ADR 0024 built,
        unmodified)."""

        record = approve_slice(
            self.project_root,
            self._require_store(),
            self._plan_slice_store(),
            plan_id,
            slice_id,
            expected_package_sha256=expected_package_sha256,
        )
        return record.model_dump(mode="json")

    def _plan_slice_store(self) -> PlanSliceExecutionStore:
        return PlanSliceExecutionStore(
            self.metadata_root / "plan-slice-executions.db"
        )

    def _plan_slice_statuses(self, record: PlanRecord) -> list[dict[str, Any]]:
        task_store = self._store()
        if task_store is None:
            return []
        slice_store = self._plan_slice_store()
        statuses = []
        for item in record.plan.slices:
            status = project_slice_status(
                self.project_root,
                self._require_plan_store(),
                slice_store,
                task_store,
                record.plan_id,
                item.slice_id,
            )
            status["title"] = item.title
            status["dependencies"] = list(item.dependencies)
            statuses.append(status)
        return statuses

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
        manual_frontier_preview_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate and durably record a review operation, then hand it to
        the background worker -- this method itself never calls a model or
        runs a command; only ``ReviewWorker`` (on its own thread) does.

        ``manual_frontier_preview_id`` is only meaningful for
        ``action="manual_frontier_handoff"`` (ADR 0031): it must name a
        preview the operator already explicitly approved in a separate,
        earlier step (``approve_manual_frontier_preview``) -- this call is
        the second of that action's two required approval steps and is
        itself the actual worktree-mutating apply."""

        store = self._require_store()
        config = self._config()
        if config is None:
            raise TaskStoreError(
                "Apoapsis is not initialized; run 'apoapsis init' first"
            )
        operation_store = self._review_operation_store()
        action_kind = ReviewActionKind(action)
        prepare_review_operation(
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
            manual_frontier_preview_id=manual_frontier_preview_id,
        )
        self._worker().submit(operation_id)
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

    def submit_intake_operation(
        self, *, request_text: str, operation_id: str
    ) -> dict[str, Any]:
        """Validate and durably record a new-task intake operation, then
        hand it to the background worker -- this method itself never calls
        a model; only ``IntakeWorker`` (on its own thread) does."""

        if self._store() is None:
            raise TaskStoreError(
                "Apoapsis is not initialized; run 'apoapsis init' first"
            )
        operation_store = self._intake_operation_store()
        prepare_intake_operation(
            self.project_root,
            self._require_store(),
            operation_store,
            request_text=request_text,
            operation_id=operation_id,
        )
        self._intake_worker_instance().submit(operation_id)
        return operation_store.get(operation_id).model_dump(mode="json")

    def intake_operation_status(self, operation_id: str) -> dict[str, Any]:
        return self._intake_operation_store().get(operation_id).model_dump(
            mode="json"
        )

    def _intake_operation_store(self) -> IntakeOperationStore:
        return IntakeOperationStore(self.metadata_root / "intake-operations.db")

    def _intake_worker_instance(self) -> IntakeWorker:
        if self._intake_worker is None:
            self._intake_worker = IntakeWorker(self.project_root)
        return self._intake_worker

    # ---- Manual subscription-based frontier coding handoff (ADR 0031) ----

    def _manual_frontier_preview_store(self) -> ManualFrontierPreviewStore:
        return ManualFrontierPreviewStore(
            self.metadata_root / "manual-frontier-previews.db"
        )

    def manual_frontier_previews(self, task_id: str) -> dict[str, Any]:
        previews = self._manual_frontier_preview_store().list_for_task(task_id)
        return {"previews": [item.model_dump(mode="json") for item in previews]}

    def export_manual_frontier_handoff(self, task_id: str) -> dict[str, Any]:
        """Builds the immutable handoff package plus the self-contained
        ``FRONTIER-CODING-HANDOFF.md`` a person uploads by hand to a
        ChatGPT/Claude subscription session -- deterministic, no model
        call, no worktree mutation. Returns both the project-relative and
        absolute artifact paths so the browser can show exactly where the
        file landed on disk."""

        task_store = self._require_store()
        config = self._config()
        if config is None:
            raise TaskStoreError(
                "Apoapsis is not initialized; run 'apoapsis init' first"
            )
        review_case = build_review_case(self.project_root, task_store, config, task_id)
        if ReviewActionKind.MANUAL_FRONTIER_HANDOFF not in review_case.eligible_actions:
            raise UIActionError(
                f"manual_frontier_handoff is not currently eligible for {task_id} "
                f"(eligible actions: "
                f"{[item.value for item in review_case.eligible_actions]})"
            )
        specification = task_store.get_task(task_id).specification
        package = build_manual_frontier_handoff_package(
            review_case,
            specification,
            config.verification.commands,
            repair_round=review_case.manual_frontier_rounds_used,
        )
        audit = TaskAuditStore(self.project_root, task_id)
        json_artifact, markdown_artifact = write_handoff_artifacts(audit, package)
        return {
            "package": package.model_dump(mode="json"),
            "package_artifact_path": json_artifact.path,
            "package_artifact_absolute_path": str(
                self.project_root / json_artifact.path
            ),
            "markdown_artifact_path": markdown_artifact.path,
            "markdown_artifact_absolute_path": str(
                self.project_root / markdown_artifact.path
            ),
        }

    def manual_frontier_package_detail(
        self, task_id: str, package_id: str
    ) -> dict[str, Any]:
        return load_manual_frontier_package(
            self.project_root, task_id, package_id
        ).model_dump(mode="json")

    def import_manual_frontier_response(
        self,
        task_id: str,
        *,
        package_id: str,
        response_text: str,
        declared_model_name: str,
        preview_id: str,
    ) -> dict[str, Any]:
        """Validates a pasted (or uploaded) response and creates an
        immutable preview -- never applies anything. Reuses exactly the
        same checks the CLI's ``frontier-manual import`` command uses."""

        task_store = self._require_store()
        config = self._config()
        if config is None:
            raise TaskStoreError(
                "Apoapsis is not initialized; run 'apoapsis init' first"
            )
        preview = import_manual_frontier_response_fn(
            self.project_root,
            task_store,
            self._manual_frontier_preview_store(),
            self._review_operation_store(),
            config,
            task_id=task_id,
            package_id=package_id,
            response_bytes=response_text.encode("utf-8"),
            declared_model_name=declared_model_name,
            preview_id=preview_id,
        )
        return preview.model_dump(mode="json")

    def approve_manual_frontier_preview(
        self, task_id: str, preview_id: str, *, expected_task_version: int
    ) -> dict[str, Any]:
        """Step 1 of 2: records explicit intent to apply a previewed
        patch. Never mutates the worktree -- applying is a distinct,
        separate ``submit_review_operation(action="manual_frontier_
        handoff", manual_frontier_preview_id=preview_id)`` call, itself
        gated by the same two-step confirmation pattern every other review
        action already uses in this UI."""

        task_store = self._require_store()
        config = self._config()
        if config is None:
            raise TaskStoreError(
                "Apoapsis is not initialized; run 'apoapsis init' first"
            )
        preview = approve_manual_frontier_preview_fn(
            self.project_root,
            task_store,
            self._manual_frontier_preview_store(),
            config,
            task_id=task_id,
            preview_id=preview_id,
            expected_task_version=expected_task_version,
        )
        return preview.model_dump(mode="json")

    # ---- Discovery and frontier planning handoff (ADR 0032) ----

    def _discovery_store(self) -> SQLiteDiscoveryStore:
        return SQLiteDiscoveryStore(self.metadata_root / "discovery-sessions.db")

    def _discovery_operation_store(self) -> DiscoveryOperationStore:
        return DiscoveryOperationStore(self.metadata_root / "discovery-operations.db")

    def _discovery_worker_instance(self) -> DiscoveryWorker:
        if self._discovery_worker is None:
            self._discovery_worker = DiscoveryWorker(self.project_root)
        return self._discovery_worker

    def _plan_store_for_write(self) -> SQLitePlanStore:
        """Unlike ``_plan_store()``/``_require_plan_store()`` (read-only;
        never creates a database as a side effect of a GET), a discovery
        response import may genuinely be the first thing in this project
        to ever create a plan -- mirrors ``apoapsis plan``'s own CLI
        helper, which always uses ``SQLitePlanStore``'s default
        ``initialize=True``."""

        return SQLitePlanStore(self.metadata_root / "architect-plans.db")

    def discovery_sessions(self) -> dict[str, Any]:
        sessions = self._discovery_store().list_sessions(limit=100)
        return {"sessions": [item.model_dump(mode="json") for item in sessions]}

    def start_discovery_session(self, idea_text: str) -> dict[str, Any]:
        if self._store() is None:
            raise TaskStoreError(
                "Apoapsis is not initialized; run 'apoapsis init' first"
            )
        record = start_discovery_session_fn(self._discovery_store(), idea_text)
        return record.model_dump(mode="json")

    def discovery_session_detail(self, session_id: str) -> dict[str, Any]:
        session = self._discovery_store().get_session(session_id)
        config = self._config()
        active_operation = self._discovery_operation_store().find_active_for_session(
            session_id
        )
        package: dict[str, Any] | None = None
        api_preview: dict[str, Any] | None = None
        if session.frontier_package_id is not None:
            try:
                package_record = load_frontier_planning_package(
                    self.project_root, session.frontier_package_id
                )
                package = package_record.model_dump(mode="json")
                if (
                    config is not None
                    and session.frontier_transport == "api"
                    and session.status.value == "frontier_package_exported"
                ):
                    try:
                        api_preview = preview_frontier_planning_api_call(
                            config, package_record
                        ).model_dump(mode="json")
                    except FrontierPlanningApiNotConfiguredError:
                        api_preview = None
            except Exception:
                package = None
        plan_summary: dict[str, Any] | None = None
        if session.plan_id is not None:
            plan_store = self._plan_store()
            if plan_store is not None:
                try:
                    plan_summary = self._plan_summary(plan_store.get_plan(session.plan_id))
                except Exception:
                    plan_summary = None
        return {
            "session": session.model_dump(mode="json"),
            "active_operation": (
                active_operation.model_dump(mode="json")
                if active_operation is not None
                else None
            ),
            "frontier_package": package,
            "api_preview": api_preview,
            "plan_summary": plan_summary,
            "max_clarification_questions": (
                config.discovery.max_clarification_questions
                if config is not None
                else None
            ),
            "max_frontier_clarification_rounds": (
                config.discovery.max_frontier_clarification_rounds
                if config is not None
                else None
            ),
            "frontier_api_configured": (
                config is not None and config.models.frontier_coder is not None
            ),
        }

    def submit_discovery_operation(
        self,
        session_id: str,
        *,
        action: str,
        operation_id: str,
        expected_version: int,
        authorized_max_spend_usd: float | None = None,
    ) -> dict[str, Any]:
        """Validate and durably record a discovery model-call operation,
        then hand it to the background worker -- this method itself never
        calls a model; only ``DiscoveryWorker`` (on its own thread) does."""

        config = self._config()
        if config is None:
            raise TaskStoreError(
                "Apoapsis is not initialized; run 'apoapsis init' first"
            )
        operation_store = self._discovery_operation_store()
        prepare_discovery_operation(
            self.project_root,
            self._discovery_store(),
            operation_store,
            session_id=session_id,
            action=DiscoveryOperationAction(action),
            operation_id=operation_id,
            expected_version=expected_version,
            authorized_max_spend_usd=authorized_max_spend_usd,
        )
        self._discovery_worker_instance().submit(operation_id)
        return operation_store.get(operation_id).model_dump(mode="json")

    def discovery_operation_status(self, operation_id: str) -> dict[str, Any]:
        return self._discovery_operation_store().get(operation_id).model_dump(
            mode="json"
        )

    def record_discovery_local_answers(
        self, session_id: str, answers: list[dict[str, Any]], *, expected_version: int
    ) -> dict[str, Any]:
        parsed = [ClarificationAnswer.model_validate(item) for item in answers]
        record = record_discovery_local_answers_fn(
            self._discovery_store(), session_id, parsed, expected_version=expected_version
        )
        return record.model_dump(mode="json")

    def approve_discovery_idea_brief(
        self, session_id: str, *, expected_version: int
    ) -> dict[str, Any]:
        record = approve_discovery_idea_brief_fn(
            self._discovery_store(), session_id, expected_version=expected_version
        )
        return record.model_dump(mode="json")

    def export_discovery_frontier_package(
        self, session_id: str, *, transport: str, expected_version: int
    ) -> dict[str, Any]:
        config = self._config()
        if config is None:
            raise TaskStoreError(
                "Apoapsis is not initialized; run 'apoapsis init' first"
            )
        session, package, json_path, markdown_path = export_frontier_planning_package(
            self.project_root,
            self._discovery_store(),
            config,
            session_id,
            transport=transport,
            expected_version=expected_version,
        )
        return {
            "session": session.model_dump(mode="json"),
            "package": package.model_dump(mode="json"),
            "package_artifact_path": json_path,
            "package_artifact_absolute_path": str(self.project_root / json_path),
            "markdown_artifact_path": markdown_path,
            "markdown_artifact_absolute_path": str(self.project_root / markdown_path),
        }

    def import_discovery_manual_response(
        self,
        session_id: str,
        *,
        package_id: str,
        response_text: str,
        declared_model_name: str,
    ) -> dict[str, Any]:
        config = self._config()
        if config is None:
            raise TaskStoreError(
                "Apoapsis is not initialized; run 'apoapsis init' first"
            )
        record = import_discovery_manual_response_fn(
            self.project_root,
            self._discovery_store(),
            self._plan_store_for_write(),
            config,
            session_id=session_id,
            package_id=package_id,
            response_bytes=response_text.encode("utf-8"),
            declared_model_name=declared_model_name,
        )
        return record.model_dump(mode="json")

    def record_discovery_frontier_answers(
        self, session_id: str, answers: list[dict[str, Any]], *, expected_version: int
    ) -> dict[str, Any]:
        parsed = [ClarificationAnswer.model_validate(item) for item in answers]
        record = record_discovery_frontier_answers_fn(
            self._discovery_store(), session_id, parsed, expected_version=expected_version
        )
        return record.model_dump(mode="json")

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
        if record.state == WorkflowState.SPEC_APPROVED:
            return ["start_execution"]
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
