# ADR 0072: One harness-owned projection of a task's current evidence

- Status: Accepted
- Date: 2026-07-29

## Context

`report.json` is written exactly once per task, by
`workflow.vertical_slice.VerticalSlice._finalize_report`, at the moment the
task first stops. It is never updated afterwards, and it must not be: audit
history is append-only, and what the original stop said is a fact worth
keeping verbatim.

Several surfaces nevertheless read that one-time snapshot as if it described
the task *now*.

### The live case

Crisis Atlas (discovery `DISC-571F85289FB8`, approved plan
`PLAN-E1B90639E58D`, run 2026-07-29 against `qwen3.6-27b` at 32K) made this
concrete. Its fourth slice, `TASK-5494B387C75F90D0FDE114A7`, stopped at
`human_review_required` carrying a failed verification. An operator then
imported a hash-bound manual-frontier patch (ADR 0031), the applied patch
verified, and the task transitioned to a persisted `COMPLETE`. The whole plan
delivered.

`architect.delivery.prepare_plan_delivery` gated on that persisted
`COMPLETE` -- and then built its `verification_summary` by reading each
task's `report.json` through a private `_report()` helper. For the final
slice that file still said `human_review_required` and still carried the
pre-repair failed run. So `delivery.json`, and the whole-project frontier
review handoff generated beside the ZIP, both contradicted the workflow state
that had authorized the delivery in the first place.

The same divergence was reachable elsewhere:

* `ui.application._task_summary` labelled every task in the overview list
  with `report["outcome"]`, so a repaired slice listed as
  `human_review_required` indefinitely.
* `ui.application.task_detail` returned only the snapshot under `report`.
* `cli.app`'s `inspect` command emitted the same snapshot.
* `review.case._fresh_evidence` *did* project newer evidence, but only for
  the stop paths it happened to enumerate, using its own private event
  tables. It was the only place in the codebase that knew a task's evidence
  could move, and nothing else could reuse what it knew.

`review.case` also decided `stop_reason_text` with a separate heuristic --
"has a continuation or a manual-frontier apply started?" -- which silently
missed the verification-retry path, where a failed retry left the operator
reading the original escalation message.

### Why not fix delivery alone

Because the defect is structural, not local. Five surfaces label a task's
outcome, each was reconstructing "is the report still current?" from its own
partial table of event types, and any new stage added later would have to
remember to update all five. Fixing `delivery.py` would have restored
agreement for one consumer and left the next stage to break it again.

## Decision

Add `apoapsis.reporting.current_state`, a single read-only projection that
computes a task's current outcome, verification evidence, acceptance
coverage, and stop/completion reason from three sources the harness owns
outright:

1. persisted task state (`SQLiteTaskStore.get_task`) -- the sole authority on
   what the outcome *is*;
2. the append-only event history -- the sole authority on *which* stage
   produced the evidence behind that outcome; and
3. the immutable operation artifact that stage wrote -- the sole authority on
   *what that evidence says*.

It returns a `CurrentTaskEvidence` record carrying both the current outcome
and `original_report_outcome`, so a consumer can display the repair and the
original stop side by side without either overwriting the other.

Nothing writes. `report.json` is not rewritten, appended to, or moved.

### Outcome comes from state, not from the report

`_OUTCOME_FOR_STATE` maps `COMPLETE`/`FAILED`/`HUMAN_REVIEW_REQUIRED` to the
corresponding `TaskOutcome`. Any other state -- mid-flight, or `ROLLED_BACK`,
which `TaskOutcome` cannot express -- yields `outcome = None`. Inventing an
outcome for a run still in progress would describe a stage that has since
moved on.

### Evidence location comes from the decisive event

The *decisive event* is the newest event whose `to_state` equals the task's
current state. `_DECISIVE_EVENT_GENERATION` maps each known decisive event
type to the artifact family holding its evidence:

| Decisive event | Evidence |
| --- | --- |
| `verification_passed`, `repair_verification_passed`, `verification_failed`, `repair_budget_exhausted`, `acceptance_coverage_incomplete`, `local_agent_verification_passed`, `local_power_sandbox_verification_passed`, `frontier_agent_verification_passed`, routing/spec stops | `report.json` |
| `frontier_escalation_not_configured` | `agent-session.json` or `local-power-session.json` |
| `bounded_frontier_requires_human` | `frontier-agent-session.json` |
| `review_verification_retry_passed`/`_failed`/`_incomplete` | `review-verification-retry-{operation_id}.json` |
| `manual_frontier_verification_passed`, `manual_frontier_apply_verification_failed` | `manual-frontier-verification-{operation_id}.json` |
| `review_local_continuation_requires_human` | local stage session |
| `review_frontier_continuation_requires_human`, `review_frontier_stage_requires_human` | `frontier-agent-session.json` |
| `review_continuation_verification_passed` | resolved from the newest preceding *started* event |

The original run's own transitions resolve to `report.json` deliberately:
`_finalize_report` writes it at that exact moment, so it is not stale
relative to anything. The report is only ever superseded by stages that run
*after* it.

The two agent-loop stops resolve to a session file instead, preserving what
`review.case` already did. At a stop, `report.verification_results`
aggregates every stage's runs (`_record_agent_result` extends the list), and
a reviewer looking at the current stop needs that stage's own narrower
evidence.

`review_continuation_verification_passed` is written identically by the local
continuation, the frontier continuation, and a fresh frontier stage, so the
completion event alone cannot identify its artifact. `_resolve_stage_generation`
scans backwards from it for the newest `*_started` event and uses that.

### Fail closed, always

