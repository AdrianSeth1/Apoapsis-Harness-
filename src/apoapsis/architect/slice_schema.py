from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict, Field

from apoapsis.architect.schema import ArchitectureDecision
from apoapsis.specification.schema import (
    AcceptanceCriterion,
    HardConstraint,
    RiskLevel,
    StrictModel,
    TaskSpecification,
    utc_now,
)


class SliceExecutionStatus(StrEnum):
    """The durable lifecycle of one plan slice's execution record (ADR
    0027), mirroring the review/intake/execution operation ledgers'
    RECORDED-like progression: ``PACKAGED`` means an immutable execution
    package has been deterministically compiled and written to the audit
    area, but no task exists yet; ``APPROVED`` means a human explicitly
    approved that exact package, and the derived task now exists at
    ``SPEC_APPROVED``; ``RUNNING``/``COMPLETE``/``HUMAN_REVIEW``/``FAILED``
    mirror the derived task's own state, read back from the normal,
    unmodified workflow/execution-operation machinery -- never invented or
    tracked independently. ``SUPERSEDED`` marks a record whose plan was
    revised to a new version after this slice was packaged or approved;
    it is never silently reused against the new version."""

    PACKAGED = "packaged"
    APPROVED = "approved"
    RUNNING = "running"
    COMPLETE = "complete"
    HUMAN_REVIEW = "human_review"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class DependencyEvidence(StrictModel):
    """Deterministic proof (or disproof) that one dependency has a real
    completed task worktree and resolvable branch tip that packaging can
    checkpoint and inherit. Status alone is insufficient because the
    worktree and commit are resolved independently."""

    slice_id: str = Field(pattern=r"^SLICE-[A-Za-z0-9._-]+$")
    satisfied: bool
    reason: str = Field(min_length=1)
    dependency_task_id: str | None = None
    dependency_branch: str | None = None
    dependency_commit: str | None = None


class PlanSliceExecutionPackage(StrictModel):
    """An immutable, hashed record of exactly what approving one plan
    slice would authorize (ADR 0027) -- deterministically compiled from
    the approved plan's own content, current configuration, and current
    repository/dependency state, with zero model calls or repository
    mutations. Never lets a model reinterpret or weaken what was
    approved: hard constraints and acceptance criteria are copied
    verbatim from the plan's own records, never re-derived."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    schema_version: str = "1.0"
    package_id: str = Field(pattern=r"^SXP-[A-Za-z0-9._-]+$")
    plan_id: str = Field(pattern=r"^PLAN-[A-Za-z0-9._-]+$")
    plan_version: int = Field(ge=1)
    # Accepts both planning origins' package id shapes: Architect Mode's
    # own ``apoapsis plan export`` (``PKG-...``) and the discovery-to-
    # frontier-planning handoff's package (``FPKG-...``, ADR 0032) -- a
    # plan's ``ArchitecturePlan`` shape is identical either way, only the
    # originating request package's id prefix differs.
    plan_package_id: str = Field(pattern=r"^(PKG|FPKG)-[A-Za-z0-9._-]+$")
    slice_id: str = Field(pattern=r"^SLICE-[A-Za-z0-9._-]+$")
    idea_text: str = Field(min_length=1)
    architecture_summary: str = Field(min_length=1)
    relevant_decisions: list[ArchitectureDecision] = Field(default_factory=list)
    interface_contracts: list[str] = Field(default_factory=list)
    objective: str = Field(min_length=1)
    exclusions: list[str] = Field(default_factory=list)
    inherited_hard_constraints: list[HardConstraint] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    dependency_evidence: list[DependencyEvidence] = Field(default_factory=list)
    integration_assumptions: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.UNCLASSIFIED
    stop_conditions: list[str] = Field(default_factory=list)
    local_model_fit_rationale: str = Field(min_length=1)
    work_brief: str = Field(min_length=1)
    advisory_suggested_paths: list[str] = Field(default_factory=list)
    advisory_suggested_symbols: list[str] = Field(default_factory=list)
    advisory_context_seeds: list[str] = Field(default_factory=list)
    repository_root: str = Field(min_length=1)
    repository_head_commit: str = Field(min_length=1)
    repository_fingerprint: str = Field(min_length=1)
    # Optional only for backward-compatible reading of packages written
    # before ADR 0039. Newly built packages always populate this field.
    execution_base_commit: str | None = Field(default=None, min_length=1)
    inherited_slice_ids: list[str] = Field(default_factory=list)
    derived_specification: TaskSpecification
    generated_at: datetime = Field(default_factory=utc_now)
    # Filled in after the rest of the package is built, excluded from its
    # own hash input along with ``generated_at`` -- see
    # ``build_plan_slice_execution_package``.
    package_sha256: str = ""


class PlanSliceExecutionRecord(StrictModel):
    """The durable, authoritative record of one plan slice's execution
    attempt. At most one record per ``(plan_id, slice_id)`` pair; at most
    one record per ``plan_id`` may be in ``APPROVED``/``RUNNING`` at a
    time (checked atomically by the store), so only one slice of a given
    plan is ever actively executing."""

    plan_id: str = Field(pattern=r"^PLAN-[A-Za-z0-9._-]+$")
    slice_id: str = Field(pattern=r"^SLICE-[A-Za-z0-9._-]+$")
    plan_version: int = Field(ge=1)
    status: SliceExecutionStatus
    package_sha256: str | None = None
    task_id: str | None = None
    task_expected_version: int | None = None
    execution_operation_id: str | None = None
    error: str | None = None
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


__all__ = [
    "DependencyEvidence",
    "PlanSliceExecutionPackage",
    "PlanSliceExecutionRecord",
    "SliceExecutionStatus",
]
