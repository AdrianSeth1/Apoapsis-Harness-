from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Callable

from apoapsis.evaluation.planning_schemas import (
    ConditionMetrics,
    MonolithicConditionResult,
    PlannedConditionResult,
    PlanningComparisonReport,
    PlanningEvaluationSummary,
    SliceAttemptResult,
    SliceMetrics,
)
from apoapsis.evaluation.schemas import DistributionMetric, MetricStatus, OracleStatus, RateMetric
from apoapsis.reporting.report import FinalTaskReport, TaskOutcome


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


def _peak_context_tokens(report: FinalTaskReport) -> float | None:
    if not report.context_measurements:
        return None
    return float(max(item.estimated_tokens for item in report.context_measurements))


def _sum_slice_field(
    planned: PlannedConditionResult, getter: Callable[[FinalTaskReport], float]
) -> float:
    return sum(
        getter(item.report) for item in planned.slices if item.report is not None
    )


def _monolithic_metrics(
    reports: list[MonolithicConditionResult], *, reason_prefix: str
) -> ConditionMetrics:
    attempted = [item for item in reports if item.report is not None]
    completions = sum(
        item.report.outcome == TaskOutcome.COMPLETE for item in attempted
    )
    true_completions = sum(
        item.report.outcome == TaskOutcome.COMPLETE
        and item.held_out_oracle is not None
        and item.held_out_oracle.status == OracleStatus.PASSED
        for item in attempted
    )
    oracle_evaluated = [
        item
        for item in attempted
        if item.report.outcome == TaskOutcome.COMPLETE
        and item.held_out_oracle is not None
        and item.held_out_oracle.status in {OracleStatus.PASSED, OracleStatus.FAILED}
    ]
    false_successes = sum(
        item.held_out_oracle.status == OracleStatus.FAILED for item in oracle_evaluated
    )
    human_reviews = sum(
        item.report.outcome == TaskOutcome.HUMAN_REVIEW_REQUIRED for item in attempted
    )
    patch_attempts = sum(item.patch_attempts for item in attempted)
    unsafe_rejections = sum(item.unsafe_patch_rejections for item in attempted)
    context_tokens = [
        peak
        for item in attempted
        if (peak := _peak_context_tokens(item.report)) is not None
    ]
    return ConditionMetrics(
        attempts=len(reports),
        true_completion_rate=_rate(
            true_completions, len(attempted), f"no {reason_prefix} attempts were evaluated"
        ),
        false_success_rate=_rate(
            false_successes,
            len(oracle_evaluated),
            "no claimed successes were evaluated by a valid held-out oracle",
        ),
        human_review_rate=_rate(
            human_reviews, len(attempted), f"no {reason_prefix} attempts were evaluated"
        ),
        policy_rejection_rate=_rate(
            unsafe_rejections, patch_attempts, "no patch attempts were recorded"
        ),
        latency_seconds=_distribution(
            [item.report.latency_seconds for item in attempted],
            unit="seconds",
            reason=f"no {reason_prefix} attempts were evaluated",
        ),
        provider_calls=_distribution(
            [item.report.number_of_calls for item in attempted],
            unit="calls",
            reason=f"no {reason_prefix} attempts were evaluated",
        ),
        local_agent_turns=_distribution(
            [item.report.local_agent_turns for item in attempted],
            unit="turns",
            reason=f"no {reason_prefix} attempts were evaluated",
        ),
        frontier_agent_turns=_distribution(
            [item.report.frontier_agent_turns for item in attempted],
            unit="turns",
            reason=f"no {reason_prefix} attempts were evaluated",
        ),
        patch_attempts=_distribution(
            [item.patch_attempts for item in attempted],
            unit="attempts",
            reason=f"no {reason_prefix} attempts were evaluated",
        ),
        verification_runs=_distribution(
            [
                item.report.agent_verification_runs
                + item.report.frontier_agent_verification_runs
                for item in attempted
            ],
            unit="runs",
            reason=f"no {reason_prefix} attempts were evaluated",
        ),
        input_tokens=_distribution(
            [item.report.input_tokens for item in attempted],
            unit="tokens",
            reason=f"no {reason_prefix} attempts were evaluated",
        ),
        output_tokens=_distribution(
            [item.report.output_tokens for item in attempted],
            unit="tokens",
            reason=f"no {reason_prefix} attempts were evaluated",
        ),
        cached_input_tokens=_distribution(
            [item.report.cached_input_tokens for item in attempted],
            unit="tokens",
            reason=f"no {reason_prefix} attempts were evaluated",
        ),
        estimated_cost_usd=_distribution(
            [item.report.estimated_cost_usd for item in attempted],
            unit="usd",
            reason=f"no {reason_prefix} attempts were evaluated",
        ),
        context_files=_distribution(
            [item.report.transmitted_files for item in attempted],
            unit="files",
            reason=f"no {reason_prefix} attempts were evaluated",
        ),
        context_lines=_distribution(
            [item.report.transmitted_lines for item in attempted],
            unit="lines",
            reason=f"no {reason_prefix} attempts were evaluated",
        ),
        context_tokens=_distribution(
            context_tokens,
            unit="estimated_tokens",
            reason="no context measurements were recorded",
        ),
    )


