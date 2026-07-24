# ADR 0056: Tavily as the authorized official-document search provider

- Status: Accepted (owner explicitly authorized Tavily after Brave Search's
  free tier was found to require a credit card and metered billing;
  implementation is real deterministic-fake-tested code, but no live call
  has been made -- no API key was available or used in this session)
- Date: 2026-07-23

## Context

ADR 0055 added `OfficialDocumentSearchProvider`, a harness-owned seam for
real official-document discovery, and deliberately shipped no concrete
vendor implementation behind it. It recommended Brave Search as a
reasonable default and the owner initially authorized it. Before wiring it
in, checking current (2026) pricing found that Brave discontinued its free
API tier in February 2026: it now requires a credit card on file, grants
roughly $5/month in credits (~1,000 queries), and bills overages at
~$5/1,000 queries with no default spending cap. The owner asked to compare
alternatives instead of accepting that.

A quick survey of the other three candidates ADR 0055 named:

- **Bing Web Search API** -- discontinued entirely; Microsoft retired it on
  August 11, 2025. Not usable at all.
- **Brave Search API** -- as above: card required, metered, no free
  no-card tier.
- **Tavily** -- 1,000 free queries/month, **no credit card required**,
  purpose-built for AI-agent/research use cases (closest philosophical fit
  to Research Mode's advisory, evidence-gathering role). Paid plans start
  at $30/month for 4,000 credits if usage ever exceeds the free tier.
- **Serper.dev** -- 2,500 free queries, no card required, cheapest paid
  tier (~$1/1,000 queries), but it is a Google-SERP wrapper rather than a
  research-oriented API.

The owner chose Tavily. This ADR supersedes the Brave-specific decision
before it was ever relied upon (the Brave provider code, its ADR, and every
reference to it were removed from the tree in the same working session,
never committed) and records Tavily as the one implemented provider
instead.

## Decision

`src/apoapsis/research/sources/tavily.py` adds
`TavilyOfficialDocumentSearchProvider`, implementing
`OfficialDocumentSearchProvider` (ADR 0055) exactly:

- `search(query, *, max_results)` sends a `POST` to
  `https://api.tavily.com/search` with a JSON body (`query`, a clamped
  `max_results` of 1-20, `search_depth: "basic"`, and answer/raw-content/
  image inclusion explicitly disabled to keep responses small and
  deterministic to parse) through the harness's existing restricted
  fetcher (`ResearchFetcher`/`ResearchFetchProcess`) -- this class never
  opens a socket itself, has no shell/filesystem access, and is never
  handed to the local model.
- The API key is read from `os.environ[api_key_env]` **at the moment of
  each call**, not cached on the instance, and sent only as an
  `Authorization: Bearer <key>` request header -- never in the request
  body, the query string, a returned `SearchResultCandidate`, a cache key,
  a prompt, or an audit artifact.
- A missing/empty credential raises `SearchProviderError` naming the
  configured environment-variable name (never a value) so a misconfigured
  deployment fails clearly instead of silently returning nothing.
- Tavily's `results[].{title,url,content}` shape is parsed into
  `SearchResultCandidate(title, url, snippet=content)`; entries missing a
  usable title/url are skipped rather than raising.
- Every returned result still passes through
  `OfficialDocumentationSource.search`'s existing, unchanged domain
  re-validation against `[research.sources.official_docs].allowed_domains`
  (ADR 0055) before it can become a candidate -- this provider does not get
  any more trust than the seam already grants any other provider.

### Configuration

```toml
[research.sources.official_docs]
enabled = true
allowed_domains = ["docs.python.org", "developers.google.com"]
search_provider = "tavily"
search_credentials_env = "TAVILY_API_KEY"   # optional; this is the default

[research.security]
allow_domains = [
  "docs.python.org", "developers.google.com", "api.tavily.com",
  # ... existing GitHub/Reddit entries
]
```

`research/factory.py`'s three-way branch on `search_provider` is now:
`"none"` (no provider, direct-URL-only, unchanged default), `"tavily"`
(constructs `TavilyOfficialDocumentSearchProvider` with
`search_credentials_env` or the `TAVILY_API_KEY` default), or anything else
(`ResearchConfigurationError`, naming both this ADR and ADR 0055, exactly
as before for unimplemented vendor names). `api.tavily.com` must be added
to `[research.security].allow_domains` -- the lower-level network
allowlist -- or every Tavily call fails closed at the restricted fetcher
before any request leaves the process, the same as any other destination
that adapter has never been told to trust.

## Consequences

- Official-document research can now perform real discovery instead of
  direct-URL-only lookups, once an owner supplies `TAVILY_API_KEY` (or
  their chosen variable name) and adds the relevant domains to both
  allowlists. Tavily's free tier (1,000 queries/month, no card) means this
  can be tried with zero billing setup, unlike Brave. Until configured,
  behavior is unchanged: `search_provider` defaults to `"none"` and nothing
  about direct-URL research changes.
- No model gained network, credential, or search-provider-selection
  authority: the provider is harness-constructed from configuration only,
  and its output is still deterministically domain-filtered and ranked
  before an already-restricted fetcher retrieves anything.
- This has deterministic fake-fetcher test coverage (missing-credential
  failure, response parsing, credential-non-leakage, and allowlist
  filtering through the full `OfficialDocumentationSource` path) in
  `tests/test_research_units.py::TavilySearchProviderTests`. It has **not**
  been exercised against the real Tavily API in this session -- no key was
  available -- and that remains explicitly unverified until someone runs
  it with real credentials and records the result in `docs/evaluation/`.
- `research/factory.py`'s branch that actually constructs
  `TavilyOfficialDocumentSearchProvider` from a full `ApoapsisConfig` has no
  dedicated test in this change (assembling a complete config for it was
  judged not worth the added surface right now); the provider class itself
  is fully covered, and the factory branch is a small, direct
  `if/elif/else` reviewed by hand. Add a factory-level test before treating
  that wiring as proven.
- Even Tavily's free tier is finite (1,000 queries/month); nothing in this
  change adds an application-level spend/query cap beyond the existing
  research budget (`max_queries`, `max_candidates`, etc., which bound how
  many search calls one research task can make, but not a monthly/account-
  wide ceiling across tasks). If usage across many tasks could approach
  the free-tier limit, add an explicit monthly-call budget before relying
  on this for routine use.

## Follow-up

- Set `TAVILY_API_KEY` (or a chosen variable name via
  `search_credentials_env`) in the environment the harness runs in, add the
  relevant vendor domains to both allowlists, and run a real, explicitly
  authorized query to confirm the end-to-end path before relying on it.
- Add a `research/factory.py`-level test constructing a full
  `ApoapsisConfig` with `search_provider = "tavily"` and asserting the
  resulting source's `search_provider` is a
  `TavilyOfficialDocumentSearchProvider` wired to the right environment
  variable name.
- If usage ever needs to scale past the free tier, revisit whether an
  account-wide (not just per-task) spend/query ceiling is needed given
  Tavily's prepaid-credit pricing.
