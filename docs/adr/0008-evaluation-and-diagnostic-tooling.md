# ADR 0008: Evaluation harness and diagnostic tooling

- Status: Accepted
- Date: 2026-07-17

## Context

ADR 0006 gave Apoapsis a deterministic local-to-frontier escalation path, but
it has only ever been exercised with `FakeModelProvider`. Before a real hosted
frontier is trusted with a live proof, two gaps need closing: there is no
preflight check for the toolchain, configured models, credentials, and
verification commands a run depends on, and there is no repeatable way to run
every execution lane against a fresh copy of a controlled fixture and compare
them. Apoapsis 0.8 adds `apoapsis doctor` and `apoapsis eval` to close both
gaps without granting any model broader authority and without spending money
or configuring credentials on its own initiative.

## Decisions

1. `apoapsis doctor` is read-only and diagnostic. It never mutates project
   state, never writes to `.apoapsis/`, and never prints or logs a credential
   value — only whether a configured environment variable is set. Checks cover
   Git, ripgrep (advisory only; its absence is a `warning`, not an `error`,
   because the deterministic lexical fallback in the context compiler still
   works), Python version, local Ollama reachability (free, loopback-only,
   always run), configured model roles and context limits, credential
   presence, and verification-command binary availability.
2. A live connectivity/structured-output probe is opt-in only, via `--probe`.
   Probing a loopback Ollama provider is free and safe to describe plainly; a
   check against an `openai_compatible` provider explicitly notes in its
   result that the call may incur hosted-provider cost. Doctor never performs
   this probe by default.
3. `apoapsis eval <fixture>` runs one or more deterministic *lanes* —
   `local`, `hybrid`, `forced-escalation`, `frontier`, `one-shot` — each
   against its own fresh, isolated copy of the named fixture in its own Git
   repository. A lane is a configuration overlay over the caller's real
   `.apoapsis/config.toml`, expressed only as `execution.mode`/`execution.route`
   and, for `forced-escalation`, a constrained local `execution.agent` turn
   budget. No lane ever changes `models.*`; provider identity and credentials
   always come from the project's own configuration. This means every lane
   reuses `VerticalSliceRunner` completely unchanged — evaluation introduces no
   parallel execution engine and no new authority.
4. `forced-escalation` proves a real local-to-frontier handoff by constraining
   the local agent to a one-turn budget, not by corrupting the task text or
   any patch. The bounded turn-loop already treats budget exhaustion as an
   escalation trigger (ADR 0005/0006); this lane simply makes that trigger
   deterministic and reproducible on a task that cannot be solved and verified
   in a single turn.
5. A lane that requires `[models.frontier_coder]` (`hybrid`, `forced-escalation`,
   `frontier`) and finds it unconfigured is recorded as **skipped**, not
   failed, and no fixture copy or provider is built for it. Absence of hosted
   credentials must never be silently treated as authorization to spend
   money, nor as a hard error that blocks the lanes that don't need it.
6. Each lane's effective configuration is written as
   `<fixture>/.apoapsis/effective-config.json` for audit, instead of a
   hand-written TOML file — the repository has no TOML *writer* dependency
   (only stdlib `tomllib`, read-only), and `api_key_env` fields only ever hold
   an environment-variable *name*, never a secret, so this stays safe to write
   in full.
7. A comparison report (`comparison.json` and `comparison.md`) aggregates the
   existing `FinalTaskReport` already produced by each lane's run — calls,
   tokens, cached tokens, cost, latency, transmitted excerpts, changed files,
   and verification results are read from that schema, not recomputed. The
   comparison introduces no new telemetry source.

## Deferred: subscription-backed provider adapter

A `claude_code_cli`/`codex_cli`-style adapter, so a user can drive the coding
stages through an existing Claude Pro or ChatGPT Plus subscription instead of
per-token API billing, was requested as a follow-up and is intentionally not
built in this milestone. If implemented, it must sit behind the existing
narrow `ModelProvider` protocol like every other adapter; it must run in an
empty temporary directory with no tool or repository access beyond the single
Apoapsis prompt for that call; it must return exactly one response per
invocation; and it must report whatever token/latency telemetry is available
through the normal `ProviderCallTelemetry` shape. This is recorded here as a
future ADR candidate, not a decision made now.

## Consequences

Apoapsis can now be preflighted and evaluated end-to-end against a real local
model without spending anything, and the exact same tooling becomes a live
hosted-frontier proof the moment a user adds real `[models.frontier_coder]`
credentials — no code change is required to go from "skipped" to "run." This
increment does not add learned routing, autonomous provider selection,
recursive agents, arbitrary tools, or a container sandbox, and it does not
itself configure, probe by default, or spend against any hosted provider.
