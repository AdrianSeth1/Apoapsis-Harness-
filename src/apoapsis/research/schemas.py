from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from apoapsis.specification.schema import StrictModel


def research_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ResearchMode(StrEnum):
    OFF = "OFF"
    AUTO = "AUTO"
    GITHUB_ONLY = "GITHUB_ONLY"
    COMMUNITY = "COMMUNITY"
    FULL = "FULL"

    @classmethod
    def from_cli(cls, value: str) -> ResearchMode:
        aliases = {
            "off": cls.OFF,
            "auto": cls.AUTO,
            "github": cls.GITHUB_ONLY,
            "github_only": cls.GITHUB_ONLY,
            "community": cls.COMMUNITY,
            "full": cls.FULL,
        }
        try:
            return aliases[value.lower()]
        except KeyError as exc:
            raise ValueError(f"unknown research mode: {value}") from exc


class ResearchSourceName(StrEnum):
    OFFICIAL_DOCS = "official_docs"
    GITHUB = "github"
    REDDIT = "reddit"
    FIXTURE = "fixture"


class ResearchSourceType(StrEnum):
    OFFICIAL_DOCUMENTATION = "official_documentation"
    GITHUB_REPOSITORY = "github_repository"
    GITHUB_FILE = "github_file"
    GITHUB_ISSUE = "github_issue"
    GITHUB_PULL_REQUEST = "github_pull_request"
    GITHUB_COMMENT = "github_comment"
    GITHUB_DISCUSSION = "github_discussion"
    REDDIT_POST = "reddit_post"
    REDDIT_COMMENT = "reddit_comment"
    FIXTURE = "fixture"


class AuthorityLevel(StrEnum):
    AUTHORITATIVE = "authoritative"
    IMPLEMENTATION_PRECEDENT = "implementation_precedent"
    ANECDOTAL = "anecdotal"


class EvidenceConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class LicenseClassification(StrEnum):
    IDEA_ONLY = "IDEA_ONLY"
    PATTERN_ALLOWED = "PATTERN_ALLOWED"
    CODE_REUSE_ALLOWED = "CODE_REUSE_ALLOWED"
    ATTRIBUTION_REQUIRED = "ATTRIBUTION_REQUIRED"
    LICENSE_REVIEW_REQUIRED = "LICENSE_REVIEW_REQUIRED"
    LICENSE_INCOMPATIBLE = "LICENSE_INCOMPATIBLE"
    UNKNOWN_LICENSE = "UNKNOWN_LICENSE"


class ResearchMemoryClass(StrEnum):
    RESEARCH_EVIDENCE = "RESEARCH_EVIDENCE"
    RESEARCH_PATTERN = "RESEARCH_PATTERN"
    CANDIDATE_DECISION = "CANDIDATE_DECISION"


class ResearchBudget(StrictModel):
    max_queries: int = Field(default=8, ge=1, le=100)
    max_candidates: int = Field(default=30, ge=1, le=500)
    max_fetched_sources: int = Field(default=12, ge=1, le=100)
    max_extracted_characters_per_source: int = Field(
        default=20_000, ge=500, le=200_000
    )
    max_research_context_tokens: int = Field(default=30_000, ge=1_000, le=500_000)
    max_seconds: float = Field(default=180.0, gt=0, le=3600)


class ResearchQuestion(StrictModel):
    id: str = Field(pattern=r"^RQ-[A-Za-z0-9._-]+$")
    question: str = Field(min_length=1)
    source_preferences: list[ResearchSourceName] = Field(default_factory=list)


