from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Callable

from apoapsis.agent.session import (
    AgentSessionOutcome,
    AgentSessionResult,
    AgentStepPromptBuilder,
    BoundedAgentSession,
)
from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import (
    AgentLoopConfig,
    AgentRoute,
    CompletionPolicy,
    ExecutionMode,
    FrontierProviderConfig,
    ApoapsisConfig,
    effective_config_for_specification,
)
from apoapsis.context.compiler import ContextCompiler, ContextPackage
from apoapsis.context.measurement import (
    ContextMeasurement,
    attribute_context_to_patch,
    measure_context,
)
from apoapsis.execution.worktree import WorktreeManager
from apoapsis.models.base import (
    ConstraintCoverage,
    ConstraintDisposition,
    ModelOperation,
    ModelRequest,
    ModelResponse,
)
from apoapsis.models.prompts import (
    implementation_prompt,
    rejected_patch_repair_prompt,
    repair_prompt,
)
from apoapsis.models.provider import ModelRole, ProviderInvocation
from apoapsis.models.telemetry import (
    InstrumentedModelProvider,
    InstrumentedProviderError,
    ProviderCallTelemetry,
)
from apoapsis.patches.apply import GitPatchApplier, PatchApplicationError
from apoapsis.patches.parser import UnifiedDiffError, UnifiedDiffParser
from apoapsis.patches.validator import PatchPolicyError, PatchPolicyValidator
from apoapsis.reporting.report import (
    FinalTaskReport,
    ModelIdentity,
    TaskOutcome,
    TransmittedExcerpt,
)
from apoapsis.repository.git import GitRepository
from apoapsis.research.engine import ResearchEngine, ResearchExecutionResult
from apoapsis.research.schemas import ResearchMode, ResearchOutcome
from apoapsis.specification.extractor import (
    SpecificationExtractionError,
    SpecificationExtractor,
)
from apoapsis.specification.schema import (
    SourceKind,
    TaskSpecification,
    TraceableStatement,
)
from apoapsis.verification.failures import FailureNormalizer
from apoapsis.verification.results import VerificationResult, VerificationStatus
from apoapsis.verification.runner import VerificationRunner
from apoapsis.workflow.acceptance import (
    AcceptanceCoverage,
    acceptance_coverage_satisfied,
    compute_acceptance_coverage,
)
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.escalation import build_local_to_frontier_escalation
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.routing import RoutingDecision, select_agent_route
from apoapsis.workflow.states import WorkflowState, transition_is_allowed


ApprovalCallback = Callable[[TaskSpecification], bool]


