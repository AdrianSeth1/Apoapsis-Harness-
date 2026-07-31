# Slice 5A minimal diagnostics and runtime profile

Date: 2026-07-30. **No model call.** Deterministic implementation and coverage.

Scope is the bounded Slice 5A profile required before repair checkpoints, and
nothing else. Slice 5 stays frozen: no 5D, no threshold work beyond ADR 0082, no
further pursuit of the accounted residual, no new instrumentation subsystem.

## Verdict

| Item | Result |
|---|---|
| Advisory syntax diagnostics in the workcell | **IMPLEMENTED** |
| Diagnostics captured as controller evidence | **IMPLEMENTED — recorded, never consulted** |
| Diagnostics cannot authorise completion | **STRUCTURAL — three guarantees, each tested** |
| Verification hierarchy preserved | **UNCHANGED — advisory / readiness / delivery** |
| One pinned runtime profile | **DEFINED from the already-qualified configuration** |
| Tuning sweep | **NOT RUN, by decision** |
| Optimisations considered | 7 — 5 rejected without benchmarking, 2 kept as candidates |
| Deterministic coverage | 17 new tests, all passing |
| Adjacent suites | 76 passed (diagnostics, checkpoint, acceptance) |
| Full suite on Python 3.11+ | **DEFERRED to qualification, per frozen scope** |

## Diagnostics

The design turns on one asymmetry: a diagnostic that finds a problem helps the
agent, and a diagnostic that finds nothing proves nothing. `DiagnosticStatus`
therefore has four values — `FINDINGS`, `CLEAN`, `TOOL_ABSENT`, `TOOL_FAILED` —
and never collapses to a boolean. All four carry an empty or short findings
list; only `CLEAN` means a tool looked.

Three guarantees make "advisory" structural rather than a naming convention:

1. `DiagnosticReport` is **not** a `StructuredWitness`. Different types, no
   conversion path, so a diagnostic cannot discharge a contract obligation.
2. `evaluate_checkpoint` keeps its `(admitted, detail, readiness)` signature. A
   test fails if a diagnostics parameter is ever added.
3. `run_checkpoint` collects diagnostics **after** computing its decision, so
   they cannot have influenced it. A collector that raises yields `TOOL_FAILED`.

`advisory` is `Literal[True]`, not a configurable flag.

### The failure mode that shaped the parser

A non-zero exit whose output cannot be parsed yields no findings. Reporting that
as `CLEAN` would make a broken toolchain read as a passing parse — the same
shape as a green suite over an unexercised file. It returns `TOOL_FAILED`
instead, and `test_unparseable_failure_output_is_not_clean` holds that.

The traceback parser also prefers the exception line over the echoed source
line. `py_compile` prints the offending source first, so a naive parser reports
`def handler(` as the finding: a location with no diagnosis attached, which
tells the agent nothing it did not already know. The first draft here did
exactly that and the test caught it.

Agent-facing wording is deliberate. `NOT CHECKED ... This is not an all-clear;
nothing was inspected.` is the sentence that keeps a crashed language server
from reading as success, and a `CLEAN` summary still says *this says the code
parses, not that the slice is implemented.*

## Runtime profile

`QUALIFIED_PROFILE` transcribes the configuration that already passed Slice 5C.
Nothing in it was chosen by benchmarking here, and its compaction trigger is the
measured **32,536**, never `0.85 x window`.

Seven optimisations were considered against the rule — *only when it plausibly
changes per-case proposal quality, false completion, latency, or tokens.* Five
were rejected without benchmarking (`llama-server` sweep, speculative decoding,
KV-cache quantisation, compaction threshold tuning, read-only parallelism); two
survive as candidates for the paired corpus (reasoning-effort routing, LSP
beyond syntax). Neither candidate is enabled by default or benchmarked here. No
default changes on latency alone.

Recording the rejections as data rather than omitting them means a later session
finds a decision with a reason attached.

## What this cost

The agent gets faster feedback and the harness gets none. A clean diagnostic
pass cannot shorten a checkpoint, so the loop is no faster than it was. That is
the correct trade: the loop was never too slow, it was too willing to stop.

The runtime profile is unoptimised. It is reproducible and already qualified,
which is what the paired corpus needs.

## Verification

17 new tests in `tests/test_workcell_diagnostics.py`; 76 passed across
diagnostics, checkpoint and acceptance under the documented Python 3.10
compatibility shim. Per the frozen scope, the **full deterministic suite runs
once on Python 3.11+ before qualification, not before every implementation
step** — that check, and the two known `test_acceptance_coverage` failures at
`d50ddf2` plus the nine `enterContext` failures, belong to qualification.

## Against the governing question

Does this help Apoapsis Qwen match or beat unharnessed Qwen per case, reduce
false completion, or preserve the authority boundary?

- Diagnostics: **repair distance and false completion.** Faster feedback should
  cut Slice 3-style route and type mistakes, and the structural separation
  ensures the mechanism cannot itself become a false-completion path.
- Runtime profile: **comparability.** A pinned, reproducible configuration is a
  precondition for a per-case verdict meaning anything.

Neither is a throughput optimisation, and no throughput optimisation entered a
default.

**Next: Slice 6 authoritative repair checkpoints. Not started.**
