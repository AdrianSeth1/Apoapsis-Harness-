# ADR 0055: Research failure classification, query feasibility, bounded recovery, and the official-doc search-provider seam

- Status: Accepted (deterministic engine/config/test changes are real and
  ready to run; no concrete search-provider vendor is implemented or
  claimed live)
- Date: 2026-07-23

## Context

Operation `DISCOP-796622810B804FE59E87536D` reproduced a real research-mode
failure. In `GITHUB_ONLY` mode, four research questions were generated;
three needed authoritative Gmail/Twilio/Vonage documentation and were
planned against `official_docs`, but every one of those planned queries had
`urls: []`. `OfficialDocumentationSource.search()` was, and remains at its
core, a direct-URL-to-candidate converter -- it performs no discovery of its
own -- so those three queries silently produced nothing. The fourth
question's GitHub query returned five candidates, all irrelevant; the local
extraction model correctly returned no findings for all five, and
`rejected-evidence.jsonl` correctly recorded `local extraction found no
relevant evidence` for each. `evidence.jsonl` ended up empty, which
`ResearchEngine.execute` had exactly one way to report:

```
ResearchEngineError: no provenance-valid research evidence remained
```

That message is wrong for this case. Nothing failed provenance validation;
nothing was even proposed. The real causes were mechanical and knowable
before any fetch: three queries could not possibly produce candidates given
the configured domains and no search capability, and the one viable query's
results were genuinely irrelevant. Collapsing every empty-evidence path into
one generic provenance message hid an actionable, fixable configuration gap
behind a message that reads like a security/data-integrity failure.

## Decisions

### Distinguish why research produced no evidence

`ResearchEngineError` (`src/apoapsis/research/engine.py`) now carries a
`reason: ResearchFailureReason` and a `detail: dict[str, object]` (counts
and short labels only -- never prompts, credentials, or source content).
`ResearchFailureReason` distinguishes:

- `NO_SOURCE_CANDIDATES` -- no query produced any candidate, or every
  selected candidate failed to fetch.
- `PLANNED_SOURCE_UNUSABLE` -- every planned query was infeasible for its
  selected adapter before any retrieval was attempted (this ADR's
  reproduction, when *no* query is viable at all).
- `NO_RELEVANT_FINDINGS` -- sources were retrieved but local extraction
  found nothing relevant (this ADR's reproduction, exactly: 5 retrieved, 5
  produced no relevant findings).
- `PROVENANCE_REJECTED` -- findings were proposed but rejected by
  provenance/security validation (unknown question ID, excerpt not an exact
  substring, quarantined/injected text).
- `INSUFFICIENT_SOURCE_DIVERSITY` -- evidence existed but did not meet
  `synthesis.minimum_distinct_sources`.

The reproduced scenario now ends with:

```
No relevant research evidence was extracted: 5 sources were retrieved and
all 5 produced no relevant findings. One bounded recovery attempt was made
over the same retrieved sources and found no additional evidence.
```

`apoapsis.discovery.operation_service.run_discovery_operation` catches
`ResearchEngineError` specifically and appends a `reason`-keyed recommended
operator action (`_RESEARCH_FAILURE_RECOMMENDATIONS`) to the operation's
persisted `error` string, so a failed discovery operation names both what
happened and what to do about it -- for `PLANNED_SOURCE_UNUSABLE` this
explicitly says authoritative vendor documentation was required but
unavailable under the configured domains/search capability. No new
database column was added; the existing `DiscoveryOperationRecord.error`
string field carries the classified summary, and every detailed audit
artifact (`unusable-queries.jsonl`, `candidates.jsonl`,
`retrieved-source-manifest.jsonl`, `rejected-evidence.jsonl`,
`recovery.json`) is still written to the task's research audit directory
before the exception propagates.

### Query feasibility is checked before retrieval

