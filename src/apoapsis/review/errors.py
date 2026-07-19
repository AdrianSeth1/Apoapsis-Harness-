from __future__ import annotations


class ReviewError(RuntimeError):
    """Base error for every human-review/resume operation."""


class ReviewCaseError(ReviewError):
    """Raised when a review case cannot be projected for a task."""


class InvalidReviewActionError(ReviewError):
    """Raised when a requested action is not in the case's eligible set."""


class ConcurrentReviewTransitionError(ReviewError):
    """Raised when the task's version no longer matches what was expected."""


class WorktreeChangedError(ReviewError):
    """Raised when the worktree fingerprint no longer matches what was
    shown to the caller before authorizing this action."""


class ContinuationCeilingExceededError(ReviewError):
    """Raised when a continuation would exceed a configured ceiling."""


class DuplicateOperationError(ReviewError):
    """Raised when an operation_id has already been submitted."""


class ActiveOperationExistsError(ReviewError):
    """Raised when a task already has a RECORDED or RUNNING operation --
    only one active operation per task is ever permitted (ADR 0021)."""


class OperationNotFoundError(ReviewError):
    """Raised when an operation_id is not present in the operation store."""


class OperationAlreadyRunningError(ReviewError):
    """Raised when an operation is already RUNNING -- fail closed rather
    than silently repeating a call that may already have been transmitted
    to a provider before an earlier process crashed."""


class FrontierUnavailableError(ReviewError):
    """Raised when frontier continuation is requested but no frontier
    coder is configured."""
