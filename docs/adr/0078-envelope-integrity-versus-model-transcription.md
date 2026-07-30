# ADR 0078: The Unicode conformance check measures the envelope, not the model

- Status: Accepted
- Date: 2026-07-30
- Amends the evidence source of one check declared in ADR 0077's conformance
  suite (`ConformanceCheck.MULTILINE_UNICODE_INTEGRITY`). ADR 0077 is not
  edited and its boundary is unchanged.
- Consequence of: `docs/evaluation/slice-2b-live-conformance-and-pins-2026-07-30.md`

## Context

This decision is being made about a check that is currently **red**, and that
fact deserves to be stated before anything else. The obvious bad version of
this ADR is a document that reasons backwards from "the gate should be green"
to whatever definition of the check would produce that. The test applied
throughout is not "does this let the run pass" but "does the check, after the
change, establish the property its name asserts, on evidence that could not
have come from anywhere else". Where the change *loses* coverage, that is
recorded below as a loss rather than as a redefinition.

### What the check claims

`conformance.py` declares it as:

> Multiline Unicode file content is not escaped, truncated, or double-encoded
> on the way through the template.

The three named corruptions are all adapter behaviours, and all three silently
damage file writes — a newline that returns as a literal `\n`, a `é` that
returns as `Ã©`, a tail that never returns at all. That is a real property and
it is worth gating on.

### What it actually measured

The Slice 2B driver obtained its evidence by sending the payload to
`llama-server` inside a prompt that asked the model to call `write_file` with
"the exact text between the markers, byte for byte", and then comparing what
came back against the constant it had sent.

The check failed: 140 characters sent, 139 received. A follow-up probe
established what had changed. The astral-plane emoji `U+1F6F0` kept its
`U+FE0F` variation selector. The em-dash survived. Both CJK characters
survived. What changed was that Qwen3.6-27B retyped the typographic quotes
`U+2018`/`U+2019` as ASCII `'`.

Nothing in the transport was lossy. The check reported an adapter defect and
had measured a model's preference about punctuation.

This is not a marginal misattribution. It is a category error with a specific
cost: a red `multiline_unicode_integrity` blocks all downstream quality
measurement under the standing rule that a malformed tool envelope is an
adapter defect until the suite proves otherwise. A check that cannot tell an
adapter defect from a model habit cannot discharge that rule in either
direction — it will block on model behaviour, and it would equally have passed
on a genuinely broken envelope had the model happened to compensate.

### Why the old design could not be repaired in place

Two independent variables were being multiplied and the product reported under
one of their names:

1. whether the adapter, relay, forwarder, and JSON encoding preserve bytes; and
2. whether the model chooses to reproduce a string exactly.

No threshold on the comparison separates them. Loosening it to "ignore
quotation marks" would make the check blind to a template that mangles
quotation marks. Tightening it changes nothing. The only repair is to remove
variable (2) from the experiment.

## Decision

### 1. The check runs against a deterministic echo provider

`multiline_unicode_integrity` now obtains its evidence from
`workcell/echo_provider.py`: an OpenAI-compatible endpoint that parses the
request, finds a marked payload, and returns a `write_file` tool call whose
`content` argument is that exact string. No sampling, no template, no model.

### 2. It runs through the real path, not around it

The probe still executes inside the workcell and still reaches the provider
through the in-container loopback forwarder, the controller-owned Unix socket,
and a real `ModelRelay`. `LiveWorkcellSession.envelope_path` stands up a second
relay and a second forwarder for the duration of the one check and tears both
down afterwards.

Removing the model is the point. Removing the transport would not be — the
relay buffers whole bodies, the forwarder copies streams, and the tool-call
arguments are a JSON string nested inside a JSON document. Those are all places
an escape or a truncation can happen, and they stay in the path.

### 3. It compares bytes, in both directions

`check_envelope_integrity` takes `sent_bytes` and `received_bytes`. The sent
bytes are the payload as the echo provider extracted it from the raw request
body it received; the received bytes are the payload as parsed out of the raw
response. Neither side is the in-process constant the probe was built from —
that constant is used only as a fallback, and when it is used the result says
so in its own detail string.

Bytes rather than `str` because two of the three named corruptions are
invisible after decoding: a UTF-8 payload re-read as Latin-1 does not survive
normalisation into Python text in a form the comparison could name, and a
truncation landing mid-code-point is a decode error rather than a shorter
string.