class ResearchSpecification(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    research_mode: ResearchMode
    research_goal: str = Field(min_length=1)
    questions: list[ResearchQuestion] = Field(min_length=1)
    project_constraints: list[str] = Field(default_factory=list)
    excluded_topics: list[str] = Field(default_factory=list)
    budget: ResearchBudget = Field(default_factory=ResearchBudget)

    @model_validator(mode="after")
    def unique_question_ids(self) -> ResearchSpecification:
        identifiers = [item.id for item in self.questions]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("research question IDs must be unique")
        return self


class ResearchQuery(StrictModel):
    query_id: str = Field(pattern=r"^QUERY-[A-Za-z0-9._-]+$")
    research_question_id: str = Field(pattern=r"^RQ-[A-Za-z0-9._-]+$")
    source: ResearchSourceName
    query: str = Field(min_length=1)
    content_types: list[ResearchSourceType] = Field(default_factory=list)
    language: str | None = None
    framework: str | None = None
    urls: list[str] = Field(default_factory=list)


class SourceBudget(StrictModel):
    max_candidates: int = Field(ge=1)
    max_response_bytes: int = Field(default=1_000_000, ge=1_000, le=10_000_000)
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)


class SourceCandidate(StrictModel):
    candidate_id: str = Field(pattern=r"^CAND-[A-Za-z0-9._-]+$")
    source: ResearchSourceName
    source_type: ResearchSourceType
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    api_url: str | None = None
    snippet: str = ""
    repository: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    deterministic_score: float = Field(default=0.0, ge=0.0, le=1.0)
    deduplication_key: str = Field(min_length=1)


