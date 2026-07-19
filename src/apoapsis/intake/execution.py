from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from pathlib import Path

from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import ApoapsisConfig, FrontierProviderConfig
from apoapsis.context.compiler import ContextPackage
from apoapsis.intake.errors import IntakeError
from apoapsis.intake.schema import IntakeOperationRecord
from apoapsis.intake.store import IntakeOperationStore
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
from apoapsis.operations.lease import (
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_LEASE_DURATION,
    LeaseHeartbeat,
    new_owner_id,
)
from apoapsis.repository.git import GitRepository
from apoapsis.specification.extractor import (
    SpecificationExtractionError,
    SpecificationExtractor,
)
from apoapsis.specification.schema import SourceKind, TaskSpecification, TraceableStatement
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState, transition_is_allowed

_INTAKE_ROLE = ModelRole.FRONTIER_IMPLEMENTATION


def _build_provider(provider_config: FrontierProviderConfig) -> InstrumentedModelProvider:
    if provider_config.provider == "ollama":
        adapter = OllamaProvider(provider_config)
    elif provider_config.provider == "openai_compatible":
        adapter = OpenAICompatibleFrontierProvider(provider_config)
    else:
        raise IntakeError(f"unsupported provider: {provider_config.provider}")
    return InstrumentedModelProvider(adapter, provider_config.pricing)


def _inference_parameters(
    provider_config: FrontierProviderConfig,
) -> dict[str, int | float | bool | None]:
    think = provider_config.specification_think
    if think is None:
        think = provider_config.think
    return {
        "context_window_tokens": provider_config.context_window_tokens,
        "max_output_tokens": provider_config.max_output_tokens,
        "temperature": provider_config.temperature,
        "think": think,
        "timeout_seconds": provider_config.timeout_seconds,
    }


def _perform_intake_model_call(
    audit: TaskAuditStore,
    provider: InstrumentedModelProvider,
    provider_config: FrontierProviderConfig,
    *,
    prompt: str,
    context: ContextPackage,
    call_number: int,
) -> ModelResponse:
    """Mirrors ``VerticalSliceRunner._model_call``'s / ``review.execution
    ._ContinuationModelCaller``'s audit discipline (call package written
    before the call, response/telemetry written after) for a specification
    draft running outside any ``VerticalSliceRunner`` -- the same
    request/context/response/telemetry files, in the same shape, at the
    same ``call-<NNN>-*`` names."""

    operation = ModelOperation.DRAFT_SPECIFICATION
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
        provider=provider.provider_name,
        model=provider.model_name,
        specification=context.specification,
        evidence=context.evidence,
        active_constraints=constraints,
        constraint_coverage=coverage,
        inference_parameters=_inference_parameters(provider_config),
        requested_output="task_specification_json",
    )
    audit.write_call_package(
        call_number,
        request,
        prompt,
        context,
        provider_role=_INTAKE_ROLE.value,
    )
    invocation = ProviderInvocation(
        request_id=request_id,
        operation=operation,
        prompt=prompt,
        role=_INTAKE_ROLE,
    )
    try:
        call = provider.complete(invocation)
    except InstrumentedProviderError as exc:
        audit.write_json(
            f"call-{call_number:03d}-telemetry.json",
            exc.telemetry,
            kind="provider_telemetry",
        )
        raise
    response = ModelResponse(
        response_id=f"MRS-{uuid.uuid4().hex}",
        request_id=request_id,
        provider=provider.provider_name,
        model=call.output.model,
        operation=operation,
        content=call.output.content,
        usage=call.output.usage,
        finish_reason=call.output.finish_reason,
    )
    audit.write_call_result(call_number, response, call.telemetry)
    return response


