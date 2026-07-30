from __future__ import annotations

import json
import os

from apoapsis.research.fetcher import FetchRequest, ResearchFetcher
from apoapsis.research.schemas import SearchResultCandidate
from apoapsis.research.sources.search_provider import SearchProviderError

TAVILY_SEARCH_ENDPOINT = "https://api.tavily.com/search"


class TavilyOfficialDocumentSearchProvider:
    """ADR 0056: the concrete official-document search provider the owner
    explicitly authorized, implementing the ``OfficialDocumentSearchProvider``
    protocol from ADR 0055.

    Every network request goes through the harness's existing restricted
    fetcher (``fetcher``, typically a ``ResearchFetchProcess``) -- this
    class never opens a socket itself. The API key is read from the single
    dedicated environment variable named by
    ``[research.sources.official_docs].search_credentials_env`` at the
    moment of each call, never cached on the instance, never logged, and
    never placed anywhere the local model, a prompt, a cache key, or an
    audit artifact could see it. The fetcher's own domain allowlist must
    include ``api.tavily.com`` (in ``[research.security].allow_domains``) or
    every call fails closed before any request is made, exactly like any
    other restricted-fetch destination.

    Results are returned as untrusted ``SearchResultCandidate`` data only;
    ``OfficialDocumentationSource.search`` -- not this class -- is
    responsible for re-validating every result URL against
    ``[research.sources.official_docs].allowed_domains`` before it can
    become a candidate.
    """

    provider_name = "tavily"

    def __init__(self, fetcher: ResearchFetcher, api_key_env: str) -> None:
        self.fetcher = fetcher
        self.api_key_env = api_key_env

    async def search(
        self, query: str, *, max_results: int
    ) -> list[SearchResultCandidate]:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise SearchProviderError(
                "Tavily requires the environment variable named by "
                f"search_credentials_env ({self.api_key_env!r}) to be set; "
                "it was empty or unset"
            )
        count = max(1, min(max_results, 20))
        body = json.dumps(
            {
                "query": query,
                "max_results": count,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
            }
        )
        response = await self.fetcher.fetch(
            FetchRequest(
                url=TAVILY_SEARCH_ENDPOINT,
                method="POST",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                body=body,
            )
        )
        try:
            raw = json.loads(response.body)
        except json.JSONDecodeError as exc:
            raise SearchProviderError(
                "Tavily returned a non-JSON response"
            ) from exc
        results = raw.get("results", []) if isinstance(raw, dict) else []
        candidates: list[SearchResultCandidate] = []
        for item in results[:count]:
            url_value = item.get("url")
            title = item.get("title") or url_value
            if not url_value or not title:
                continue
            candidates.append(
                SearchResultCandidate(
                    title=title,
                    url=url_value,
                    snippet=item.get("content", "") or "",
                )
            )
        return candidates


__all__ = ["TAVILY_SEARCH_ENDPOINT", "TavilyOfficialDocumentSearchProvider"]
