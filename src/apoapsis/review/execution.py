from __future__ import annotations

import uuid
from pathlib import Path

from apoapsis.agent.session import (
    AgentSessionOutcome,
    AgentSessionResult,
    BoundedAgentSession,
)
from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import ApoapsisConfig, CompletionPolicy, FrontierProviderConfig
from apoapsis.context.compiler import ContextCompiler
from apoapsis.execution.worktree import WorktreeManager
from apoapsis.models.base import (
    ConstraintCoverage,
    ConstraintDisposition,
    ModelOperation,
    ModelRequest,
    ModelResponse,
)
from apoapsis.models.frontier import OpenAICompatibleFrontierProvider
from apoapsis.models.local import OllamaProvider
from apoapsis.models.provider import ModelRole, ProviderInvocation
from apoapsis.models.telemetry import InstrumentedModelProvider, InstrumentedProviderError
from apoapsis.patches.apply import GitPatchApplier
from apoapsis.patches.parser import UnifiedDiffParser
from apoapsis.patches.validator import PatchPolicyValidator
from apoapsis.review.case import (
    FRONTIER_CONTINUATION_STARTED,
    LOCAL_CONTINUATION_STARTED,
    build_review_case,
    continuation_additional_turns,
    read_agent_session,
    task_slug,
)
from apoapsis.review.errors import (
    ContinuationCeilingExceededError,
    FrontierUnavailableError,
    InvalidReviewActionError,
    ReviewError,
    WorktreeChangedError,
)
from apoapsis.review.package import build_continuation_package, write_continuation_package
from apoapsis.review.schema import (
    ContinuationBudget,
    ReviewActionKind,
    ReviewCase,
    ReviewOperationRecord,
)
from apoapsis.review.store import ReviewOperationStore
from apoapsis.verification.results import VerificationStatus
from apoapsis.verification.runner import VerificationRunner
from apoapsis.workflow.acceptance import (
    acceptance_coverage_satisfied,
    compute_acceptance_coverage,
)
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState

_CONTINUATION_ACTIONS = frozenset(
    {ReviewActionKind.LOCAL_CONTINUATION, ReviewActionKind.FRONTIER_CONTINUATION}
)
_WORKTREE_CHECKED_ACTIONS = frozenset(
    {
        ReviewActionKind.VERIFICATION_ONLY_RETRY,
        ReviewActionKind.LOCAL_CONTINUATION,
        ReviewActionKind.FRONTIER_CONTINUATION,
    }
)


def _build_provider(provider_config: FrontierProviderConfig) -> InstrumentedModelProvider:
    if provider_config.provider == "ollama":
        adapter = OllamaProvider(provider_config)
    elif provider_config.provider == "openai_compatible":
        adapter = OpenAICompatibleFrontierProvider(provider_config)
    else:
        raise ReviewError(f"unsupported provider: {provider_config.provider}")
    return InstrumentedModelProvider(adapter, provider_config.pricing)


