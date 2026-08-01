# Research Mode

Research Mode adds bounded external precedent to Apoapsis without granting external
content or a model workflow authority. It runs after the user approves the task
specification and before deterministic repository context compilation.

## Flow and authority

```text
approved task specification
  -> deterministic trigger and mode policy
  -> local model proposes structured questions and queries
  -> configured source adapters search within fixed budgets
  -> restricted fetch process performs allowlisted HTTPS requests
  -> deterministic sanitizer quarantines suspicious instructions
  -> local model proposes evidence from exact sanitized excerpts
  -> harness attaches immutable provenance, authority, and license
  -> local model compares evidence and proposes a project adaptation
  -> harness validates evidence IDs and complete constraint coverage
  -> compact brief enters the normal frontier context package
  -> diff policy, isolated worktree, and verification decide the outcome
```

The local researcher has no shell, file, Git, package-manager, credential,
worktree, deployment, or network tools. Network operations are performed by a
single-purpose process with a scrubbed environment, isolated temporary working
directory, HTTPS domain allowlist, content-type and size limits, redirect limit,
and timeout. The worker has no code path for opening or executing fetched files.
Archives, executables, binaries, and package artifacts are rejected by suffix.

GitHub authentication is resolved by the deterministic adapter in this order:
an existing GitHub CLI session, `GITHUB_TOKEN`, then anonymous access. Tokens are
used only as request headers and are not included in prompts, cache keys, or
audit artifacts. Reddit uses its approved OAuth API and is disabled by default.

## Configuration

`apoapsis init` writes a complete TOML example. The essential local-model section is:

```toml
[models.local_research]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3.6:27b"
api_key_env = "APOAPSIS_LOCAL_RESEARCH_API_KEY"
timeout_seconds = 600
max_output_tokens = 8192
context_window_tokens = 32768
max_structured_retries = 1

[models.local_research.modes.extraction]
think = false
require_structured_output = true

[models.local_research.modes.synthesis]
think = true
require_structured_output = true
```

Use the native Ollama provider when available. It sends the Pydantic JSON schema
through Ollama's native `format` field and records model digest, prompt hash,
input/output/thinking tokens, prompt evaluation time, generation time, model
load time, structured validation, and retry count. Set `provider` to
`openai_compatible` only when a compatible local endpoint is required.
Local-research endpoints must use a loopback host. The fallback uses a separate
credential variable and configuration rejects reuse of the frontier key name.

The research budget is deterministic:

```toml
[research]
default_mode = "AUTO"

[research.budget]
max_queries = 8
max_candidates = 30
max_fetched_sources = 12
max_extracted_characters_per_source = 20000
max_research_context_tokens = 30000
max_seconds = 180
```

`OFF` disables research. `AUTO` uses trigger rules. `GITHUB_ONLY` permits GitHub
and official docs. `COMMUNITY` permits Reddit. `FULL` permits all configured
sources. A mode never enables an adapter disabled in configuration.

Official documentation uses a separate `allowed_domains` list under
`[research.sources.official_docs]`; it does not inherit the broader network
allowlist or accept arbitrary model-selected sites as authoritative. A domain
must be present in **both** this list and the lower-level
`[research.security].allow_domains` for official-doc research to reach it --
add every ecosystem the project actually needs to both lists, for example
`developers.google.com` for Gmail, `www.twilio.com` for Twilio, and
`developer.vonage.com` for Vonage. `apoapsis init`'s generated configuration
still defaults to `["docs.python.org"]` only; this is deliberate (research
never gets unrestricted internet access by default), and the comments in the
generated file point at these examples rather than silently widening any
existing project's allowlist.

