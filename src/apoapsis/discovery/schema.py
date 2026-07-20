from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from apoapsis.architect.schema import ArchitecturePlan, VerificationCatalogEntry
from apoapsis.context.compiler import ContextPackage
from apoapsis.repository.git import RepositorySnapshot
from apoapsis.specification.schema import HardConstraint, StrictModel, utc_now


class ClarificationQuestion(StrictModel):
    question_id: str = Field(pattern=r"^Q-[A-Za-z0-9._-]+$")
    text: str = Field(min_length=1)


class ClarificationAnswer(StrictModel):
    """The user's own words, preserved verbatim -- never rewritten,
    summarized, or answered on the user's behalf by any model."""

    question_id: str = Field(pattern=r"^Q-[A-Za-z0-9._-]+$")
    text: str = Field(min_length=1)


class LocalQuestionsProposal(StrictModel):
    """The local model's raw proposal shape -- ``extra="forbid"`` so it can
    only ever propose questions, never a status, count override, or any
    other field. The harness still independently caps the accepted count
    at ``config.discovery.max_clarification_questions`` regardless of how
    many the model returns."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    schema_version: Literal["1.0"] = "1.0"
    questions: list[ClarificationQuestion] = Field(default_factory=list)


class IdeaBrief(StrictModel):
    """The local model's proposed brief -- ``extra="forbid"``, no status or
    approval field of any kind. Only the user's own explicit approval
    (``discovery.store.SQLiteDiscoveryStore.approve_idea_brief``) ever
    changes a session's status because of this content."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    schema_version: Literal["1.0"] = "1.0"
    summary: str = Field(min_length=1)
    goals: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    key_constraints: list[HardConstraint] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class DiscoveryStatus(StrEnum):
    IDEA_ENTERED = "idea_entered"
    LOCAL_QUESTIONS_PROPOSED = "local_questions_proposed"
    LOCAL_ANSWERS_RECORDED = "local_answers_recorded"
    BRIEF_PROPOSED = "brief_proposed"
    BRIEF_APPROVED = "brief_approved"
    FRONTIER_PACKAGE_EXPORTED = "frontier_package_exported"
    FRONTIER_CLARIFICATION_PROPOSED = "frontier_clarification_proposed"
    FRONTIER_ANSWERS_RECORDED = "frontier_answers_recorded"
    FRONTIER_PLAN_PROPOSED = "frontier_plan_proposed"
    PLAN_IMPORTED = "plan_imported"
    FAILED = "failed"


class DiscoverySessionRecord(StrictModel):
    """Harness-owned, optimistically-versioned discovery session state.
    Every mutation is a deterministic, version-checked transition
    (``discovery.store.SQLiteDiscoveryStore``) -- no field here is ever set
    from a model's own claim about session state."""

    session_id: str = Field(pattern=r"^DISC-[A-Za-z0-9._-]+$")
    idea_text: str = Field(min_length=1)
    local_questions: list[ClarificationQuestion] = Field(default_factory=list)
    local_answers: list[ClarificationAnswer] = Field(default_factory=list)
    idea_brief: IdeaBrief | None = None
    brief_approved: bool = False
    frontier_transport: Literal["api", "manual"] | None = None
    frontier_round: int = Field(default=0, ge=0)
    frontier_package_id: str | None = None
    frontier_questions: list[ClarificationQuestion] = Field(default_factory=list)
    frontier_answers: list[ClarificationAnswer] = Field(default_factory=list)
    plan_id: str | None = None
    status: DiscoveryStatus
    failure_reason: str | None = None
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class FrontierPlanningResponseKind(StrEnum):
    CLARIFICATION_QUESTIONS = "clarification_questions"
    PLAN = "plan"