def _planned_metrics(reports: list[PlannedConditionResult]) -> ConditionMetrics:
    completions = sum(item.all_slices_complete for item in reports)
    true_completions = sum(
        item.all_slices_complete
        and item.held_out_oracle is not None
        and item.held_out_oracle.status == OracleStatus.PASSED
        for item in reports
    )
    oracle_evaluated = [
        item
        for item in reports
        if item.all_slices_complete
        and item.held_out_oracle is not None
        and item.held_out_oracle.status in {OracleStatus.PASSED, OracleStatus.FAILED}
    ]
    false_successes = sum(item.integration_failure for item in oracle_evaluated)
    human_reviews = sum(
        item.stopped_at_slice_id is not None
        and any(
            slice_item.slice_id == item.stopped_at_slice_id
            and slice_item.report is not None
            and slice_item.report.outcome == TaskOutcome.HUMAN_REVIEW_REQUIRED
            for slice_item in item.slices
        )
        for item in reports
    )
    patch_attempts = sum(
        slice_item.patch_attempts for item in reports for slice_item in item.slices
    )
    unsafe_rejections = sum(
        slice_item.unsafe_patch_rejections
        for item in reports
        for slice_item in item.slices
    )
    context_token_totals = [
        _sum_slice_field(
            item, lambda report: _peak_context_tokens(report) or 0.0
        )
        for item in reports
        if any(slice_item.report is not None for slice_item in item.slices)
    ]
    return ConditionMetrics(
        attempts=len(reports),
        true_completion_rate=_rate(
            true_completions, len(reports), "no planned attempts were evaluated"
        ),
        false_success_rate=_rate(
            false_successes,
            len(oracle_evaluated),
            "no fully-complete planned attempts were evaluated by a valid held-out oracle",
        ),
        human_review_rate=_rate(
            human_reviews, len(reports), "no planned attempts were evaluated"
        ),
        policy_rejection_rate=_rate(
            unsafe_rejections, patch_attempts, "no patch attempts were recorded"
        ),
        latency_seconds=_distribution(
            [item.duration_seconds for item in reports],
            unit="seconds",
            reason="no planned attempts were evaluated",
        ),
        provider_calls=_distribution(
            [
                _sum_slice_field(item, lambda report: float(report.number_of_calls))
                for item in reports
            ],
            unit="calls",
            reason="no planned attempts were evaluated",
        ),
        local_agent_turns=_distribution(
            [
                _sum_slice_field(item, lambda report: float(report.local_agent_turns))
                for item in reports
            ],
            unit="turns",
            reason="no planned attempts were evaluated",
        ),
        frontier_agent_turns=_distribution(
            [
                _sum_slice_field(item, lambda report: float(report.frontier_agent_turns))
                for item in reports
            ],
            unit="turns",
            reason="no planned attempts were evaluated",
        ),
        patch_attempts=_distribution(
            [
                float(sum(slice_item.patch_attempts for slice_item in item.slices))
                for item in reports
            ],
            unit="attempts",
            reason="no planned attempts were evaluated",
        ),
        verification_runs=_distribution(
            [
                _sum_slice_field(
                    item,
                    lambda report: float(
                        report.agent_verification_runs
                        + report.frontier_agent_verification_runs
                    ),
                )
                for item in reports
            ],
            unit="runs",
            reason="no planned attempts were evaluated",
        ),
        input_tokens=_distribution(
            [
                _sum_slice_field(item, lambda report: float(report.input_tokens))
                for item in reports
            ],
            unit="tokens",
            reason="no planned attempts were evaluated",
        ),
        output_tokens=_distribution(
            [
                _sum_slice_field(item, lambda report: float(report.output_tokens))
                for item in reports
            ],
            unit="tokens",
            reason="no planned attempts were evaluated",
        ),
        cached_input_tokens=_distribution(
            [
                _sum_slice_field(item, lambda report: float(report.cached_input_tokens))
                for item in reports
            ],
            unit="tokens",
            reason="no planned attempts were evaluated",
        ),
        estimated_cost_usd=_distribution(
            [
                _sum_slice_field(item, lambda report: report.estimated_cost_usd)
                for item in reports
            ],
            unit="usd",
            reason="no planned attempts were evaluated",
        ),
        context_files=_distribution(
            [
                _sum_slice_field(item, lambda report: float(report.transmitted_files))
                for item in reports
            ],
            unit="files",
            reason="no planned attempts were evaluated",
        ),
        context_lines=_distribution(
            [
                _sum_slice_field(item, lambda report: float(report.transmitted_lines))
                for item in reports
            ],
            unit="lines",
            reason="no planned attempts were evaluated",
        ),
        context_tokens=_distribution(
            context_token_totals,
            unit="estimated_tokens",
            reason="no context measurements were recorded",
        ),
    )


