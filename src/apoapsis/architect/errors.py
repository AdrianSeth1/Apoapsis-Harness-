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
