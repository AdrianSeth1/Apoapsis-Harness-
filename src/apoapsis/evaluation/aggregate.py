from __future__ import annotations

import math
import statistics
from collections import defaultdict

from apoapsis.evaluation.schemas import (
    ContextProfileSummary,
    DistributionMetric,
    EvalComparisonReport,
    EvalEvidenceKind,
    EvalLane,
    EvalLaneResult,
    EvaluationAggregateReport,
    HostedSavingsMetrics,
    LocalOneShotComparison,
    MetricStatus,
    OracleStatus,
    RateMetric,
)
from apoapsis.models.provider import ModelRole
from apoapsis.reporting.report import FinalTaskReport, TaskOutcome


_LOCAL_FIRST_LANES = {
    EvalLane.LOCAL,
    EvalLane.HYBRID,
    EvalLane.FORCED_ESCALATION,
}


def _rate(numerator: int, denominator: int, reason: str) -> RateMetric:
    if denominator <= 0:
        return RateMetric(
            status=MetricStatus.UNMEASURED,
            numerator=numerator,
            denominator=denominator,
            value=None,
            reason=reason,
        )
    return RateMetric(
        status=MetricStatus.MEASURED,
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator,
    )


def _distribution(
    values: list[float | int], *, unit: str, reason: str
) -> DistributionMetric:
    if not values:
        return DistributionMetric(
            status=MetricStatus.UNMEASURED,
            sample_count=0,
            median=None,
            p95=None,
            unit=unit,
            reason=reason,
        )
    ordered = sorted(float(value) for value in values)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return DistributionMetric(
        status=MetricStatus.MEASURED,
        sample_count=len(ordered),
        median=float(statistics.median(ordered)),
        p95=ordered[p95_index],
        unit=unit,
    )


def _attempts(report: EvalComparisonReport) -> list[EvalLaneResult]:
    return [
        item for item in report.lanes if not item.skipped and item.report is not None
    ]


def _frontier_usage(report: FinalTaskReport) -> tuple[int, int, int, float]:
    calls = [
        item
        for item in report.provider_calls
        if item.role == ModelRole.FRONTIER_CODING_AGENT
    ]
    return (
        len(calls),
        sum(item.input_tokens for item in calls),
        sum(item.output_tokens for item in calls),
        sum(item.estimated_cost_usd for item in calls),
    )


def _lane(
    comparison: EvalComparisonReport, lane: EvalLane
) -> EvalLaneResult | None:
    return next(
        (
            item
            for item in comparison.lanes
            if item.lane == lane and not item.skipped and item.report is not None
        ),
        None,
    )


def _hosted_savings(
    comparisons: list[EvalComparisonReport],
) -> HostedSavingsMetrics:
    pairs: list[tuple[EvalLaneResult, EvalLaneResult]] = []
    for comparison in comparisons:
        direct = _lane(comparison, EvalLane.FRONTIER)
        if direct is None or direct.evidence_kind != EvalEvidenceKind.LIVE_HOSTED:
            continue
        local_first = next(
            (
                candidate
                for lane in (EvalLane.LOCAL, EvalLane.HYBRID, EvalLane.FORCED_ESCALATION)
                if (candidate := _lane(comparison, lane)) is not None
                and candidate.evidence_kind
                in {EvalEvidenceKind.LIVE_LOCAL, EvalEvidenceKind.LIVE_HOSTED}
            ),
            None,
        )
        if local_first is not None:
            pairs.append((local_first, direct))
    if not pairs:
        return HostedSavingsMetrics(
            status=MetricStatus.UNMEASURED,
            reason=(
                "no paired real hosted-frontier and local-first runs exist for "
                "an identical task; fake-provider runs are not measurement evidence"
            ),
        )

    calls_saved = input_saved = output_saved = 0
    cost_saved = 0.0
    local_complete = direct_complete = 0
    local_latency: list[float] = []
    direct_latency: list[float] = []
    for local_result, direct_result in pairs:
        assert local_result.report is not None
        assert direct_result.report is not None
        local_usage = _frontier_usage(local_result.report)
        direct_usage = _frontier_usage(direct_result.report)
        calls_saved += direct_usage[0] - local_usage[0]
        input_saved += direct_usage[1] - local_usage[1]
        output_saved += direct_usage[2] - local_usage[2]
        cost_saved += direct_usage[3] - local_usage[3]
        local_complete += local_result.report.outcome == TaskOutcome.COMPLETE
        direct_complete += direct_result.report.outcome == TaskOutcome.COMPLETE
        local_latency.append(local_result.report.latency_seconds)
        direct_latency.append(direct_result.report.latency_seconds)

    pair_count = len(pairs)
    local_rate = local_complete / pair_count
    direct_rate = direct_complete / pair_count
    return HostedSavingsMetrics(
        status=MetricStatus.MEASURED,
        paired_comparisons=pair_count,
        hosted_calls_avoided=calls_saved,
        hosted_input_tokens_saved=input_saved,
        hosted_output_tokens_saved=output_saved,
        hosted_cost_saved_usd=cost_saved,
        local_first_completion_rate=local_rate,
        direct_frontier_completion_rate=direct_rate,
        completion_rate_delta=local_rate - direct_rate,
        local_first_median_latency_seconds=float(statistics.median(local_latency)),
        direct_frontier_median_latency_seconds=float(
            statistics.median(direct_latency)
        ),
    )


