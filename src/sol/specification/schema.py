from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceKind(StrEnum):
    USER = "user"
    DERIVED = "derived"
    REPOSITORY = "repository"
    APPROVED_DECISION = "approved_decision"


class ConstraintStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"


class ConstraintScope(StrEnum):
    TASK = "task"
    PROJECT = "project"


class RiskLevel(StrEnum):
    UNCLASSIFIED = "unclassified"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TraceableStatement(StrictModel):
    text: str = Field(min_length=1)
    source: SourceKind
    source_reference: str = Field(min_length=1)


class AcceptanceCriterion(TraceableStatement):
    id: str = Field(pattern=r"^AC-[A-Za-z0-9._-]+$")
    status: ConstraintStatus = ConstraintStatus.ACTIVE


class HardConstraint(StrictModel):
    """A hard constraint whose original user wording is immutable evidence."""

    id: str = Field(pattern=r"^HC-[A-Za-z0-9._-]+$")
    text: str = Field(min_length=1)
    verbatim_source: str = Field(min_length=1)
    interpreted_meaning: str = Field(min_length=1)
    source: SourceKind
    source_reference: str = Field(min_length=1)
    scope: ConstraintScope = ConstraintScope.TASK
    status: ConstraintStatus = ConstraintStatus.ACTIVE
    verification_method: str = Field(min_length=1)
    superseded_by: str | None = None
    introduced_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_supersession(self) -> HardConstraint:
        if self.status == ConstraintStatus.SUPERSEDED and not self.superseded_by:
            raise ValueError("superseded constraints must identify superseded_by")
        if self.superseded_by == self.id:
            raise ValueError("a constraint cannot supersede itself")
        return self


class TaskSpecification(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    objective: TraceableStatement
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    hard_constraints: list[HardConstraint] = Field(default_factory=list)
    user_preferences: list[TraceableStatement] = Field(default_factory=list)
    known_facts: list[TraceableStatement] = Field(default_factory=list)
    assumptions: list[TraceableStatement] = Field(default_factory=list)
    open_questions: list[TraceableStatement] = Field(default_factory=list)
    requested_output: Literal["unified_diff", "structured_edits", "none"] = (
        "unified_diff"
    )
    verification_requirements: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.UNCLASSIFIED

    @model_validator(mode="after")
    def validate_identifiers(self) -> TaskSpecification:
        identifiers = [item.id for item in self.acceptance_criteria]
        identifiers.extend(item.id for item in self.hard_constraints)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("criterion and constraint IDs must be unique")
        constraint_ids = {item.id for item in self.hard_constraints}
        for item in self.hard_constraints:
            if item.superseded_by and item.superseded_by not in constraint_ids:
                raise ValueError(
                    f"constraint {item.id} references unknown superseder "
                    f"{item.superseded_by}"
                )
        return self

    @property
    def active_hard_constraints(self) -> list[HardConstraint]:
        return [
            item
            for item in self.hard_constraints
            if item.status == ConstraintStatus.ACTIVE
        ]

