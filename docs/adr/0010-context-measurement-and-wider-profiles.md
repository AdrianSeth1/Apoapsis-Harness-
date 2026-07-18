# ADR 0010: Context measurement and wider context profiles (Apoapsis 1.0a)

- Status: Accepted
- Date: 2026-07-18

## Context

Apoapsis 1.0 is scoped in three phases: **1.0a** context profiles and
measurement infrastructure (this ADR), **1.0b** deterministic context-quality
improvements, and **1.0c** a broader multi-metric benchmarking framework.
`HANDOFF.md`'s "Apoapsis 1.0 phased plan" section is the authoritative
description of all three phases and the interfaces between them; this ADR
records only the 1.0a decision.

The reference machine has an RTX 4090 (24GB VRAM) and 64GB system RAM. The
installed model (`qwen3-coder-next:q4_K_M`) is a ~48GB file — larger than
VRAM alone, so it already runs GPU+CPU split. Its native context length,
confirmed via Ollama's `/api/tags` (`details.context_length`), is 262,144
tokens. Having VRAM headroom is not the same as a larger context window
being fast or useful in practice; per the explicit instruction that produced
this milestone, wider profiles "must be explicit and measured," and a
profile must never become the default merely because it fits.

## Decisions

1. **Two new opt-in profiles, `128k` and `256k`, added to the existing
   `_CONTEXT_PROFILES` dict in `cli/app.py`** (no new mechanism — this
   reuses and extends `_apply_context_profile`/`--context-profile` exactly
   as `16k`/`32k`/`64k` already work): `128k` = 131,072 window / 32 files /
   320 excerpt lines / 360,000 chars; `256k` = 262,144 window / 40 files /
   400 excerpt lines / 600,000 chars. `256k` exactly matches the installed
   model's reported native maximum — nothing here requests more than the
   model claims to support. The default project configuration is untouched;
   choosing a wider profile remains an explicit per-run/per-eval opt-in.
2. **`apoapsis doctor` gains a `context_window_support:<role>` check** that
   queries the same `/api/tags` endpoint already used for reachability and
   compares the configured `context_window_tokens` against the model's
   reported `details.context_length`: `ERROR` if the configured value
   exceeds the model's native support, `WARNING` if the model can't be
   found or didn't report a length, `OK` otherwise. This is the "confirming
   model and provider support" step — a real, queried fact, not an
   assumption from available VRAM.
3. **A new deterministic, read-only `ContextMeasurement` schema**
   (`src/apoapsis/context/measurement.py`, function `measure_context`)
   reports exactly what the requirement list asked for: model context
   window, repository file limit, excerpt line limit, total transmitted
   characters/lines, agent observation budget, a deterministic
   chars/4 token estimate (the same heuristic `apoapsis doctor` already
   uses, kept identical on purpose), model-window utilization, a
   composition breakdown by `EvidenceKind`, file/char truncation counters,
   and a stable-versus-newly-introduced evidence split. It never influences
   retrieval, ranking, or truncation — it only reports what the compiler
   already decided, computed *after* compilation as a pure function over an
   already-built `ContextPackage`.
4. **Stable-versus-new evidence is an identity-key diff** against the
   immediately preceding call's `ContextPackage` for the same task, using
   the exact `(path, start_line, end_line, content_sha256)` tuple
   `BoundedAgentSession` already uses for its own turn-over-turn dedup — no
   new identity concept was invented. With no previous package (the first
   call of a task), everything is reported as new.
5. **Truncation/candidate counters are computed by minimal, additive
   instrumentation inside `ContextCompiler.compile()`**, not a rewrite:
   `candidate_file_count` (size of the reasons dict before the
   `max_files` slice), `files_truncated_by_limit`, `files_dropped_for_char_
   budget`, and `excerpts_truncated_for_char_budget` are recorded into the
   existing `compiler_parameters` metadata bag (which already held a full
   dump of `ContextCompilerConfig`) alongside the existing selection logic,
   which is byte-for-byte unchanged. `measure_context` reads these back out
   of `compiler_parameters` rather than requiring a new parameter on every
   call site.
6. **Measurement is computed once per model call inside
   `VerticalSliceRunner._model_call`**, right after the call's context is
   recorded, using the already-in-scope `selected_config.context_window_
   tokens` and the immediately preceding call's context (if any) for the
   stable/new comparison. The agent observation budget is read back from
   `compiler_parameters["agent_loop"]`, which `BoundedAgentSession._context_
   for_turn` was already stashing there — no new parameter needed on
   `_model_call` at all.
7. **Persistence and reporting**: each measurement is written as its own
   audit artifact, `call-<NNN>-context-measurement.json`, alongside (not
   replacing) the existing `call-<NNN>-context.json`. `FinalTaskReport`
   gained an additive `context_measurements: list[ContextMeasurement]`
   field. `apoapsis eval`'s comparison Markdown gained three columns (peak
   estimated context tokens, peak model-window utilization, stable/new
   evidence totals) computed from the same per-call measurements — no new
   telemetry source, just a rollup of what's already on the report.
8. **No behavior changed by default.** Every new field is additive with a
   safe default; the default backend/profile/config is untouched;
   `tests/test_verification.py`-style regression coverage for the compiler
   and vertical slice passed unmodified. This phase adds observability, not
   a new retrieval or ranking algorithm — that is explicitly 1.0b's job, not
   this one.

## Non-goals (deferred to later 1.0 phases, or explicitly out of scope)

- No change to retrieval, ranking, or excerpting logic (1.0b).
- No embeddings or learned retrieval — consistent with every prior ADR's
  explicit non-goal; 1.0b's own mandate is that embeddings are only even
  considered after benchmarks show a repeatable failure of the existing
  lexical/symbol/import/test/diff retrieval.
- No cross-task/cross-fixture benchmarking framework, no rate/delta metrics
  (local-only completion rate, frontier rescue rate, false-success rate,
  hosted cost/tokens saved, etc.) — that is 1.0c, and several of those
  metrics are structurally blocked until real hosted-frontier credentials
  exist (see `HANDOFF.md`'s 1.0 plan for the exact deferred-metric list and
  why).
- No live run at the new 128k/256k profiles is included in this phase's
  evidence; the profiles and the measurement tooling to evaluate them exist,
  but "compare each profile on identical tasks" (the requirement's own
  words) is 1.0b/1.0c work once retrieval-quality changes exist to compare
  against a baseline.

## Consequences

Every task run now carries a deterministic, auditable record of how much
context was actually built, how it was composed, how much of it was
genuinely new information versus already-seen evidence, and how full the
configured model's window is — without changing what gets sent to a model
today. 128k and 256k are available to opt into and doctor-checked against
the real installed model's reported capability, but remain deliberately
non-default until real comparative data exists.
