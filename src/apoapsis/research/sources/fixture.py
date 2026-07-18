from __future__ import annotations

from apoapsis.research.schemas import (
    ResearchQuery,
    RetrievedSource,
    SourceBudget,
    SourceCandidate,
)


class FixtureSource:
    adapter_name = "fixture"
    adapter_version = "1"

    def __init__(
        self,
        candidates_by_query: dict[str, list[SourceCandidate]],
        sources_by_candidate: dict[str, RetrievedSource],
    ) -> None:
        self.candidates_by_query = candidates_by_query
        self.sources_by_candidate = sources_by_candidate

    async def search(
        self, query: ResearchQuery, budget: SourceBudget
    ) -> list[SourceCandidate]:
        return list(self.candidates_by_query.get(query.query, []))[
            : budget.max_candidates
        ]

    async def fetch(self, candidate: SourceCandidate) -> RetrievedSource:
        return self.sources_by_candidate[candidate.candidate_id]

