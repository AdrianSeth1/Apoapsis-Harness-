"""Render and persist a paired corpus report.

The Markdown renders the two scorecards side by side and the four gates as four
separate rows. There is no summary row and no overall score, matching
`PairedCorpusReport`'s deliberate omission of one.
"""

from __future__ import annotations

import json
from pathlib import Path

from apoapsis.evaluation.paired import (
    GateStatus,
    PairedCorpusReport,
    PairedVerdict,
)


def _tokens(value: float | None) -> str:
    return f"{value:,.0f}" if value is not None else "unmeasured"


def _distance(value: int | None) -> str:
    return str(value) if value is not None else "?"


def _seconds(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "unmeasured"


def render_paired_markdown(report: PairedCorpusReport) -> str:
    lines = [
        f"# Paired corpus — {report.corpus_id}",
        "",
        f"Control arm: `{report.control_arm.value}`  ",
        f"Candidate arm: `{report.candidate_arm.value}`  ",
        f"Generated: {report.generated_at.isoformat()}",
        "",
        "## Release gates",
        "",
        "Each gate is reported on its own. A failure in one is never cancelled "
        "out by a pass in another, and `unmeasured` never counts as a pass.",
        "",
        "| Gate | Status | Detail |",
        "| --- | --- | --- |",
    ]
    for gate in report.gates:
        lines.append(
            f"| {gate.gate.value} | {gate.status.value} | {gate.detail} |"
        )

    lines.extend(
        [
            "",
            f"Recommended as the default local mode: "
            f"**{'yes' if report.recommended_for_default else 'no'}**",
            "",
            "## Model proposal quality (before external repair)",
            "",
            "| Case | Arm | Pre-repair outcome | Obligations | First-checkpoint checks | "
            "Artifact defects | Runtime defects | Repair files/lines | Calls | "
            "Input tokens | Output tokens | Ceiling events |",
            "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for case in report.cases:
        for label, record in (("control", case.control), ("candidate", case.candidate)):
            proposal = record.proposal
            lines.append(
                f"| {case.case_id} | {label} | "
                f"{record.independent_case_outcome.value} | "
                f"{_rate(proposal.obligations_implemented_before_repair)} | "
                f"{_rate(proposal.independent_checks_passed_at_first_checkpoint)} | "
                f"{len(proposal.production_artifact_defects)} | "
                f"{proposal.runtime_defects_found} | "
                f"{_distance(proposal.repair_distance_files)}/"
                f"{_distance(proposal.repair_distance_lines)} | "
                f"{proposal.model_calls} | {proposal.input_tokens:,} | "
                f"{proposal.output_tokens:,} | {len(proposal.ceiling_events)} |"
            )

    lines.extend(
        [
            "",
            "## Harness defect-detection quality",
            "",
            "| Case | Arm | Defects detected | Negative controls | False completes | "
            "Weak claims refused | Stale evidence rejected | Caught before delivery | "
            "Escaped acceptance |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for case in report.cases:
        for label, record in (("control", case.control), ("candidate", case.candidate)):
            detection = record.detection
            lines.append(
                f"| {case.case_id} | {label} | "
                f"{_rate(detection.defects_detected)} | "
                f"{_rate(detection.negative_controls_caught)} | "
                f"{detection.false_complete_count} | "
                f"{detection.weak_command_name_only_claims_refused} | "
                f"{detection.stale_or_inherited_evidence_rejected} | "
                f"{detection.integrated_defects_caught_before_delivery} | "
                f"{detection.defects_escaping_acceptance} |"
            )

    lines.extend(
        [
            "",
            "## Verdicts",
            "",
            "| Case | Proposal | Delivered |",
            "| --- | --- | --- |",
        ]
    )
    for case in report.cases:
        lines.append(
            f"| {case.case_id} | {case.proposal_verdict.value} | "
            f"{case.delivered_verdict.value} |"
        )

    lines.extend(
        [
            "",
            "## Efficiency",
            "",
            f"- Median control input tokens: {_tokens(report.median_control_input_tokens)}",
            f"- Median candidate input tokens: {_tokens(report.median_candidate_input_tokens)}",
            "- Median control provider latency (s): "
            f"{_seconds(report.median_control_provider_latency_seconds)}",
            "- Median candidate provider latency (s): "
            f"{_seconds(report.median_candidate_provider_latency_seconds)}",
            f"- Frontier repair calls: {report.frontier_repair_calls}",
            f"- Frontier repair cost (USD): {report.frontier_repair_cost_usd:.4f}",
            "",
        ]
    )

    findings = [item for case in report.cases for item in case.findings]
    findings.extend(item for gate in report.gates for item in gate.findings)
    if findings:
        lines.extend(["## Findings", "", "| Severity | Code | Case | Detail |", "| --- | --- | --- | --- |"])
        seen: set[tuple[str, str, str, str]] = set()
        for finding in findings:
            key = (
                finding.severity.value,
                finding.code.value,
                finding.case_id or "",
                finding.detail,
            )
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f"| {finding.severity.value} | {finding.code.value} | "
                f"{finding.case_id or '-'} | {finding.detail} |"
            )
        lines.append("")

    incomparable = [
        case
        for case in report.cases
        if PairedVerdict.INCOMPARABLE in {case.proposal_verdict, case.delivered_verdict}
    ]
    if incomparable:
        lines.extend(
            [
                "## Not a matched pair",
                "",
                "These cases record a controlled variable that differs between arms "
                "or was never written down. No win or loss can be read from them.",
                "",
            ]
        )
        for case in incomparable:
            lines.append(f"- `{case.case_id}`")
        lines.append("")

    if any(gate.status == GateStatus.UNMEASURED for gate in report.gates):
        lines.extend(
            [
                "An `unmeasured` gate is an absence of evidence, not a pass. The mode "
                "stays experimental until it is measured.",
                "",
            ]
        )
    return "\n".join(lines)


def _rate(metric) -> str:
    if metric.value is None:
        return f"unmeasured ({metric.reason})"
    return f"{metric.numerator}/{metric.denominator} ({metric.value:.2f})"


def write_paired_corpus(output_dir: Path, report: PairedCorpusReport) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "paired.json").write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "paired.md").write_text(
        render_paired_markdown(report), encoding="utf-8"
    )
