# ADR 0108: Sample the parity guard instead of pairing every slice

## Status

Accepted and implemented on 2026-08-03.

## Context

`high_assurance_parity_guard` runs the unrestricted control arm on every slice.
It was right to. The question it answered — is the supervised sandbox no worse
than the native baseline? — could only be answered by running both, and it was
answered, with six paired 1.0/1.0 slots.

As a standing default it means every user slice costs two complete model
executions, forever. That is not what the evidence bought. A question that has
been answered does not need re-answering on every slice; it needs *monitoring*,
and monitoring is a sampling problem.

## Decision

Three modes, under `[execution.capability_sandbox]`:

- `always` — every slice. The pre-0108 behaviour, one config line away.
- `sample` — **the default.** The first slice of a plan, then every
  `parity_sample_every`th after it (4 by default).
- `off` — no control arm. The qualification evidence stands and nothing
  re-checks it during a run.

**Selection is deterministic, from the slice's position in its plan.** This
matters more than it sounds. A sample chosen at random is a comparison nobody
can reproduce, and "the parity check happened to skip the slice that regressed"
is precisely the sentence this must never make true by accident. The same plan
samples the same slices every time.

**The first slice of every plan always pairs.** A plan that never paired would
inherit its confidence entirely from someone else's qualification run against
someone else's codebase.

**An unknown position pairs rather than skips.** If the harness cannot
establish where a slice sits in its plan, it runs the control arm. When the
policy cannot be evaluated, the safe direction is the expensive one.

**The reason is recorded, not recomputed.** `ParitySelection` carries the mode,
the position, the interval and a sentence explaining the decision, and it
travels into `result.json`. "No control arm ran" is not evidence of anything;
"no control arm ran because this is slice 3 and the policy pairs the first and
every 4th" is.

**Escalation is untouched.** `evaluate_parity` was extracted from
`product_live` so the invariant is testable rather than inline: a slice that
was *expected* to pair and produced no scoreable control is unavailable, and a
sandbox candidate proving fewer obligations than its control is a regression.
Both stop the slice for review exactly as before. Sampling changes how often
the question is asked, never what happens to the answer. The `expected` flag is
what distinguishes an unsampled slice with no control (the policy working) from
a sampled slice with no control (a comparison that was supposed to happen and
did not).

**The old switch still means what it said.** An operator with
`high_assurance_parity_guard = true` in their configuration asked for a control
arm on every slice; a validator upgrades them to `always` rather than silently
downgrading them to sampling and spending their evidence for them. A
configuration that states both is taken at its word.

**The cost is stated where the choice is made.** The UI reports the policy as
"1st slice + every 4th" or "every slice (~2x inference)" rather than "ON", with
a note that regressions escalate identically either way. A toggle whose price
is documented only in an ADR is a toggle nobody prices.

## Consequences

Steady-state inference for a plan run falls by roughly half at the default
setting: a 15-slice plan pairs 4 slices instead of 15. Combined with ADR 0107's
single model load, the wall-clock difference on a long plan should be large;
neither has been measured live yet, and both are recorded in NEXT_STEPS as
claims awaiting numbers.

A regression introduced on an unsampled slice is now detected on the next
sampled slice rather than immediately. That is the trade being made, and it is
the reason `sample` rather than `off` is the default: the alternative to
sampling was not "detect everything", it was paying 2x forever to detect
something the qualification run already established.

The UI's existing boolean toggle maps to `always`/`sample`. A three-way
selector belongs with MH-9's status work rather than as a second control added
here.