def _validate_operation_preconditions(
    review_case: ReviewCase,
    *,
    action: ReviewActionKind,
    expected_version: int,
    expected_worktree_fingerprint: str | None,
    budget: ContinuationBudget | None,
    max_additional_turns_per_continuation: int,
) -> None:
    """The one set of precondition checks every operation must pass --
    used both by ``prepare_review_operation`` (against caller-supplied
    expectations, before recording) and ``run_review_operation`` (against
    the durably recorded expectations, freshly re-checked immediately
    before doing anything -- ADR 0021). Raises on the first violation.
    """

    if expected_version != review_case.task_version:
        raise ReviewError(
            f"expected task version {expected_version}, found "
            f"{review_case.task_version}"
        )
    if action not in review_case.eligible_actions:
        raise InvalidReviewActionError(
            f"action {action.value} is not eligible for task "
            f"{review_case.task_id} "
            f"(eligible: {[item.value for item in review_case.eligible_actions]})"
        )
    if action in _WORKTREE_CHECKED_ACTIONS:
        if (
            expected_worktree_fingerprint is None
            or expected_worktree_fingerprint != review_case.worktree_fingerprint
        ):
            raise WorktreeChangedError(
                "the worktree fingerprint no longer matches what was shown "
                "before authorizing this action; inspect the task again"
            )
    if action in _CONTINUATION_ACTIONS:
        if budget is None:
            raise ReviewError(f"{action.value} requires an authorized budget")
        if budget.additional_turns > max_additional_turns_per_continuation:
            raise ContinuationCeilingExceededError(
                f"additional_turns {budget.additional_turns} exceeds the "
                f"configured ceiling of {max_additional_turns_per_continuation} "
                "per continuation"
            )
        if review_case.continuations_used >= review_case.max_continuations_per_task:
            raise ContinuationCeilingExceededError(
                f"task {review_case.task_id} has already used "
                f"{review_case.continuations_used} of "
                f"{review_case.max_continuations_per_task} authorized "
                "continuations"
            )
        if (
            action == ReviewActionKind.FRONTIER_CONTINUATION
            and not review_case.frontier_available
        ):
            raise FrontierUnavailableError(
                "no frontier coder is configured for this project"
            )


def _inference_parameters(
    operation: ModelOperation, provider_config: FrontierProviderConfig
) -> dict[str, int | float | bool | None]:
    think = provider_config.think
    if (
        operation == ModelOperation.DRAFT_SPECIFICATION
        and provider_config.specification_think is not None
    ):
        think = provider_config.specification_think
    return {
        "context_window_tokens": provider_config.context_window_tokens,
        "max_output_tokens": provider_config.max_output_tokens,
        "temperature": provider_config.temperature,
        "think": think,
        "timeout_seconds": provider_config.timeout_seconds,
    }


class _ContinuationModelCaller:
    """Mirrors ``VerticalSliceRunner._model_call``'s audit discipline
    (call package written before the call, response/telemetry written
    after) for a continuation running outside any ``VerticalSliceRunner``.
    Call numbers continue from whatever the task's audit directory already
    has, so a continuation's calls are never numbered over the original
    run's."""

    def __init__(
        self,
        audit: TaskAuditStore,
        provider: InstrumentedModelProvider,
        provider_config: FrontierProviderConfig,
        *,
        start_call_number: int,
    ) -> None:
        self.audit = audit
        self.provider = provider
        self.provider_config = provider_config
        self.call_number = start_call_number - 1

    def __call__(
        self,
        operation: ModelOperation,
        prompt: str,
        context,
        *,
        requested_output: str,
        response_schema: dict[str, object] | None = None,
        role: ModelRole = ModelRole.FRONTIER_IMPLEMENTATION,
    ) -> ModelResponse:
        self.call_number += 1
        call_number = self.call_number
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
            inference_parameters=_inference_parameters(operation, self.provider_config),
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
        invocation = ProviderInvocation(
            request_id=request_id,
            operation=operation,
            prompt=prompt,
            role=role,
            response_schema=response_schema,
        )
        try:
            call = self.provider.complete(invocation)
        except InstrumentedProviderError as exc:
            self.audit.write_json(
                f"call-{call_number:03d}-telemetry.json",
                exc.telemetry,
                kind="provider_telemetry",
            )
            raise
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


def _make_apply_patch(audit: TaskAuditStore, config: ApoapsisConfig, worktree_path: str, prefix: str):
    parser = UnifiedDiffParser()
    validator = PatchPolicyValidator(config.patch)
    applier = GitPatchApplier()

    def apply_patch(patch: str, attempt: int) -> None:
        audit.write_text(
            f"review-{prefix}patch-{attempt:03d}.diff", patch, kind="model_patch"
        )
        parsed = parser.parse(patch)
        proposal = patch.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
        if parsed.raw != proposal:
            audit.write_text(
                f"review-{prefix}patch-{attempt:03d}-normalized.diff",
                parsed.raw,
                kind="normalized_model_patch",
            )
        validation = validator.validate(parsed, worktree_path)
        audit.write_json(
            f"review-{prefix}patch-{attempt:03d}-policy.json",
            validation,
            kind="patch_policy",
        )
        validation.require_accepted()
        applier.apply(parsed, worktree_path)
        if (
            applier.last_applied_patch is not None
            and applier.last_applied_patch != parsed.raw
        ):
            audit.write_text(
                f"review-{prefix}patch-{attempt:03d}-rebased.diff",
                applier.last_applied_patch,
                kind="rebased_model_patch",
            )

    return apply_patch