def _local_vs_one_shot(
    comparisons: list[EvalComparisonReport],
) -> LocalOneShotComparison:
    pairs: list[tuple[FinalTaskReport, FinalTaskReport]] = []
    for comparison in comparisons:
        local = _lane(comparison, EvalLane.LOCAL)
        one_shot = _lane(comparison, EvalLane.ONE_SHOT)
        if local is not None and one_shot is not None:
            assert local.report is not None and one_shot.report is not None
            pairs.append((local.report, one_shot.report))
    if not pairs:
        return LocalOneShotComparison(
            status=MetricStatus.UNMEASURED,
            reason="no comparison contains both local and one-shot lanes",
        )
    count = len(pairs)
    local_rate = sum(
        item[0].outcome == TaskOutcome.COMPLETE for item in pairs
    ) / count
    one_shot_rate = sum(
        item[1].outcome == TaskOutcome.COMPLETE for item in pairs
    ) / count
    return LocalOneShotComparison(
        status=MetricStatus.MEASURED,
        paired_comparisons=count,
        local_completion_rate=local_rate,
        one_shot_completion_rate=one_shot_rate,
        completion_rate_delta=local_rate - one_shot_rate,
        local_median_latency_seconds=float(
            statistics.median(item[0].latency_seconds for item in pairs)
        ),
        one_shot_median_latency_seconds=float(
            statistics.median(item[1].latency_seconds for item in pairs)
        ),
    )