class VerticalSliceRunner:
    """Approved task workflow with one-shot and bounded-agent execution modes."""

    def __init__(
        self,
        project_root: str | Path,
        store: SQLiteTaskStore,
        provider: InstrumentedModelProvider,
        config: ApoapsisConfig,
        *,
        local_coder_provider: InstrumentedModelProvider | None = None,
        frontier_coder_provider: InstrumentedModelProvider | None = None,
        context_compiler: ContextCompiler | None = None,
        research_engine: ResearchEngine | None = None,
        research_mode: ResearchMode = ResearchMode.OFF,
        agent_step_prompt_fn: AgentStepPromptBuilder | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.store = store
        self.provider = provider
        self.local_coder_provider = local_coder_provider or provider
        self.frontier_coder_provider = frontier_coder_provider
        self.config = config
        self.provider_config = config.models.frontier
        self.local_coder_config = (
            config.models.local_coder or config.models.frontier
        )
        self.frontier_coder_config = config.models.frontier_coder
        self._context_compiler_supplied = context_compiler is not None
        self.context_compiler = context_compiler or ContextCompiler(config.context)
        self.research_engine = research_engine
        self.research_mode = research_mode
        self.extractor = SpecificationExtractor()
        self.parser = UnifiedDiffParser()
        self.validator = PatchPolicyValidator(config.patch)
        self.applier = GitPatchApplier()
        self.failure_normalizer = FailureNormalizer()
        self.telemetry: list[ProviderCallTelemetry] = []
        self.contexts: list[tuple[int, ContextPackage]] = []
        self.context_measurements: list[ContextMeasurement] = []
        self.verification_results: list[VerificationResult] = []
        self.files_changed: list[str] = []
        self.worktree_path: str | None = None
        self.audit: TaskAuditStore | None = None
        self.specification: TaskSpecification | None = None
        self.research_execution: ResearchExecutionResult | None = None
        self.research_outcome: ResearchOutcome | None = None
        self.research_calls: list[ProviderCallTelemetry] = []
        self.agent_result: AgentSessionResult | None = None
        self.local_agent_result: AgentSessionResult | None = None
        self.frontier_agent_result: AgentSessionResult | None = None
        self.routing_decision: RoutingDecision | None = None
        self.escalation_package_path: str | None = None
        self.escalation_reason: str | None = None
        self.patch_audit_attempts = 0
        self.acceptance_coverage: list[AcceptanceCoverage] = []
        # `None` by default -- `BoundedAgentSession` then uses its own
        # production `agent_step_prompt` default, byte-for-byte identical to
        # before this parameter existed. Only evaluation-only diagnostic
        # infrastructure (ADR 0029) ever passes an override; no product CLI/
        # UI/service call site does.
        self.agent_step_prompt_fn = agent_step_prompt_fn

    def _execution_base(self, task_id: str, repository_head: str) -> tuple[str, list[str]]:
        """Return the exact human-approved base for a packaged plan slice."""

        for event in reversed(self.store.events(task_id)):
            if event.event_type != "plan_slice_specification_approved":
                continue
            candidate = event.payload.get("execution_base_commit")
            inherited = event.payload.get("inherited_slice_ids", [])
            if not isinstance(candidate, str) or not candidate.strip():
                return repository_head, []
            resolved = GitRepository(self.project_root).run(
                ["rev-parse", "--verify", f"{candidate}^{{commit}}"]
            ).stdout.strip()
            return resolved, [str(item) for item in inherited]
        return repository_head, []

    def _prompt_patch_policy(self) -> dict[str, bool]:
        return {
            "allow_dependency_changes": self.config.patch.allow_dependency_changes,
            "allow_test_changes": self.config.patch.allow_test_changes,
        }

    def run(
        self, request: str, *, approve: ApprovalCallback
    ) -> FinalTaskReport:
        task_id = f"TASK-{uuid.uuid4().hex[:12].upper()}"
        preliminary = TaskSpecification(
            task_id=task_id,
            objective=TraceableStatement(
                text=request,
                source=SourceKind.USER,
                source_reference="cli-request",
            ),
        )
        self.specification = preliminary
        self.store.create_task(preliminary)
        self.audit = TaskAuditStore(self.project_root, task_id)
        try:
            head = GitRepository(self.project_root).run(
                ["rev-parse", "HEAD"]
            ).stdout.strip()
            spec_context = ContextPackage.specification_only(preliminary, head)
            spec_prompt = self.extractor.build_prompt(
                request, task_id, self.config.verification.commands
            )
            spec_response = self._model_call(
                ModelOperation.DRAFT_SPECIFICATION,
                spec_prompt,
                spec_context,
                requested_output="task_specification_json",
            )
            try:
                specification = self.extractor.parse(
                    spec_response.content,
                    request,
                    task_id,
                    self.config.verification.commands,
                )
            except SpecificationExtractionError as exc:
                # Exactly one bounded correction attempt (ADR 0018): the
                # failed response and its telemetry are already persisted
                # by `_model_call` above. A model never gets a second
                # correction -- if this one also fails to parse, the
                # exception propagates uncaught to the outer handler and
                # the task stops deterministically at FAILED.
                self.audit.write_json(
                    "specification-extraction-failure-001.json",
                    {
                        "attempt": 1,
                        "error": str(exc),
                        "raw_response": spec_response.content,
                    },
                    kind="specification_extraction_failure",
                )
                correction_prompt = self.extractor.build_correction_prompt(
                    request,
                    task_id,
                    self.config.verification.commands,
                    spec_response.content,
                    str(exc),
                )
                correction_response = self._model_call(
                    ModelOperation.DRAFT_SPECIFICATION,
                    correction_prompt,
                    spec_context,
                    requested_output="task_specification_json",
                )
                specification = self.extractor.parse(
                    correction_response.content,
                    request,
                    task_id,
                    self.config.verification.commands,
                )
            self.specification = specification
            self.store.update_specification(
                specification,
                actor=WorkflowActor.SYSTEM,
            )
            drafted = self.store.transition(
                task_id,
                WorkflowState.SPEC_DRAFTED,
                actor=WorkflowActor.SYSTEM,
                event_type="frontier_specification_validated",
                payload={
                    "constraints": len(specification.hard_constraints),
                    "verbatim_constraints_validated": True,
                },
            )
            self.audit.write_json(
                "approved-specification-candidate.json",
                specification,
                kind="specification_candidate",
            )
            if not approve(specification):
                self.store.transition(
                    task_id,
                    WorkflowState.HUMAN_REVIEW_REQUIRED,
                    actor=WorkflowActor.USER,
                    event_type="specification_not_approved",
                    expected_version=drafted.version,
                )
                return self._finalize_report(
                    TaskOutcome.HUMAN_REVIEW_REQUIRED,
                    error="user did not approve the extracted specification",
                )
            if not any(
                command.required for command in self.config.verification.commands
            ):
                raise ValueError(
                    "at least one required verification command must be configured"
                )
            approved = self.store.transition(
                task_id,
                WorkflowState.SPEC_APPROVED,
                actor=WorkflowActor.USER,
                event_type="specification_approved",
                expected_version=drafted.version,
            )
        except Exception as exc:
            return self._handle_failure(exc)
        return self._run_from_approved(
            task_id, specification, approved_version=approved.version
        )

    def execute_approved_task(self, task_id: str) -> FinalTaskReport:
        """Resume an already-approved task from persisted state alone (ADR
        0024) -- the entry point the durable execution service uses. Unlike
        ``run()``, this never drafts a specification or asks for approval;
        it requires the task to already be at ``SPEC_APPROVED`` and drives
        exactly the same post-approval spine ``run()`` does, through the
        shared ``_run_from_approved`` continuation -- no routing, context,
        worktree, agent, patch, verification, escalation, or reporting
        logic is duplicated between the two entry points."""

        record = self.store.get_task(task_id)
        if record.state != WorkflowState.SPEC_APPROVED:
            raise ValueError(
                f"task {task_id} is not eligible for execution: expected "
                f"SPEC_APPROVED, found {record.state.value}"
            )
        return self._run_from_approved(
            task_id, record.specification, approved_version=record.version
        )

    def _run_from_approved(
        self,
        task_id: str,
        specification: TaskSpecification,
        *,
        approved_version: int,
    ) -> FinalTaskReport:
        """The shared post-SPEC_APPROVED execution spine: research, context
        compilation, routing, worktree creation, the selected coding stage
        (one-shot or bounded agent, with escalation), verification, and
        final reporting -- unchanged from ``run()``'s original body, now
        reused by both ``run()`` (fresh drafting) and
        ``execute_approved_task()`` (an already-approved task resumed by
        the durable execution service)."""

        self.specification = specification
        self.config = effective_config_for_specification(self.config, specification)
        if not self._context_compiler_supplied:
            self.context_compiler = ContextCompiler(self.config.context)
        if self.audit is None:
            self.audit = TaskAuditStore(self.project_root, task_id)
        try:
            head = GitRepository(self.project_root).run(
                ["rev-parse", "HEAD"]
            ).stdout.strip()
            execution_base, inherited_slice_ids = self._execution_base(task_id, head)
            self.audit.write_json(
                "approved-specification.json",
                specification,
                kind="approved_specification",
            )
            if self.research_engine is not None:
                self.research_execution = asyncio.run(
                    self.research_engine.execute(
                        specification, self.research_mode
                    )
                )
                self.research_outcome = self.research_execution.outcome
                self.research_calls = list(
                    self.research_engine.last_model_calls
                )
            elif self.research_mode != ResearchMode.OFF:
                raise ValueError(
                    "Research Mode requires a configured local research model"
                )
            research_brief = (
                self.research_outcome.brief if self.research_outcome else None
            )
            research_ids = (
                [item.evidence_id for item in self.research_outcome.evidence]
                if self.research_outcome
                else []
            )
            analyzed = self.store.transition(
                task_id,
                WorkflowState.REPOSITORY_ANALYZED,
                actor=WorkflowActor.SYSTEM,
                event_type="repository_analyzed",
                payload={
                    "head_commit": head,
                    "research_triggered": bool(self.research_outcome),
                    "research_evidence": research_ids,
                },
                expected_version=approved_version,
            )
            implementation_context = self.context_compiler.compile(
                specification,
                self.project_root,
                external_research_brief=research_brief,
                research_evidence_ids=research_ids,
            )
            compiled = self.store.transition(
                task_id,
                WorkflowState.CONTEXT_COMPILED,
                actor=WorkflowActor.SYSTEM,
                event_type="context_compiled",
                payload={
                    "context_sha256": implementation_context.context_sha256,
                    "evidence_count": len(implementation_context.evidence),
                },
                expected_version=analyzed.version,
            )
            if self.config.execution.mode == ExecutionMode.AGENT:
                self.routing_decision = select_agent_route(
                    specification,
                    self.config.execution,
                    frontier_available=(
                        self.frontier_coder_provider is not None
                        and self.frontier_coder_config is not None
                    ),
                )
                route_payload = self.routing_decision.model_dump(mode="json")
                self.audit.write_json(
                    "routing-decision.json",
                    self.routing_decision,
                    kind="routing_decision",
                )
            else:
                route_payload = {"route": "FRONTIER_IMPLEMENTATION"}
            routed = self.store.transition(
                task_id,
                WorkflowState.ROUTED,
                actor=WorkflowActor.SYSTEM,
                event_type="rule_based_route_selected",
                payload=route_payload,
                expected_version=compiled.version,
            )
            if (
                self.routing_decision is not None
                and self.routing_decision.route
                == AgentRoute.HUMAN_REVIEW_REQUIRED
            ):
                self.store.transition(
                    task_id,
                    WorkflowState.HUMAN_REVIEW_REQUIRED,
                    actor=WorkflowActor.SYSTEM,
                    event_type="deterministic_route_requires_human",
                    payload={"reason": self.routing_decision.reason},
                    expected_version=routed.version,
                )
                return self._finalize_report(
                    TaskOutcome.HUMAN_REVIEW_REQUIRED,
                    error=self.routing_decision.reason,
                )
            manager = WorktreeManager(self.project_root)
            worktree = manager.create(
                self._task_slug(task_id), base_ref=execution_base
            )
            self.worktree_path = worktree.path
            if execution_base != head:
                implementation_context = self.context_compiler.compile(
                    specification,
                    self.worktree_path,
                    external_research_brief=research_brief,
                    research_evidence_ids=research_ids,
                )
            implementing = self.store.transition(
                task_id,
                WorkflowState.IMPLEMENTING,
                actor=WorkflowActor.SYSTEM,
                event_type="isolated_worktree_created",
                payload={
                    "branch": worktree.branch,
                    "path": worktree.path,
                    "base_commit": worktree.base_commit,
                    "repository_head": head,
                    "inherited_slice_ids": inherited_slice_ids,
                    "execution_context_sha256": (
                        implementation_context.context_sha256
                    ),
                },
                expected_version=routed.version,
            )
            if self.config.execution.mode == ExecutionMode.AGENT:
                return self._run_bounded_agent(
                    implementation_context,
                    implementing_version=implementing.version,
                )
            patch_response = self._model_call(
                ModelOperation.IMPLEMENT_PATCH,
                implementation_prompt(
                    implementation_context,
                    patch_policy=self._prompt_patch_policy(),
                ),
                implementation_context,
                requested_output="unified_diff",
            )
            repair_budget_remaining = 1
            patch_attempt = 1
            try:
                self._validate_apply_and_audit(patch_response.content, attempt=1)
            except (UnifiedDiffError, PatchPolicyError, PatchApplicationError) as exc:
                repair_budget_remaining = 0
                patch_attempt = 2
                rejection = f"{type(exc).__name__}: {exc}"
                rejected_worktree_diff = GitRepository(self.worktree_path).run(
                    ["diff", "--no-ext-diff", "HEAD"]
                ).stdout
                if rejected_worktree_diff.strip():
                    raise PatchApplicationError(
                        "rejected patch changed the worktree; refusing repair"
                    ) from exc
                self.audit.write_json(
                    "patch-failure-001.json",
                    {
                        "stage": "patch_validation_or_application",
                        "root_error": rejection,
                        "worktree_unchanged": True,
                    },
                    kind="normalized_patch_failure",
                )
                escalation = self.store.transition(
                    task_id,
                    WorkflowState.ESCALATION_REQUIRED,
                    actor=WorkflowActor.SYSTEM,
                    event_type="targeted_patch_repair_required",
                    payload={
                        "attempt": 1,
                        "retry_budget_remaining": 1,
                        "error": rejection,
                    },
                    expected_version=implementing.version,
                )
                repair_context = self.context_compiler.compile(
                    specification,
                    self.worktree_path,
                    extra_queries=[rejection],
                    external_research_brief=research_brief,
                    research_evidence_ids=research_ids,
                )
                implementing_repair = self.store.transition(
                    task_id,
                    WorkflowState.IMPLEMENTING,
                    actor=WorkflowActor.SYSTEM,
                    event_type="targeted_patch_repair_started",
                    payload={"retry_budget_remaining": 0},
                    expected_version=escalation.version,
                )
                replacement_response = self._model_call(
                    ModelOperation.PROPOSE_REPAIR,
                    rejected_patch_repair_prompt(
                        repair_context,
                        patch_response.content[:20_000],
                        rejection[:8_000],
                        patch_policy=self._prompt_patch_policy(),
                    ),
                    repair_context,
                    requested_output="unified_diff",
                )
                self._validate_apply_and_audit(
                    replacement_response.content, attempt=2
                )
            patch_ready = self.store.transition(
                task_id,
                WorkflowState.PATCH_READY,
                actor=WorkflowActor.SYSTEM,
                event_type=(
                    "frontier_patch_applied"
                    if patch_attempt == 1
                    else "frontier_replacement_patch_applied"
                ),
                payload={
                    "attempt": patch_attempt,
                    "files_changed": self.files_changed,
                    "retry_budget_remaining": repair_budget_remaining,
                },
                expected_version=(
                    implementing.version
                    if patch_attempt == 1
                    else implementing_repair.version
                ),
            )
            verifying = self.store.transition(
                task_id,
                WorkflowState.VERIFYING,
                actor=WorkflowActor.VERIFICATION_ENGINE,
                event_type="verification_started",
                payload={"attempt": 1},
                expected_version=patch_ready.version,
            )
            first_result = self._run_verification(attempt=1)
            if first_result.status == VerificationStatus.PASSED:
                return self._one_shot_complete_or_gap(
                    task_id,
                    first_result,
                    verifying_version=verifying.version,
                    attempt=1,
                    complete_event_type="verification_passed",
                )

            failing_command, failure = self.failure_normalizer.extract(
                first_result, self.worktree_path
            )
            self.audit.write_json(
                "verification-failure-001.json",
                failure,
                kind="normalized_failure",
            )
            if repair_budget_remaining == 0:
                self.store.transition(
                    task_id,
                    WorkflowState.FAILED,
                    actor=WorkflowActor.VERIFICATION_ENGINE,
                    event_type="repair_budget_exhausted",
                    payload={
                        "attempt": 2,
                        "retry_budget_remaining": 0,
                        "stage": "verification",
                    },
                    expected_version=verifying.version,
                )
                return self._finalize_report(
                    TaskOutcome.FAILED,
                    error=(
                        "verification failed after the single repair budget was "
                        "used to replace a rejected patch"
                    ),
                )
            escalation = self.store.transition(
                task_id,
                WorkflowState.ESCALATION_REQUIRED,
                actor=WorkflowActor.VERIFICATION_ENGINE,
                event_type="targeted_repair_required",
                payload={"attempt": 1, "retry_budget_remaining": 1},
                expected_version=verifying.version,
            )
            current_repository = GitRepository(self.worktree_path)
            current_diff = current_repository.run(
                ["diff", "--no-ext-diff", "--unified=3", "HEAD"]
            ).stdout
            repair_context = self.context_compiler.compile(
                specification,
                self.worktree_path,
                extra_queries=[failure.root_error, failure.relevant_error],
                preferred_paths=self.files_changed,
                preferred_line_anchors={
                    location.path: location.line for location in failure.locations
                },
                external_research_brief=research_brief,
                research_evidence_ids=research_ids,
            )
            implementing_repair = self.store.transition(
                task_id,
                WorkflowState.IMPLEMENTING,
                actor=WorkflowActor.SYSTEM,
                event_type="targeted_frontier_repair_started",
                payload={"retry_budget_remaining": 0},
                expected_version=escalation.version,
            )
            repair_response = self._model_call(
                ModelOperation.PROPOSE_REPAIR,
                repair_prompt(
                    repair_context,
                    failing_command,
                    failure.relevant_error,
                    current_diff,
                    patch_policy=self._prompt_patch_policy(),
                ),
                repair_context,
                requested_output="unified_diff",
            )
            self._validate_apply_and_audit(repair_response.content, attempt=2)
            repair_ready = self.store.transition(
                task_id,
                WorkflowState.PATCH_READY,
                actor=WorkflowActor.SYSTEM,
                event_type="frontier_repair_patch_applied",
                payload={"attempt": 2, "files_changed": self.files_changed},
                expected_version=implementing_repair.version,
            )
            verifying_repair = self.store.transition(
                task_id,
                WorkflowState.VERIFYING,
                actor=WorkflowActor.VERIFICATION_ENGINE,
                event_type="verification_started",
                payload={"attempt": 2, "retry_budget_remaining": 0},
                expected_version=repair_ready.version,
            )
            repair_result = self._run_verification(attempt=2)
            if repair_result.status == VerificationStatus.PASSED:
                return self._one_shot_complete_or_gap(
                    task_id,
                    repair_result,
                    verifying_version=verifying_repair.version,
                    attempt=2,
                    complete_event_type="repair_verification_passed",
                )
            self.store.transition(
                task_id,
                WorkflowState.FAILED,
                actor=WorkflowActor.VERIFICATION_ENGINE,
                event_type="repair_budget_exhausted",
                payload={"attempt": 2, "retry_budget_remaining": 0},
                expected_version=verifying_repair.version,
            )
            return self._finalize_report(
                TaskOutcome.FAILED,
                error="verification still failed after one targeted repair",
            )
        except Exception as exc:
            return self._handle_failure(exc)

    def _one_shot_complete_or_gap(
        self,
        task_id: str,
        verification_result: VerificationResult,
        *,
        verifying_version: int,
        attempt: int,
        complete_event_type: str,
    ) -> FinalTaskReport:
        """Verification already passed at this call site. Under the
        strict completion policy, additionally require every active
        acceptance criterion to be proven before reaching COMPLETE -- one
        shot's single repair budget is not spent chasing coverage, so an
        unproven gap here goes straight to human review rather than back
        through the existing patch-repair path."""

        assert self.specification is not None
        if self.config.execution.completion_policy == CompletionPolicy.STRICT:
            command_results = {
                command.name: command.status
                for command in verification_result.commands
                if command.status != VerificationStatus.SKIPPED
            }
            coverage = compute_acceptance_coverage(
                self.specification,
                self.config.verification.commands,
                command_results,
            )
            self.acceptance_coverage = coverage
            if not acceptance_coverage_satisfied(coverage):
                self.store.transition(
                    task_id,
                    WorkflowState.HUMAN_REVIEW_REQUIRED,
                    actor=WorkflowActor.VERIFICATION_ENGINE,
                    event_type="acceptance_coverage_incomplete",
                    payload={
                        "attempt": attempt,
                        "coverage": [
                            item.model_dump(mode="json") for item in coverage
                        ],
                    },
                    expected_version=verifying_version,
                )
                return self._finalize_report(
                    TaskOutcome.HUMAN_REVIEW_REQUIRED,
                    error=(
                        "configured verification passed but not every "
                        "active acceptance criterion is proven under the "
                        "strict completion policy"
                    ),
                )
        self.store.transition(
            task_id,
            WorkflowState.COMPLETE,
            actor=WorkflowActor.VERIFICATION_ENGINE,
            event_type=complete_event_type,
            payload={"attempt": attempt},
            expected_version=verifying_version,
        )
        return self._finalize_report(TaskOutcome.COMPLETE)

    def _run_bounded_agent(
        self,
        implementation_context: ContextPackage,
        *,
        implementing_version: int,
    ) -> FinalTaskReport:
        assert self.audit is not None
        assert self.specification is not None
        assert self.worktree_path is not None
        assert self.routing_decision is not None

        if self.routing_decision.route == AgentRoute.FRONTIER_ONLY:
            assert self.frontier_coder_provider is not None
            assert self.frontier_coder_config is not None
            result = self._run_agent_session(
                provider=self.frontier_coder_provider,
                provider_config=self.frontier_coder_config,
                context=implementation_context,
                agent_config=self.config.execution.frontier_agent,
                role=ModelRole.FRONTIER_CODING_AGENT,
                audit_prefix="frontier-",
            )
            self.frontier_agent_result = result
            self.agent_result = result
            self._record_agent_result(result)
            if result.outcome == AgentSessionOutcome.COMPLETE:
                return self._complete_agent_workflow(
                    result,
                    implementing_version=implementing_version,
                    event_prefix="frontier_agent",
                )
            return self._require_human_after_agent(
                result,
                implementing_version=implementing_version,
                event_type="frontier_agent_budget_exhausted",
            )

        local_result = self._run_agent_session(
            provider=self.local_coder_provider,
            provider_config=self.local_coder_config,
            context=implementation_context,
            agent_config=self.config.execution.agent,
            role=ModelRole.LOCAL_CODING_AGENT,
            audit_prefix="",
        )
        self.local_agent_result = local_result
        self.agent_result = local_result
        self._record_agent_result(local_result)
        if local_result.outcome == AgentSessionOutcome.COMPLETE:
            return self._complete_agent_workflow(
                local_result,
                implementing_version=implementing_version,
                event_prefix="local_agent",
            )

        escalation = self.store.transition(
            self.specification.task_id,
            WorkflowState.ESCALATION_REQUIRED,
            actor=WorkflowActor.SYSTEM,
            event_type="bounded_local_agent_escalation_required",
            payload={
                "turns": local_result.turns,
                "patch_attempts": local_result.patch_attempts,
                "verification_runs": local_result.verification_runs,
                "reason": local_result.stop_reason,
            },
            expected_version=implementing_version,
        )
        if (
            self.routing_decision.route == AgentRoute.LOCAL_THEN_FRONTIER
            and self.frontier_coder_provider is not None
            and self.frontier_coder_config is not None
        ):
            return self._run_frontier_escalation(
                local_result,
                escalation_version=escalation.version,
            )

        self.escalation_reason = local_result.stop_reason
        self.store.transition(
            self.specification.task_id,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            actor=WorkflowActor.SYSTEM,
            event_type="frontier_escalation_not_configured",
            payload={"reason": local_result.stop_reason},
            expected_version=escalation.version,
        )
        return self._finalize_report(
            TaskOutcome.HUMAN_REVIEW_REQUIRED,
            error=(
                f"bounded local coding agent requires escalation: "
                f"{local_result.stop_reason}"
            ),
        )

    def _run_agent_session(
        self,
        *,
        provider: InstrumentedModelProvider,
        provider_config: FrontierProviderConfig,
        context: ContextPackage,
        agent_config: AgentLoopConfig,
        role: ModelRole,
        audit_prefix: str,
    ) -> AgentSessionResult:
        assert self.audit is not None
        assert self.specification is not None
        assert self.worktree_path is not None

        def model_call(*args, **kwargs):
            return self._model_call(
                *args,
                **kwargs,
                provider=provider,
                provider_config=provider_config,
            )

        def apply_patch(patch: str, session_attempt: int) -> None:
            del session_attempt
            self.patch_audit_attempts += 1
            self._validate_apply_and_audit(
                patch, attempt=self.patch_audit_attempts
            )

        session_kwargs: dict[str, object] = dict(
            specification=self.specification,
            worktree=self.worktree_path,
            initial_context=context,
            context_compiler=self.context_compiler,
            config=agent_config,
            verification_config=self.config.verification,
            audit=self.audit,
            model_call=model_call,
            apply_patch=apply_patch,
            model_role=role,
            audit_prefix=audit_prefix,
            completion_policy=self.config.execution.completion_policy,
            patch_policy=self.config.patch,
        )
        if self.agent_step_prompt_fn is not None:
            session_kwargs["agent_step_prompt_fn"] = self.agent_step_prompt_fn
        session = BoundedAgentSession(**session_kwargs)
        try:
            return session.run()
        except InstrumentedProviderError as exc:
            return session.interrupted(
                f"{role.value} provider call failed: {exc}"
            )

    def _record_agent_result(self, result: AgentSessionResult) -> None:
        self.files_changed = list(result.changed_files)
        self.verification_results.extend(result.verification_results)
        if result.acceptance_coverage:
            self.acceptance_coverage = result.acceptance_coverage

    def _run_frontier_escalation(
        self,
        local_result: AgentSessionResult,
        *,
        escalation_version: int,
    ) -> FinalTaskReport:
        assert self.audit is not None
        assert self.specification is not None
        assert self.worktree_path is not None
        assert self.frontier_coder_provider is not None
        assert self.frontier_coder_config is not None

        frontier_context, package = build_local_to_frontier_escalation(
            task_id=self.specification.task_id,
            specification=self.specification,
            worktree_path=self.worktree_path,
            local_result=local_result,
            context_compiler=self.context_compiler,
            files_changed=self.files_changed,
            local_provider_name=self.local_coder_provider.provider_name,
            local_model_name=self.local_coder_provider.model_name,
            frontier_provider_name=self.frontier_coder_provider.provider_name,
            frontier_model_name=self.frontier_coder_provider.model_name,
            frontier_budget=self.config.execution.frontier_agent,
            external_research_brief=(
                self.research_outcome.brief if self.research_outcome else None
            ),
            research_evidence_ids=(
                [item.evidence_id for item in self.research_outcome.evidence]
                if self.research_outcome
                else []
            ),
        )
        artifact = self.audit.write_json(
            "frontier-escalation-package.json",
            package,
            kind="frontier_escalation_package",
        )
        self.escalation_package_path = artifact.path
        self.escalation_reason = local_result.stop_reason
        implementing = self.store.transition(
            self.specification.task_id,
            WorkflowState.IMPLEMENTING,
            actor=WorkflowActor.SYSTEM,
            event_type="bounded_frontier_escalation_started",
            payload={
                "package": artifact.path,
                "context_sha256": frontier_context.context_sha256,
                "frontier_provider": self.frontier_coder_provider.provider_name,
                "frontier_model": self.frontier_coder_provider.model_name,
            },
            expected_version=escalation_version,
        )
        frontier_result = self._run_agent_session(
            provider=self.frontier_coder_provider,
            provider_config=self.frontier_coder_config,
            context=frontier_context,
            agent_config=self.config.execution.frontier_agent,
            role=ModelRole.FRONTIER_CODING_AGENT,
            audit_prefix="frontier-",
        )
        self.frontier_agent_result = frontier_result
        self.agent_result = frontier_result
        self._record_agent_result(frontier_result)
        if frontier_result.outcome == AgentSessionOutcome.COMPLETE:
            return self._complete_agent_workflow(
                frontier_result,
                implementing_version=implementing.version,
                event_prefix="frontier_agent",
            )
        return self._require_human_after_agent(
            frontier_result,
            implementing_version=implementing.version,
            event_type="frontier_agent_budget_exhausted",
        )

    def _complete_agent_workflow(
        self,
        result: AgentSessionResult,
        *,
        implementing_version: int,
        event_prefix: str,
    ) -> FinalTaskReport:
        assert self.specification is not None
        patch_ready = self.store.transition(
            self.specification.task_id,
            WorkflowState.PATCH_READY,
            actor=WorkflowActor.SYSTEM,
            event_type=f"{event_prefix}_patch_ready",
            payload={
                "turns": result.turns,
                "patch_attempts": result.patch_attempts,
                "files_changed": self.files_changed,
            },
            expected_version=implementing_version,
        )
        verifying = self.store.transition(
            self.specification.task_id,
            WorkflowState.VERIFYING,
            actor=WorkflowActor.VERIFICATION_ENGINE,
            event_type=f"{event_prefix}_verification_recorded",
            payload={
                "verification_runs": result.verification_runs,
                "final_status": "passed",
            },
            expected_version=patch_ready.version,
        )
        self.store.transition(
            self.specification.task_id,
            WorkflowState.COMPLETE,
            actor=WorkflowActor.VERIFICATION_ENGINE,
            event_type=f"{event_prefix}_verification_passed",
            payload={"stop_reason": result.stop_reason},
            expected_version=verifying.version,
        )
        return self._finalize_report(TaskOutcome.COMPLETE)

    def _require_human_after_agent(
        self,
        result: AgentSessionResult,
        *,
        implementing_version: int,
        event_type: str,
    ) -> FinalTaskReport:
        assert self.specification is not None
        self.escalation_reason = result.stop_reason
        escalation = self.store.transition(
            self.specification.task_id,
            WorkflowState.ESCALATION_REQUIRED,
            actor=WorkflowActor.SYSTEM,
            event_type=event_type,
            payload={"reason": result.stop_reason},
            expected_version=implementing_version,
        )
        self.store.transition(
            self.specification.task_id,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            actor=WorkflowActor.SYSTEM,
            event_type="bounded_frontier_requires_human",
            payload={"reason": result.stop_reason},
            expected_version=escalation.version,
        )
        return self._finalize_report(
            TaskOutcome.HUMAN_REVIEW_REQUIRED,
            error=f"bounded frontier coding agent stopped: {result.stop_reason}",
        )

    def _model_call(
        self,
        operation: ModelOperation,
        prompt: str,
        context: ContextPackage,
        *,
        requested_output: str,
        response_schema: dict[str, object] | None = None,
        role: ModelRole = ModelRole.FRONTIER_IMPLEMENTATION,
        provider: InstrumentedModelProvider | None = None,
        provider_config: FrontierProviderConfig | None = None,
    ) -> ModelResponse:
        assert self.audit is not None
        selected_provider = provider or self.provider
        selected_config = provider_config or self.provider_config
        call_number = len(self.telemetry) + 1
        request_id = f"MRQ-{uuid.uuid4().hex}"
        constraints = list(context.specification.active_hard_constraints)
        coverage = [
            ConstraintCoverage(
                constraint_id=item.id,
                disposition=ConstraintDisposition.INCLUDED,
                reason="included verbatim in the model request package",
            )
            for item in constraints
        ]
        request = ModelRequest(
            request_id=request_id,
            task_id=context.task_id,
            operation=operation,
            provider=selected_provider.provider_name,
            model=selected_provider.model_name,
            specification=context.specification,
            evidence=context.evidence,
            active_constraints=constraints,
            constraint_coverage=coverage,
            inference_parameters=self._inference_parameters(
                operation, selected_config
            ),
            requested_output=requested_output,
        )
        self.audit.write_call_package(
            call_number,
            request,
            prompt,
            context,
            provider_role=role.value,
            response_schema=response_schema,
        )
        self.contexts.append((call_number, context))
        previous_context = self.contexts[-2][1] if len(self.contexts) >= 2 else None
        measurement = measure_context(
            context,
            call_number=call_number,
            model_context_window_tokens=selected_config.context_window_tokens,
            previous_package=previous_context,
        )
        self.context_measurements.append(measurement)
        self.audit.write_json(
            f"call-{call_number:03d}-context-measurement.json",
            measurement,
            kind="context_measurement",
        )
        invocation = ProviderInvocation(
            request_id=request_id,
            operation=operation,
            prompt=prompt,
            role=role,
            response_schema=response_schema,
        )
        try:
            call = selected_provider.complete(invocation)
        except InstrumentedProviderError as exc:
            self.telemetry.append(exc.telemetry)
            self.audit.write_json(
                f"call-{call_number:03d}-telemetry.json",
                exc.telemetry,
                kind="provider_telemetry",
            )
            raise
        self.telemetry.append(call.telemetry)
        response = ModelResponse(
            response_id=f"MRS-{uuid.uuid4().hex}",
            request_id=request_id,
            provider=selected_provider.provider_name,
            model=call.output.model,
            operation=operation,
            content=call.output.content,
            unified_diff=(
                call.output.content
                if operation
                in {ModelOperation.IMPLEMENT_PATCH, ModelOperation.PROPOSE_REPAIR}
                else None
            ),
            usage=call.output.usage,
            finish_reason=call.output.finish_reason,
        )
        self.audit.write_call_result(call_number, response, call.telemetry)
        return response

    def _inference_parameters(
        self,
        operation: ModelOperation,
        provider_config: FrontierProviderConfig,
    ) -> dict[str, int | float | bool | None]:
        frontier = provider_config
        think = frontier.think
        if (
            operation == ModelOperation.DRAFT_SPECIFICATION
            and frontier.specification_think is not None
        ):
            think = frontier.specification_think
        return {
            "context_window_tokens": frontier.context_window_tokens,
            "max_output_tokens": frontier.max_output_tokens,
            "temperature": frontier.temperature,
            "think": think,
            "timeout_seconds": frontier.timeout_seconds,
        }

    def _validate_apply_and_audit(self, patch: str, *, attempt: int) -> None:
        assert self.audit is not None
        assert self.worktree_path is not None
        self.audit.write_text(
            f"patch-{attempt:03d}.diff", patch, kind="model_patch"
        )
        parsed = self.parser.parse(patch)
        proposal = patch.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
        if parsed.raw != proposal:
            self.audit.write_text(
                f"patch-{attempt:03d}-normalized.diff",
                parsed.raw,
                kind="normalized_model_patch",
            )
        validation = self.validator.validate(parsed, self.worktree_path)
        self.audit.write_json(
            f"patch-{attempt:03d}-policy.json",
            validation,
            kind="patch_policy",
        )
        validation.require_accepted()
        self.files_changed = self.applier.apply(parsed, self.worktree_path)
        if (
            self.applier.last_applied_patch is not None
            and self.applier.last_applied_patch != parsed.raw
        ):
            self.audit.write_text(
                f"patch-{attempt:03d}-rebased.diff",
                self.applier.last_applied_patch,
                kind="rebased_model_patch",
            )

    def _run_verification(self, *, attempt: int) -> VerificationResult:
        assert self.audit is not None
        assert self.specification is not None
        assert self.worktree_path is not None
        result = VerificationRunner(self.config.verification).run(
            self.specification.task_id, self.worktree_path, attempt=attempt
        )
        self.verification_results.append(result)
        self.audit.write_json(
            f"verification-{attempt:03d}.json",
            result,
            kind="verification_result",
        )
        return result

    def _handle_failure(self, exc: Exception) -> FinalTaskReport:
        assert self.specification is not None
        if self.audit is not None:
            self.audit.write_json(
                "fatal-error.json",
                {"type": type(exc).__name__, "message": str(exc)},
                kind="fatal_error",
            )
        try:
            record = self.store.get_task(self.specification.task_id)
            if transition_is_allowed(record.state, WorkflowState.FAILED):
                self.store.transition(
                    record.task_id,
                    WorkflowState.FAILED,
                    actor=WorkflowActor.SYSTEM,
                    event_type="vertical_slice_failed",
                    payload={"type": type(exc).__name__, "message": str(exc)},
                    expected_version=record.version,
                )
        except Exception:
            pass
        return self._finalize_report(
            TaskOutcome.FAILED,
            error=f"{type(exc).__name__}: {exc}",
        )

    def _finalize_report(
        self, outcome: TaskOutcome, *, error: str | None = None
    ) -> FinalTaskReport:
        assert self.specification is not None
        assert self.audit is not None
        constraint_coverage = [
            ConstraintCoverage(
                constraint_id=item.id,
                disposition=ConstraintDisposition.INCLUDED,
                reason="included verbatim in each post-approval model package",
            )
            for item in self.specification.active_hard_constraints
        ]
        all_calls = self._all_provider_calls()
        identities = sorted({(item.provider, item.model) for item in all_calls})
        excerpts: list[TransmittedExcerpt] = []
        for call_number, context in self.contexts:
            for evidence in context.evidence:
                lines = (
                    evidence.end_line - evidence.start_line + 1
                    if evidence.start_line is not None and evidence.end_line is not None
                    else len(evidence.content.splitlines())
                )
                excerpts.append(
                    TransmittedExcerpt(
                        call_number=call_number,
                        path=evidence.path,
                        start_line=evidence.start_line,
                        end_line=evidence.end_line,
                        lines=lines,
                        content_sha256=evidence.content_sha256 or "0" * 64,
                    )
                )
        context_attribution = attribute_context_to_patch(
            [context for _call_number, context in self.contexts],
            changed_files=self.files_changed,
            accepted_patch=outcome == TaskOutcome.COMPLETE,
        )
        self.audit.write_json(
            "context-attribution.json",
            context_attribution,
            kind="context_attribution",
        )
        predicted_report = (
            self.audit.root / "report.json"
        ).relative_to(self.project_root).as_posix()
        locations = [item.path for item in self.audit.artifacts()]
        if self.research_execution and self.research_execution.audit_directory:
            research_root = (
                self.project_root / self.research_execution.audit_directory
            )
            if research_root.is_dir():
                locations.extend(
                    path.relative_to(self.project_root).as_posix()
                    for path in research_root.iterdir()
                    if path.is_file()
                )
        locations.extend([".apoapsis/apoapsis.db", predicted_report])
        staged_results = [
            item
            for item in (
                self.local_agent_result,
                self.frontier_agent_result,
            )
            if item is not None
        ]
        report = FinalTaskReport(
            task_id=self.specification.task_id,
            outcome=outcome,
            error=error,
            worktree_path=self.worktree_path,
            execution_mode=self.config.execution.mode,
            agent_route=(
                self.routing_decision.route if self.routing_decision else None
            ),
            agent_turns=sum(item.turns for item in staged_results),
            agent_patch_attempts=sum(
                item.patch_attempts for item in staged_results
            ),
            agent_verification_runs=sum(
                item.verification_runs for item in staged_results
            ),
            agent_stop_reason=(
                self.agent_result.stop_reason if self.agent_result else None
            ),
            local_agent_turns=(
                self.local_agent_result.turns
                if self.local_agent_result
                else 0
            ),
            frontier_agent_turns=(
                self.frontier_agent_result.turns
                if self.frontier_agent_result
                else 0
            ),
            frontier_agent_patch_attempts=(
                self.frontier_agent_result.patch_attempts
                if self.frontier_agent_result
                else 0
            ),
            frontier_agent_verification_runs=(
                self.frontier_agent_result.verification_runs
                if self.frontier_agent_result
                else 0
            ),
            escalation_triggered=self.escalation_reason is not None,
            escalation_reason=self.escalation_reason,
            escalation_package_path=self.escalation_package_path,
            constraint_coverage=constraint_coverage,
            models_used=[
                ModelIdentity(provider=provider, model=model)
                for provider, model in identities
            ],
            provider_calls=all_calls,
            number_of_calls=len(all_calls),
            input_tokens=sum(item.input_tokens for item in all_calls),
            output_tokens=sum(item.output_tokens for item in all_calls),
            cached_input_tokens=sum(
                item.cached_input_tokens for item in all_calls
            ),
            estimated_cost_usd=sum(
                item.estimated_cost_usd for item in all_calls
            ),
            latency_seconds=sum(item.latency_seconds for item in all_calls),
            transmitted_excerpts=excerpts,
            transmitted_files=len(
                {item.path for item in excerpts if not item.path.startswith("<")}
            ),
            transmitted_lines=sum(item.lines for item in excerpts),
            files_changed=self.files_changed,
            verification_results=self.verification_results,
            audit_artifact_locations=sorted(set(locations)),
            research_triggered=bool(self.research_outcome),
            research_mode=(
                self.research_execution.decision.effective_mode
                if self.research_execution
                else ResearchMode.OFF
            ),
            research_patterns=(
                [item.name for item in self.research_outcome.synthesis.patterns]
                if self.research_outcome
                else []
            ),
            research_evidence_in_frontier_request=(
                [item.evidence_id for item in self.research_outcome.evidence]
                if self.research_outcome
                else []
            ),
            research_influenced_plan=(
                self.research_outcome.telemetry.changed_proposed_plan
                if self.research_outcome
                else False
            ),
            research_audit_directory=(
                self.research_execution.audit_directory
                if self.research_execution
                else None
            ),
            research_telemetry=(
                self.research_outcome.telemetry
                if self.research_outcome
                else None
            ),
            context_measurements=self.context_measurements,
            context_attribution=context_attribution,
            completion_policy=self.config.execution.completion_policy,
            acceptance_coverage=self.acceptance_coverage,
            local_agent_budget=(
                self.config.execution.agent
                if self.config.execution.mode == ExecutionMode.AGENT
                else None
            ),
            frontier_agent_budget=(
                self.config.execution.frontier_agent
                if self.frontier_coder_provider is not None
                and self.frontier_coder_config is not None
                else None
            ),
            frontier_available=(
                self.frontier_coder_provider is not None
                and self.frontier_coder_config is not None
            ),
            rejected_tool_requests=sum(
                1
                for item in staged_results
                for record in item.turn_records
                if not record.accepted
            ),
        )
        self.audit.write_json("report.json", report, kind="final_report")
        return report

    def _all_provider_calls(self) -> list[ProviderCallTelemetry]:
        if not self.research_calls:
            return list(self.telemetry)
        if not self.telemetry:
            return list(self.research_calls)
        return [self.telemetry[0], *self.research_calls, *self.telemetry[1:]]

    @staticmethod
    def _task_slug(task_id: str) -> str:
        return task_id.removeprefix("TASK-").lower()
