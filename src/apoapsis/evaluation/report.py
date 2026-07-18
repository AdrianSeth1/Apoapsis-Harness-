from __future__ import annotations

import json
from pathlib import Path

from apoapsis.evaluation.schemas import (
    EvalComparisonReport,
    EvaluationAggregateReport,
    MetricStatus,
    RateMetric,
)

_HEADER = (
    "| Lane | Completion Policy | Outcome | Calls | Input Tokens | Output Tokens | "
    "Cached Tokens | Cost USD | Latency s | Files Changed | Escalation | "
    "Verification | Peak Ctx Tokens | Peak Window Util | Stable/New Evidence | "
    "Unsafe Rejections | Held-out Oracle |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | "
    "--- | --- | --- | --- |\n"
)


def render_markdown(report: EvalComparisonReport) -> str:
    rows = [_render_row(lane_result) for lane_result in report.lanes]
    body = "\n".join(rows)
    return (
        f"# Evaluation comparison — {report.run_id}\n\n"
        f"Fixture: `{report.fixture_source}`\n\n"
        f"Task:\n\n```\n{report.task_text}\n```\n\n"
        f"{_HEADER}{body}\n"
    )


def _render_row(lane_result) -> str:
    if lane_result.skipped or lane_result.report is None:
        reason = lane_result.skip_reason or ""
        return (
            f"| {lane_result.lane.value} | - | skipped | - | - | - | - | - | - | "
            f"- | - | {reason} | - | - | - | - |"
        )
    task_report = lane_result.report
    verification = (
        task_report.verification_results[-1].status.value
        if task_report.verification_results
        else "not run"
    )
    measurements = task_report.context_measurements
    if measurements:
        peak_tokens = max(item.estimated_tokens for item in measurements)
        utilizations = [
            item.model_window_utilization
            for item in measurements
            if item.model_window_utilization is not None
        ]
        peak_utilization = f"{max(utilizations):.1%}" if utilizations else "-"
        stable_total = sum(item.stable_evidence_count for item in measurements)
        new_total = sum(item.new_evidence_count for item in measurements)
        stable_vs_new = f"{stable_total}/{new_total}"
    else:
        peak_tokens = "-"
        peak_utilization = "-"
        stable_vs_new = "-"
    oracle_status = (
        lane_result.held_out_oracle.status.value
        if lane_result.held_out_oracle is not None
        else "not configured"
    )
    return (
        f"| {lane_result.lane.value} | {task_report.completion_policy.value} | "
        f"{task_report.outcome.value} | "
        f"{task_report.number_of_calls} | {task_report.input_tokens} | "
        f"{task_report.output_tokens} | {task_report.cached_input_tokens} | "
        f"{task_report.estimated_cost_usd:.4f} | "
        f"{task_report.latency_seconds:.2f} | "
        f"{len(task_report.files_changed)} | "
        f"{task_report.escalation_triggered} | {verification} | "
        f"{peak_tokens} | {peak_utilization} | {stable_vs_new} | "
        f"{lane_result.unsafe_patch_rejections} | {oracle_status} |"
    )


def write_comparison(output_dir: Path, report: EvalComparisonReport) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison.json").write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "comparison.md").write_text(
        render_markdown(report), encoding="utf-8"
    )


def _rate_text(metric: RateMetric) -> str:
    if metric.status == MetricStatus.UNMEASURED:
        return f"unmeasured — {metric.reason}"
    assert metric.value is not None
    return f"{metric.value:.1%} ({metric.numerator}/{metric.denominator})"


def render_aggregate_markdown(report: EvaluationAggregateReport) -> str:
    rates = [
        ("Local-only verified completion", report.local_only_verified_completion_rate),
        ("Frontier rescue", report.frontier_rescue_rate),
        ("Overall verified completion", report.overall_verified_completion_rate),
        ("Human review", report.human_review_rate),
        ("Unsafe-patch rejection", report.unsafe_patch_rejection_rate),
        ("False success", report.false_success_rate),
    ]
    lines = [
        f"# Evaluation aggregate — {report.aggregate_id}",
        "",
        f"Source runs: {', '.join(report.source_run_ids) or '(none)'}",
        "",
        f"Attempts: {report.attempt_count}",
        "",
        "| Metric | Result |",
        "| --- | --- |",
    ]
    lines.extend(f"| {name} | {_rate_text(metric)} |" for name, metric in rates)
    lines.extend(["", "## Hosted-frontier comparison", ""])
    hosted = report.hosted_savings
    if hosted.status == MetricStatus.UNMEASURED:
        lines.append(f"Unmeasured — {hosted.reason}")
    else:
        lines.extend(
            [
                f"- Paired comparisons: {hosted.paired_comparisons}",
                f"- Hosted calls avoided: {hosted.hosted_calls_avoided}",
                f"- Hosted input tokens saved: {hosted.hosted_input_tokens_saved}",
                f"- Hosted output tokens saved: {hosted.hosted_output_tokens_saved}",
                f"- Hosted cost saved: ${hosted.hosted_cost_saved_usd:.4f}",
                f"- Completion-rate delta versus direct frontier: {hosted.completion_rate_delta:+.1%}",
            ]
        )
    lines.extend(["", "## Context profiles", ""])
    if not report.context_profiles:
        lines.append("No profile data loaded.")
    else:
        lines.extend(
            [
                "| Profile | Attempts | Completion | Median/P95 context tokens | Median/P95 latency s |",
                "| --- | ---: | --- | ---: | ---: |",
            ]
        )
        for profile in report.context_profiles:
            token_text = (
                f"{profile.context_tokens.median:.0f}/{profile.context_tokens.p95:.0f}"
                if profile.context_tokens.status == MetricStatus.MEASURED
                else "unmeasured"
            )
            latency_text = (
                f"{profile.latency_seconds.median:.2f}/{profile.latency_seconds.p95:.2f}"
                if profile.latency_seconds.status == MetricStatus.MEASURED
                else "unmeasured"
            )
            lines.append(
                f"| {profile.profile} | {profile.attempts} | "
                f"{_rate_text(profile.completion_rate)} | {token_text} | {latency_text} |"
            )
    lines.extend(
        [
            "",
            f"Oracle infrastructure errors: {report.oracle_infrastructure_errors}",
            "",
        ]
    )
    return "\n".join(lines)


def write_aggregate(output_dir: Path, report: EvaluationAggregateReport) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "aggregate.json").write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "aggregate.md").write_text(
        render_aggregate_markdown(report), encoding="utf-8"
    )
