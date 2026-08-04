# ADR 0103: Tell the sandbox agent how completion is judged

## Status

Accepted and implemented on 2026-08-03.

## Context

`CAP-4EE9F101146E4556/evidence/sandbox/qwen-stdout.log` contains hundreds of
lines of the agent reasoning about our own harness: "the witness system might
parse the output for markers… let me invent a format: `AC-007 PASS / EXERCISED
backend/app.py sha256:…` … wait, maybe it uses a coverage tool… OK, let me just
implement it and see if it works." It then wrote marker-emitting code that does
nothing at all.

That was not a failure of reasoning. The repair packet tells the model *what*
is unproved — "no current-state witness proves it is reached" — and never *how*
proof is established. Given only the error string, guessing at the mechanism is
the rational move, and the model spent turns and thousands of tokens on it, in a
32K-window session, every attempt.

The mechanics are not secret and there is no reason to withhold them. Knowing
that the harness re-runs the approved commands under a coverage trace does not
let a model fake a pass: it cannot reach the trace, and printing a claim about
coverage has never been read as evidence.

## Decision

One constant, `_judgement_contract`, states the proof mechanics in the model's
own terms, and is appended both to `task.md` and to every repair-packet
preamble. Same text in both places, from one source, so the initial task and the
continuation cannot drift into describing two different systems.

It says the five things that decide the slice: the harness snapshots and
compares by content (so Git history is irrelevant); admission judges the change
as one unit against the approved limits, quoted from `patch_policy`; the
approved commands are re-run by the harness from that snapshot while recording
which lines execute; an acceptance criterion is met only if those commands
pass; and every production file, function or class added must have at least one
line execute in that run. It ends with the one action that follows — for
everything you add, add or extend a test that calls it — and with the explicit
statement that markers, hashes and coverage summaries are not read, so nobody
reinvents them.

**No internal vocabulary appears.** Not witness, obligation, behaviour unit,
readiness or capsule. A model that must learn our nouns to comply is being
taxed for our convenience, and a test asserts their absence.

**It is bounded at 250 tokens, and the bound is enforced by a test.** This is a
contract statement, not a rule wall. Long instruction blocks measurably degrade
a small model's compliance with the rules that are *not* mechanically enforced,
so a contract that grew unchecked would cost more than the speculation it
replaces. A budget nothing checks is a comment; this text is sent on every turn
of every slice, so the ceiling is asserted alongside the content.

## Consequences

The agent no longer has to reverse-engineer the gate from refusal strings. The
expected effect is fewer speculative turns and less thinking spend per slice,
which is now *measurable* rather than anecdotal because ADR 0101 records
per-call tokens: compare thinking-token share and call count per slice on the
next live run against the CAP-4EE9 baseline (46 turns, 1,978,100 cumulative
input tokens, 36,304 output).

Stating the limits and commands inside the prompt means the prompt now depends
on `patch_policy` and `verification_commands` being accurate in the request. They
already governed admission; now a mismatch is visible to the model rather than
only to the audit trail, which is the safer direction for a disagreement to
surface in.
