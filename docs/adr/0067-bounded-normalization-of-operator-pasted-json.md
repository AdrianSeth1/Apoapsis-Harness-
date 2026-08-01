# ADR 0067: Bounded normalization of operator-pasted JSON

Date: 2026-07-26

Status: Accepted

Extends ADR 0031 (manual subscription-based frontier handoff), ADR 0032
(discovery and frontier planning handoff), and ADR 0066 (literal response
shape). Same reasoning as ADR 0058, applied to a different transport.

## Context

A pasted frontier planning response was rejected with:

```text
response is not valid JSON: Expecting value: line 1 column 1 (char 0)
```

The response began:

```text
`json
{
  "package_id": "FPKG-A11D933EADC7",
```

A Markdown code fence. Character 0 was a backtick, so `json.loads` stopped
immediately. The plan behind it was complete and correct.

Two things were wrong.

**The wrapper.** The handoff already instructs the model to return the object
with "no markdown code fence, no extra text before or after it," and ADR 0066
repeats it. It happens anyway, because a chat interface renders JSON in a
fenced block and the fence comes along when the operator copies it. This is an
artifact of *how the text was transported*, not something a model chose to
write, and the operator has no reason to expect it matters.

**The message.** `Expecting value: line 1 column 1 (char 0)` names the position
of the problem without showing the character sitting there — the one piece of
information that makes it obvious. The operator sees a correct-looking JSON
document rejected as "not valid JSON" at its very first character.

This was the third failed round-trip on one planning handoff, each costing a
full trip back to the subscription session. ADR 0058 already established that
transport-level noise from a model interface may be normalized deterministically
and audibly, rather than treated as the operator's problem; that precedent was
never applied to the paste transport.

## Decision

`apoapsis.specification.pasted_json` provides `parse_pasted_json`, used by both
paste importers (`discovery/manual.py` and `manual_frontier/importer.py`).

It removes exactly two things before parsing:

- a leading UTF-8 byte-order mark;
- one surrounding Markdown code fence (one or more backticks, or three or more
  tildes, with an optional language tag, and its matching closer).

Both are transport artifacts, both put a non-JSON character at position 0, and
both are unambiguous to detect. Each removal is recorded as a note included in
the error text if parsing still fails, so the normalization is never silent.

**Nothing else is normalized.** Prose preambles, trailing commentary, and
scanning forward for the first `{` are all guesses about intent, and a wrong
guess would silently parse a fragment of what the model meant to say. Those
still fail — loudly.

When parsing does fail, the error quotes the first 80 characters of what was
actually received:

```text
response is not valid JSON: Expecting value: line 1 column 1 (char 0);
it starts: "Here is the plan:\n{"kind": "plan"}"
```

## Consequences

- The single most common paste failure stops costing a round-trip.
- An operator facing a genuine malformation can see what the parser saw without
  opening an audit file.
- Normalization stays inspectable: what was stripped is reported, and the
  unmodified paste is still what the audit store records.
- Model authority is unchanged. This affects only how bytes already authorized
  by the operator are read; every hash check, schema validation, and approval
  step downstream is untouched.

## Alternatives rejected

- **Extract the first balanced `{...}` from anywhere in the text.** Would
  accept a plan buried in commentary, and would silently pick the wrong object
  when a model narrates with JSON examples. Robustness bought with ambiguity.
- **Strip a prose preamble up to the first `{`.** Same objection, and it would
  mask a model that ignored the response format entirely.
- **Do nothing; the instruction is already in the handoff.** True and
  insufficient. The fence is added by the interface, not chosen by the model,
  and the instruction cannot reach it.
- **Normalize in the browser before POSTing.** Puts the rule somewhere it is
  untestable and unauditable, and leaves the CLI path unfixed.

## Verification performed

```powershell
python -m unittest tests.test_schemas                        # 15/15
python -m unittest tests.test_schemas tests.test_discovery `
    tests.test_manual_frontier tests.test_manual_frontier_ui `
    tests.test_discovery_ui tests.test_review                 # 126/126
python -m compileall -q src tests                            # passed
```

New coverage in `tests/test_schemas.py::PastedJsonTests`: the exact
single-backtick fence observed live, triple-backtick and bare fences, a
byte-order mark, plain JSON passing through unchanged, prose being rejected
rather than guessed at with the text shown in the error, and an empty fence
reported as empty.
