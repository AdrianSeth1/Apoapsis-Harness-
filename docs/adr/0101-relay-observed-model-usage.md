# ADR 0101: Report Capability Sandbox token usage from the relay

## Status

Accepted and implemented on 2026-08-03.

## Context

Every completed Capability Sandbox task reported `input_tokens: 0`,
`output_tokens: 0` and no model call count. The numbers were not wrong by a
margin; they were absent and published as zeros.

The cause is structural rather than a bug in the report builder. The report's
token totals are summed from `ProviderCallTelemetry` — one record per call the
*harness* makes through an instrumented provider. On the sandbox path the
harness makes no such call: a native Qwen CLI inside the workcell talks to the
model server, and the only component that observes those exchanges is the
controller-owned relay, which counted bytes, requests and completion but never
tokens.

The consequence is that the pathway which actually ships work produced the
least evidence about itself. "Did context balloon on this slice?", "how many
calls did it take?" and "what did it cost?" could not be answered from the
audit artifacts of a project whose thesis is evidence over claims.

A second, smaller obstacle: an OpenAI-compatible *streamed* response carries no
usage at all unless the request asked for it with
`stream_options.include_usage`. The pinned CLI does not ask.

## Decision

**The relay records usage, because the relay is the only witness.** Usage is
parsed from bytes the relay is already pumping: from the body of a non-streamed
response (buffered up to a fixed 2 MiB cap), and from the usage frame in the
tail of an event stream (a fixed 64 KiB tail window). Neither introduces
unbounded buffering, and streaming is still forwarded chunk by chunk.

`None` is preserved as a distinct answer throughout. A record whose upstream
reported nothing carries `usage = None`, never zero, and every total is
published alongside the count of exchanges that reported one. A summary that
cannot distinguish "spent nothing" from "measured nothing" is the defect this
ADR closes, and reproducing it one layer up would fix nothing.

**The relay may add `stream_options.include_usage`, under an explicit policy
switch.** This is the one place the relay modifies a forwarded body, so it is a
policy field (`inject_stream_usage_options`), defaults to off, applies only to
the completions routes, is refused when the client stated its own preference
either way, and is recorded per request as `usage_probe_injected`. There is no
additional check on the destination, because the relay has exactly one: the
upstream its controller configured before the workcell started. A rewritten
body cannot reach anywhere else, and a host test here would imply it could.
The forwarded `Content-Length` is corrected with the body. Qualification
pilots keep the default off, so their frozen wire behaviour is unchanged; the
product path passes it explicitly.

**Sandbox usage is reported as a summary, not as manufactured call records.**
`FinalTaskReport.local_model_usage` carries the observed totals, the peak
single-prompt size, the measured-exchange count, and a pointer to the per-call
series artifact. Its totals are included in the report's `input_tokens`,
`output_tokens` and `cached_input_tokens`. It is deliberately *not* appended to
`provider_calls`: that list's length is `number_of_calls`, one record per call
the harness issued, and each record claims a request id, a prompt hash and a
harness-side latency that do not exist for traffic the harness never held.
Fabricating them would put invented fields in the audit surface to make one sum
look tidier.

**The per-call series is an artifact, not just a total.** The sandbox writes
`evidence/sandbox/model-usage-series.json` — call index to input and output
tokens, in order — into the task's audit directory. A total answers what a
slice cost; only the series answers whether context grew across it, which is
the open question behind the "16K by slice 4" trajectory.

The control arm's usage is reported under `parity_guard.control_model_usage`
rather than merged into the slice total: with the parity guard on, a merged
number would report a slice as costing twice what the delivered work cost.

## Consequences

Token totals on the sandbox path are now measured rather than absent, so the
later context-reduction work (orientation brief, model-server lease, parity
sampling) can be verified as numbers instead of impressions. The relay now
parses two response shapes it previously only counted, and rewrites at most one
field of a completions request bound for its single configured upstream. Any usage the upstream does not report
is visible as a gap — `calls` below `exchanges_observed` — rather than silently
summing to a smaller, confident-looking total.
