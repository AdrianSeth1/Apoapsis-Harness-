from __future__ import annotations

from typing import Protocol, runtime_checkable

from apoapsis.research.schemas import (
    ResearchQuery,
    RetrievedSource,
    SourceBudget,
    SourceCandidate,
)


@runtime_checkable
class ResearchSource(Protocol):
    adapter_name: str
    adapter_version: str

    async def search(
        self, query: ResearchQuery, budget: SourceBudget
    ) -> list[SourceCandidate]: ...

    async def fetch(self, candidate: SourceCandidate) -> RetrievedSource: ...