`OfficialDocumentationSource` was, until ADR 0055, a direct-URL-to-candidate
converter only: it performed no discovery. It still supports direct URLs
identically with no further configuration, and this remains the only way it
produces candidates when no official-document search provider is
configured. ADR 0055 added the seam for real discovery --
`OfficialDocumentSearchProvider` in `research/sources/search_provider.py`
-- and ADR 0056 records the owner's explicit authorization of Tavily as
the one concrete, implemented provider
(`TavilyOfficialDocumentSearchProvider` in `research/sources/tavily.py`;
Brave Search was the initial pick but was dropped after its free tier
turned out to require a credit card and metered billing, unlike Tavily's
1,000-query/month no-card free tier). Set `search_provider = "tavily"` and
`search_credentials_env` (or accept the `TAVILY_API_KEY` default) under
`[research.sources.official_docs]`, add `api.tavily.com` to
`[research.security].allow_domains`, and provide the API key via that
environment variable to enable it. Any other `search_provider` value still
fails clearly in `research/factory.py` rather than guessing at Bing, Brave,
Serper, or another vendor -- only Tavily is implemented. **No live call to
the real Tavily API has been made or verified**; the integration has
deterministic fake-fetcher test coverage
only. Every result a configured provider returns is re-validated against
`allowed_domains` before it can become a candidate -- search results are
untrusted external content, not pre-approved URLs. A provider's credential
is read only from the one environment variable named in
`search_credentials_env`, at the moment of each call, never cached, and
never placed in a prompt, cache key, or audit artifact; the seam's `search`
method has no credential parameter at all, so a credential structurally
cannot reach a prompt through it.

Before any query is searched, the engine checks whether its selected adapter
can actually produce candidates given current configuration (today: whether
an `official_docs` query has usable URLs or a configured search provider,
and whether its URLs are covered by `allowed_domains`). Infeasible queries
are recorded in `unusable-queries.jsonl` with a reason and excluded from
retrieval rather than silently contributing nothing. If literally no
planned query is viable, research fails immediately with an actionable
reason instead of proceeding with an unrelated adapter; if at least one
query is viable, execution proceeds with that subset, and the unusable ones
are still visible in the audit trail.

The global candidate ceiling is divided across all validated planned queries,
so one broad first query cannot prevent later, more specific questions from
being searched. When only one source adapter actually returns candidates, it
may fill the fetch budget; cross-source balancing applies when multiple sources
are present, and, since ADR 0055, so does cross-question balancing at the
final source-selection/ranking step -- a broad query for one research
question cannot consume the entire fetch allowance meant to be shared with
every other viable question, even when they share a source adapter.

A retrieved source from which the local model extracts no relevant finding is
written to `rejected-evidence.jsonl` with that reason instead of disappearing
behind a generic empty-evidence error. If every retrieved source's first
extraction pass found nothing relevant, the engine runs exactly one bounded
recovery pass over the same already-fetched sources (no new fetch, no larger
budget, never a second recovery round), giving the model a concise summary
of why the first pass failed and one more chance; `recovery.json` in the
task's research audit directory always records whether recovery was
attempted and what it found. The final failure message, when evidence still
cannot be produced, distinguishes at least: no source candidates at all;
every planned query being unusable for its adapter; sources retrieved but
nothing relevant extracted; findings proposed but rejected by
provenance/security validation; and evidence present but below the
configured source-diversity minimum -- for example, a run where five sources
were retrieved and all five produced no relevant findings now ends with
"No relevant research evidence was extracted: 5 sources were retrieved and
all 5 produced no relevant findings," not a generic provenance error. See
ADR 0055 for the full classification and the discovery operation's
resulting recommended-action text.

Enable Reddit only after configuring approved API access:

```toml
[research.sources.reddit]
enabled = true
priority = 4
client_id_env = "REDDIT_CLIENT_ID"
client_secret_env = "REDDIT_CLIENT_SECRET"
user_agent = "your-registered-client/1.0"
purposes = ["user_pain_points", "product_expectations", "failure_discovery"]
```

Reddit evidence is always anecdotal and `IDEA_ONLY`. It must not establish
security rules, API semantics, legal requirements, compatibility, or correct
implementation details.

## Quarantine, provenance, and licenses

