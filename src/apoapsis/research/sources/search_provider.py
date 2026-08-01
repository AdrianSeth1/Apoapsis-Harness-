from __future__ import annotations

from typing import Protocol, runtime_checkable

from apoapsis.research.schemas import SearchResultCandidate


class SearchProviderError(RuntimeError):
    """A configured official-document search provider could not be used."""


@runtime_checkable
class OfficialDocumentSearchProvider(Protocol):
    """Deterministic search-provider seam for real official-document
    discovery (ADR 0055).

    ``OfficialDocumentationSource`` was, until ADR 0055, a direct-URL
    fetcher only: it converted model-supplied ``urls`` into candidates and
    performed no discovery of its own. This protocol is the harness-owned
    boundary a real search backend must implement to add actual discovery,
    without ever handing the local model network access.

    Implementations MUST:

    * perform the HTTP call themselves (or via the harness's restricted
      fetch process) -- the local model never sees a URL to fetch;
    * read credentials only from the single dedicated environment-variable
      name configured for this purpose, never from a prompt, and never log
      or persist that value;
    * return results as untrusted ``SearchResultCandidate`` data only --
      ``OfficialDocumentationSource.search`` is responsible for filtering
      every result against the configured ``allowed_domains`` allowlist
      before it becomes a ``SourceCandidate`` the ranker or model can see.

    No concrete implementation ships in this repository yet. See ADR 0055
    for the specific vendor decision this seam intentionally defers.
    """

    provider_name: str

    async def search(
        self, query: str, *, max_results: int
    ) -> list[SearchResultCandidate]: ...


__all__ = [
    "OfficialDocumentSearchProvider",
    "SearchProviderError",
]
