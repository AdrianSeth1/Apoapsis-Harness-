# ADR 0105: State each repair rule once, and write stops for the operator

## Status

Accepted and implemented on 2026-08-03.

## Context

Two audiences read the harness's refusals, and both were being served the same
text — which suited neither.

**The model.** Repair packets listed one finding per line, and each line
carried the whole rule behind it. `CAP-4EE9F101146E4556/result.json` contains
"…is new in this candidate and no current-state witness proves it is reached.
Inherited tests staying green is not evidence: they stay green because they
never reach it." three times, once per new file. A twelve-file checkpoint would
have carried it twelve times, into a 32K-window model, on every repair turn.
The repetition adds no information: the rule is the same, only the paths differ.

**The operator.** Stop text is written in harness-internal vocabulary. "No
witness survived validation, so nothing current proves anything about this
candidate" is exactly true and tells a person watching a stopped run neither
what happened nor what to do next. The precision is not the problem; being the
*first* thing a person reads is.

## Decision

**One rule, then the list it applies to.** `ReadinessFinding` gains an
`explanation` field, separate from `detail`: `detail` is what is wrong with
*this* item, `explanation` is the rule it is an instance of, identical across
every finding that shares it. `readiness_packet` groups by
`(block, explanation)` and prints the explanation once with its items beneath.
Findings whose detail already stands alone keep the old flat form, because
hoisting a rule that appears once adds a line rather than removing one.

Grouping preserves first-appearance order, so a packet does not reshuffle
between turns for a model reading it against the previous one. A three-file
unexercised-behaviour group fell from three paragraphs to one paragraph and
three paths; the marginal cost of the twelfth file is now a path, not a
paragraph.

**Every stop gets an operator rendering, in three parts.**
`OperatorExplanation` carries `attempted`, `refusal`, `next_action`, and the
verbatim internal text in `detail`. `reporting.operator` holds one table for
all three families — `CheckpointOutcome`, `SessionOutcome` and
`StopReasonKind` — because checkpoint verdicts, session ends and review stops
written in three places become three voices, and the operator experiences them
as one product.

`next_action` is deliberately singular. A stop that offers three options offers
none, because choosing between them needs exactly the knowledge the operator
does not have.

`CheckpointDecision.operator` and `ReviewCase.operator` carry the rendering;
neither `detail` nor `stop_reason_text` changed, so the precise record is
untouched. The review page shows the three parts first and puts the harness's
own wording behind a disclosure directly underneath. Nothing is hidden — the
ordering is the whole change.

A test asserts that no operator-facing string contains `witness`,
`obligation`, `behaviour unit`, `exop`, `capsule`, `workcell` or `fingerprint`.
Those words earn their keep inside the harness; they are not the operator's
vocabulary, and F10's naming tax is a real cost between a new user and their
first success.

## Consequences

Repair packets shrink with the number of affected files rather than growing a
paragraph each, which matters most in exactly the case that was worst: a large
slice with many new files, repaired repeatedly against a small context window.

The operator table must be extended when an outcome is added. That is enforced:
the exhaustiveness tests iterate the enums, so a new `SessionOutcome` without a
rendering fails rather than silently reaching a person as a raw enum value.

The slice card in the plan view still uses its own static explanation text
rather than this rendering; that view is MH-9's subject and is left alone here.