FRONTIER_PLANNING_AUTHORITY_RULES: tuple[str, ...] = (
    "You may only return JSON matching the response schema below -- either "
    "clarification_questions (at most the configured maximum) or a "
    "complete plan, never both, never anything else.",
    "You cannot mark a plan approved, validated, or executed. That is "
    "decided solely by the Apoapsis harness after a human explicitly "
    "approves it through the harness's own, unmodified plan-approval flow.",
    "verification_commands entries in any slice must name commands from "
    "VERIFICATION_CATALOG only; inventing a command name is rejected by "
    "deterministic validation, never executed as a request.",
    "You have no shell, filesystem, Git, network, or workflow-transition "
    "authority; nothing you write executes anything.",
    "Clarification rounds are capped at a small, fixed maximum shown "
    "below. Do not ask for another round after that; return your best "
    "complete plan given what you already know.",
    "The user answers your questions in their own words. You never answer "
    "on the user's behalf.",
    "This package and your response are both retained verbatim as an "
    "immutable audit record before any further action is taken.",
)


class FrontierPlanningRequestPackage(StrictModel):
    """Everything a frontier model needs to propose an ``ArchitecturePlan``
    for an already-user-approved idea brief, and nothing more: no
    credentials, no execution path, no ambient authority. Built once by
    ``discovery.frontier_package`` and written to disk before it ever
    leaves Apoapsis, over either transport."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    schema_version: Literal["1.0"] = "1.0"
    package_id: str = Field(pattern=r"^FPKG-[A-Za-z0-9._-]+$")
    session_id: str = Field(pattern=r"^DISC-[A-Za-z0-9._-]+$")
    frontier_round: int = Field(ge=0)
    idea_text: str = Field(min_length=1)
    idea_brief: IdeaBrief
    local_questions: list[ClarificationQuestion] = Field(default_factory=list)
    local_answers: list[ClarificationAnswer] = Field(default_factory=list)
    frontier_prior_questions: list[ClarificationQuestion] = Field(default_factory=list)
    frontier_prior_answers: list[ClarificationAnswer] = Field(default_factory=list)
    repository: RepositorySnapshot
    context: ContextPackage
    active_hard_constraints: list[HardConstraint] = Field(default_factory=list)
    verification_catalog: list[VerificationCatalogEntry] = Field(default_factory=list)
    architect_ceilings: dict[str, Any]
    plan_json_schema: dict[str, Any]
    response_json_schema: dict[str, Any]
    authority_rules: list[str] = Field(
        default_factory=lambda: list(FRONTIER_PLANNING_AUTHORITY_RULES)
    )
    max_clarification_rounds: int = Field(ge=0)
    max_clarification_questions: int = Field(ge=1)
    generated_at: datetime = Field(default_factory=utc_now)
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FrontierPlanningResponseEnvelope(StrictModel):
    """The strict, bounded response format -- from either transport.
    ``extra="forbid"`` and the exclusivity check below mean a returned
    payload is always unambiguously either a bounded question set or a
    complete plan, never a partial mix, and never anything claiming
    completion/approval/execution -- there is no field for that."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    schema_version: Literal["1.0"] = "1.0"
    package_id: str = Field(pattern=r"^FPKG-[A-Za-z0-9._-]+$")
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=r"^DISC-[A-Za-z0-9._-]+$")
    kind: FrontierPlanningResponseKind
    clarification_questions: list[ClarificationQuestion] | None = None
    plan: ArchitecturePlan | None = None

    @model_validator(mode="after")
    def exactly_one_variant(self) -> FrontierPlanningResponseEnvelope:
        if self.kind == FrontierPlanningResponseKind.CLARIFICATION_QUESTIONS:
            if not self.clarification_questions:
                raise ValueError(
                    "kind=clarification_questions requires a non-empty "
                    "clarification_questions list"
                )
            if self.plan is not None:
                raise ValueError(
                    "kind=clarification_questions must not also include a plan"
                )
        else:
            if self.plan is None:
                raise ValueError("kind=plan requires a plan")
            if self.clarification_questions is not None:
                raise ValueError(
                    "kind=plan must not also include clarification_questions"
                )
        return self


__all__ = [
    "ClarificationQuestion",
    "ClarificationAnswer",
    "LocalQuestionsProposal",
    "IdeaBrief",
    "DiscoveryStatus",
    "DiscoverySessionRecord",
    "FrontierPlanningResponseKind",
    "FRONTIER_PLANNING_AUTHORITY_RULES",
    "FrontierPlanningRequestPackage",
    "FrontierPlanningResponseEnvelope",
]
