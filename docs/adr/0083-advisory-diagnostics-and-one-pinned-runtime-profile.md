# ADR 0083: advisory diagnostics, and one pinned runtime profile

Date: 2026-07-30

Status: accepted. Completes the bounded Slice 5A profile. Does not reopen
Slice 5, which is frozen.

## Context

Two things were outstanding before repair checkpoints could be built.

**Diagnostics.** SWE-agent reports that immediate syntax feedback at edit time
materially helps a model repair its own mistakes, and Qwen Code ships an LSP
surface. The agent should have that feedback. It is also, precisely, the shape
of signal that ended Crisis Atlas Slice 2 early: something cheap and green,
arriving before the work was finished, treated as though it meant the work was
finished. ADR 0069 removed a green suite's authority to end a session; adding a
diagnostics subsystem without the same discipline would reintroduce it with
better tooling.

**Runtime profile.** The handoff lists eleven server knobs worth benchmarking.
Working through them is a research programme, and it would answer a question
nobody is currently asking. The open question is whether Apoapsis Qwen matches
or beats unharnessed Qwen per case, with fewer false completions and lower
median input tokens. A sweep run before the paired corpus exists optimises
against a benchmark that cannot yet detect a quality regression.

## Decision

### Diagnostics are advisory by construction, not by convention

One asymmetry governs the design:

> A diagnostic that finds a problem is useful to the agent.
> A diagnostic that finds nothing proves nothing at all.

`DiagnosticStatus` has four values and never collapses to a boolean:
`FINDINGS`, `CLEAN`, `TOOL_ABSENT`, `TOOL_FAILED`. All four carry an empty or
short findings list, and only `CLEAN` means a tool looked and found nothing. A
missing, crashed, or timed-out language server yields `NOT_CHECKED`, and
`NOT_CHECKED` is not `CLEAN`.

Three structural guarantees, each tested:

1. **`DiagnosticReport` is not a `StructuredWitness`.** Different types, so one
   cannot be passed where the other is expected, and no code path converts
   between them. A diagnostic therefore cannot discharge a contract obligation
   even by accident.
2. **`evaluate_checkpoint` cannot see diagnostics.** Its signature remains
   `(admitted, detail, readiness)`, asserted by a test that fails if a
   diagnostics parameter is ever added.
3. **Collection happens after the decision.** `run_checkpoint` computes its
   outcome first and collects diagnostics afterwards, so they are structurally
   incapable of having influenced it. A collector that raises produces
   `TOOL_FAILED`, never a skipped field.

`advisory` is `Literal[True]` — not a flag a configuration file can flip later.

The verification hierarchy is unchanged and now has three explicit tiers: agent
self-checks and diagnostics are **advisory**; checkpoint witnesses determine
**readiness**; ADR 0074 final integrated verification governs **delivery**.

Syntax checking only, for now. It is the cheapest useful signal, has no project
configuration to get wrong, and cannot be mistaken for a test. A richer LSP pass
belongs behind the same `DiagnosticReport` contract and the same `NOT_CHECKED`
discipline, and nothing needs to change to add one.

### One profile, and recorded refusals

`QUALIFIED_PROFILE` transcribes the configuration that already passed Slice 5C
containment, readiness, native compaction and continuation, plus the threshold
ladder ADR 0082 measured from it. Nothing in it was chosen by benchmarking here.
Its compaction trigger is the measured 32,536, never `0.85 x window`.

Optimisations are recorded as decisions rather than silently skipped:

| Optimisation | Verdict | Why |
|---|---|---|
| `llama-server` tuning sweep | rejected without benchmark | throughput, not proposal quality |
| Speculative decoding | rejected without benchmark | latency only, and it perturbs sampling |
| KV-cache quantisation | rejected without benchmark | risks the long-context recall Slice 5C established |
| Compaction threshold tuning | rejected without benchmark | Slice 5 is frozen; the ladder is the CLI's |
| Read-only tool parallelism | rejected without benchmark | wall-clock only; concurrent writes make the fingerprint ambiguous |
| Reasoning-effort routing | **candidate** | plausibly moves proposal quality and false completion |
| LSP beyond syntax | **candidate** | plausibly reduces repair distance |

The rule applied: **consider an optimisation only when it plausibly changes
per-case proposal quality, false completion, latency, or tokens; otherwise
record it as rejected without benchmarking.** Two of seven survive, and neither
is enabled by default or benchmarked here. No default changes on latency alone;
quality must remain non-inferior.

## Consequences

The agent gets faster feedback and the harness gets none. That asymmetry is the
point, and it costs something real: a diagnostic pass that is clean still cannot
shorten a checkpoint, so the loop is no faster than before. That is the correct
trade — the loop was never too slow, it was too willing to stop.

Deferring the tuning sweep means the runtime profile is unoptimised. It is also
reproducible and already qualified, which is what the paired corpus needs. The
knobs remain available and now carry written reasons, so a later session finds a
decision rather than an oversight.

Next: Slice 6 authoritative repair checkpoints. Corpus qualification and rollout
do not begin until that exits.
