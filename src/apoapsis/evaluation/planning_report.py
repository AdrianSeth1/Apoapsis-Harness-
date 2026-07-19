from __future__ import annotations

import json
from pathlib import Path

from apoapsis.evaluation.planning_schemas import PlanningComparisonReport


def render_planning_markdown(report: PlanningComparisonReport) -> str:
    mono = report.monolithic
    planned = report.planned
    lines = [
        f"# Planning comparison — {report.run_id}",
        "",
        f"Scenario: `{report.scenario_id}` version `{report.scenario_version}`",
        "",
        f"Coding model: `{report.coding_model}`",
        "",
        f"Task:\n\n```\n{report.task_text}\n```",
        "",
        "## Monolithic",
        "",
    ]
    if mono.report is None:
        lines.append("No report was recorded.")
    else:
        oracle = mono.held_out_oracle.status.value if mono.held_out_oracle else "not configured"
        lines.extend(
            [
                f"- Outcome: `{mono.report.outcome.value}`",
                f"- Held-out oracle: `{oracle}`",
                f"- Calls: {mono.report.number_of_calls}, "
                f"tokens in/out/cached: {mono.report.input_tokens}/"
                f"{mono.report.output_tokens}/{mono.report.cached_input_tokens}",
                f"- Estimated cost: ${mono.report.estimated_cost_usd:.4f}",
                f"- Latency: {mono.report.latency_seconds:.2f}s",
                f"- Patch attempts: {mono.patch_attempts} "
                f"(policy-rejected: {mono.unsafe_patch_rejections})",
                f"- Policy-rejected tool requests: {mono.report.rejected_tool_requests}",
            ]
        )
    lines.extend(["", "## Planned", ""])
    lines.append(f"- All slices complete: `{planned.all_slices_complete}`")
    if planned.stopped_at_slice_id is not None:
        lines.append(f"- Stopped advancing at: `{planned.stopped_at_slice_id}`")
    if planned.held_out_oracle is not None:
        lines.append(f"- Held-out oracle: `{planned.held_out_oracle.status.value}`")
    lines.append(f"- Integration failure: `{planned.integration_failure}`")
    lines.append(
        f"- Planner: `{planned.planner.planner_model}` "
        f"({planned.planner.planner_method.value}), plan "
        f"`{planned.planner.plan_id}`@v{planned.planner.plan_version}"
    )
    lines.extend(
        [
            "",
            "| Slice | Attempted | Outcome | Dependencies |",
            "| --- | --- | --- | --- |",
        ]
    )
    for slice_item in planned.slices:
        outcome = (
            slice_item.report.outcome.value
            if slice_item.report is not None
            else (slice_item.skip_reason or "not attempted")
        )
        lines.append(
            f"| {slice_item.slice_id} | {slice_item.attempted} | {outcome} | "
            f"{', '.join(slice_item.dependencies) or '(none)'} |"
        )
    return "\n".join(lines) + "\n"


def write_planning_comparison(output_dir: Path, report: PlanningComparisonReport) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "planning-comparison.json").write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "planning-comparison.md").write_text(
        render_planning_markdown(report), encoding="utf-8"
    )


__all__ = ["render_planning_markdown", "write_planning_comparison"]
