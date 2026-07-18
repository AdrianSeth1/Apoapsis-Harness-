from __future__ import annotations

import hashlib
from html.parser import HTMLParser

from apoapsis.research.fetcher import FetchRequest, ResearchFetcher
from apoapsis.research.schemas import (
    LicenseClassification,
    ResearchQuery,
    ResearchSourceName,
    ResearchSourceType,
    RetrievedSource,
    SourceBudget,
    SourceCandidate,
    SourceLocator,
)
from apoapsis.research.security import validate_domain


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


class OfficialDocumentationSource:
    adapter_name = "official_docs"
    adapter_version = "1"

    def __init__(self, fetcher: ResearchFetcher, allow_domains: list[str]) -> None:
        self.fetcher = fetcher
        self.allow_domains = allow_domains

    async def search(
        self, query: ResearchQuery, budget: SourceBudget
    ) -> list[SourceCandidate]:
        candidates: list[SourceCandidate] = []
        for url in query.urls[: budget.max_candidates]:
            validate_domain(url, self.allow_domains)
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
            candidates.append(
                SourceCandidate(
                    candidate_id=f"CAND-DOC-{digest}",
                    source=ResearchSourceName.OFFICIAL_DOCS,
                    source_type=ResearchSourceType.OFFICIAL_DOCUMENTATION,
                    title=url,
                    url=url,
                    snippet=query.query,
                    deterministic_score=0.8,
                    deduplication_key=url.lower(),
                )
            )
        return candidates

    async def fetch(self, candidate: SourceCandidate) -> RetrievedSource:
        response = await self.fetcher.fetch(FetchRequest(url=candidate.url))
        content = response.body
        if response.content_type == "text/html":
            parser = _TextExtractor()
            parser.feed(content)
            content = "\n".join(parser.parts)
        return RetrievedSource(
            candidate_id=candidate.candidate_id,
            source=ResearchSourceName.OFFICIAL_DOCS,
            source_type=ResearchSourceType.OFFICIAL_DOCUMENTATION,
            title=candidate.title,
            locator=SourceLocator(url=response.final_url),
            content=content,
            metadata={"content_type": response.content_type},
            license=LicenseClassification.IDEA_ONLY,
        )