def _per_slice_metrics(reports: list[PlannedConditionResult]) -> list[SliceMetrics]:
    by_slice: dict[str, list[SliceAttemptResult]] = defaultdict(list)
    for report in reports:
        for slice_item in report.slices:
            if slice_item.attempted:
                by_slice[slice_item.slice_id].append(slice_item)
    summaries: list[SliceMetrics] = []
    for slice_id, attempts in sorted(by_slice.items()):
        completions = sum(
            item.report is not None and item.report.outcome == TaskOutcome.COMPLETE
            for item in attempts
        )
        human_reviews = sum(
            item.report is not None
            and item.report.outcome == TaskOutcome.HUMAN_REVIEW_REQUIRED
            for item in attempts
        )
        summaries.append(
            SliceMetrics(
                slice_id=slice_id,
                attempts=len(attempts),
                completion_rate=_rate(
                    completions, len(attempts), "no attempts exist for this slice"
                ),
                human_review_rate=_rate(
                    human_reviews, len(attempts), "no attempts exist for this slice"
                ),
            )
        )
    return summaries


def summarize_planning_comparisons(
    comparisons: list[PlanningComparisonReport], *, summary_id: str
) -> PlanningEvaluationSummary:
    """Aggregate persisted `PlanningComparisonReport`s for one scenario/
    version without invoking a provider. Refuses to silently mix
    comparisons from different scenarios or scenario versions -- the
    caller must partition its inputs first, exactly like a different
    `context_profile` was already never silently blended in the existing
    single-shot evaluation aggregator."""

    if not comparisons:
        raise ValueError("summarize_planning_comparisons requires at least one report")
    scenario_ids = {item.scenario_id for item in comparisons}
    scenario_versions = {item.scenario_version for item in comparisons}
    if len(scenario_ids) > 1 or len(scenario_versions) > 1:
        raise ValueError(
            "refusing to aggregate comparisons from different scenarios/versions: "
            f"scenario_ids={sorted(scenario_ids)}, "
            f"scenario_versions={sorted(scenario_versions)}"
        )

    monolithic_reports = [item.monolithic for item in comparisons]
    planned_reports = [item.planned for item in comparisons]
    return PlanningEvaluationSummary(
        summary_id=summary_id,
        source_run_ids=[item.run_id for item in comparisons],
        scenario_id=next(iter(scenario_ids)),
        scenario_version=next(iter(scenario_versions)),
        monolithic=_monolithic_metrics(monolithic_reports, reason_prefix="monolithic"),
        planned=_planned_metrics(planned_reports),
        planned_integration_failure_rate=_rate(
            sum(item.integration_failure for item in planned_reports if item.all_slices_complete),
            sum(1 for item in planned_reports if item.all_slices_complete),
            "no fully-complete planned attempts were evaluated",
        ),
        per_slice=_per_slice_metrics(planned_reports),
    )


__all__ = ["summarize_planning_comparisons"]
