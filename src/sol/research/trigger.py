from __future__ import annotations

from pydantic import Field

from sol.research.schemas import ResearchMode
from sol.specification.schema import RiskLevel, StrictModel, TaskSpecification


class ResearchTriggerDecision(StrictModel):
    requested_mode: ResearchMode
    effective_mode: ResearchMode
    triggered: bool
    reasons: list[str] = Field(default_factory=list)


class ResearchTriggerEngine:
    _explicit = {
        "research",
        "example",
        "examples",
        "precedent",
        "template",
        "templates",
        "inspiration",
        "complaints",
        "convention",
    }
    _judgment = {
        "ux",
        "user experience",
        "onboarding",
        "dashboard",
        "report",
        "public api",
        "cli",
        "design",
        "feels generic",
        "users dislike",
        "product",
        "real-world user",
        "result presentation",
    }
    _ecosystem = {
        "unfamiliar framework",
        "unfamiliar integration",
        "ecosystem convention",
        "prior attempt failed",
        "previous attempt failed",
        "multiple implementation patterns",
    }
    _mechanical = {
        "rename",
        "formatting only",
        "typo",
        "apply review comment",
        "update comment",
        "simple localized bug",
        "existing-pattern replication",
        "follow the existing pattern",
        "fully specified by existing tests",
    }

    def decide(
        self, specification: TaskSpecification, requested_mode: ResearchMode
    ) -> ResearchTriggerDecision:
        if requested_mode == ResearchMode.OFF:
            return ResearchTriggerDecision(
                requested_mode=requested_mode,
                effective_mode=ResearchMode.OFF,
                triggered=False,
                reasons=["research explicitly disabled"],
            )
        if requested_mode != ResearchMode.AUTO:
            return ResearchTriggerDecision(
                requested_mode=requested_mode,
                effective_mode=requested_mode,
                triggered=True,
                reasons=[f"research explicitly requested: {requested_mode.value}"],
            )
        text = "\n".join(
            [specification.objective.text]
            + [item.text for item in specification.acceptance_criteria]
            + [item.verbatim_source for item in specification.hard_constraints]
        ).lower()
        explicit = any(marker in text for marker in self._explicit)
        judgment = any(marker in text for marker in self._judgment)
        ecosystem = any(marker in text for marker in self._ecosystem)
        if (
            any(marker in text for marker in self._mechanical)
            and not explicit
            and not judgment
            and not ecosystem
            and not specification.open_questions
            and specification.risk_level not in {RiskLevel.HIGH, RiskLevel.CRITICAL}
        ):
            return ResearchTriggerDecision(
                requested_mode=requested_mode,
                effective_mode=ResearchMode.OFF,
                triggered=False,
                reasons=["task is mechanical and localized"],
            )
        reasons: list[str] = []
        if explicit:
            reasons.append("task explicitly asks for examples or research precedent")
        if judgment:
            reasons.append("task requires product, UX, API, CLI, or reporting judgment")
        if ecosystem:
            reasons.append("task needs ecosystem precedent or follows a failed attempt")
        if specification.open_questions:
            reasons.append("approved specification contains unresolved questions")
        if specification.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            reasons.append("task risk warrants external precedent")
        triggered = bool(reasons)
        return ResearchTriggerDecision(
            requested_mode=requested_mode,
            effective_mode=ResearchMode.FULL if triggered else ResearchMode.OFF,
            triggered=triggered,
            reasons=reasons or ["existing project evidence is sufficient"],
        )