### 4. Absence of the echo path is `NOT_RUN`, which fails

There is no fallback to the model-transcription probe. If the echo path is
unavailable the check reports `NOT_RUN`, and `evaluate_conformance` treats
`NOT_RUN` as failure. A check that quietly degrades to a weaker evidence source
is how the original defect arose.

### 5. Model transcription accuracy survives as a non-gating metric

`workcell/transcription.py` keeps the original probe — real server, real chat
template, real model, asked to retype the payload — and reports
`TranscriptionFidelity`: whether the transcription was exact, and every
substituted code point named by its Unicode name.

It carries an `attribution` field fixed to `MODEL_BEHAVIOUR` and a `gating`
field fixed to `False`, both as data rather than as prose, because the 2B
failure happened precisely by reading a model result off an adapter-shaped
report. It has no `ConformanceStatus`, it is not a `CheckResult`, and nothing
in `evaluate_conformance` or `evaluate_slice3_gate` can reach it.

## What this stops measuring, and why that is correct

Three things are genuinely lost. Each is stated as a loss.

**1. End-to-end fidelity with a model in the loop.** The suite no longer gates
on "will the bytes the agent intended to write survive all the way from the
model's decision to the file". That composite is what a user experiences. It is
also not a property of the adapter, and attributing it to the adapter is what
made the 2B result uninterpretable. It moves to the transcription metric, where
it is visible, named, and attributed to the component that produces it.

**2. Chat-template coverage of this specific payload.** This is the most
significant loss and it should not be glossed. `llama-server`'s Jinja template
is the component `conformance.py`'s own docstring identifies as most able to
mangle a tool envelope, and an echo provider does not exercise it at all.

What remains gating against the real template is `single_tool_call` and
`parallel_tool_calls`, both of which assert *exact* argument values through the
real template — `Ωmega-42` includes a non-ASCII code point, and the parallel
check compares two argument objects field by field. So template-level argument
corruption is still gated; template-level corruption *of a long multiline
payload specifically* is now observed by the non-gating transcription metric
rather than gated.

That is a real narrowing. It is accepted because the alternative on offer is
not "gate on the template with a multiline payload" — it is "gate on the
template with a multiline payload *and* on the model's punctuation habits,
indistinguishably", which is what was there and what did not work. A narrower
check that means what it says is worth more than a wider one that does not.

**3. Evidence that the real provider can carry the payload at all.** The echo
provider is written by us and could, in principle, share a bug with the check.
This is mitigated but not eliminated: the transcription metric sends the same
payload to the real server on every run, so a real server that could not carry
it would show up there immediately, as an unmeasurable or wildly divergent
transcription rather than as silence.

## Consequences

- `multiline_unicode_integrity` can now fail only for adapter reasons, and its
  failures are actionable without a follow-up diagnostic probe.
- The suite acquires a second, short-lived upstream. `envelope_path` narrows
  its relay to `/v1/chat/completions` and `/health`, budgets it to sixteen
  requests, and closes it in a `finally`. An echo provider outliving its check
  would be an unpinned second upstream, which is the thing the relay exists to
  prevent.
- `ConformanceCheck.MULTILINE_UNICODE_INTEGRITY` keeps its name. The name was
  never the problem; it described the intended property accurately and the
  driver did not implement it.
- One hazard surfaced while implementing this and is recorded because it is
  the same mistake one layer in: `StrictModel` sets
  `str_strip_whitespace=True`, so the first `EchoExchange` silently trimmed the
  payload's trailing newline and the check compared a normalised copy against
  the original. It was caught by the round-trip test rather than by
  inspection. Both `EchoExchange` and `CodepointDifference` now disable
  stripping, with a comment saying why.

## Alternatives considered

**Compare against a normalised form (fold quotes, strip variation
selectors).** Rejected. Every normalisation is a corruption the check would
then be unable to see, and quotation-mark mangling in a chat template is a
plausible real defect.

**Delete the check.** Rejected. Escaping, truncation, and double-encoding are
real, they damage file writes silently, and nothing else in the suite gates on
a multiline payload.

**Leave it failing and measure nothing.** This is what Slice 2B did, and it was
the right call *for a slice that had not analysed the failure*. It is not a
stable resting place: a permanently red check that everyone knows is red for
the wrong reason is a gate that has stopped carrying information.

**Keep the model in the loop but instruct it harder.** Rejected. It makes the
result depend on prompt phrasing, which is neither deterministic nor a property
of the adapter.
