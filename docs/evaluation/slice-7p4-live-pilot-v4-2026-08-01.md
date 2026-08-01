# Crisis Atlas live pilot v4 — independent scoring

Date: 2026-08-01
Evidence: `/home/arya/apoapsis-live-evidence/crisis-atlas-live-pilot-v4/`

## Outcome

The authorized live runner completed all six frozen slots and wrote
`live-pilot-result.json` with status
`six_slots_complete_pending_independent_scoring`. Every slot also wrote its own
`result.json` and clean `teardown.json`; no `llama-server`, GPU compute process,
controller container, or workcell container remained afterward.

The evidence-before-summary digest is
`9d1451db37ab58354fb52db6bcb8253824091d43ed2503bfd1dc573c5dcab434`.
The run is bound to manifest digest `f369760e...`, lock digest `61b36743...`,
runner commit `5c38553`, authorization commit `36fb86c`, and controller image
`sha256:394334e67eb263a65b43b92bc8b0795d8d672c387d08fdd9f6a3e92f7b905a57`.

## Proposal score

Proposal quality is the fraction of the authoritative checkpoint's acceptance
criteria satisfied. Repaired quality is excluded. All six first proposals were
`COMPLETE` with all three criteria satisfied and no readiness blocks.

| Repetition | Default Qwen control | Apoapsis sandbox | Per-pair result |
| --- | ---: | ---: | --- |
| rep-1 | 1.0 | 1.0 | non-inferior |
| rep-2 | 1.0 | 1.0 | non-inferior |
| rep-3 | 1.0 | 1.0 | non-inferior |

No sandbox continuation or external repair was needed. All three comparisons
therefore pass the locked per-pair non-inferiority rule; no aggregate result is
used to hide a pair.

## Detection score

The live sandbox proposals were complete, so the six live slots contained no
incomplete candidate for Apoapsis to catch. Under the locked scorer a clean pass
on a complete shape has detection quality 1.0, but this must not be restated as
a live defect catch.

Defect detection is established separately by the zero-model v8 rehearsal:
17/17 injected controls fired their mapped detector, including incomplete work,
stale evidence, truncation, configuration drift, contamination, and attempts to
hide pair regression. The combined claim is therefore narrow: proposal
non-inferiority was observed live on the Crisis Atlas regression benchmark, and
the harness's detection machinery was proven deterministically on its mapped
controls. Crisis Atlas is not held out and this is not broad-corpus superiority.

## Telemetry

Across six slots the trace records 1,166,038 input tokens, 18,039 output tokens,
and 80 tool calls. All runs ended normally with zero malformed responses and
zero model errors. Per-slot relay-observed request counts match the retained raw
records; they are not substituted for the CLI trace's differently scoped model
request count.

## Release boundary

This result clears the owner-selected Crisis Atlas pilot's proposal
non-inferiority gate. It does not by itself turn the evaluation-only live runner
into a production execution path, and it does not make the older typed Local
Power protocol equivalent to the qualified native Qwen workcell.