`ResearchEngine._validated_plan` now calls
`_infeasibility_reason(planned_query)` for every planned query that passed
the existing source/mode-allowlist check, before it is added to the
executable query list. Today this has one concrete rule: an `official_docs`
query with no URLs and no configured search provider, or whose URLs are all
outside `[research.sources.official_docs].allowed_domains`, is infeasible.
Infeasible queries are recorded in a new `unusable-queries.jsonl` audit file
with their reason and excluded from the search loop entirely -- they no
longer silently contribute nothing while looking like they were tried. If
literally no planned query is viable, `_validated_plan` fails early with
`PLANNED_SOURCE_UNUSABLE` and a reason breakdown instead of continuing with
whatever unrelated adapter happens to still work. If at least one query is
viable (as in the reproduction, where the GitHub query was), execution
proceeds with the viable subset only -- an unusable question does not block
questions that can actually be answered.

This check cannot expand what a model is allowed to do: it only ever
narrows the planned queries the engine will execute, using configuration
(`allowed_domains`, `search_provider_configured`) the model never controls.

### Fair allocation across research questions, not just source adapters

`SourceCandidate` gained an optional `research_question_id` field, set by
the engine (not adapters) from the originating query immediately after
`source.search()` returns. `SourceRanker.rank` (`ranking.py`) now applies a
per-question cap mirroring the existing per-source cap
(`max(2, (limit + 1) // 2)`, only engaged when more than one question is
actually represented among the candidates) alongside the pre-existing
per-repository and per-source caps and the pre-existing per-query candidate
budget split. A single broad query for one research question cannot
consume the entire fetch allowance and starve every other question's
candidates, whether or not the broad and narrow queries share a source
adapter. Fixtures/tests that predate this field are unaffected: the cap
only activates when more than one non-null question ID is present, so a
single-question research task selects exactly as before.

Both `retrieved-source-manifest.jsonl` and `candidates.jsonl` now carry
`research_question_id`, so candidate and selected-source counts per
question (as well as per source, already present) can be read directly
from existing audit artifacts without a new file format.

### One bounded, deterministic recovery attempt

When retrieval produced retrieved sources but the first extraction pass
found no relevant evidence at all, `ResearchEngine.execute` now runs exactly
one additional extraction pass (`_extract_evidence_for_sources(...,
recovery=True, rejection_context=...)`) over the *same* already-fetched,
already-sanitized sources -- no new fetch, no new candidates, no larger
time/context/candidate/fetched-source ceiling. The recovery prompt appends a
concise, deterministic summary of why the first pass failed (reason counts
only) and asks the model to look again more broadly, explicitly instructing
it not to lower the bar for what counts as an exact-substring excerpt. The
recovery pass uses a distinct cache key (`"recovery": true` in the payload)
so a cached failure is never mistaken for a cached recovery result. There is
no second recovery round and no recursion regardless of outcome; a
`recovery.json` audit file always records whether recovery was attempted,
its trigger, and how much evidence (if any) it found. `ResearchTelemetry`
gained `recovery_attempted`, `recovery_evidence_found`,
`sources_with_no_relevant_findings`, and
`sources_with_provenance_rejected_findings`.

### Real official-document discovery gets a seam, not a vendor

`OfficialDocumentationSource` (`sources/official.py`) is restructured to the
architecture requirement's exact pipeline: query proposal -> deterministic
search provider -> domain filtering -> candidate ranking (existing
`SourceRanker`, unchanged) -> restricted fetcher (existing
`ResearchFetchProcess`, unchanged). A new protocol,
`OfficialDocumentSearchProvider`
(`sources/search_provider.py`), is the harness-owned boundary a future
provider must implement: `async def search(self, query: str, *,
max_results: int) -> list[SearchResultCandidate]`. Its signature has no
credential parameter by construction -- a provider must read its own
credential directly from the one dedicated environment variable named in
`[research.sources.official_docs].search_credentials_env`, never accept one
via the query path, and never let it enter a prompt, log, cache key, or
audit artifact. Every result a provider returns is re-validated against
`allowed_domains` by `OfficialDocumentationSource.search` itself before
becoming a candidate -- a search result is untrusted external content, not a
pre-approved URL, and gets no special trust for merely appearing in results.
Redirects are re-validated by the existing, unchanged
`_RestrictedRedirectHandler` at fetch time.