Every source excerpt sent to the local model is enclosed in
`UNTRUSTED_EXTERNAL_CONTENT` delimiters. Deterministic rules flag and replace
lines that request instruction overrides, prompts, commands, downloads,
repository uploads, environment reads, disabled checks, rule changes, tokens,
or trust promotion. The system boundary independently tells the local model that
source text is evidence only; phrase detection is not the sole defense.

An extracted excerpt is accepted only when it is an exact substring of the
sanitized source. The harness—not the model—adds the immutable locator,
retrieval time, source type, authority, license, and injection flags. Unknown
evidence IDs, unknown constraint IDs, missing active constraint coverage,
adopted malicious instructions, inadequate source diversity, or an empty
comparative synthesis fail closed.

GitHub licenses are classified conservatively. Unknown or absent licenses are
`IDEA_ONLY`; custom and weak-copyleft licenses require review; GPL and AGPL are
incompatible for reuse; permissive licenses are recorded but still require
provenance. Research Mode never copies external code in this milestone.

## Audits and cache

Each triggered task writes:

```text
.apoapsis/tasks/<task-id>/research/
  research-spec.json
  queries.jsonl
  unusable-queries.jsonl
  candidates.jsonl
  retrieved-source-manifest.jsonl
  evidence.jsonl
  rejected-evidence.jsonl
  recovery.json
  synthesis.json
  research-brief.md
  security-warnings.json
  telemetry.json
```

`unusable-queries.jsonl` (ADR 0055) records every planned query excluded
before retrieval, with its research question, source, and reason.
`candidates.jsonl` and `retrieved-source-manifest.jsonl` both carry
`research_question_id`, so candidate and selected-source counts per
question (as well as per source) can be read directly from these files.
`recovery.json` always records whether the one bounded recovery attempt was
triggered, why, and how much evidence (if any) it found; these files are
written even when the task ultimately fails, so the audit trail explains
the failure without needing to re-run anything.

The retrieved-source manifest contains metadata and a digest, not source text.
Cached fetched content is sanitized before storage. Reddit cache entries use a
shorter configurable lifetime. Extraction and synthesis keys include the model,
prompt version, and repository dependency fingerprint, so a relevant model,
prompt, dependency, adapter, query, or retrieval-date change invalidates reuse.

Use `apoapsis research inspect TASK-ID` to review the structured result,
`apoapsis research refresh TASK-ID` to bypass reusable entries, and the cache inspect
and clear commands to manage local storage.

## Testing

The default suite is entirely offline:

```bash
python -m unittest discover -s tests -v
```

It includes malicious source fixtures, fake GitHub and Reddit adapters, cache and
budget assertions, a fake local research model, and an end-to-end verified patch
for the task-report example. `tests/test_research_units.py` also covers the
official-doc search-provider seam (no-URL/no-provider fail-closed behavior,
the still-supported direct-URL path, provider results filtered to
`allowed_domains`, and the credential-name-only config shape) and
cross-question fair allocation in `SourceRanker`; `tests/test_research_integration.py`
adds `ResearchFailureClassificationTests`, which reproduces the exact
unusable-official-doc-query/irrelevant-GitHub-results scenario end to end,
plus separate cases for a domain-outside-allowlist unusable query, a
successful bounded recovery, and provenance-rejected findings (ADR 0055).
`TavilySearchProviderTests` (ADR 0056) covers the one implemented search
provider against a fake fetcher: missing-credential failure, response
parsing, credential non-leakage, and allowlist filtering through the full
`OfficialDocumentationSource` path. None of this is exercised against the
real Tavily API -- it is deterministic fake-provider coverage only. Optional
bounded live smoke tests require explicit flags and never run by default:

```bash
APOAPSIS_RUN_LIVE_GITHUB_TESTS=1 python -m unittest tests.test_research_live -v
APOAPSIS_RUN_LIVE_REDDIT_TESTS=1 python -m unittest tests.test_research_live -v
```

The Reddit live test also requires `REDDIT_CLIENT_ID` and
`REDDIT_CLIENT_SECRET`.
