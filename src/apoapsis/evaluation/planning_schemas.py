from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from apoapsis.evaluation.schemas import (
    DistributionMetric,
    EvalEvidenceKind,
    HeldOutOracleResult,
    MetricStatus,
    RateMetric,
)
from apoapsis.reporting.report import FinalTaskReport
from apoapsis.specification.schema import StrictModel, utc_now


class PlannerMethod(StrEnum):
    MANUAL_SUBSCRIPTION_PASTE = "manual_subscription_paste"
    # Deterministic fake-provider tests only -- a fake plan authored by this
    # framework's own tests, never accepted as live evidence of anything.
    FRAMEWORK_FAKE = "framework_fake"


class PlannerProvenance(StrictModel):
    """How the approved plan behind a planned-condition attempt was
    produced -- recorded separately from the coding model used to execute
    its slices, per ADR 0028. This framework never generates a plan itself:
    it only ever accepts an already-approved ``plan_id``/``plan_version``
    and records exactly where that plan came from.

    ``planner_tokens_status`` is deliberately ``UNMEASURED`` (never a
    fabricated zero) for a manually-pasted subscription session, since no
    API telemetry exists for it."""

    package_id: str = Field(pattern=r"^PKG-[A-Za-z0-9._-]+$")
    plan_id: str = Field(pattern=r"^PLAN-[A-Za-z0-9._-]+$")
    plan_version: int = Field(ge=1)
    request_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    planner_model: str = Field(min_length=1)
    planner_method: PlannerMethod
    planner_tokens_status: MetricStatus
    planner_input_tokens: int | None = Field(default=None, ge=0)
    planner_output_tokens: int | None = Field(default=None, ge=0)
    planner_cost_usd: float | None = Field(default=None, ge=0)
    reason: str | None = None

    @model_validator(mode="after")
    def validate_token_state(self) -> "PlannerProvenance":
        values = (
            self.planner_input_tokens,
            self.planner_output_tokens,
            self.planner_cost_usd,
        )
        if self.planner_tokens_status == MetricStatus.MEASURED:
            if any(item is None for item in values):
                raise ValueError("measured planner tokens/cost require every value")
        elif any(item is not None for item in values) or not self.reason:
            raise ValueError(
                "unmeasured planner tokens/cost require null values and a reason"
            )
        return self


class SliceAttemptResult(StrictModel):
    """One slice's own attempt inside a planned-condition run -- the
    per-slice equivalent of an evaluation lane result, tagged with the
    slice id and its declared dependencies rather than a lane name."""

    slice_id: str = Field(pattern=r"^SLICE-[A-Za-z0-9._-]+$")
    dependencies: list[str] = Field(default_factory=list)
    attempted: bool
    report: FinalTaskReport | None = None
    patch_attempts: int = Field(default=0, ge=0)
    unsafe_patch_rejections: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0)
    skip_reason: str | None = None


class PlannedConditionResult(StrictModel):
    """The planned condition's whole attempt: one or more slices advanced
    strictly in dependency order, each through the exact, unmodified D3a
    package/approve/start functions -- auto-advance happens only inside
    this evaluation-only driver, gated by an explicit flag, and stops the
    moment a slice fails to reach ``COMPLETE`` (no auto-repair)."""

    scenario_id: str = Field(min_length=1)
    scenario_version: str = Field(min_length=1)
    planner: PlannerProvenance
    slices: list[SliceAttemptResult] = Field(default_factory=list)
    all_slices_complete: bool
    stopped_at_slice_id: str | None = None
    merged_repository_path: str | None = None
    held_out_oracle: HeldOutOracleResult | None = None
    integration_failure: bool = False
    duration_seconds: float = Field(default=0.0, ge=0)
    evidence_kind: EvalEvidenceKind = EvalEvidenceKind.DETERMINISTIC_FAKE


class MonolithicConditionResult(StrictModel):
    """The same task, attempted as a single request -- a thin wrapper
    around the existing evaluation lane mechanics (`run_eval_lane`),
    tagged with the scenario id/version this comparison used."""

    scenario_id: str = Field(min_length=1)
    scenario_version: str = Field(min_length=1)
    report: FinalTaskReport | None = None
    patch_attempts: int = Field(default=0, ge=0)
    unsafe_patch_rejections: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0)
    held_out_oracle: HeldOutOracleResult | None = None
    evidence_kind: EvalEvidenceKind = EvalEvidenceKind.DETERMINISTIC_FAKE


class PlanningComparisonReport(StrictModel):
    """One byte-identical-fixture-copy comparison: the same coding model,
    quantization, context profile, inference settings, completion policy,
    verification backend, and total authorized per-attempt budget, run
    once monolithically and once through the approved fixed plan."""

    schema_version: str = "1.0"
    run_id: str = Field(min_length=1)
    generated_at: datetime = Field(default_factory=utc_now)
    scenario_id: str = Field(min_length=1)
    scenario_version: str = Field(min_length=1)
    task_text: str = Field(min_length=1)
    coding_model: str = Field(min_length=1)
    context_profile: str | None = None
    monolithic: MonolithicConditionResult
    planned: PlannedConditionResult


class SliceMetrics(StrictModel):
    slice_id: str = Field(pattern=r"^SLICE-[A-Za-z0-9._-]+$")
    attempts: int = Field(ge=0)
    completion_rate: RateMetric
    human_review_rate: RateMetric


class ConditionMetrics(StrictModel):
    """One condition's (monolithic or planned) rollup across every
    persisted comparison report -- computed only from already-persisted
    `FinalTaskReport` fields, never from a fresh model call."""

    attempts: int = Field(ge=0)
    true_completion_rate: RateMetric
    false_success_rate: RateMetric
    human_review_rate: RateMetric
    policy_rejection_rate: RateMetric
    latency_seconds: DistributionMetric
    provider_calls: DistributionMetric
    local_agent_turns: DistributionMetric
    frontier_agent_turns: DistributionMetric
    patch_attempts: DistributionMetric
    verification_runs: DistributionMetric
    input_tokens: DistributionMetric
    output_tokens: DistributionMetric
    cached_input_tokens: DistributionMetric
    estimated_cost_usd: DistributionMetric
    context_files: DistributionMetric
    context_lines: DistributionMetric
    context_tokens: DistributionMetric


class PlanningEvaluationSummary(StrictModel):
    """Cross-run rollup of one or more `PlanningComparisonReport`s for a
    single scenario/version, aggregated without any model call. Never
    combines reports from different `scenario_id`/`scenario_version`
    values, so an extended fixture's evidence is never silently mixed
    with a different scenario's."""

    schema_version: str = "1.0"
    summary_id: str = Field(min_length=1)
    generated_at: datetime = Field(default_factory=utc_now)
    source_run_ids: list[str] = Field(default_factory=list)
    scenario_id: str = Field(min_length=1)
    scenario_version: str = Field(min_length=1)
    monolithic: ConditionMetrics
    planned: ConditionMetrics
    planned_integration_failure_rate: RateMetric
    per_slice: list[SliceMetrics] = Field(default_factory=list)