def prepare_review_operation(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    operation_store: ReviewOperationStore,
    config: ApoapsisConfig,
    *,
    task_id: str,
    action: ReviewActionKind,
    operation_id: str,
    expected_version: int,
    expected_worktree_fingerprint: str | None = None,
    additional_turns: int | None = None,
) -> ReviewCase:
    """Every fast, synchronous, read-only check plus operation-record
    creation -- never a model call, never a worktree mutation. Safe to call
    directly from an HTTP request handler (ADR 0020 Commit C2): a caller
    gets an immediate, authoritative accept/reject before any slow work is
    ever enqueued. Raises on any validation failure (including
    ``ActiveOperationExistsError`` if this task already has a RECORDED or
    RUNNING operation); otherwise the operation -- and everything needed to
    later re-derive and re-check it, including
    ``expected_worktree_fingerprint`` -- is durably recorded as ``RECORDED``
    and ready for ``run_review_operation``.
    """

    root = Path(project_root).resolve()
    review_case = build_review_case(root, task_store, config, task_id)
    budget = (
        ContinuationBudget(additional_turns=additional_turns)
        if additional_turns is not None
        else None
    )
    _validate_operation_preconditions(
        review_case,
        action=action,
        expected_version=expected_version,
        expected_worktree_fingerprint=expected_worktree_fingerprint,
        budget=budget,
        max_additional_turns_per_continuation=(
            config.review.max_additional_turns_per_continuation
        ),
    )
    operation_store.create(
        operation_id,
        task_id,
        action,
        expected_task_version=expected_version,
        expected_worktree_fingerprint=expected_worktree_fingerprint,
        authorized_budget=budget,
    )
    return review_case


def run_review_operation(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    operation_store: ReviewOperationStore,
    config: ApoapsisConfig,
    *,
    operation_id: str,
    local_coder_provider: InstrumentedModelProvider | None = None,
    frontier_coder_provider: InstrumentedModelProvider | None = None,
) -> ReviewOperationRecord:
    """The actual work -- a resumed model call, a verification run, or a
    worktree cleanup -- for an operation ``prepare_review_operation`` has
    already validated and recorded. Takes only ``operation_id``; every
    other input (task, action, expected version/fingerprint, budget) is
    reloaded from the durable operation record, never carried in memory
    from submission time (ADR 0021) -- a worker queue entry, a crash, or a
    long delay between submission and execution can never cause stale
    in-memory state to be acted on.

    Marks the operation ``RUNNING`` before anything else -- including
    provider construction -- so any preflight failure (a bad provider
    config, a task that changed underneath the operation, an exhausted
    ceiling) reaches a deterministic terminal status (``FAILED``) instead
    of leaving the operation ``RECORDED`` forever. Immediately re-projects
    a fresh ``ReviewCase`` and re-validates every precondition against the
    operation's own recorded expectations before dispatching to the action
    handler -- never trusting anything computed earlier.

    Intended to run on a background worker thread (ADR 0020 Commit C2),
    never inside an HTTP request handler.
    """

    root = Path(project_root).resolve()
    record = operation_store.get(operation_id)
    operation_store.mark_running(operation_id)
    try:
        review_case = build_review_case(root, task_store, config, record.task_id)
        _validate_operation_preconditions(
            review_case,
            action=record.action,
            expected_version=record.expected_task_version,
            expected_worktree_fingerprint=record.expected_worktree_fingerprint,
            budget=record.authorized_budget,
            max_additional_turns_per_continuation=(
                config.review.max_additional_turns_per_continuation
            ),
        )

        if record.action == ReviewActionKind.INSPECT_ONLY:
            summary = "inspected only; no state change was made"
        elif record.action == ReviewActionKind.ABANDON:
            summary = _execute_abandon(
                root, task_store, review_case, record.expected_task_version
            )
        elif record.action == ReviewActionKind.VERIFICATION_ONLY_RETRY:
            summary = _execute_verification_retry(
                root,
                task_store,
                config,
                review_case,
                record.expected_task_version,
                operation_id=operation_id,
            )
        elif record.action == ReviewActionKind.LOCAL_CONTINUATION:
            assert record.authorized_budget is not None
            provider = local_coder_provider or _build_provider(
                config.models.local_coder or config.models.frontier
            )
            summary = _execute_continuation(
                root,
                task_store,
                config,
                review_case,
                record.expected_task_version,
                action=record.action,
                operation_id=operation_id,
                budget=record.authorized_budget,
                provider=provider,
            )
        elif record.action == ReviewActionKind.FRONTIER_CONTINUATION:
            assert record.authorized_budget is not None
            assert config.models.frontier_coder is not None
            provider = frontier_coder_provider or _build_provider(
                config.models.frontier_coder
            )
            summary = _execute_continuation(
                root,
                task_store,
                config,
                review_case,
                record.expected_task_version,
                action=record.action,
                operation_id=operation_id,
                budget=record.authorized_budget,
                provider=provider,
            )
        else:
            raise AssertionError(f"unhandled review action: {record.action}")
    except Exception as exc:
        operation_store.mark_failed(operation_id, error=f"{type(exc).__name__}: {exc}")
        raise
    return operation_store.mark_succeeded(operation_id, result_summary=summary)