**No concrete provider ships in this repository.** `search_provider =
"none"` is the only value `research/factory.py` currently accepts;
anything else raises `ResearchConfigurationError` naming ADR 0055 rather
than silently ignoring the configured name or guessing at a vendor. Which
vendor to authorize (Brave Search API, Bing Web Search API, Tavily, Serper,
or another) is an explicit product/architecture decision this ADR
deliberately defers to the owner -- it is not something a coding agent
should choose unilaterally. **Recommendation, not a decision:** of the
common options, the Brave Search API is the most likely fit for this
harness's constraints (a straightforward REST API with a JSON response
shape a thin deterministic adapter can parse without HTML scraping, a
usable free tier for bounded research-mode volumes, and no dependency on a
Microsoft/Google cloud account). Implementing it would mean: adding a
`BraveOfficialDocumentSearchProvider` behind this same protocol in
`research/sources/`, wiring `research_credentials_env` to read the API key
via the restricted fetch process (or an equivalently harness-owned HTTP
call, never a call the local model makes itself), and changing
`research/factory.py`'s `search_provider != "none"` branch to construct it
-- all without touching `OfficialDocumentationSource`, `ResearchEngine`, or
the security/quarantine pipeline. That implementation, and the owner's
sign-off on the vendor choice, remains future work.

Direct URLs continue to work identically with no search provider configured
at all -- this was, and remains, the only way `official_docs` produces
candidates today.

### Configuration guidance, not a wider default allowlist

The generated project configuration (`apoapsis init`, `cli/app.py`) still
defaults `[research.sources.official_docs].allowed_domains` to
`["docs.python.org"]` -- this ADR does not widen it, silently or otherwise,
for existing or new projects. It adds inline comments with concrete examples
(`developers.google.com` for Gmail, `www.twilio.com` for Twilio,
`developer.vonage.com` for Vonage) and an explicit reminder that a domain
must be present in **both** `[research.sources.official_docs].allowed_domains`
and the lower-level `[research.security].allow_domains` for official-doc
research to reach it -- the query-feasibility check above now also makes a
missing domain surface as an unusable-query reason before retrieval, rather
than as a silent empty result after.

## Consequences

- The reproduced operation no longer ends in a misleading provenance
  message; it names retrieved/no-findings/provenance-rejected counts and
  whether recovery was attempted.
- An official-doc query that cannot possibly produce candidates is recorded
  and excluded before any network access is attempted, and a specification
  that is *entirely* unanswerable given current configuration fails fast
  with an actionable reason instead of silently returning nothing.
- A broad query for one research question can no longer consume the entire
  fetch allowance meant to be shared across every viable question.
- Research Mode gets exactly one bounded, audited recovery attempt on
  genuine total extraction failure -- never a second attempt, never a larger
  budget, never new network access.
- Real web-search-backed official-document discovery has a secure,
  provider-agnostic seam and an explicit, disclosed vendor decision point,
  but is not implemented; nothing in this repository should be described as
  live web search working.
- No model gained shell, filesystem, Git, raw network, workflow-transition,
  retry-limit, verification, completion, or audit authority. Query
  feasibility, fair allocation, recovery triggering/bounding, and all search
  results are harness-decided from configuration and deterministic rules;
  the model only ever proposes typed queries and extraction findings.

## Coverage

`tests/test_research_units.py` adds `OfficialDocsSearchProviderSeamTests`
(no-URL/no-provider fail-closed behavior, direct-URL regression, provider
results filtered to the allowlist, the provider-protocol's lack of a
credential parameter, and the config-shape assurance that credentials are
never anything but an environment-variable *name*) and
`FairAllocationRankingTests` (multi-question fairness and a single-question
regression guard). `tests/test_research_integration.py` adds
`ResearchFailureClassificationTests`: the exact reproduction shape (three
unusable official-doc queries, one viable but irrelevant GitHub query, five
retrieved sources, bounded recovery, the final actionable message), a
domain-outside-allowlist unusable-query case, a successful bounded-recovery
case, and a provenance-rejected-findings classification case. None of these
tests were executed in this authoring session per the owner's standing
instruction not to run tests; see `HANDOFF.md`'s Snapshot and the commands
listed there for what to run and how to interpret the result.
