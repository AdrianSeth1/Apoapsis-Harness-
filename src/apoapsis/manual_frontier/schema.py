from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, Field

from apoapsis.review.schema import StopReasonKind
from apoapsis.agent.session import AgentSessionResult
from apoapsis.architect.slice_schema import PlanSliceExecutionPackage
from apoapsis.context.compiler import ContextPackage
from apoapsis.specification.schema import HardConstraint, StrictModel, TaskSpecification, utc_now
from apoapsis.verification.failures import NormalizedFailure
from apoapsis.verification.results import VerificationResult


class VerificationCatalogEntry(StrictModel):
    """One configured verification command, described for a manual
    subscription model exactly as it is for the automated acceptance
    catalog (ADR 0016) -- name, category, description, and whether it is
    acceptance-designated. Purely descriptive: the response envelope never
    lets a model select or invoke a command."""

    name: str
    category: str
    description: str = ""
    required: bool
    acceptance_designated: bool


class ManualFrontierHandoffPackage(StrictModel):
    """The immutable, hashed package a user manually uploads to a ChatGPT/
    Claude subscription session (ADR 0031). Bound to the exact task id and
    version, the exact worktree fingerprint, the approved specification and
    its active constraints, the current diff, relevant evidence/failures,
    the configured verification catalog, and the exact JSON response
    schema the model must answer with. Contains no secrets, no unrelated
    files, no held-out oracle content, and no audit-only private data --
    only what the existing automated escalation package already includes.

    ``package_sha256`` is computed over the full package payload excluding
    ``package_id`` and ``generated_at`` (the same exclude-the-fresh-
    identifiers convention ``execution.authorization`` uses, ADR 0026), so
    re-deriving the same package deterministically reproduces the same
    hash, and any tampering with the stored file is detectable.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    schema_version: Literal["1.0", "1.1"] = "1.1"
    package_id: str = Field(pattern=r"^MFH-[A-Za-z0-9._-]+$")
    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    task_version: int = Field(ge=1)
    repair_round: int = Field(default=0, ge=0)
    worktree_fingerprint: str = Field(min_length=1)
    repository_head_commit: str = Field(min_length=1)
    specification: TaskSpecification
    active_constraints: list[HardConstraint] = Field(default_factory=list)
    current_diff: str
    stop_reason_kind: StopReasonKind
    stop_reason_text: str
    normalized_failures: list[NormalizedFailure] = Field(default_factory=list)
    verification_results: list[VerificationResult] = Field(default_factory=list)
    repository_context: ContextPackage | None = None
    prior_agent_sessions: list[AgentSessionResult] = Field(default_factory=list)
    approved_slice_package: PlanSliceExecutionPackage | None = None
    verification_catalog: list[VerificationCatalogEntry] = Field(default_factory=list)
    response_schema: dict[str, object]
    authority_rules: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManualFrontierResponseEnvelope(StrictModel):
    """The strict, bounded response format the operator pastes back after
    the model produces it (ADR 0031). ``extra="forbid"`` rejects any field
    not listed here -- in particular there is no status/completion field,
    no command-selection field, and no budget field of any kind. The model
    cannot claim completion, choose a command, expand its own budget, or
    alter workflow state through this envelope; it can only propose one
    bounded unified-diff patch and a free-text summary, both of which
    Apoapsis alone validates, previews, and (only after explicit two-step
    user approval) applies and verifies.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    schema_version: Literal["1.0"] = "1.0"
    package_id: str = Field(pattern=r"^MFH-[A-Za-z0-9._-]+$")
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    task_version: int = Field(ge=1)
    patch: str = Field(min_length=1, max_length=2_000_000)
    summary: str = Field(default="", max_length=20_000)


class ManualFrontierPreviewStatus(StrEnum):
    PREVIEWED = "previewed"
    APPROVED = "approved"
    APPLIED = "applied"
    SUPERSEDED = "superseded"


class ManualFrontierPreviewRecord(StrictModel):
    """The durable record of one imported-and-validated (but not yet
    applied) manual-frontier response (ADR 0031). Import only ever creates
    this preview; nothing about applying the patch happens until a
    separate, explicit two-step approval (``approved`` here) and then a
    real ``MANUAL_FRONTIER_HANDOFF`` review operation."""

    preview_id: str = Field(pattern=r"^MFPV-[A-Za-z0-9._-]+$")
    package_id: str = Field(pattern=r"^MFH-[A-Za-z0-9._-]+$")
    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    task_version_at_import: int = Field(ge=1)
    worktree_fingerprint_at_import: str = Field(min_length=1)
    declared_model_name: str = Field(min_length=1)
    patch: str
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    changed_lines: int = Field(default=0, ge=0)
    status: ManualFrontierPreviewStatus
    created_at: datetime
    approved_at: datetime | None = None


__all__ = [
    "VerificationCatalogEntry",
    "ManualFrontierHandoffPackage",
    "ManualFrontierResponseEnvelope",
    "ManualFrontierPreviewStatus",
    "ManualFrontierPreviewRecord",
]
