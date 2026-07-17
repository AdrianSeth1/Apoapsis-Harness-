# ADR 0003: Quarantined Local Research Mode

- Status: Accepted
- Date: 2026-07-17

## Context

The frontier vertical slice has strong repository context but no controlled way
to compare external implementation precedent or user-reported pain. Giving a
model unrestricted browsing, tools, or fetched-code execution would undermine
the deterministic authority boundary established by the substrate.

## Decision

SOL will use a separate `LOCAL_RESEARCH_MODEL` role for structured planning,
ranking, evidence extraction, and comparative synthesis. Native Ollama is the
preferred provider because it exposes JSON-schema output, thinking control,
token/duration metrics, and model loading metadata. The existing
OpenAI-compatible boundary remains a fallback.

The local model never performs network operations. Deterministic source adapters
and a dedicated restricted fetch process own all HTTPS requests. Allowed sources
are official documentation, GitHub, Reddit when explicitly configured, and
offline fixtures. Modes and trigger rules are deterministic and budgets are
validated before adapter calls.

All external content is untrusted. It is size-limited, sanitized, delimited,
license-classified, and offered only to the tool-free local model. Evidence is
accepted only from an exact sanitized excerpt. Source adapters supply immutable
provenance; the model cannot supply or modify it. Reddit is anecdotal. GitHub is
implementation precedent. Official documentation is authoritative only within
its documented scope. Popularity is a weak ranking signal, never proof.

Synthesis must reference known evidence, address every active project constraint,
meet the configured source-diversity floor, and declare that no external code
was copied. Only its compact brief and evidence IDs enter the frontier context.
The approved task, repository state, patch policy, isolated worktree, and
verification continue to control all workflow transitions and completion.

## Consequences

- External precedent can make plans more specific without gaining execution or
  policy authority.
- Research is reproducible through versioned schemas, source manifests, prompt
  hashes, model digests, dependency-aware cache keys, and complete audits.
- Source availability, API quotas, and local-model quality can cause research to
  fail closed or be skipped, but cannot silently weaken patch safety.
- Reddit is an optional configured integration and complete threads are not kept
  in the audit directory.
- This milestone does not create authoritative research memory, copy external
  code, execute downloads, clone arbitrary repositories, install packages, add
  embeddings, learn routing, or create a generalized browser or agent swarm.