def execute_review_action(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    operation_store: ReviewOperationStore,
    config: ApoapsisConfig,
    *,
    task_id: str,
    action: ReviewActionKind,
    operation_id: str,
    expected_version: int,
    expected_worktree_fingerprint: str | None = None,
    additional_turns: int | None = None,
    local_coder_provider: InstrumentedModelProvider | None = None,
    frontier_coder_provider: InstrumentedModelProvider | None = None,
) -> ReviewOperationRecord:
    """Convenience wrapper for synchronous callers (the CLI): prepare and
    run in one call. The UI (ADR 0020 Commit C2) calls
    ``prepare_review_operation`` from its HTTP handler and
    ``run_review_operation`` from a background worker instead, so a
    resumed model call never blocks a request thread."""

    prepare_review_operation(
        project_root,
        task_store,
        operation_store,
        config,
        task_id=task_id,
        action=action,
        operation_id=operation_id,
        expected_version=expected_version,
        expected_worktree_fingerprint=expected_worktree_fingerprint,
        additional_turns=additional_turns,
    )
    return run_review_operation(
        project_root,
        task_store,
        operation_store,
        config,
        operation_id=operation_id,
        local_coder_provider=local_coder_provider,
        frontier_coder_provider=frontier_coder_provider,
    )


def _execute_abandon(
    root: Path,
    task_store: SQLiteTaskStore,
    review_case: ReviewCase,
    expected_version: int,
) -> str:
    # The version-checked transition happens BEFORE any destructive
    # worktree cleanup (ADR 0021): a stale operation must fail its version
    # check and never delete anything, rather than deleting the worktree
    # and only then discovering the task had already moved on.
    task_store.transition(
        review_case.task_id,
        WorkflowState.ROLLED_BACK,
        actor=WorkflowActor.USER,
        event_type="review_abandoned",
        payload={
            "reason": "user chose to abandon the task from human review",
            "had_worktree": review_case.worktree_exists,
        },
        expected_version=expected_version,
    )
    if review_case.worktree_exists:
        WorktreeManager(root).cleanup(
            task_slug(review_case.task_id), force=True, delete_branch=False
        )
    return "task abandoned and rolled back"


