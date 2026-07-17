from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Callable

from sol.audit.store import TaskAuditStore
from sol.config import SolConfig
from sol.context.compiler import ContextCompiler, ContextPackage
from sol.execution.worktree import WorktreeManager
from sol.models.base import (
    ConstraintCoverage,
    ConstraintDisposition,
    ModelOperation,
    ModelRequest,
    ModelResponse,
)
from sol.models.prompts import (
    implementation_prompt,
    rejected_patch_repair_prompt,
    repair_prompt,
)
from sol.models.provider import ProviderInvocation
from sol.models.telemetry import (
    InstrumentedModelProvider,
    InstrumentedProviderError,
    ProviderCallTelemetry,
)
from sol.patches.apply import GitPatchApplier, PatchApplicationError
from sol.patches.parser import UnifiedDiffError, UnifiedDiffParser
from sol.patches.validator import PatchPolicyError, PatchPolicyValidator
from sol.reporting.report import (
    FinalTaskReport,
    ModelIdentity,
    TaskOutcome,
    TransmittedExcerpt,
)
from sol.repository.git import GitRepository
from sol.research.engine import ResearchEngine, ResearchExecutionResult
from sol.research.schemas import ResearchMode, ResearchOutcome
from sol.specification.extractor import SpecificationExtractor
from sol.specification.schema import (
    SourceKind,
    TaskSpecification,
    TraceableStatement,
)
from sol.verification.failures import FailureNormalizer
from sol.verification.results import VerificationResult, VerificationStatus
from sol.verification.runner import VerificationRunner
from sol.workflow.engine import SQLiteTaskStore
from sol.workflow.events import WorkflowActor
from sol.workflow.states import WorkflowState, transition_is_allowed


ApprovalCallback = Callable[[TaskSpecification], bool]


class VerticalSliceRunner:
    """One frontier implementation call plus at most one frontier repair call."""

    def __init__(
        self,
        project_root: str | Path,
        store: SQLiteTaskStore,
        provider: InstrumentedModelProvider,
        config: SolConfig,
        *,
        context_compiler: ContextCompiler | None = None,
        research_engine: ResearchEngine | None = None,
        research_mode: ResearchMode = ResearchMode.OFF,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.store = store
        self.provider = provider
        self.config = config
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
        self.verification_results: list[VerificationResult] = []
        self.files_changed: list[str] = []
        self.worktree_path: str | None = None
        self.audit: TaskAuditStore | None = None
        self.specification: TaskSpecification | None = None
        self.research_execution: ResearchExecutionResult | None = None
        self.research_outcome: ResearchOutcome | None = None
        self.research_calls: list[ProviderCallTelemetry] = []

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
            spec_prompt = self.extractor.build_prompt(request, task_id)
            spec_response = self._model_call(
                ModelOperation.DRAFT_SPECIFICATION,
                spec_prompt,
                spec_context,
                requested_output="task_specification_json",
            )
            specification = self.extractor.parse(
                spec_response.content, request, task_id
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
                expected_version=approved.version,
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
            routed = self.store.transition(
                task_id,
                WorkflowState.ROUTED,
                actor=WorkflowActor.SYSTEM,
                event_type="rule_based_route_selected",
                payload={"route": "FRONTIER_IMPLEMENTATION"},
                expected_version=compiled.version,
            )
            manager = WorktreeManager(self.project_root)
            worktree = manager.create(self._task_slug(task_id), base_ref=head)
            self.worktree_path = worktree.path
            implementing = self.store.transition(
                task_id,
                WorkflowState.IMPLEMENTING,
                actor=WorkflowActor.SYSTEM,
                event_type="isolated_worktree_created",
                payload={
                    "branch": worktree.branch,
                    "path": worktree.path,
                    "base_commit": worktree.base_commit,
                },
                expected_version=routed.version,
            )
            patch_response = self._model_call(
                ModelOperation.IMPLEMENT_PATCH,
                implementation_prompt(implementation_context),
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
                self.store.transition(
                    task_id,
                    WorkflowState.COMPLETE,
                    actor=WorkflowActor.VERIFICATION_ENGINE,
                    event_type="verification_passed",
                    payload={"attempt": 1},
                    expected_version=verifying.version,
                )
                return self._finalize_report(TaskOutcome.COMPLETE)

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
                self.store.transition(
                    task_id,
                    WorkflowState.COMPLETE,
                    actor=WorkflowActor.VERIFICATION_ENGINE,
                    event_type="repair_verification_passed",
                    payload={"attempt": 2},
                    expected_version=verifying_repair.version,
                )
                return self._finalize_report(TaskOutcome.COMPLETE)
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

    def _model_call(
        self,
        operation: ModelOperation,
        prompt: str,
        context: ContextPackage,
        *,
        requested_output: str,
    ) -> ModelResponse:
        assert self.audit is not None
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
            provider=self.provider.provider_name,
            model=self.provider.model_name,
            specification=context.specification,
            evidence=context.evidence,
            active_constraints=constraints,
            constraint_coverage=coverage,
            inference_parameters=self._inference_parameters(operation),
            requested_output=requested_output,
        )
        self.audit.write_call_package(call_number, request, prompt, context)
        self.contexts.append((call_number, context))
        invocation = ProviderInvocation(
            request_id=request_id,
            operation=operation,
            prompt=prompt,
        )
        try:
            call = self.provider.complete(invocation)
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
            provider=self.provider.provider_name,
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
        self, operation: ModelOperation
    ) -> dict[str, int | float | bool | None]:
        frontier = self.config.models.frontier
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
            f"patch-{attempt:03d}.diff", patch, kind="frontier_patch"
        )
        parsed = self.parser.parse(patch)
        proposal = patch.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
        if parsed.raw != proposal:
            self.audit.write_text(
                f"patch-{attempt:03d}-normalized.diff",
                parsed.raw,
                kind="normalized_frontier_patch",
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
                kind="rebased_frontier_patch",
            )

    def _run_verification(self, *, attempt: int) -> VerificationResult:
        assert self.audit is not None
        assert self.specification is not None
        assert self.worktree_path is not None
        result = VerificationRunner(self.config.verification).run(
            self.specification.task_id, self.worktree_path
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
        locations.extend([".sol/sol.db", predicted_report])
        report = FinalTaskReport(
            task_id=self.specification.task_id,
            outcome=outcome,
            error=error,
            worktree_path=self.worktree_path,
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