def aggregate_evaluations(
    comparisons: list[EvalComparisonReport], *, aggregate_id: str
) -> EvaluationAggregateReport:
    """Aggregate persisted comparison reports without invoking a provider.

    Fake-provider reports exercise formulas but can never populate the metrics
    that explicitly require live hosted evidence.
    """

    attempts = [item for comparison in comparisons for item in _attempts(comparison)]
    local_first = [item for item in attempts if item.lane in _LOCAL_FIRST_LANES]
    local_only_successes = sum(
        item.report is not None
        and item.report.outcome == TaskOutcome.COMPLETE
        and not item.report.escalation_triggered
        for item in local_first
    )
    hosted_escalations = [
        item
        for item in attempts
        if item.evidence_kind == EvalEvidenceKind.LIVE_HOSTED
        and item.report is not None
        and item.report.escalation_triggered
    ]
    hosted_rescues = sum(
        item.report is not None and item.report.outcome == TaskOutcome.COMPLETE
        for item in hosted_escalations
    )
    completions = sum(
        item.report is not None and item.report.outcome == TaskOutcome.COMPLETE
        for item in attempts
    )
    human_reviews = sum(
        item.report is not None
        and item.report.outcome == TaskOutcome.HUMAN_REVIEW_REQUIRED
        for item in attempts
    )
    patch_attempts = sum(item.patch_attempts for item in attempts)
    unsafe_rejections = sum(item.unsafe_patch_rejections for item in attempts)

    oracle_evaluated = [
        item
        for item in attempts
        if item.report is not None
        and item.report.outcome == TaskOutcome.COMPLETE
        and item.held_out_oracle is not None
        and item.held_out_oracle.status in {OracleStatus.PASSED, OracleStatus.FAILED}
    ]
    false_successes = sum(
        item.report is not None
        and item.report.outcome == TaskOutcome.COMPLETE
        and item.held_out_oracle is not None
        and item.held_out_oracle.status == OracleStatus.FAILED
        for item in oracle_evaluated
    )
    oracle_infrastructure_errors = sum(
        item.held_out_oracle is not None
        and item.held_out_oracle.status == OracleStatus.INFRASTRUCTURE_ERROR
        for item in attempts
    )

    profile_attempts: dict[str, list[EvalLaneResult]] = defaultdict(list)
    for comparison in comparisons:
        profile_attempts[comparison.context_profile or "configured"].extend(
            _attempts(comparison)
        )
    profile_summaries: list[ContextProfileSummary] = []
    for profile, items in sorted(profile_attempts.items()):
        profile_completions = sum(
            item.report is not None and item.report.outcome == TaskOutcome.COMPLETE
            for item in items
        )
        peak_tokens = [
            max(measurement.estimated_tokens for measurement in item.report.context_measurements)
            for item in items
            if item.report is not None and item.report.context_measurements
        ]
        profile_summaries.append(
            ContextProfileSummary(
                profile=profile,
                attempts=len(items),
                completion_rate=_rate(
                    profile_completions,
                    len(items),
                    "no attempts exist for this context profile",
                ),
                context_tokens=_distribution(
                    peak_tokens,
                    unit="estimated_tokens",
                    reason="no context measurements exist for this profile",
                ),
                latency_seconds=_distribution(
                    [item.report.latency_seconds for item in items if item.report],
                    unit="seconds",
                    reason="no completed lane reports exist for this profile",
                ),
            )
        )

    return EvaluationAggregateReport(
        aggregate_id=aggregate_id,
        source_run_ids=[item.run_id for item in comparisons],
        run_count=len(comparisons),
        attempt_count=len(attempts),
        local_only_verified_completion_rate=_rate(
            local_only_successes,
            len(local_first),
            "no local-first attempts were evaluated",
        ),
        frontier_rescue_rate=_rate(
            hosted_rescues,
            len(hosted_escalations),
            "no real hosted-frontier escalations were evaluated",
        ),
        overall_verified_completion_rate=_rate(
            completions, len(attempts), "no evaluation attempts were loaded"
        ),
        human_review_rate=_rate(
            human_reviews, len(attempts), "no evaluation attempts were loaded"
        ),
        unsafe_patch_rejection_rate=_rate(
            unsafe_rejections,
            patch_attempts,
            "no patch attempts were recorded",
        ),
        false_success_rate=_rate(
            false_successes,
            len(oracle_evaluated),
            "no claimed successes were evaluated by a valid held-out oracle",
        ),
        oracle_infrastructure_errors=oracle_infrastructure_errors,
        latency_seconds=_distribution(
            [item.report.latency_seconds for item in attempts if item.report],
            unit="seconds",
            reason="no evaluation attempts were loaded",
        ),
        transmitted_files=_distribution(
            [item.report.transmitted_files for item in attempts if item.report],
            unit="files",
            reason="no evaluation attempts were loaded",
        ),
        transmitted_lines=_distribution(
            [item.report.transmitted_lines for item in attempts if item.report],
            unit="lines",
            reason="no evaluation attempts were loaded",
        ),
        hosted_savings=_hosted_savings(comparisons),
        local_vs_one_shot=_local_vs_one_shot(comparisons),
        context_profiles=profile_summaries,
    )
