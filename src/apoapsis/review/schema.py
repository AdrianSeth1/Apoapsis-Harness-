from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict, Field

from apoapsis.config import AgentLoopConfig
from apoapsis.specification.schema import (
    HardConstraint,
    StrictModel,
    TaskSpecification,
    utc_now,
)
from apoapsis.verification.failures import NormalizedFailure
from apoapsis.verification.results import VerificationResult
from apoapsis.workflow.acceptance import AcceptanceCoverage
from apoapsis.workflow.states import WorkflowState


class StopReasonKind(StrEnum):
    """The deterministic classification of why a task is at
    HUMAN_REVIEW_REQUIRED, derived from its own persisted event history --
    never from anything a model claims."""

    SPECIFICATION_NOT_APPROVED = "specification_not_approved"
    ROUTING_REQUIRES_HUMAN = "routing_requires_human"
    ACCEPTANCE_COVERAGE_INCOMPLETE = "acceptance_coverage_incomplete"
    VERIFICATION_FAILED = "verification_failed"
    LOCAL_AGENT_ESCALATION_UNAVAILABLE = "local_agent_escalation_unavailable"
    FRONTIER_AGENT_EXHAUSTED = "frontier_agent_exhausted"
    UNKNOWN = "unknown"


class ReviewActionKind(StrEnum):
    INSPECT_ONLY = "inspect_only"
    ABANDON = "abandon"
    VERIFICATION_ONLY_RETRY = "verification_only_retry"
    LOCAL_CONTINUATION = "local_continuation"
    FRONTIER_CONTINUATION = "frontier_continuation"


class ReviewOperationStatus(StrEnum):
    RECORDED = "recorded"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # A RUNNING operation whose owning process appears to have died before
    # reaching SUCCEEDED/FAILED (ADR 0021). Terminal and inspectable, but
    # never automatically repeated: whether a model call was transmitted
    # before the process died is genuinely unknown.
    AMBIGUOUS = "ambiguous"


class ContinuationBudget(StrictModel):
    """The one user-authorized number per continuation. The same delta is
    added to the resumed agent's turn, patch-attempt, and verification-run
    ceilings together (ADR 0020) -- never just turns alone, which could
    leave a session unable to ever apply or verify a patch again."""

    additional_turns: int = Field(ge=1)


class ReviewCase(StrictModel):
    """A deterministic, harness-computed projection of one task currently
    stopped at HUMAN_REVIEW_REQUIRED. Every field is derived from
    persisted task state, workflow events, the final report (if any),
    audit artifacts, current worktree/repository state, and configuration
    -- never from anything a model claims."""

    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    task_version: int = Field(ge=1)
    workflow_state: WorkflowState
    stop_reason_kind: StopReasonKind
    stop_reason_text: str = Field(min_length=1)
    stop_event_type: str = Field(min_length=1)
    objective_text: str = ""
    worktree_path: str | None = None
    worktree_exists: bool = False
    worktree_fingerprint: str | None = None
    repository_head_commit: str | None = None
    active_hard_constraints: list[HardConstraint] = Field(default_factory=list)
    current_diff: str | None = None
    verification_results: list[VerificationResult] = Field(default_factory=list)
    acceptance_coverage: list[AcceptanceCoverage] = Field(default_factory=list)
    normalized_failures: list[NormalizedFailure] = Field(default_factory=list)
    models_used: list[str] = Field(default_factory=list)
    consumed_local_turns: int = Field(default=0, ge=0)
    consumed_local_patch_attempts: int = Field(default=0, ge=0)
    consumed_local_verification_runs: int = Field(default=0, ge=0)
    configured_local_budget: AgentLoopConfig | None = None
    consumed_frontier_turns: int = Field(default=0, ge=0)
    consumed_frontier_patch_attempts: int = Field(default=0, ge=0)
    consumed_frontier_verification_runs: int = Field(default=0, ge=0)
    configured_frontier_budget: AgentLoopConfig | None = None
    frontier_available: bool = False
    continuations_used: int = Field(default=0, ge=0)
    max_continuations_per_task: int = Field(ge=1)
    max_additional_turns_per_continuation: int = Field(ge=1)
    eligible_actions: list[ReviewActionKind] = Field(default_factory=list)
    audit_artifact_locations: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)


class ReviewOperationRecord(StrictModel):
    """The durable, authoritative record of one human-review operation
    (ADR 0021). Carries everything needed to execute it -- a worker never
    needs anything but ``operation_id`` to reload the rest."""

    operation_id: str = Field(pattern=r"^RVOP-[A-Za-z0-9._-]+$")
    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    action: ReviewActionKind
    expected_task_version: int = Field(ge=1)
    expected_worktree_fingerprint: str | None = None
    authorized_budget: ContinuationBudget | None = None
    status: ReviewOperationStatus
    created_at: datetime
    updated_at: datetime
    result_summary: str | None = None
    error: str | None = None


class ReviewContinuationPackage(StrictModel):
    """The immutable record written before any resumed model call (ADR
    0020) -- mirrors the "package written before it leaves Apoapsis"
    discipline already used by Architect Mode's ``PlannerRequestPackage``
    and the existing frontier ``EscalationPackage``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    schema_version: str = "1.0"
    operation_id: str = Field(pattern=r"^RVOP-[A-Za-z0-9._-]+$")
    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    action: ReviewActionKind
    specification: TaskSpecification
    active_constraints: list[HardConstraint] = Field(default_factory=list)
    current_diff: str
    stop_reason_kind: StopReasonKind
    stop_reason_text: str
    prior_turn_count: int = Field(ge=0)
    normalized_failures: list[NormalizedFailure] = Field(default_factory=list)
    verification_catalog: list[str] = Field(default_factory=list)
    authorized_budget: ContinuationBudget
    effective_agent_budget: AgentLoopConfig
    worktree_fingerprint: str = Field(min_length=1)
    repository_head_commit: str = Field(min_length=1)
    generated_at: datetime = Field(default_factory=utc_now)
