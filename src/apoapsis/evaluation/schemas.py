from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from apoapsis.reporting.report import FinalTaskReport
from apoapsis.specification.schema import StrictModel, utc_now
from apoapsis.verification.results import VerificationResult


class EvalLane(StrEnum):
    LOCAL = "local"
    HYBRID = "hybrid"
    FORCED_ESCALATION = "forced-escalation"
    FRONTIER = "frontier"
    ONE_SHOT = "one-shot"
    LOCAL_STRICT = "local-strict"


class EvalEvidenceKind(StrEnum):
    DETERMINISTIC_FAKE = "deterministic_fake"
    LIVE_LOCAL = "live_local"
    LIVE_HOSTED = "live_hosted"


class OracleStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    NOT_RUN = "not_run"


class HeldOutOracleResult(StrictModel):
    oracle_id: str = Field(min_length=1)
    oracle_version: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: OracleStatus
    duration_seconds: float = Field(default=0.0, ge=0)
    verification_result: VerificationResult | None = None
    reason: str | None = None
    audit_artifact: str | None = None


DEFAULT_LANE_ORDER: tuple[EvalLane, ...] = (
    EvalLane.LOCAL,
    EvalLane.HYBRID,
    EvalLane.FORCED_ESCALATION,
    EvalLane.FRONTIER,
    EvalLane.ONE_SHOT,
)
# `LOCAL_STRICT` is deliberately excluded from the default order: it is an
# opt-in lane (`--lane local-strict`) that measures the ADR 0015/0016/0017
# strict completion policy against a model-visible acceptance check, not a
# baseline-completion lane. Every lane in `DEFAULT_LANE_ORDER` above must
# stay explicitly `BASELINE` (`evaluation/lanes.py`) so historical
# false-success measurement remains comparable across runs.


class EvalLaneResult(StrictModel):
    lane: EvalLane
    fixture_path: str | None = None
    report: FinalTaskReport | None = None
    duration_seconds: float = Field(default=0.0, ge=0)
    skipped: bool = False
    skip_reason: str | None = None
    evidence_kind: EvalEvidenceKind = EvalEvidenceKind.DETERMINISTIC_FAKE
    patch_attempts: int = Field(default=0, ge=0)
    unsafe_patch_rejections: int = Field(default=0, ge=0)
    held_out_oracle: HeldOutOracleResult | None = None


class EvalComparisonReport(StrictModel):
    schema_version: str = "1.0"
    run_id: str = Field(min_length=1)
    generated_at: datetime = Field(default_factory=utc_now)
    fixture_source: str
    task_text: str
    context_profile: str | None = None
    lanes: list[EvalLaneResult] = Field(default_factory=list)


class MetricStatus(StrEnum):
    MEASURED = "measured"
    UNMEASURED = "unmeasured"


class RateMetric(StrictModel):
    status: MetricStatus
    numerator: int = Field(default=0, ge=0)
    denominator: int = Field(default=0, ge=0)
    value: float | None = Field(default=None, ge=0, le=1)
    reason: str | None = None

    @model_validator(mode="after")
    def validate_measurement_state(self) -> RateMetric:
        if self.status == MetricStatus.MEASURED:
            if self.denominator <= 0 or self.value is None:
                raise ValueError("a measured rate requires a positive denominator")
            expected = self.numerator / self.denominator
            if abs(self.value - expected) > 1e-12:
                raise ValueError("rate value does not match numerator/denominator")
        elif self.value is not None or not self.reason:
            raise ValueError("an unmeasured rate requires null value and a reason")
        return self


class DistributionMetric(StrictModel):
    status: MetricStatus
    sample_count: int = Field(default=0, ge=0)
    median: float | None = None
    p95: float | None = None
    unit: str = Field(min_length=1)
    reason: str | None = None

    @model_validator(mode="after")
    def validate_measurement_state(self) -> DistributionMetric:
        if self.status == MetricStatus.MEASURED:
            if self.sample_count <= 0 or self.median is None or self.p95 is None:
                raise ValueError("a measured distribution requires samples")
        elif self.median is not None or self.p95 is not None or not self.reason:
            raise ValueError(
                "an unmeasured distribution requires null values and a reason"
            )
        return self


