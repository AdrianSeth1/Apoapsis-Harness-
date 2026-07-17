from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from sol.context.provenance import ContextEvidence
from sol.specification.schema import (
    ConstraintStatus,
    HardConstraint,
    StrictModel,
    TaskSpecification,
    utc_now,
)


class ModelOperation(StrEnum):
    DRAFT_SPECIFICATION = "draft_specification"
    IDENTIFY_AMBIGUITIES = "identify_ambiguities"
    RANK_CONTEXT = "rank_context"
    IMPLEMENT_PATCH = "implement_patch"
    REVIEW_PATCH = "review_patch"
    DIAGNOSE_FAILURE = "diagnose_failure"
    PROPOSE_REPAIR = "propose_repair"
    SUMMARIZE_DECISION = "summarize_decision"
    PLAN_RESEARCH_QUESTIONS = "plan_research_questions"
    GENERATE_SOURCE_QUERIES = "generate_source_queries"
    RANK_SEARCH_RESULTS = "rank_search_results"
    EXTRACT_EVIDENCE = "extract_evidence"
    COMPARE_PATTERNS = "compare_patterns"
    IDENTIFY_DISAGREEMENTS = "identify_disagreements"
    SYNTHESIZE_RESEARCH_BRIEF = "synthesize_research_brief"
    DETECT_PROMPT_INJECTION = "detect_possible_prompt_injection"


class ConstraintDisposition(StrEnum):
    INCLUDED = "included"
    IRRELEVANT = "irrelevant"
    BLOCKED = "blocked"


class ConstraintCoverage(StrictModel):
    constraint_id: str = Field(pattern=r"^HC-[A-Za-z0-9._-]+$")
    disposition: ConstraintDisposition
    reason: str = Field(min_length=1)


class TokenUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)


class ModelRequest(StrictModel):
    request_id: str = Field(pattern=r"^MRQ-[A-Za-z0-9._-]+$")
    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    operation: ModelOperation
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    specification: TaskSpecification
    evidence: list[ContextEvidence] = Field(default_factory=list)
    active_constraints: list[HardConstraint] = Field(default_factory=list)
    constraint_coverage: list[ConstraintCoverage] = Field(default_factory=list)
    inference_parameters: dict[str, int | float | bool | None] = Field(
        default_factory=dict
    )
    requested_output: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_complete_constraint_coverage(self) -> ModelRequest:
        if self.task_id != self.specification.task_id:
            raise ValueError("request task_id must match specification task_id")
        active_ids = {
            constraint.id
            for constraint in self.active_constraints
            if constraint.status == ConstraintStatus.ACTIVE
        }
        spec_ids = {item.id for item in self.specification.active_hard_constraints}
        if active_ids != spec_ids:
            raise ValueError(
                "active_constraints must exactly match active specification constraints"
            )
        coverage_ids = [item.constraint_id for item in self.constraint_coverage]
        if len(coverage_ids) != len(set(coverage_ids)):
            raise ValueError("constraint coverage entries must be unique")
        if set(coverage_ids) != active_ids:
            raise ValueError("every active constraint requires a coverage disposition")
        return self


class ModelResponse(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    response_id: str = Field(pattern=r"^MRS-[A-Za-z0-9._-]+$")
    request_id: str = Field(pattern=r"^MRQ-[A-Za-z0-9._-]+$")
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    operation: ModelOperation
    content: str
    unified_diff: str | None = None
    structured_output: dict[str, Any] | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    finish_reason: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
