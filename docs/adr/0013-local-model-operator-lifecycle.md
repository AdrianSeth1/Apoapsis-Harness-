# ADR 0013: Local-model operator lifecycle

- Status: Accepted
- Date: 2026-07-18

## Context

Apoapsis uses large loopback Ollama models whose memory remains allocated for a
provider-controlled keep-alive period. The repository had no obvious owner-level
Start/Stop controls, and manually stopping one remembered model name could leave
other configured roles resident. Any convenience control must remain local,
configuration-derived, and unable to touch hosted endpoints or acquire model
workflow authority.

## Decisions

1. `START_APOAPSIS.cmd` and `STOP_APOAPSIS.cmd` are the supported Windows owner
   entrypoints. They invoke the standard-library-only
   `apoapsis.operator_lifecycle` module from this checkout.
2. The lifecycle module reads `.apoapsis/config.toml`, considers only the known
   model roles, filters to `provider = "ollama"`, revalidates a credential-free
   loopback HTTP endpoint, and deduplicates identical endpoint/model pairs.
   Hosted providers are never contacted.
3. Start warms coding-role models with an empty Ollama generate request, the
   configured maximum context window for the deduplicated roles, and an explicit
   30-minute keep-alive by default. Research-only models remain lazy unless
   `--include-research` is supplied. Model installation is checked before warmup;
   missing models fail with an explicit manual `ollama pull` instruction and are
   never downloaded automatically.
4. If the default loopback endpoint is unavailable, Start may launch the fixed
   local command `ollama serve` in a hidden detached process and wait at most 30
   seconds for readiness. It cannot launch a service for a custom endpoint.
5. Stop sends an explicit empty generate request with `keep_alive = 0` for every
   configured, installed Ollama model, including research. If the service is
   already unreachable, model memory is necessarily unavailable and this is
   reported as already unloaded. Stop intentionally leaves the shared Ollama
   service running; it does not kill a possibly shared process or Docker.
6. The most recent lifecycle result is atomically recorded under the ignored
   `.apoapsis/runtime/` directory. No credentials, prompts, or repository content
   are included.

## Authority and safety

Warmup and unload calls have empty prompts and never enter the task workflow.
They cannot create tasks, propose patches, run checks, alter transitions, consume
retry budgets, or mark completion. Endpoint validation occurs before network
access. The scripts expose `APOAPSIS_NO_PAUSE=1` only as an automation convenience;
it does not alter lifecycle behavior.

## Consequences

The owner has obvious double-click controls and Stop releases all configured
model RAM/VRAM without relying on remembered names. Starting both coder and
research models remains an explicit choice because their combined footprint may
exceed available memory. The Ollama daemon can be shared with other applications
and is not treated as an Apoapsis-owned process.
