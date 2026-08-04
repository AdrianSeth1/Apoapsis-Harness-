from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from apoapsis.config import AgentLoopConfig, AgentRoute, CompletionPolicy, ExecutionMode
from apoapsis.context.measurement import ContextAttribution, ContextMeasurement
from apoapsis.models.base import ConstraintCoverage
from apoapsis.models.telemetry import ProviderCallTelemetry, RelayObservedModelUsage
from apoapsis.research.schemas import ResearchMode, ResearchTelemetry
from apoapsis.specification.schema import StrictModel
from apoapsis.verification.contract import VerificationContractAssessment
from apoapsis.verification.results import VerificationResult
from apoapsis.workflow.acceptance import AcceptanceCoverage


class TaskOutcome(StrEnum):
    COMPLETE = "complete"
    FAILED = "failed"
    HUMAN_REVIEW_REQUIRED = "human_review_required"


class ModelIdentity(StrictModel):
    provider: str
    model: str


class TransmittedExcerpt(StrictModel):
    call_number: int = Field(ge=1)
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    lines: int = Field(ge=0)
    content_sha256: str


class FinalTaskReport(StrictModel):
    schema_version: str = "1.0"
    task_id: str
    outcome: TaskOutcome
    error: str | None = None
    worktree_path: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.ONE_SHOT
    agent_route: AgentRoute | None = None
    agent_turns: int = Field(default=0, ge=0)
    agent_patch_attempts: int = Field(default=0, ge=0)
    agent_verification_runs: int = Field(default=0, ge=0)
    agent_stop_reason: str | None = None
    local_agent_turns: int = Field(default=0, ge=0)
    frontier_agent_turns: int = Field(default=0, ge=0)
    frontier_agent_patch_attempts: int = Field(default=0, ge=0)
    frontier_agent_verification_runs: int = Field(default=0, ge=0)
    escalation_triggered: bool = False
    escalation_reason: str | None = None
    escalation_package_path: str | None = None
    constraint_coverage: list[ConstraintCoverage] = Field(default_factory=list)
    models_used: list[ModelIdentity] = Field(default_factory=list)
    provider_calls: list[ProviderCallTelemetry] = Field(default_factory=list)
    number_of_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0)
    latency_seconds: float = Field(ge=0)
    transmitted_excerpts: list[TransmittedExcerpt] = Field(default_factory=list)
    transmitted_files: int = Field(ge=0)
    transmitted_lines: int = Field(ge=0)
    files_changed: list[str] = Field(default_factory=list)
    verification_results: list[VerificationResult] = Field(default_factory=list)
    audit_artifact_locations: list[str] = Field(default_factory=list)
    research_triggered: bool = False
    research_mode: ResearchMode = ResearchMode.OFF
    research_patterns: list[str] = Field(default_factory=list)
    research_evidence_in_frontier_request: list[str] = Field(default_factory=list)
    research_influenced_plan: bool = False
    research_audit_directory: str | None = None
    research_telemetry: ResearchTelemetry | None = None
    context_measurements: list[ContextMeasurement] = Field(default_factory=list)
    context_attribution: ContextAttribution | None = None
    completion_policy: CompletionPolicy = CompletionPolicy.BASELINE
    acceptance_coverage: list[AcceptanceCoverage] = Field(default_factory=list)
    # ADR 0069: how much the configured contract could prove, recorded
    # alongside the outcome it qualifies. Optional so that reports written
    # before this field existed still validate.
    verification_contract: VerificationContractAssessment | None = None
    local_agent_budget: AgentLoopConfig | None = None
    frontier_agent_budget: AgentLoopConfig | None = None
    frontier_available: bool = False
    rejected_tool_requests: int = Field(default=0, ge=0)
    #: Token usage for model traffic the harness did not issue itself -- the
    #: contained agent's own calls, observed at the relay. Its totals are
    #: *included* in `input_tokens`/`output_tokens`/`cached_input_tokens`
    #: above, and broken out here so a reader can always tell how much of a
    #: total came from harness-issued provider calls (`provider_calls`, one
    #: record each) and how much from a contained agent's traffic (summarised,
    #: because the harness never held those prompts). `None` means this task
    #: ran no contained agent, which is not the same as one that spent nothing.
    local_model_usage: RelayObservedModelUsage | None = None

    @model_validator(mode="after")
    def validate_totals(self) -> FinalTaskReport:
        if self.number_of_calls != len(self.provider_calls):
            raise ValueError("number_of_calls must match provider_calls")
        return self
