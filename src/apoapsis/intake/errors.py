from __future__ import annotations


class IntakeError(RuntimeError):
    """Base error for every durable new-task intake operation."""


class DuplicateIntakeOperationError(IntakeError):
    """Raised when an operation_id has already been submitted and reached
    a terminal status."""


class ActiveIntakeOperationExistsError(IntakeError):
    """Raised when a task already has a RECORDED or RUNNING intake
    operation -- only one active operation per task is ever permitted,
    mirroring the review-operation ledger's own guarantee (ADR 0021)."""


class IntakeOperationNotFoundError(IntakeError):
    """Raised when an operation_id is not present in the operation store."""


class IntakeOperationAlreadyRunningError(IntakeError):
    """Raised when an operation is already RUNNING -- fail closed rather
    than silently repeating a call that may already have been transmitted
    to a provider before an earlier process crashed."""
