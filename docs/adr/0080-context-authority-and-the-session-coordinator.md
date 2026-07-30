# ADR 0080: Context authority and the session coordinator

- Status: Accepted
- Date: 2026-07-30
- Builds on ADR 0077 (workcell boundary) and ADR 0079 (readiness-based
  completion). Supersedes nothing; it adds the loop that ADR 0079's checkpoint
  decision runs inside.

## Context

Three Crisis Atlas readings sit behind this decision, and none of them is about
the model being weak.

The unrestricted control died at a 64,409-token prompt against a 65,536-token
window, having fired no compaction event. The same control spent 2,080,801
input tokens against the sliced arm's 258,632, largely by resending a growing
conversation and every shell observation on every call. And the sliced arm's
budget — twelve protocol turns — described neither the work the owner wanted
nor the work the control's sixty-two cycles actually did.

Handoff slice 5 built policy for all three: a stable task kernel, a state
capsule that survives compaction, proactive two-tier compaction, and budgets
expressed as wall time, process time, tokens and no-progress. It wired none of
it to anything, which is the same operational gap review found in slice 4.

Review of slice 5 also identified three places where the policy claimed
authority it had not earned. This ADR records the corrected policy and the
coordinator that enforces it.

## Decision

### 1. Prompt stability is provenance, not lexical shape

The kernel is rendered **once**, at session start, written to disk, hashed, and
thereafter read back. `KernelArtifact.load_text` verifies the digest on every
read and raises `KernelDriftError` if the file moved.

The rejected version refused timestamp-shaped, UUID-shaped and request-id-shaped
text at construction. That test is wrong in both directions. A legitimate
objective may quote a fixed upstream tenant UUID or a historical incident
timestamp — neither changes between calls, and refusing them makes the owner
rewrite a correct task. Conversely a genuinely per-run value need not match any
pattern the regex knows. **Volatility is a property of how a value is produced,
not of how it looks**, so the control is production: one artifact, reused.

The shape scan survives as `scan_volatility`, recorded on the artifact as
advisory hints so an owner can confirm a suspicious-looking value is fixed. It
blocks nothing.

Assembly order is fixed — system prompt, sorted tool schemas, task kernel,
compacted history, latest observation — and `check_prefix_stability` reports
the call at which a prefix first moved.

### 2. Only provider-reported usage may compact or stop a session

`TokenLedger` carries provider-reported `input_tokens`, `output_tokens` and
`cached_input_tokens`, lifted from the CLI's own `usage` events by
`WorkcellEventAdapter` — the same telemetry that classified the Crisis Atlas
ceiling stops. It also carries the controller's local estimates, and those are
barred from every gate.

`CompactionPolicy.should_compact_reading` returns `False` for an unreported
reading. `evaluate_budget` places the token ceilings in `unenforced` rather
than treating a missing ledger as zero spend.

Both directions matter. An estimate that reads high compacts a session that did
not need it, discarding context for nothing; an estimate that reads low is how
a run reaches 64,409 tokens with no compaction event. Neither failure should be
attributable to the controller's own arithmetic. Estimates are retained because
`TokenLedger.estimate_error` is the only way to notice that the estimator is
wrong.

An unenforced ceiling is not a passing one. `within_budget=True` alongside a
non-empty `unenforced` means "nothing measurable was exceeded", and the verdict
detail says `UNENFORCED` in those words.

### 3. Progress is authoritative state advancement

A turn made progress if the worktree fingerprint changed, **or** an obligation
the readiness evaluator did not previously consider discharged now is, **or**
the controller produced a new evidence artifact. Any one suffices.

The rejected version counted only the worktree. That punishes exactly the
behaviour the unrestricted control did well: a turn that runs a failing test,
reads the trace and produces a coverage artifact naming a new diagnosis has
advanced the session without editing a file.

"Newly" is load-bearing in the obligation case — re-reporting a discharge from
three turns ago would let a stalled session claim progress forever, so
`ProgressTracker` remembers what it has already seen.

