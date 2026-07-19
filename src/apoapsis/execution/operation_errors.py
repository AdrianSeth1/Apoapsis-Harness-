from __future__ import annotations


class ExecutionOperationError(RuntimeError):
    """Base error for every durable post-approval execution operation."""


class DuplicateExecutionOperationError(ExecutionOperationError):
    """Raised when an operation_id has already been submitted and reached
    a terminal status."""


class ActiveExecutionOperationExistsError(ExecutionOperationError):
    """Raised when a task already has a RECORDED or RUNNING execution
    operation -- only one active operation per task is ever permitted,
    mirroring the review/intake operation ledgers' own guarantee."""


class ExecutionOperationNotFoundError(ExecutionOperationError):
    """Raised when an operation_id is not present in the operation store."""


class ExecutionOperationAlreadyRunningError(ExecutionOperationError):
    """Raised when an operation is already RUNNING -- fail closed rather
    than silently repeating a call that may already have been transmitted
    to a provider before an earlier process crashed."""


class StaleExecutionStartError(ExecutionOperationError):
    """Raised when the task's version or the repository HEAD no longer
    match what was observed when this operation was recorded."""
