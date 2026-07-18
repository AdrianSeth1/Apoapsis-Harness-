from __future__ import annotations

from pydantic import Field

from apoapsis.context.compiler import ContextPackage
from apoapsis.context.provenance import ContextEvidence, EvidenceKind
from apoapsis.specification.schema import StrictModel

_CHARS_PER_TOKEN_ESTIMATE = 4
"""The same char/4 heuristic already used by `apoapsis.doctor`'s context
check -- kept identical so the two reported numbers never disagree."""


class EvidenceKindBreakdown(StrictModel):
    kind: EvidenceKind
    item_count: int = Field(ge=0)
    char_count: int = Field(ge=0)


class ContextMeasurement(StrictModel):
    """A deterministic, read-only measurement of an already-compiled
    `ContextPackage`. Never influences retrieval, ranking, or truncation --
    it only reports what the compiler already decided, so it can be added
    without changing any existing behavior."""

    call_number: int | None = Field(default=None, ge=1)
    task_id: str
    context_sha256: str | None = None
    model_context_window_tokens: int | None = Field(default=None, ge=1)
    repository_file_limit: int = Field(ge=0)
    excerpt_line_limit: int = Field(ge=0)
    agent_observation_budget_chars: int | None = Field(default=None, ge=0)
    files_included: int = Field(ge=0)
    candidate_file_count: int = Field(ge=0)
    files_truncated_by_limit: int = Field(ge=0)
    files_dropped_for_char_budget: int = Field(ge=0)
    excerpts_truncated_for_char_budget: int = Field(ge=0)
    total_transmitted_chars: int = Field(ge=0)
    total_transmitted_lines: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    model_window_utilization: float | None = Field(default=None, ge=0)
    composition: list[EvidenceKindBreakdown] = Field(default_factory=list)
    stable_evidence_count: int = Field(ge=0)
    new_evidence_count: int = Field(ge=0)
    stable_evidence_chars: int = Field(ge=0)
    new_evidence_chars: int = Field(ge=0)


def _identity_key(
    evidence: ContextEvidence,
) -> tuple[str, int | None, int | None, str | None]:
    # Matches the exact dedup key already used by BoundedAgentSession, so
    # "stable" here means the identical evidence item, not merely a similar
    # or overlapping one.
    return (evidence.path, evidence.start_line, evidence.end_line, evidence.content_sha256)


def measure_context(
    package: ContextPackage,
    *,
    call_number: int | None = None,
    model_context_window_tokens: int | None = None,
    agent_observation_budget_chars: int | None = None,
    previous_package: ContextPackage | None = None,
) -> ContextMeasurement:
    parameters = package.compiler_parameters
    if agent_observation_budget_chars is None:
        agent_loop_parameters = parameters.get("agent_loop")
        if isinstance(agent_loop_parameters, dict):
            agent_observation_budget_chars = agent_loop_parameters.get(
                "max_observation_chars"
            )

    total_chars = sum(len(item.content) for item in package.evidence)
    total_lines = sum(item.content.count("\n") + 1 for item in package.evidence)
    estimated_tokens = -(-total_chars // _CHARS_PER_TOKEN_ESTIMATE)
    utilization = (
        estimated_tokens / model_context_window_tokens
        if model_context_window_tokens
        else None
    )

    composition_totals: dict[EvidenceKind, list[int]] = {}
    for item in package.evidence:
        totals = composition_totals.setdefault(item.kind, [0, 0])
        totals[0] += 1
        totals[1] += len(item.content)
    composition = [
        EvidenceKindBreakdown(kind=kind, item_count=totals[0], char_count=totals[1])
        for kind, totals in sorted(
            composition_totals.items(), key=lambda pair: pair[0].value
        )
    ]

    stable_count = new_count = stable_chars = new_chars = 0
    if previous_package is None:
        new_count = len(package.evidence)
        new_chars = total_chars
    else:
        previous_keys = {_identity_key(item) for item in previous_package.evidence}
        for item in package.evidence:
            if _identity_key(item) in previous_keys:
                stable_count += 1
                stable_chars += len(item.content)
            else:
                new_count += 1
                new_chars += len(item.content)

    files_included = len(
        {item.path for item in package.evidence if not item.path.startswith("<")}
    )

    return ContextMeasurement(
        call_number=call_number,
        task_id=package.task_id,
        context_sha256=package.context_sha256,
        model_context_window_tokens=model_context_window_tokens,
        repository_file_limit=int(parameters.get("max_files", 0) or 0),
        excerpt_line_limit=int(parameters.get("max_excerpt_lines", 0) or 0),
        agent_observation_budget_chars=agent_observation_budget_chars,
        files_included=files_included,
        candidate_file_count=int(parameters.get("candidate_file_count", 0) or 0),
        files_truncated_by_limit=int(parameters.get("files_truncated_by_limit", 0) or 0),
        files_dropped_for_char_budget=int(
            parameters.get("files_dropped_for_char_budget", 0) or 0
        ),
        excerpts_truncated_for_char_budget=int(
            parameters.get("excerpts_truncated_for_char_budget", 0) or 0
        ),
        total_transmitted_chars=total_chars,
        total_transmitted_lines=total_lines,
        estimated_tokens=estimated_tokens,
        model_window_utilization=utilization,
        composition=composition,
        stable_evidence_count=stable_count,
        new_evidence_count=new_count,
        stable_evidence_chars=stable_chars,
        new_evidence_chars=new_chars,
    )
