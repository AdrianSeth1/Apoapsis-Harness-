from __future__ import annotations


class ArchitectError(RuntimeError):
    """Base error for every Architect Mode operation."""


class PlanStoreError(ArchitectError):
    """Base error for persisted plan-store operations."""


class PlanNotFoundError(PlanStoreError):
    """Raised when a plan identifier is not present in the store."""


class ConcurrentPlanTransitionError(PlanStoreError):
    """Raised when optimistic plan-version validation fails."""


class InvalidPlanTransitionError(PlanStoreError):
    """Raised when a requested plan-status edge is not allowed."""


class PlanActionError(PlanStoreError):
    """Raised when a plan action's business rule is violated."""


class PlanImportError(ArchitectError):
    """Raised when an imported planner response fails to reconcile with its
    originating request package."""


class SliceExecutionError(ArchitectError):
    """Base error for every plan-slice execution operation (ADR 0027)."""


class SliceExecutionNotFoundError(SliceExecutionError):
    """Raised when no execution record exists for a given (plan_id,
    slice_id) pair."""


class ActiveSliceExecutionExistsError(SliceExecutionError):
    """Raised when the plan already has another slice execution in
    ``APPROVED``/``RUNNING`` status -- only one slice of a given plan may
    be actively executing at a time."""


class ConcurrentSliceExecutionTransitionError(SliceExecutionError):
    """Raised when optimistic slice-execution-record version validation
    fails."""


class SlicePackagingError(SliceExecutionError):
    """Raised when a slice cannot be safely packaged: the plan is not
    approved, its version does not match, it fails revalidation against
    current configuration, a referenced constraint/criterion cannot be
    recovered exactly, a dependency is not provably satisfied, or the
    repository is not identifiably the one the plan was built against."""


class SliceApprovalError(SliceExecutionError):
    """Raised when a slice cannot be approved: no matching package exists,
    the supplied package hash does not match, or the record is not in the
    expected status."""
