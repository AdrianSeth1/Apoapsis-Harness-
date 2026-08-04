# ADR 0104: Raise the patch changed-line ceiling to slice-sized work

## Status

Accepted and implemented on 2026-08-03.

## Context

`[patch] max_changed_lines` shipped as 500 in both `PatchPolicyConfig` and the
`apoapsis init` template. Two live projects — `test project 3` and
`test project 6` — carry 5000 in their own `.apoapsis/config.toml`, edited by
hand. Nothing in this repository's history introduced that number
(`git log -S"max_changed_lines = 5000"` is empty), so the operator raised it
locally each time and the shipped default never followed.

500 was sized for the bounded protocol, where a turn is one patch and one patch
is a single focused edit. The Capability Sandbox admits an entire slice as one
unit: a slice that adds three modules with the tests that exercise them —
exactly what ADR 0103's contract now tells the model to do — routinely exceeds
500 changed lines. Admission then refuses the whole candidate, and the refusal
is unsolvable from inside: the work is correct, the ceiling is wrong, and the
model can only shrink work it was instructed to produce.

A default that every real project must override is not a default. Worse, the
override is invisible: a project created after a bump silently gets the old
ceiling, and the failure appears as a model that cannot finish a slice.

## Decision

`PatchPolicyConfig.max_changed_lines` and the `apoapsis init` template both
become **5000**, matching what the live projects already run.

`max_files` stays at **20**. File count is the ceiling that actually catches a
runaway change — a candidate touching forty files is a different kind of event
from one touching twenty and writing more lines in them — and a slice that
needs more than twenty files is usually mis-sliced rather than large. Raising
both would remove the only cheap signal that something went wide.

The class default and the template are asserted together in one test
(`test_patch_ceilings_match_adr_0104_in_class_and_template`). This shape is
taken directly from ADR 0049's follow-up, where the template was updated and
the Pydantic class default silently drifted back, so every library caller and
every project whose `config.toml` omitted the field kept the old number. Two
places can hold one value only if something checks that they agree.

Existing projects are not rewritten. `apoapsis init` never edits an existing
`config.toml`, and a project that deliberately runs a tighter ceiling keeps it.

## Consequences

New projects can complete a normal multi-file slice without the operator
discovering the ceiling through a refused candidate. The admission surface is
correspondingly wider: a genuinely runaway change now has 5000 lines of room
before the line ceiling stops it, with `max_files`, the forbidden-path classes,
and the test/dependency switches unchanged as the other bounds.

ADR 0103's judgement contract quotes these numbers into the prompt from the
request, so the model is told the real ceiling it is judged against rather than
a stale one — which is what makes a wrong default visible rather than merely
expensive.