class SourceLocator(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    repository: str | None = None
    url: str = Field(min_length=1)
    commit_sha: str | None = None
    path: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    issue_number: int | None = Field(default=None, ge=1)
    pull_request_number: int | None = Field(default=None, ge=1)
    discussion_number: int | None = Field(default=None, ge=1)
    comment_id: str | None = None

    @model_validator(mode="after")
    def valid_lines(self) -> SourceLocator:
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("source line range must be complete")
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("source end_line precedes start_line")
        return self


class PromptInjectionFlag(StrictModel):
    rule_id: str
    phrase: str
    line_number: int = Field(ge=1)
    severity: Literal["medium", "high", "critical"]


class RetrievedSource(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str
    source: ResearchSourceName
    source_type: ResearchSourceType
    title: str
    locator: SourceLocator
    content: str
    retrieved_at: datetime = Field(default_factory=research_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
    license: LicenseClassification = LicenseClassification.IDEA_ONLY
    license_identifier: str | None = None
    prompt_injection_flags: list[PromptInjectionFlag] = Field(default_factory=list)
    content_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    quarantine_label: Literal["UNTRUSTED_EXTERNAL_CONTENT"] = (
        "UNTRUSTED_EXTERNAL_CONTENT"
    )

    @model_validator(mode="after")
    def derive_content_digest(self) -> RetrievedSource:
        digest = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_sha256 is None:
            self.content_sha256 = digest
        elif self.content_sha256 != digest:
            raise ValueError("retrieved source content digest mismatch")
        return self


class EvidenceFindingProposal(StrictModel):
    research_question_id: str
    claim: str = Field(min_length=1)
    excerpt: str = Field(min_length=1, max_length=1000)
    relevance: float = Field(ge=0, le=1)
    confidence: EvidenceConfidence
    applicability: str = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)


class EvidenceExtractionProposal(StrictModel):
    findings: list[EvidenceFindingProposal] = Field(default_factory=list)


class ResearchEvidence(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    evidence_id: str = Field(pattern=r"^RSEV-[A-Za-z0-9._-]+$")
    memory_class: ResearchMemoryClass = ResearchMemoryClass.RESEARCH_EVIDENCE
    research_question_id: str = Field(pattern=r"^RQ-[A-Za-z0-9._-]+$")
    claim: str = Field(min_length=1)
    source_type: ResearchSourceType
    source_locator: SourceLocator
    excerpt: str = Field(min_length=1, max_length=1000)
    retrieved_at: datetime
    authoritative_level: AuthorityLevel
    relevance: float = Field(ge=0, le=1)
    confidence: EvidenceConfidence
    license: LicenseClassification
    license_identifier: str | None = None
    prompt_injection_flags: tuple[PromptInjectionFlag, ...] = ()
    applicability: str = Field(min_length=1)
    limitations: tuple[str, ...] = ()


class PlannedQuery(StrictModel):
    research_question_id: str
    source: ResearchSourceName
    query: str = Field(min_length=1)
    content_types: list[ResearchSourceType] = Field(default_factory=list)
    language: str | None = None
    framework: str | None = None
    urls: list[str] = Field(default_factory=list)


class ResearchPlanProposal(StrictModel):
    research_goal: str = Field(min_length=1)
    questions: list[ResearchQuestion] = Field(min_length=1)
    queries: list[PlannedQuery] = Field(min_length=1)
    excluded_topics: list[str] = Field(default_factory=list)


class CandidateRanking(StrictModel):
    candidate_id: str
    relevance: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1)


class CandidateRankingProposal(StrictModel):
    rankings: list[CandidateRanking] = Field(default_factory=list)


class ResearchPattern(StrictModel):
    memory_class: ResearchMemoryClass = ResearchMemoryClass.RESEARCH_PATTERN
    name: str = Field(min_length=1)
    supporting_evidence: list[str] = Field(min_length=1)
    advantages: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ResearchDisagreement(StrictModel):
    question: str = Field(min_length=1)
    positions: list[str] = Field(min_length=2)
    evidence: list[str] = Field(default_factory=list)


class UserPainPoint(StrictModel):
    description: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)


class ProjectAdaptation(StrictModel):
    memory_class: ResearchMemoryClass = ResearchMemoryClass.CANDIDATE_DECISION
    proposal: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    constraints_addressed: list[str] = Field(default_factory=list)


class ResearchSynthesis(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    research_goal: str = Field(min_length=1)
    patterns: list[ResearchPattern] = Field(default_factory=list)
    disagreements: list[ResearchDisagreement] = Field(default_factory=list)
    user_pain_points: list[UserPainPoint] = Field(default_factory=list)
    recommended_project_adaptation: ProjectAdaptation
    copied_code: Literal[False] = False
    unresolved_questions: list[str] = Field(default_factory=list)

    def validate_evidence_references(self, valid_ids: set[str]) -> None:
        referenced: set[str] = set()
        for pattern in self.patterns:
            referenced.update(pattern.supporting_evidence)
        for disagreement in self.disagreements:
            referenced.update(disagreement.evidence)
        for pain_point in self.user_pain_points:
            referenced.update(pain_point.evidence)
        unknown = referenced - valid_ids
        if unknown:
            raise ValueError(f"synthesis references unknown evidence: {sorted(unknown)}")


class ResearchTelemetry(StrictModel):
    triggered: bool
    trigger_reasons: list[str] = Field(default_factory=list)
    effective_mode: ResearchMode
    queries_generated: int = Field(ge=0)
    sources_searched: list[ResearchSourceName] = Field(default_factory=list)
    candidates_found: int = Field(ge=0)
    candidates_after_deduplication: int = Field(ge=0)
    sources_fetched: int = Field(ge=0)
    sources_accepted: int = Field(ge=0)
    sources_rejected: int = Field(ge=0)
    duplicate_rate: float = Field(ge=0, le=1)
    cache_hits: int = Field(default=0, ge=0)
    cache_misses: int = Field(default=0, ge=0)
    model_calls: int = Field(ge=0)
    structured_output_failures: int = Field(ge=0)
    local_input_tokens: int = Field(ge=0)
    local_output_tokens: int = Field(ge=0)
    peak_context_characters: int = Field(ge=0)
    prompt_injection_flags: int = Field(ge=0)
    license_classifications: dict[str, int] = Field(default_factory=dict)
    evidence_included: list[str] = Field(default_factory=list)
    research_latency_seconds: float = Field(ge=0)
    changed_proposed_plan: bool = False
    user_accepted_recommendation: bool | None = None


class ResearchOutcome(StrictModel):
    specification: ResearchSpecification
    evidence: list[ResearchEvidence]
    synthesis: ResearchSynthesis
    brief: str
    telemetry: ResearchTelemetry
    audit_directory: str
