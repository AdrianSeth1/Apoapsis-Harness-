from __future__ import annotations

from apoapsis.research.schemas import ResearchEvidence, ResearchSynthesis


class ResearchBriefCompiler:
    compiler_version = "1"

    def compile(
        self,
        synthesis: ResearchSynthesis,
        evidence: list[ResearchEvidence],
        *,
        max_tokens: int,
    ) -> str:
        evidence_ids = {item.evidence_id for item in evidence}
        lines = [
            "EXTERNAL RESEARCH BRIEF",
            "",
            "Research goal:",
            synthesis.research_goal,
            "",
            "Observed patterns:",
        ]
        for pattern in synthesis.patterns:
            references = [
                item for item in pattern.supporting_evidence if item in evidence_ids
            ]
            lines.append(
                f"- {pattern.name} (evidence: {', '.join(references)})"
            )
            if pattern.advantages:
                lines.append(f"  Advantages: {'; '.join(pattern.advantages)}")
            if pattern.risks:
                lines.append(f"  Risks: {'; '.join(pattern.risks)}")
        lines.extend(["", "Observed user pain points:"])
        if synthesis.user_pain_points:
            for pain in synthesis.user_pain_points:
                lines.append(
                    f"- {pain.description} (evidence: {', '.join(pain.evidence)})"
                )
        else:
            lines.append("- None established by the selected evidence.")
        lines.extend(["", "Observed disagreements and uncertainty:"])
        if synthesis.disagreements:
            for disagreement in synthesis.disagreements:
                lines.append(
                    f"- {disagreement.question}: {' vs. '.join(disagreement.positions)}"
                )
        else:
            lines.append("- No material disagreement identified.")
        lines.extend(
            [
                "",
                "Model interpretation and project-specific recommendation:",
                synthesis.recommended_project_adaptation.proposal,
                f"Reason: {synthesis.recommended_project_adaptation.reason}",
                "Constraints addressed: "
                + ", ".join(
                    synthesis.recommended_project_adaptation.constraints_addressed
                ),
                "",
                "External code copied:",
                "None",
                "",
                "Evidence references:",
                ", ".join(sorted(evidence_ids)),
            ]
        )
        if synthesis.unresolved_questions:
            lines.extend(
                [
                    "",
                    "Unresolved questions:",
                    *[f"- {item}" for item in synthesis.unresolved_questions],
                ]
            )
        brief = "\n".join(lines).strip() + "\n"
        max_characters = max_tokens * 4
        if len(brief) > max_characters:
            brief = brief[: max(0, max_characters - 34)]
            brief += "\n[Research brief budget reached]\n"
        return brief