class HostedSavingsMetrics(StrictModel):
    status: MetricStatus
    paired_comparisons: int = Field(default=0, ge=0)
    hosted_calls_avoided: int | None = None
    hosted_input_tokens_saved: int | None = None
    hosted_output_tokens_saved: int | None = None
    hosted_cost_saved_usd: float | None = None
    local_first_completion_rate: float | None = Field(default=None, ge=0, le=1)
    direct_frontier_completion_rate: float | None = Field(default=None, ge=0, le=1)
    completion_rate_delta: float | None = Field(default=None, ge=-1, le=1)
    local_first_median_latency_seconds: float | None = Field(default=None, ge=0)
    direct_frontier_median_latency_seconds: float | None = Field(default=None, ge=0)
    reason: str | None = None

    @model_validator(mode="after")
    def validate_measurement_state(self) -> HostedSavingsMetrics:
        values = (
            self.hosted_calls_avoided,
            self.hosted_input_tokens_saved,
            self.hosted_output_tokens_saved,
            self.hosted_cost_saved_usd,
            self.local_first_completion_rate,
            self.direct_frontier_completion_rate,
            self.completion_rate_delta,
            self.local_first_median_latency_seconds,
            self.direct_frontier_median_latency_seconds,
        )
        if self.status == MetricStatus.MEASURED:
            if self.paired_comparisons <= 0 or any(item is None for item in values):
                raise ValueError("measured hosted savings require paired values")
        elif any(item is not None for item in values) or not self.reason:
            raise ValueError(
                "unmeasured hosted savings require null values and a reason"
            )
        return self


class LocalOneShotComparison(StrictModel):
    status: MetricStatus
    paired_comparisons: int = Field(default=0, ge=0)
    local_completion_rate: float | None = Field(default=None, ge=0, le=1)
    one_shot_completion_rate: float | None = Field(default=None, ge=0, le=1)
    completion_rate_delta: float | None = Field(default=None, ge=-1, le=1)
    local_median_latency_seconds: float | None = Field(default=None, ge=0)
    one_shot_median_latency_seconds: float | None = Field(default=None, ge=0)
    reason: str | None = None

    @model_validator(mode="after")
    def validate_measurement_state(self) -> LocalOneShotComparison:
        values = (
            self.local_completion_rate,
            self.one_shot_completion_rate,
            self.completion_rate_delta,
            self.local_median_latency_seconds,
            self.one_shot_median_latency_seconds,
        )
        if self.status == MetricStatus.MEASURED:
            if self.paired_comparisons <= 0 or any(item is None for item in values):
                raise ValueError("measured local/one-shot comparison requires pairs")
        elif any(item is not None for item in values) or not self.reason:
            raise ValueError(
                "unmeasured local/one-shot comparison requires null values and a reason"
            )
        return self


class ContextProfileSummary(StrictModel):
    profile: str
    attempts: int = Field(ge=0)
    completion_rate: RateMetric
    context_tokens: DistributionMetric
    latency_seconds: DistributionMetric


class EvaluationAggregateReport(StrictModel):
    schema_version: str = "1.0"
    aggregate_id: str = Field(min_length=1)
    generated_at: datetime = Field(default_factory=utc_now)
    source_run_ids: list[str] = Field(default_factory=list)
    run_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    local_only_verified_completion_rate: RateMetric
    frontier_rescue_rate: RateMetric
    overall_verified_completion_rate: RateMetric
    human_review_rate: RateMetric
    unsafe_patch_rejection_rate: RateMetric
    false_success_rate: RateMetric
    oracle_infrastructure_errors: int = Field(default=0, ge=0)
    latency_seconds: DistributionMetric
    transmitted_files: DistributionMetric
    transmitted_lines: DistributionMetric
    hosted_savings: HostedSavingsMetrics
    local_vs_one_shot: LocalOneShotComparison
    context_profiles: list[ContextProfileSummary] = Field(default_factory=list)