def _execute_verification_retry(
    root: Path,
    task_store: SQLiteTaskStore,
    config: ApoapsisConfig,
    review_case: ReviewCase,
    expected_version: int,
    *,
    operation_id: str,
) -> str:
    assert review_case.worktree_path is not None
    task_id = review_case.task_id
    verifying = task_store.transition(
        task_id,
        WorkflowState.VERIFYING,
        actor=WorkflowActor.VERIFICATION_ENGINE,
        event_type="review_verification_retry_started",
        payload={
            "reason": "human-authorized verification-only retry",
            "operation_id": operation_id,
        },
        expected_version=expected_version,
    )
    result = VerificationRunner(config.verification).run(
        task_id, review_case.worktree_path, attempt=1
    )
    audit = TaskAuditStore(root, task_id)
    audit.write_json(
        f"review-verification-retry-{operation_id}.json",
        result,
        kind="verification_result",
    )

    if result.status != VerificationStatus.PASSED:
        task_store.transition(
            task_id,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            actor=WorkflowActor.VERIFICATION_ENGINE,
            event_type="review_verification_retry_failed",
            payload={
                "reason": "configured verification still failed on retry",
                "operation_id": operation_id,
            },
            expected_version=verifying.version,
        )
        return "verification retry still failing"

    if config.execution.completion_policy == CompletionPolicy.STRICT:
        specification = task_store.get_task(task_id).specification
        command_results = {
            command.name: command.status
            for command in result.commands
            if command.status != VerificationStatus.SKIPPED
        }
        coverage = compute_acceptance_coverage(
            specification, config.verification.commands, command_results
        )
        if not acceptance_coverage_satisfied(coverage):
            task_store.transition(
                task_id,
                WorkflowState.HUMAN_REVIEW_REQUIRED,
                actor=WorkflowActor.VERIFICATION_ENGINE,
                event_type="review_verification_retry_incomplete",
                payload={
                    "reason": (
                        "configured verification passed but not every active "
                        "acceptance criterion is proven under the strict "
                        "completion policy"
                    ),
                    "operation_id": operation_id,
                    "coverage": [item.model_dump(mode="json") for item in coverage],
                },
                expected_version=verifying.version,
            )
            return "verification passed but acceptance coverage remains incomplete"

    task_store.transition(
        task_id,
        WorkflowState.COMPLETE,
        actor=WorkflowActor.VERIFICATION_ENGINE,
        event_type="review_verification_retry_passed",
        payload={"operation_id": operation_id},
        expected_version=verifying.version,
    )
    return "verification retry passed; task marked COMPLETE"


