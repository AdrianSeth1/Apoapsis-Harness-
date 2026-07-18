# ADR 0011: Deterministic context-quality improvements (Apoapsis 1.0b)

- Status: Accepted
- Date: 2026-07-18

## Context

ADR 0010 made context size and composition measurable without changing
retrieval. The phased plan identified the next safe increment as improving the
existing deterministic Git/lexical/symbol/import/test path before considering
embeddings or learned ranking. Agent sessions also retained every observation
for every later turn, and prompt builders did not expose explicit stable-prefix
boundaries for provider caching.

## Decisions

1. `ContextCompiler` version `deterministic-python-v2` parses Git's own current
   unified diff into changed current-line locations. For Python files, changed
   function/class definitions and the enclosing AST definition around a changed
   line become changed-symbol hints. Pure renames remain selected through the
   existing `git diff --name-only` path.
2. Selected and changed Python symbols gain a one-hop, AST-only call-reference
   neighborhood. Only `ast.Call` targets (`name()` or the terminal attribute in
   `object.name()`) count; comments, strings, imports, and unrelated identifier
   occurrences do not. This is deterministic name-level expansion, not dynamic
   call-graph resolution.
3. `FailureNormalizer` extracts repository-relative `FailureLocation` records
   from tracebacks and `path.py:line` diagnostics. Every path is resolved below
   the task worktree; outside paths are discarded. Compiler callers pass those
   locations as preferred excerpt anchors, centering source evidence on the
   failing line before falling back to the first lexical match.
4. Agent observations remain an append-only, bounded ledger. What is sent on a
   turn is a deterministic compacted view: newest failure first, newest diff
   second, then newest evidence per path/range slot, bounded by the new
   `max_transmitted_observation_chars` (24,000 by default and never larger than
   `max_observation_chars`). Every `agent-turn-*.json` retains the full
   uncompacted ledger; session-history JSON sent back to the model excludes
   those duplicated contents. Context measurements record ledger, transmitted,
   and compacted item/character counts.
5. Each prompt builder now concatenates an explicit byte-stable static prefix
   before any task, turn, history, failure, diff, or evidence data. The public
   `prompt_static_prefix()` helper makes the boundary testable. Existing cache
   telemetry (`cache_hit`, `cached_input_tokens`, prompt evaluation time) remains
   the measurement source; no cache success is inferred merely from structure.
6. Final reports add conservative file-level context attribution. Evidence is
   attributed only when its repository path is in the verifier-accepted patch;
   unchanged tests/supporting evidence may be useful but deliberately counts as
   noise under this narrow definition. `context-attribution.json` records the
   numerator, denominator, ratio, and limitation.

## Authority and safety

All selection, anchoring, compaction, and attribution is deterministic harness
logic. Models still request only typed actions and receive no direct filesystem,
Git, shell, verification, retry, transition, completion, or audit authority.
Failure paths are hints only after worktree containment validation.

## Non-goals

- No embeddings, vector database, learned ranker, or model-selected retrieval.
- No multi-hop or dynamic Python call graph.
- No claim that file-level patch attribution measures semantic usefulness.
- No claim of provider-cache hits without telemetry from a real provider run.

## Consequences

Changed code, callers, tests, and exact failure locations are more likely to be
shown within the same deterministic budgets. Repeated history no longer grows
monotonically in model input, while the uncompacted audit remains complete.
Prompt-prefix stability is now a structural invariant that real profile runs
can measure rather than an assumption.