def prepare_intake_operation(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    operation_store: IntakeOperationStore,
    *,
    request_text: str,
    operation_id: str,
    source_reference: str = "ui-request",
) -> IntakeOperationRecord:
    """Fast, synchronous, deterministic operation-record creation -- never
    a model call. Safe to call directly from an HTTP request handler: a
    caller gets an immediate, authoritative accept/reject before any slow
    work is ever enqueued.

    Allocates a fresh, deterministic ``task_id`` (the same ``TASK-<hex>``
    convention every other task-creation path uses) and creates its task
    row -- at ``INTAKE``, holding the exact, verbatim request text as its
    preliminary objective -- before the operation record is created, and
    strictly before any model call. Raises ``ActiveIntakeOperationExists
    Error``/``DuplicateIntakeOperationError`` on operation_id reuse (the
    task row this reuse attempt would have owned, if any, is left in place,
    inspectable and harmless, at ``INTAKE``).
    """

    if not request_text.strip():
        raise IntakeError("request_text must not be empty")
    root = Path(project_root).resolve()
    task_id = f"TASK-{uuid.uuid4().hex[:12].upper()}"
    preliminary = TaskSpecification(
        task_id=task_id,
        objective=TraceableStatement(
            text=request_text,
            source=SourceKind.USER,
            source_reference=source_reference,
        ),
    )
    created = task_store.create_task(preliminary)
    TaskAuditStore(root, task_id)
    request_sha256 = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
    return operation_store.create(
        operation_id,
        task_id,
        request_text,
        request_sha256=request_sha256,
        expected_task_version=created.version,
        provider_role=_INTAKE_ROLE.value,
    )


