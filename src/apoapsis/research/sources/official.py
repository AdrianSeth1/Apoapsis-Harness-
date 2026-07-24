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
from apoapsis.research.security import ResearchSecurityError, validate_domain
from apoapsis.research.sources.search_provider import OfficialDocumentSearchProvider


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


class OfficialDocumentationSource:
    """query proposal -> deterministic search provider -> domain filtering
    -> candidate ranking -> restricted fetcher (ADR 0055).

    Two independent ways to produce candidates:

    1. Direct URLs (``query.urls``). This is the original, still-supported
       behavior: the local model can only ever name a URL, never fetch one,
       and every URL is validated against ``allow_domains`` before it
       becomes a candidate.
    2. A configured ``search_provider`` (optional). When present, it is
       asked to search for ``query.query`` and every returned result is
       independently re-validated against ``allow_domains`` -- search
       results are untrusted external content, not pre-approved URLs, so
       they get no special trust merely for coming back from a search call.

    With neither a URL nor a configured provider, this adapter cannot
    produce candidates. ``ResearchEngine`` is expected to detect that case
    before calling ``search`` (see ``ResearchEngine._infeasibility_reason``)
    so it is recorded as an unusable query with a clear reason rather than
    silently contributing nothing; ``search`` itself still fails closed
    with a clear error if it is ever reached in that state.
    """

    adapter_name = "official_docs"
    adapter_version = "2"

    def __init__(
        self,
        fetcher: ResearchFetcher,
        allow_domains: list[str],
        search_provider: OfficialDocumentSearchProvider | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.allow_domains = allow_domains
        self.search_provider = search_provider

    @property
    def search_provider_configured(self) -> bool:
        return self.search_provider is not None

    async def search(
        self, query: ResearchQuery, budget: SourceBudget
    ) -> list[SourceCandidate]:
        if query.urls:
            return self._candidates_from_urls(query, budget)
        if self.search_provider is None:
            raise ResearchSecurityError(
                "official_docs query supplied no URLs and no search provider "
                "is configured; this query should have been rejected as "
                "unusable before retrieval"
            )
        results = await self.search_provider.search(
            query.query, max_results=budget.max_candidates
        )
        candidates: list[SourceCandidate] = []
        for result in results:
            try:
                validate_domain(result.url, self.allow_domains)
            except ResearchSecurityError:
                # Search-engine results are untrusted external content, not
                # user-approved URLs. A result outside the configured
                # official-document allowlist is dropped rather than
                # trusted as authoritative merely for appearing in results.
                continue
            digest = hashlib.sha256(result.url.encode("utf-8")).hexdigest()[:16]
            candidates.append(
                SourceCandidate(
                    candidate_id=f"CAND-DOC-{digest}",
                    source=ResearchSourceName.OFFICIAL_DOCS,
                    source_type=ResearchSourceType.OFFICIAL_DOCUMENTATION,
                    title=result.title,
                    url=result.url,
                    snippet=result.snippet,
                    deterministic_score=0.6,
                    deduplication_key=result.url.lower(),
                )
            )
            if len(candidates) >= budget.max_candidates:
                break
        return candidates

    def _candidates_from_urls(
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
