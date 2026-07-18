from __future__ import annotations

import json
from pathlib import Path

from apoapsis.evaluation.schemas import EvalComparisonReport

_HEADER = (
    "| Lane | Outcome | Calls | Input Tokens | Output Tokens | Cached Tokens | "
    "Cost USD | Latency s | Files Changed | Escalation | Verification | "
    "Peak Ctx Tokens | Peak Window Util | Stable/New Evidence |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | "
    "--- | --- | --- |\n"
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
            f"| {lane_result.lane.value} | skipped | - | - | - | - | - | - | - | "
            f"- | {reason} | - | - | - |"
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
    return (
        f"| {lane_result.lane.value} | {task_report.outcome.value} | "
        f"{task_report.number_of_calls} | {task_report.input_tokens} | "
        f"{task_report.output_tokens} | {task_report.cached_input_tokens} | "
        f"{task_report.estimated_cost_usd:.4f} | "
        f"{task_report.latency_seconds:.2f} | "
        f"{len(task_report.files_changed)} | "
        f"{task_report.escalation_triggered} | {verification} | "
        f"{peak_tokens} | {peak_utilization} | {stable_vs_new} |"
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