def run_intake_operation(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    operation_store: IntakeOperationStore,
    config: ApoapsisConfig,
    *,
    operation_id: str,
    provider: InstrumentedModelProvider | None = None,
    lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    heartbeat_interval: timedelta = DEFAULT_HEARTBEAT_INTERVAL,
) -> IntakeOperationRecord:
    """The actual work -- one model-assisted specification-extraction call,
    plus at most one bounded correction call -- for an operation ``prepare
    _intake_operation`` has already validated and recorded. Takes only
    ``operation_id``; the request text, task id, and expected task version
    are all reloaded from the durable operation record, never carried in
    memory from submission time -- a worker queue entry, a crash, or a long
    delay between submission and execution can never cause stale in-memory
    state to be acted on.

    Marks the operation ``RUNNING`` before anything else -- including
    provider construction -- so any preflight failure reaches a
    deterministic terminal status (``FAILED``) instead of leaving the
    operation ``RECORDED`` forever. Immediately re-fetches the task and
    rechecks its identity (still ``INTAKE``) and version against the
    operation's own recorded expectation before doing anything else.

    Claims a fresh, unique lease (ADR 0025) and starts a wall-clock
    heartbeat that renews it independent of model latency; the heartbeat
    always stops before this function returns.

    Intended to run on a background worker thread, never inside an HTTP
    request handler.
    """

    root = Path(project_root).resolve()
    record = operation_store.get(operation_id)
    owner_id = new_owner_id()
    operation_store.mark_running(
        operation_id, owner_id=owner_id, lease_duration=lease_duration
    )
    heartbeat = LeaseHeartbeat(
        lambda: operation_store.renew_lease(
            operation_id, owner_id=owner_id, lease_duration=lease_duration
        ),
        interval=heartbeat_interval,
    )
    heartbeat.start()
    try:
        try:
            task = task_store.get_task(record.task_id)
            if (
                task.state != WorkflowState.INTAKE
                or task.version != record.expected_task_version
            ):
                raise IntakeError(
                    f"task {record.task_id} is no longer eligible for intake "
                    f"extraction (state={task.state.value}, version="
                    f"{task.version}, expected version="
                    f"{record.expected_task_version})"
                )
            audit = TaskAuditStore(root, record.task_id)
            selected_provider = provider or _build_provider(config.models.frontier)
            head = GitRepository(root).run(["rev-parse", "HEAD"]).stdout.strip()
            spec_context = ContextPackage.specification_only(task.specification, head)
            extractor = SpecificationExtractor()
            prompt = extractor.build_prompt(
                record.request_text, record.task_id, config.verification.commands
            )
            response = _perform_intake_model_call(
                audit,
                selected_provider,
                config.models.frontier,
                prompt=prompt,
                context=spec_context,
                call_number=1,
            )
            try:
                specification = extractor.parse(
                    response.content,
                    record.request_text,
                    record.task_id,
                    config.verification.commands,
                )
            except SpecificationExtractionError as exc:
                # Exactly one bounded correction attempt (mirrors ADR 0018):
                # the failed response and its telemetry are already persisted
                # by `_perform_intake_model_call` above. A model never gets a
                # second correction -- if this one also fails to parse, the
                # exception is caught below and the task stops deterministically
                # at FAILED.
                audit.write_json(
                    "specification-extraction-failure-001.json",
                    {
                        "attempt": 1,
                        "error": str(exc),
                        "raw_response": response.content,
                    },
                    kind="specification_extraction_failure",
                )
                correction_prompt = extractor.build_correction_prompt(
                    record.request_text,
                    record.task_id,
                    config.verification.commands,
                    response.content,
                    str(exc),
                )
                correction_response = _perform_intake_model_call(
                    audit,
                    selected_provider,
                    config.models.frontier,
                    prompt=correction_prompt,
                    context=spec_context,
                    call_number=2,
                )
                specification = extractor.parse(
                    correction_response.content,
                    record.request_text,
                    record.task_id,
                    config.verification.commands,
                )
        except SpecificationExtractionError as exc:
            # Both attempts failed: a bounded, deterministic, expected outcome
            # -- not a crash -- so this stops cleanly at FAILED rather than
            # propagating an exception to the caller.
            audit.write_json(
                "intake-extraction-failed.json",
                {"operation_id": operation_id, "error": str(exc)},
                kind="fatal_error",
            )
            if transition_is_allowed(task.state, WorkflowState.FAILED):
                task_store.transition(
                    record.task_id,
                    WorkflowState.FAILED,
                    actor=WorkflowActor.SYSTEM,
                    event_type="intake_extraction_failed",
                    payload={"operation_id": operation_id, "error": str(exc)},
                    expected_version=task.version,
                )
            return operation_store.mark_failed(
                operation_id,
                owner_id=owner_id,
                error=str(exc),
                audit_artifact_locations=[item.path for item in audit.artifacts()],
            )
        except Exception as exc:
            operation_store.mark_failed(
                operation_id, owner_id=owner_id, error=f"{type(exc).__name__}: {exc}"
            )
            raise

        updated = task_store.update_specification(
            specification, actor=WorkflowActor.SYSTEM, expected_version=task.version
        )
        task_store.transition(
            record.task_id,
            WorkflowState.SPEC_DRAFTED,
            actor=WorkflowActor.SYSTEM,
            event_type="intake_specification_drafted",
            payload={
                "operation_id": operation_id,
                "constraints": len(specification.hard_constraints),
                "verbatim_constraints_validated": True,
            },
            expected_version=updated.version,
        )
        audit.write_json(
            "approved-specification-candidate.json",
            specification,
            kind="specification_candidate",
        )
        return operation_store.mark_pending_approval(
            operation_id,
            owner_id=owner_id,
            result_summary="specification drafted; pending human approval",
            audit_artifact_locations=[item.path for item in audit.artifacts()],
        )
    finally:
        heartbeat.stop()


def execute_intake_operation(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    operation_store: IntakeOperationStore,
    config: ApoapsisConfig,
    *,
    request_text: str,
    operation_id: str,
    source_reference: str = "cli-request",
    provider: InstrumentedModelProvider | None = None,
) -> IntakeOperationRecord:
    """Convenience wrapper for synchronous callers (the CLI): prepare and
    run in one call. The UI submits via ``prepare_intake_operation`` from
    its HTTP handler and runs ``run_intake_operation`` from a background
    worker instead, so an extraction call never blocks a request thread."""

    prepare_intake_operation(
        project_root,
        task_store,
        operation_store,
        request_text=request_text,
        operation_id=operation_id,
        source_reference=source_reference,
    )
    return run_intake_operation(
        project_root,
        task_store,
        operation_store,
        config,
        operation_id=operation_id,
        provider=provider,
    )


__all__ = [
    "execute_intake_operation",
    "prepare_intake_operation",
    "run_intake_operation",
]