Model narration is never progress. `TurnObservation` has no field for the
model's account of its own turn, which is the point: every input is
controller-observed. This is what separates real work from the Slice 2C sandbox
arm's nine identical calls.

### 4. Compaction is proactive, two-tier, and fails loudly

Compaction begins at a configurable fraction of the window, default 0.70,
matching Qwen Code's own default and **recorded as a first experiment point,
not an Apoapsis constant**. The target must be strictly below the threshold, or
a session compacts, lands just under the line, and compacts again next turn.

Tier one is mechanical: drop reasoning, spill old tool output to
content-addressed artifacts, keep recent tool calls verbatim. Tier two is
semantic and costs a model call, so it is requested rather than assumed.

If tier one is insufficient and semantic compaction is unconfigured, returns no
summary, or raises, the session **stops** with `COMPACTION_FAILED`. Continuing
over a context known to be too full is precisely the control's failure.

The capsule is never compacted away. Output with nowhere to spill is kept
rather than discarded, and `BoundedObservation` rejects a truncation that names
no artifact.

### 5. Every ending is a recorded transition

`SessionCoordinator` owns the kernel artifact, the capsule, the budget, the
compaction policy and the checkpoint loop, and it is the only place the five
meet. There is no return from the middle of `run`: every ending goes through
`_stop`, which appends a `SessionTransition` and sets one of seven
`SessionOutcome` values — `COMPLETE`, `BUDGET_EXHAUSTED`, `CANDIDATE_REFUSED`,
`HUMAN_REVIEW_REQUIRED`, `COMPACTION_FAILED`, `KERNEL_DRIFT`, `AGENT_STOPPED`.

`AGENT_STOPPED` exists so that an agent falling silent cannot be mistaken for
one that finished. No completion decision was made, and the record says so.

The budget is evaluated **before** the call, not after. Spending a call and
then discovering the allowance was already gone makes a ceiling advisory.

The coordinator decides nothing about correctness. It hands admitted work to
`run_checkpoint`, and `evaluate_checkpoint` — which cannot see a command's exit
code — decides completion. That separation is ADR 0079's and is untouched.

## Consequences

### Migration

`ProgressTracker.record_turn` now takes a `TurnObservation` and returns a
`TurnProgress`, rather than taking keyword arguments and returning a bool.
`BudgetUsage.input_tokens` and `.output_tokens` are replaced by
`BudgetUsage.tokens: TokenLedger`. `TaskKernel` no longer raises on
volatile-looking text; callers that relied on that refusal should read
`volatility_hints` and use `persist_kernel`.

### What this does not establish

**No live session has run through this coordinator.** Every test here is
deterministic, driven by a scripted agent. The following remain owed, and none
of them is a detail:

- a live workcell session that compacts at the boundary and **continues editing
  and testing afterwards** — the deterministic tests show the policy fires and
  that the capsule survives, not that a real Qwen picks up from it;
- semantic compaction is a `SemanticCompactor` callable with no production
  implementation, so today every over-threshold session that mechanical
  compaction cannot rescue stops rather than summarising;
- cached-input telemetry from a real run. `cached_input_tokens` is plumbed and
  `PrefixDrift` proves the prefix is *stable*; whether stability actually buys
  cache hits against this provider is unmeasured, and the efficiency claim does
  not exist until it is measured;
- `TurnResult.observation` is accepted but not yet routed through
  `bound_observation`, so per-tool output budgets are enforced only where a
  caller applies them.

Until the live run exists, this ADR records a policy that is enforced
deterministically and a context-safety claim that is **not yet evidenced**.

### Rejected alternatives

**Keep the regex refusal and add an allowlist.** An allowlist of permitted
volatile-looking strings is a second thing to maintain that still cannot tell a
fixed UUID from a regenerated one. Byte reuse answers the question directly.

**Let the estimate trigger compaction when telemetry is missing.** Attractive
because it degrades gracefully, and wrong: it makes the estimator's error
indistinguishable from a real context condition, in the one subsystem whose
whole purpose is to know how full the window is.

**Treat an unenforced ceiling as satisfied.** This is the ADR 0069 mistake in
another costume — absence of a measurement read as a passing measurement.