When the event history says an evidence generation exists but its artifact is
missing, malformed, or unidentifiable (no `operation_id` on the event),
the projection reports `EvidenceIntegrity.MISSING`/`MALFORMED` with **empty**
verification results. It never falls back to `report.json`.

An unrecognized decisive event fails closed the same way, for the reason ADR
0021 gave for stop classification: an event type the table has not been
taught about is not evidence that the first stop still stands, and a future
stage must not inherit an old pass by default.

The direction of error is deliberate. Delivery can refuse a task whose
completion is unproven; it cannot detect a plausible substitution.

### Acceptance coverage from the immutable result

For a superseding generation, coverage is recomputed by
`coverage_from_verification_result`, which reconstructs the configured
commands from each `VerificationCommandResult`'s own `required`/`acceptance`
flags. ADR 0018 put those flags on the result record precisely so audit
consumers would not have to rebuild authority from mutable current
configuration; reading today's `config.verification.commands` instead would
let an edit made after the fact change what a past run is said to have
proven.

When the deciding event carries a `coverage` payload -- the STRICT rejection
paths in `review.execution` and `manual_frontier.apply` both serialize one --
that is preferred, because it is what the harness actually decided on.

### Delivery gates on the projection

`prepare_plan_delivery` now requires `evidence.is_verified_complete` for
every slice: persisted `COMPLETE`, **and** intact evidence, **and** a passing
deciding run. A slice that is formally `COMPLETE` but whose artifact cannot
be read raises `SlicePackagingError`; the plan stays `APPROVED`, no ZIP is
written, and no `delivery.json` is recorded.

The delivered `verification_summary` gains `evidence_generation`,
`evidence_event_type`, `evidence_sources`, `evidence_integrity`,
`supersedes_original_report`, and `original_report_outcome`. The existing
`slice_id`/`task_id`/`outcome`/`verification` keys keep their shape.

The whole-project frontier handoff's verification section is retitled
"Per-slice verification history" and states plainly that it is per-slice
history, not evidence that the integrated project verifies as a whole.

## Consumers

Every surface that labels a task outcome now reads the projection:

* `ui.application.task_detail` -- adds `current_evidence` alongside the
  preserved `report`;
* `ui.application._task_summary` -- `outcome` now comes from state, with
  `original_report_outcome`, `evidence_generation`, and `evidence_integrity`
  beside it;
* `review.case.build_review_case` -- `verification_results`,
  `acceptance_coverage`, and `stop_reason_text` all come from the projection;
  the private `_fresh_evidence` helper and its three event tables are deleted;
* `architect.delivery.prepare_plan_delivery` -- gate and summary, as above;
* `architect.slice_service.project_slice_status` -- adds `current_evidence`;
* `cli.app`'s `inspect` -- adds `current_evidence` beside `report`.

## Consequences

### Behaviour changes an operator will see

* A task repaired after its first stop now reports its real current outcome
  everywhere, instead of `human_review_required` in the overview list, the
  Report page, `delivery.json`, and the frontier handoff.
* `stop_reason_text` after a failed verification-only retry now reads
  "configured verification still failed on retry" instead of the original
  escalation message.
* Delivery can now fail on a plan whose slices are all `COMPLETE`, when a
  task audit directory has been damaged or hand-edited. The error names the
  slice, the generation, and the integrity problem.

### What this does not do

It does not touch the authority boundary. No model chooses, proposes, or
influences any value in the projection; every input is harness-written state.
It does not add a whole-project integrated verification gate before delivery
-- per-slice verification is now *reported* honestly as per-slice history,
but the gate itself is separate work.

It does not repair the Crisis Atlas product defects (offline-mode dashboard,
absent browser/API integration, seed README). Those are product outcomes of a
verification contract that could not distinguish a same-origin API call from
an external resource, which is a different change.

### Rejected alternatives

**Rewrite `report.json` after a repair.** Destroys the original-stop record
and makes audit history mutable. The whole point of the append-only rule is
that a later stage cannot edit what an earlier one observed.

**Write a second `report-current.json` at each repair.** Adds a write path to
every stage, and a stage that forgets to write it reintroduces the divergence
silently. A read-time projection cannot be forgotten.

**Let each consumer keep its own freshness logic, fixing delivery only.**
This is what produced the defect: five surfaces, five partial tables, one of
which was right.

**Fall back to `report.json` when newer evidence is unreadable.** This is the
substitution the failure mode is made of. An unproven task is a visible
problem; an old pass wearing a new task's label is not.

## Verification

`tests/test_current_evidence_projection.py` (21 cases) covers, from persisted
state and on-disk artifacts only:

* the untouched first stop, where `report.json` is correctly current;
* local continuation, Local Power sandbox continuation, frontier
  continuation, and fresh frontier stage completions superseding a
  `human_review_required` report;
* manual-frontier repair completion -- the literal Crisis Atlas shape;
* a verification retry that remains `HUMAN_REVIEW_REQUIRED`, reporting the
  retry's commands rather than the report's;
* missing, malformed, and unidentifiable newer evidence, each failing closed
  against a deliberately *passing* stale report;
* an unmapped decisive event and a continuation completion with no preceding
  started event, both failing closed;
* `report.json` byte-identical after projection;
* coverage recomputed from immutable result flags, including the
  non-acceptance and skipped-command cases; and
* coverage on a stop event payload taking precedence.

`tests/test_architect_slice.py::DeliveryCurrentEvidenceTests` covers delivery
end to end: the summary agreeing with the projection on outcome, commands,
generation, and sources; the retitled handoff section; delivery refusing a
`COMPLETE` slice with missing evidence and with malformed evidence, leaving
the plan `APPROVED` and writing no `delivery.json`; and
`project_slice_status` agreeing with `delivery.json` on the generation.
