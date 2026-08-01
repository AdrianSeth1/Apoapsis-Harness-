# ADR 0082: pinned runtime thresholds are measured, not derived

Date: 2026-07-30

Status: accepted. Supersedes the threshold-modelling portion of ADR 0081 only.
ADR 0081's delegation of live history management to the native loop, its
handoff-capsule design, and `NativeContextPin`'s existence all stand.

## Context

ADR 0081 pinned Qwen's own context settings rather than reimplementing them,
and `NativeContextPin` carried `auto_compact_threshold = 0.85` with a docstring
calling it "the resolved default for the pinned 0.21.1". Its neighbouring
comment gave the reason for pinning rather than duplicating:

> Duplicating that ladder would give Apoapsis a second, subtly different model
> of when compaction happens, and the two would diverge without anyone noticing
> -- which is the failure mode this whole slice keeps rediscovering.

That is precisely what happened, and the mechanism was not duplication of the
ladder but **substitution of a single number for it**.

Executing the pinned bundle's own exported `computeThresholds` inside
`apoapsis-qwen-workcell:0.21.1` with no network shows:

```
computeThresholds(window, pct):
  effectivePct    = clamp(pct ?? DEFAULT_PCT, 0, 1)
  effectiveWindow = window - SUMMARY_RESERVE
  proportional    = effectivePct * window
  absoluteCeiling = effectiveWindow - AUTOCOMPACT_BUFFER
  auto            = min(proportional, absoluteCeiling)
```

`getAutoCompactThreshold()` returns `undefined` when the setting is unset —
there is no `0.85` fallback at the configuration layer at all. `DEFAULT_PCT` is
genuinely 0.85, but it is one input to a minimum, and at the pinned window it is
not the input that wins:

| Quantity | Value | Provenance |
|---|---:|---|
| Configured percentage | unset | settings, observed |
| Built-in percentage | 0.85 | probe at a 1,000,000 window |
| Summary reserve | 20,000 | `window - effectiveWindow` |
| Auto-compaction buffer | 13,000 | probe at `pct = 1` |
| Effective window | 45,536 | returned |
| **Effective auto trigger** | **32,536** | returned |
| warn / auto / hard | 12,536 / 32,536 / 42,536 | returned |
| Governing term | absolute ceiling | derived from the above |
| Effective ratio | 0.4965 | `auto / window` |

Source chunk SHA-256:
`634214ecb16ef3ab8e6e4046413c965606fd7c7c1194f6db93cd707cb5381c5c`.

`tools/slice5c/qualify.py` predicted its trigger as
`auto_compact_threshold * limit` = 55,706 — **1.71x the real value**. Slice 5C
still observed three compaction events, because the real trigger fires *earlier*
than the predicted one. The error was in the benign direction, which is exactly
why it survived a slice and a review.

## Decision

**A runtime threshold that governs behaviour is captured by executing the
pinned implementation, not derived from a pinned parameter.**

Four consequences, each binding:

1. **The ladder is pinned, not a percentage.** `WorkcellPin.threshold_ladder`
   carries `ResolvedThresholdLadder`: configured percentage, built-in
   percentage, summary reserve, auto-compaction buffer, effective window,
   warn/auto/hard, the governing term, and the SHA-256 of the chunk whose
   algorithm produced them. Each quantity is modelled separately because the
   relationship between them is a minimum, and a minimum cannot be recovered
   from either operand alone.

2. **The constants are measured, not read.** They are recovered by probing the
   shipped function — the percentage from a window wide enough that the
   proportional term governs, the buffer from a `pct = 1` call where the
   ceiling must govern. A release that changes a constant moves these numbers
   automatically. A release that renames one cannot silently defeat the
   capture, because nothing matches on a name.

3. **A consumer that needs a trigger must refuse to synthesise one.**
   `qualify.py` now raises when the ladder is absent rather than falling back to
   `pct * window`. A fallback here would reproduce the withdrawn figure under a
   new name, and it would look like a reasonable default while doing it.

4. **`PIN_SCHEMA_VERSION` goes to 1.2.** Manifests written before the ladder
   cannot be compared with ones written after. That is the honest outcome: the
   earlier runs did not know the window they compacted at.

**Explicitly not decided here:** whether to write
`context.autoCompactThreshold` into the installed settings. Doing so would make
the configured percentage resolvable, and would also change compaction
behaviour by moving the proportional term. It is a behaviour change requiring a
measured before/after and an owner decision, and this ADR deliberately does not
take it. The capture reports `unset` because the run was unset.

## What is withdrawn, and what is not

Withdrawn: **the claim that the Slice 5C runner watched the correct predicted
trigger.** It watched 55,706 against a real threshold of 32,536.

Not withdrawn, and not weakened:

- native compaction was **directly observed** as the CLI's own events, three
  times, never inferred from a token count;
- the post-compaction dependent edit was **verified by the controller running
  the tests**, not by the model's report; and
- the 2,173-token stable-prefix cache benefit, measured on the first exposed
  provider message.

The safety result never depended on the predicted trigger. It depended on
observing events that did occur, which is why an incorrect prediction did not
invalidate it — and why the prediction being wrong went unnoticed.

## Token accounting this establishes for the paired scorer

The retained Slice 5C evidence shows the CLI's `result` event is a **session
aggregate**, not a call, and that every stage-7 invocation carries an
unattributed residual — roughly 10,997 input tokens in five of six, and 30,964
in the sixth. Four rules follow, and they are binding on the paired scorer
(Slice 5A task 6):

1. **Total token cost comes from CLI session aggregates.** They are the only
   quantity that includes the residual, and the residual is real spend.
2. **Per-call and cache comparisons come from exposed provider messages.**
   Only those carry a prompt whose prefix can be controlled.
3. **The unattributed residual is reported separately, always.** It is about a
   third of a controlled invocation's input.
4. **The aggregate is never counted as another call, and its cost is never
   omitted.** Counting it as a call compares a sum with its own component;
   dropping it understates real spend by that same third. Both errors have
   already occurred in this programme, in opposite directions.

## Consequences

Threshold-dependent work must not proceed until a run carries a captured
ladder. That is a real cost: it blocks Slice 5A tasks that predict compaction.
It is smaller than the alternative, which is a benchmark suite keyed to a
threshold 1.71x from the truth.

More generally: a pinned parameter is evidence about configuration, not about
behaviour. Where the two can differ, the behaviour is what must be pinned. The
programme has now made this mistake twice — an unrecorded seed commit, and a
percentage standing in for a ladder — and both times the manifest asserted
comparability the runs did not have.
