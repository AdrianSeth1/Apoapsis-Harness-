from __future__ import annotations

from pathlib import Path

from apoapsis.config import ApoapsisConfig, FrontierProviderConfig
from apoapsis.execution.operation_errors import (
    ExecutionOperationError,
    StaleExecutionStartError,
)
from apoapsis.execution.operation_schema import ExecutionOperationRecord
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.models.frontier import OpenAICompatibleFrontierProvider
from apoapsis.models.local import OllamaProvider
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.repository.git import GitRepository
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.states import WorkflowState
from apoapsis.workflow.vertical_slice import VerticalSliceRunner


def _build_provider(provider_config: FrontierProviderConfig) -> InstrumentedModelProvider:
    if provider_config.provider == "ollama":
        adapter = OllamaProvider(provider_config)
    elif provider_config.provider == "openai_compatible":
        adapter = OpenAICompatibleFrontierProvider(provider_config)
    else:
        raise ExecutionOperationError(f"unsupported provider: {provider_config.provider}")
    return InstrumentedModelProvider(adapter, provider_config.pricing)


def _build_providers(
    config: ApoapsisConfig,
) -> tuple[
    InstrumentedModelProvider, InstrumentedModelProvider, InstrumentedModelProvider | None
]:
    provider = _build_provider(config.models.frontier)
    local_coder_provider = (
        _build_provider(config.models.local_coder)
        if config.models.local_coder is not None
        else provider
    )
    frontier_coder_provider = (
        _build_provider(config.models.frontier_coder)
        if config.models.frontier_coder is not None
        else None
    )
    return provider, local_coder_provider, frontier_coder_provider


def prepare_execution_operation(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    operation_store: ExecutionOperationStore,
    *,
    task_id: str,
    operation_id: str,
    expected_version: int,
) -> ExecutionOperationRecord:
    """Fast, synchronous, deterministic operation-record creation -- never
    a model call, worktree mutation, or command execution. Safe to call
    directly from an HTTP request handler: a caller gets an immediate,
    authoritative accept/reject before any slow work is ever enqueued.

    Requires the task to be at ``SPEC_APPROVED`` and at exactly the
    caller-supplied ``expected_version`` (``StaleExecutionStartError``
    otherwise). Captures the current repository HEAD so
    ``run_execution_operation`` can later detect the parent repository
    changing underneath a queued operation, then durably records the
    operation as ``RECORDED``.
    """

    root = Path(project_root).resolve()
    task = task_store.get_task(task_id)
    if task.version != expected_version:
        raise StaleExecutionStartError(
            f"expected task version {expected_version}, found {task.version}"
        )
    if task.state != WorkflowState.SPEC_APPROVED:
        raise ExecutionOperationError(
            f"task {task_id} is not eligible for execution: expected "
            f"SPEC_APPROVED, found {task.state.value}"
        )
    head = GitRepository(root).run(["rev-parse", "HEAD"]).stdout.strip()
    return operation_store.create(
        operation_id,
        task_id,
        expected_task_version=expected_version,
        expected_repository_head=head,
    )


