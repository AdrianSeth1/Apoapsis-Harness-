# Research Mode

Research Mode adds bounded external precedent to SOL without granting external
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

`sol init` writes a complete TOML example. The essential local-model section is:

```toml
[models.local_research]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3.6:27b"
api_key_env = "SOL_LOCAL_RESEARCH_API_KEY"
timeout_seconds = 600
max_output_tokens = 8192
context_window_tokens = 16384
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
allowlist or accept arbitrary model-selected sites as authoritative.

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
.sol/tasks/<task-id>/research/
  research-spec.json
  queries.jsonl
  candidates.jsonl
  retrieved-source-manifest.jsonl
  evidence.jsonl
  rejected-evidence.jsonl
  synthesis.json
  research-brief.md
  security-warnings.json
  telemetry.json
```

The retrieved-source manifest contains metadata and a digest, not source text.
Cached fetched content is sanitized before storage. Reddit cache entries use a
shorter configurable lifetime. Extraction and synthesis keys include the model,
prompt version, and repository dependency fingerprint, so a relevant model,
prompt, dependency, adapter, query, or retrieval-date change invalidates reuse.

Use `sol research inspect TASK-ID` to review the structured result,
`sol research refresh TASK-ID` to bypass reusable entries, and the cache inspect
and clear commands to manage local storage.

## Testing

The default suite is entirely offline:

```bash
python -m unittest discover -s tests -v
```

It includes malicious source fixtures, fake GitHub and Reddit adapters, cache and
budget assertions, a fake local research model, and an end-to-end verified patch
for the task-report example. Optional bounded live smoke tests require explicit
flags and never run by default:

```bash
SOL_RUN_LIVE_GITHUB_TESTS=1 python -m unittest tests.test_research_live -v
SOL_RUN_LIVE_REDDIT_TESTS=1 python -m unittest tests.test_research_live -v
```

The Reddit live test also requires `REDDIT_CLIENT_ID` and
`REDDIT_CLIENT_SECRET`.
