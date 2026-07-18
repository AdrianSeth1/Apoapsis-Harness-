# ADR 0004: Native Ollama frontier-proposal path

## Status

Accepted for Apoapsis Harness 0.4.0; context profiles amended in 0.4.1 and
deterministic import-neighbor expansion corrected in 0.4.2. Configurable,
audited sampling and unique edge-context patch normalization were added in
0.4.3.

## Decision

The implementation and repair role may use the native Ollama chat API through
the same provider-neutral `ModelProvider` boundary as a hosted frontier model.
Native Ollama frontier endpoints are restricted to loopback HTTP(S) URLs and do
not require placeholder credentials.

The generated development configuration uses `qwen3-coder:30b` for patch
proposals and `qwen3.6:27b` for Research Mode, with a zero-temperature default,
bounded output, a 32K context window, and thinking disabled for the coder and
specification extraction. Frontier runs expose deterministic `16k`, `32k`, and
`64k` context profiles. Each profile jointly controls the model window and the
repository excerpt budget so allocating more context can transmit more evidence.
Sampling temperature defaults to zero but is provider-configurable and included in the
reproducible request package; this permits an explicitly recorded model-recommended
sampling comparison without changing deterministic workflow authority.

## Trust boundary

Local execution does not make model output trusted. Specification validation,
constraint coverage, context selection, diff-only parsing, patch policy,
worktree application, verification, the single-repair budget, telemetry, and
audit recording are unchanged and remain deterministic.

The native adapter exposes no tools and grants no shell, Git, worktree, network,
credential, workflow-transition, or completion authority to the model.

## Consequences

- A complete vertical slice can run without a hosted model or API key.
- Native Ollama token, duration, load, thinking, and model-digest metadata flow
  through the existing telemetry report.
- Each audited frontier request records its context window, output limit,
  thinking setting, and timeout; its paired context package records the exact
  file, excerpt-line, and total-character limits.
- Searchable singular stems remain adjacent to plural query terms at bounded
  term limits, and Python imports are followed through two deterministic levels
  so package re-exports lead retrieval back to implementation modules.
- The authenticated OpenAI-compatible provider remains available for hosted
  comparisons.
- A 52 GB Q4 Coder-Next model is not made the default; it can be selected later
  without changing workflow policy.
- Exact Markdown diff wrappers, unmarked blank context, uniquely resolvable hunk
  coordinates, and CRLF worktree line endings are canonicalized deterministically
  and audited; ambiguous or semantic changes remain rejected.
- A hunk with missing edge context may receive one adjacent unchanged repository
  line only after its complete old side has exactly one source match. Apoapsis retains
  strict Git application instead of globally permitting zero-context patches.