def run_execution_operation(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    operation_store: ExecutionOperationStore,
    config: ApoapsisConfig,
    *,
    operation_id: str,
    provider: InstrumentedModelProvider | None = None,
    local_coder_provider: InstrumentedModelProvider | None = None,
    frontier_coder_provider: InstrumentedModelProvider | None = None,
) -> ExecutionOperationRecord:
    """The actual work -- routing, context compilation, worktree creation,
    the selected coding stage, verification, and reporting -- for an
    operation ``prepare_execution_operation`` has already validated and
    recorded. Takes only ``operation_id``; the task id, expected version,
    and expected repository HEAD are all reloaded from the durable
    operation record, never carried in memory from submission time -- a
    worker queue entry, a crash, or a long delay between submission and
    execution can never cause stale in-memory state to be acted on.

    Marks the operation ``RUNNING`` before anything else -- including
    provider construction -- so any preflight failure reaches a
    deterministic terminal status (``FAILED``) instead of leaving the
    operation ``RECORDED`` forever. Immediately re-fetches the task and
    rechecks its identity (still ``SPEC_APPROVED``), version, and the
    repository HEAD before doing anything else.

    Marks the operation ``SUCCEEDED`` once ``VerticalSliceRunner
    .execute_approved_task()`` returns a report at all -- regardless of
    whether the task itself reached ``COMPLETE``, ``FAILED``, or
    ``HUMAN_REVIEW_REQUIRED``, all of which are legitimate, deterministic
    task-level outcomes; only an operation-level exception (a crash before
    or during execution) marks the *operation* ``FAILED``.

    Intended to run on a background worker thread, never inside an HTTP
    request handler.
    """

    root = Path(project_root).resolve()
    record = operation_store.get(operation_id)
    operation_store.mark_running(operation_id)
    try:
        task = task_store.get_task(record.task_id)
        if (
            task.state != WorkflowState.SPEC_APPROVED
            or task.version != record.expected_task_version
        ):
            raise StaleExecutionStartError(
                f"task {record.task_id} is no longer eligible for execution "
                f"(state={task.state.value}, version={task.version}, "
                f"expected version={record.expected_task_version})"
            )
        current_head = GitRepository(root).run(["rev-parse", "HEAD"]).stdout.strip()
        if current_head != record.expected_repository_head:
            raise StaleExecutionStartError(
                "the repository HEAD changed since this operation was "
                f"authorized (expected {record.expected_repository_head}, "
                f"found {current_head})"
            )
        # Constructing an adapter is side-effect-free (no network I/O
        # happens until `.complete()` is actually called), so the natural,
        # configured providers are always built and only overridden by
        # whichever ones the caller explicitly supplied -- each of the
        # three is defaulted independently, never all-or-nothing.
        built_provider, built_local, built_frontier = _build_providers(config)
        provider = provider or built_provider
        local_coder_provider = local_coder_provider or built_local
        frontier_coder_provider = frontier_coder_provider or built_frontier
        runner = VerticalSliceRunner(
            root,
            task_store,
            provider,
            config,
            local_coder_provider=local_coder_provider,
            frontier_coder_provider=frontier_coder_provider,
        )
        report = runner.execute_approved_task(record.task_id)
    except Exception as exc:
        operation_store.mark_failed(operation_id, error=f"{type(exc).__name__}: {exc}")
        raise
    report_path = f".apoapsis/tasks/{record.task_id}/report.json"
    return operation_store.mark_succeeded(
        operation_id,
        result_summary=f"execution finished with outcome {report.outcome.value}",
        report_path=report_path,
    )


def execute_execution_operation(
    project_root: str | Path,
    task_store: SQLiteTaskStore,
    operation_store: ExecutionOperationStore,
    config: ApoapsisConfig,
    *,
    task_id: str,
    operation_id: str,
    expected_version: int,
    provider: InstrumentedModelProvider | None = None,
    local_coder_provider: InstrumentedModelProvider | None = None,
    frontier_coder_provider: InstrumentedModelProvider | None = None,
) -> ExecutionOperationRecord:
    """Convenience wrapper for synchronous callers (the CLI): prepare and
    run in one call. The UI submits via ``prepare_execution_operation``
    from its HTTP handler and runs ``run_execution_operation`` from a
    background worker instead, so an execution run never blocks a request
    thread."""

    prepare_execution_operation(
        project_root,
        task_store,
        operation_store,
        task_id=task_id,
        operation_id=operation_id,
        expected_version=expected_version,
    )
    return run_execution_operation(
        project_root,
        task_store,
        operation_store,
        config,
        operation_id=operation_id,
        provider=provider,
        local_coder_provider=local_coder_provider,
        frontier_coder_provider=frontier_coder_provider,
    )


__all__ = [
    "execute_execution_operation",
    "prepare_execution_operation",
    "run_execution_operation",
]