def _execute_continuation(
    root: Path,
    task_store: SQLiteTaskStore,
    config: ApoapsisConfig,
    review_case: ReviewCase,
    expected_version: int,
    *,
    action: ReviewActionKind,
    operation_id: str,
    budget: ContinuationBudget,
    provider: InstrumentedModelProvider | None,
) -> str:
    assert review_case.worktree_path is not None
    task_id = review_case.task_id
    is_frontier = action == ReviewActionKind.FRONTIER_CONTINUATION
    prefix = "frontier-" if is_frontier else ""
    role = ModelRole.FRONTIER_CODING_AGENT if is_frontier else ModelRole.LOCAL_CODING_AGENT
    provider_config = (
        config.models.frontier_coder
        if is_frontier
        else (config.models.local_coder or config.models.frontier)
    )
    if provider is None or provider_config is None:
        raise ReviewError(
            f"{'frontier' if is_frontier else 'local'} coder is not configured"
        )

    task_directory = root / ".apoapsis" / "tasks" / task_id
    prior_session = read_agent_session(task_directory, prefix)
    if prior_session is None:
        raise ReviewError(
            f"no prior {'frontier' if is_frontier else 'local'} agent session "
            "exists to continue"
        )

    specification = task_store.get_task(task_id).specification
    base_agent_config = (
        config.execution.frontier_agent if is_frontier else config.execution.agent
    )
    events = task_store.events(task_id)
    started_event_type = (
        FRONTIER_CONTINUATION_STARTED if is_frontier else LOCAL_CONTINUATION_STARTED
    )
    past_additional = continuation_additional_turns(events, started_event_type)
    delta = past_additional + budget.additional_turns
    effective_config = base_agent_config.model_copy(
        update={
            "max_turns": base_agent_config.max_turns + delta,
            "max_patch_attempts": base_agent_config.max_patch_attempts + delta,
            "max_verification_runs": base_agent_config.max_verification_runs + delta,
        }
    )

    audit = TaskAuditStore(root, task_id)
    package = build_continuation_package(
        review_case,
        specification,
        operation_id=operation_id,
        action=action,
        authorized_budget=budget,
        effective_agent_budget=effective_config,
        verification_catalog=[item.name for item in config.verification.commands],
    )
    write_continuation_package(audit, package)

    context_compiler = ContextCompiler(config.context)
    context = context_compiler.compile(specification, review_case.worktree_path)
    existing_calls = len(list(task_directory.glob("call-*-request.json")))
    model_call = _ContinuationModelCaller(
        audit, provider, provider_config, start_call_number=existing_calls + 1
    )
    apply_patch = _make_apply_patch(audit, config, review_case.worktree_path, prefix)

    session = BoundedAgentSession.resume(
        specification=specification,
        worktree=review_case.worktree_path,
        initial_context=context,
        context_compiler=context_compiler,
        config=effective_config,
        verification_config=config.verification,
        audit=audit,
        model_call=model_call,
        apply_patch=apply_patch,
        prior_result=prior_session,
        model_role=role,
        audit_prefix=prefix,
        completion_policy=config.execution.completion_policy,
    )

    started_event = f"review_{'frontier' if is_frontier else 'local'}_continuation_started"
    started = task_store.transition(
        task_id,
        WorkflowState.IMPLEMENTING,
        actor=WorkflowActor.USER,
        event_type=started_event,
        payload={
            "reason": "human-authorized continuation",
            "operation_id": operation_id,
            "authorized_budget": budget.model_dump(mode="json"),
        },
        expected_version=expected_version,
    )
    try:
        result: AgentSessionResult = session.run(
            start_turn=len(prior_session.turn_records) + 1
        )
    except InstrumentedProviderError as exc:
        result = session.interrupted(
            f"{role.value} provider call failed during continuation: {exc}"
        )

    if result.outcome == AgentSessionOutcome.COMPLETE:
        patch_ready = task_store.transition(
            task_id,
            WorkflowState.PATCH_READY,
            actor=WorkflowActor.SYSTEM,
            event_type="review_continuation_patch_ready",
            payload={"turns": result.turns, "patch_attempts": result.patch_attempts},
            expected_version=started.version,
        )
        verifying = task_store.transition(
            task_id,
            WorkflowState.VERIFYING,
            actor=WorkflowActor.VERIFICATION_ENGINE,
            event_type="review_continuation_verification_recorded",
            payload={"verification_runs": result.verification_runs},
            expected_version=patch_ready.version,
        )
        task_store.transition(
            task_id,
            WorkflowState.COMPLETE,
            actor=WorkflowActor.VERIFICATION_ENGINE,
            event_type="review_continuation_verification_passed",
            payload={"stop_reason": result.stop_reason},
            expected_version=verifying.version,
        )
        return "continuation completed the task"

    escalation_event = (
        f"review_{'frontier' if is_frontier else 'local'}_continuation_escalation_required"
    )
    human_event = (
        f"review_{'frontier' if is_frontier else 'local'}_continuation_requires_human"
    )
    escalation = task_store.transition(
        task_id,
        WorkflowState.ESCALATION_REQUIRED,
        actor=WorkflowActor.SYSTEM,
        event_type=escalation_event,
        payload={"reason": result.stop_reason},
        expected_version=started.version,
    )
    task_store.transition(
        task_id,
        WorkflowState.HUMAN_REVIEW_REQUIRED,
        actor=WorkflowActor.SYSTEM,
        event_type=human_event,
        payload={"reason": result.stop_reason},
        expected_version=escalation.version,
    )
    return f"continuation stopped again: {result.stop_reason}"


__all__ = [
    "execute_review_action",
    "prepare_review_operation",
    "run_review_operation",
]
