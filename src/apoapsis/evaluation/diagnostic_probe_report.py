from __future__ import annotations

import json
from pathlib import Path

from apoapsis.evaluation.diagnostic_probe import DiagnosticProbeResult


def render_diagnostic_probe_markdown(result: DiagnosticProbeResult) -> str:
    behavior = result.behavior
    lines = [
        f"# D4c diagnostic probe — {result.probe_id}",
        "",
        f"Scenario: `{result.scenario_id}` version `{result.scenario_version}`",
        f"Plan: `{result.plan_id}`@v{result.plan_version}, slice `{result.slice_id}`",
        "",
        f"Prompt condition: **{result.prompt_condition.value}**",
        f"Model: **{result.model.model}** (source: `{result.model.source}`)",
        f"Evidence kind: `{result.evidence_kind.value}`",
        "",
        "## Behavior summary (deterministic, computed from persisted turn records)",
        "",
        f"- Total turns: {behavior.total_turns}",
        f"- Invoked `run_check`: **{behavior.invoked_run_check}**",
        f"- Invoked `submit_for_verification`: **{behavior.invoked_submit_for_verification}**",
        f"- First no-progress turn: {behavior.first_no_progress_turn or '(none observed)'}",
        f"- Maximum identical-action streak: {behavior.max_identical_action_streak}",
        f"- Verification runs: {behavior.verification_runs}",
        f"- Patch attempts: {behavior.patch_attempts}",
        f"- Outcome: `{behavior.outcome.value if behavior.outcome else 'unknown'}`",
        f"- Stop reason: {behavior.stop_reason or '(none recorded)'}",
        "",
    ]
    if result.report is not None:
        lines.extend(
            [
                "## Report",
                "",
                f"- Task id: `{result.task_id}`",
                f"- Calls: {result.report.number_of_calls}, tokens in/out/cached: "
                f"{result.report.input_tokens}/{result.report.output_tokens}/"
                f"{result.report.cached_input_tokens}",
                f"- Latency: {result.report.latency_seconds:.2f}s",
                "",
            ]
        )
    lines.append(
        "This is a single-slice diagnostic probe (ADR 0029), not a "
        "monolithic-vs-planned comparison. It does not by itself support "
        "any completion-rate or Architect-Mode-advantage claim."
    )
    return "\n".join(lines) + "\n"


def write_diagnostic_probe_report(output_dir: Path, result: DiagnosticProbeResult) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "diagnostic-probe.json").write_text(
        json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "diagnostic-probe.md").write_text(
        render_diagnostic_probe_markdown(result), encoding="utf-8"
    )


__all__ = ["render_diagnostic_probe_markdown", "write_diagnostic_probe_report"]
