# ADR 0020: Deterministic human review and resume

- Status: Accepted
- Date: 2026-07-19

## Context

`WorkflowState.HUMAN_REVIEW_REQUIRED` has existed since ADR 0001, and
`ALLOWED_TRANSITIONS` has always defined real outgoing edges from it back to
`SPEC_DRAFTED`, `SPEC_APPROVED`, `IMPLEMENTING`, `PATCH_READY`, `VERIFYING`,
`FAILED`, and `ROLLED_BACK` -- but nothing ever used them. A full inventory of
every code path that reaches `HUMAN_REVIEW_REQUIRED`
(`workflow/vertical_slice.py`, the only module that ever transitions there)
found exactly five distinct stop reasons, and confirmed that `apoapsis
rollback` would fail outright on the two stop reasons that occur before any
worktree is created (`WorktreeManager.describe` raises when the directory
never existed). Resuming a task was, in practice, impossible: `apoapsis
verify` hard-requires `PATCH_READY`, and no command read or acted on
`HUMAN_REVIEW_REQUIRED` at all.

### The five stop reasons

1. **`specification_not_approved`** -- the user (or `--yes`-less CLI prompt)
   rejected the extracted specification. Edge: `SPEC_DRAFTED ->
   HUMAN_REVIEW_REQUIRED`. No worktree exists yet.
2. **`deterministic_route_requires_human`** -- `select_agent_route()`
   (`workflow/routing.py`) returned `AgentRoute.HUMAN_REVIEW_REQUIRED`:
   `CRITICAL` risk, `HIGH` risk with no frontier configured, or an explicit
   `FRONTIER_ONLY`/`LOCAL_THEN_FRONTIER` route override (via `--agent-route`,
   which bypasses `ApoapsisConfig`'s own model validator since `model_copy`
   does not re-run it) with no frontier configured. Edge: `ROUTED ->
   HUMAN_REVIEW_REQUIRED`. No worktree exists yet.
3. **`acceptance_coverage_incomplete`** -- one-shot mode, `STRICT` completion
   policy, configured verification passed but an active acceptance criterion
   is not proven. Edge: `VERIFYING -> HUMAN_REVIEW_REQUIRED`. A worktree
   exists with a verified-passing, uncommitted diff.
4. **`frontier_escalation_not_configured`** -- the bounded local coding agent
   exhausted its turn budget (or was explicitly asked to escalate, or its
   provider call failed) and no `LOCAL_THEN_FRONTIER` continuation is
   configured. Edge chain: `IMPLEMENTING -> ESCALATION_REQUIRED ->
   HUMAN_REVIEW_REQUIRED`. A worktree exists with the local agent's full turn
   history and partial, uncommitted diff.
5. **`bounded_frontier_requires_human`** -- the bounded frontier coding agent
   (direct `FRONTIER_ONLY` route, or after a local-to-frontier escalation)
   exhausted its own budget. Same edge chain. A worktree exists with the
   frontier agent's turn history and partial, uncommitted diff.

Nothing else -- the second (bounded-correction) specification-extraction
failure still stops at `FAILED`, unchanged from ADR 0018, and is out of
scope here.

## Decision

A new top-level package, `src/apoapsis/review/`, adds a deterministic review
and resume layer entirely on top of the existing state machine --
**`workflow/states.py`'s `ALLOWED_TRANSITIONS` did not change at all**; every
edge this milestone uses already existed.

### 1. `ReviewCase`: a deterministic projection, never a model's claim

`review/case.py`'s `build_review_case()` projects a `ReviewCase` from
persisted task state, workflow events, the final report (if one exists),
audit artifacts, live worktree/repository state, and current configuration.
It never trusts anything a model said. Key fields: `stop_reason_kind`
(one of six `StopReasonKind` values covering the five scenarios above, plus
`UNKNOWN` as a fail-closed fallback when no recognized event is found --
never a guessed capability set), `stop_reason_text` (the original run's
`FinalTaskReport.error` for an unmodified stop, or the *current* triggering
event's own `reason` payload once a continuation has run -- so the text
never describes a stop reason that no longer applies), `worktree_exists` /
`worktree_fingerprint` / `repository_head_commit` (all recomputed fresh,
every time), `current_diff`, `verification_results`, `acceptance_coverage`,
`normalized_failures`, consumed vs. configured local/frontier turn-patch-
verification budgets (read live from `agent-session.json`/
`frontier-agent-session.json`, not from the possibly-stale report), whether
frontier is available (checked against *current* configuration, not the
stale routing decision that originally stopped the task -- so adding
frontier credentials after a local-only stop makes frontier continuation
newly visible without re-running anything), `continuations_used` (derived
from the append-only event log, never a separately stored counter that could
drift), and `eligible_actions`.

### 2. Exactly five action kinds, eligibility computed deterministically

`ReviewActionKind`: `inspect_only`, `abandon`, `verification_only_retry`,
`local_continuation`, `frontier_continuation`. `review/classify.py` maps
each `StopReasonKind` to a fixed base eligible-action set, then removes
`frontier_continuation` if frontier is unavailable and removes both
continuation kinds once `continuations_used >= max_continuations_per_task`.
The eligible set is data the caller reads, never data it can widen -- every
mutation in `review/execution.py` independently re-checks `action in
review_case.eligible_actions` before doing anything.

**Continuation eligibility is deliberately narrow and mechanical**: a
continuation always resumes the *exact* agent session (local or frontier)
that already exists and stopped for this task -- reading its own
`agent-session.json`/`frontier-agent-session.json`. There is no "let frontier
pick up where local left off" path in this milestone: `frontier_continuation`
is only ever eligible when a frontier session already ran and stopped
(scenario 5), never merely because frontier configuration now exists for a
task that only ever ran locally (scenario 4). This is a deliberate scope
boundary (see Non-goals), not an oversight -- it keeps every continuation a
resume of session state that genuinely exists, never a fresh launch built
from a stale escalation package.

### 3. Resuming a `BoundedAgentSession` without resetting anything

`agent/session.py` gains `BoundedAgentSession.resume(..., prior_result=)`, a
classmethod that seeds `records`, `observations`/`observation_chars` (from
the last turn's own ledger snapshot), `verification_results`,
`patch_attempts`, `verification_runs`, `last_acceptance_coverage`, and the
digest-scoped `command_results` entry for the current worktree fingerprint,
all from a prior `AgentSessionResult` rather than starting empty. `run()`
gains a `start_turn` parameter (default `1`, fully backward compatible) so a
resumed session continues turn numbering rather than restarting it. The one
piece of state deliberately **not** restored is the identical-verification
dedup cache (`verification_cache`) -- a resumed session may re-run the exact
check that was failing when it stopped once more before duplicate rejection
resumes; this affects only whether that one re-run is permitted, never any
budget, authority, or correctness guarantee.

Because `AgentSessionResult.turns`/`patch_attempts`/`verification_runs` are
computed from the seeded (cumulative) `records`/counters, a continuation's
final result is, by construction, the *total* consumed budget across the
original run and every continuation -- there is no separate bookkeeping to
keep in sync, and nothing can silently reset what was already spent.

### 4. Ceilings: continuation count and additional turns, applied together

`config.py`'s `ReviewConfig` (`max_continuations_per_task`,
`max_additional_turns_per_continuation`) is the entire user-facing budget
surface for a continuation -- a single `additional_turns` number. The same
delta is added to the resumed agent's turn, patch-attempt, and
verification-run ceilings together (never turns alone, which could leave a
session able to keep talking but never patch or verify again), and the
ceiling used is always *cumulative*: original config ceiling, plus every
`additional_turns` ever authorized for this task's continuations
(`review/case.py::continuation_additional_turns`, summed from the event
log), plus this continuation's own delta. A continuation can never request
more than `max_additional_turns_per_continuation` at once, and never proceed
once `max_continuations_per_task` continuations have already run.

### 5. The immutable continuation package

`review/package.py::build_continuation_package()` builds a
`ReviewContinuationPackage` -- the original task specification, active hard
constraints, the current diff, the exact stop reason, prior normalized
failures, the configured verification-command catalog, the authorized
budget, and the effective (cumulative) agent budget -- and
`write_continuation_package()` writes it to
`.apoapsis/tasks/<task_id>/review-continuation-<operation_id>.json` via the
existing `TaskAuditStore`, **before any resumed model call is made**. This
mirrors the same "package written before it leaves Apoapsis" discipline
already used by Architect Mode's `PlannerRequestPackage` (ADR 0019) and the
existing frontier `EscalationPackage` (ADR 0006).

### 6. Idempotent, crash-safe operations

`review/store.py::ReviewOperationStore` is a small SQLite ledger
(`.apoapsis/review-operations.db`) keyed by a caller-supplied
`operation_id` (a stable idempotency key). Creating the same `operation_id`
twice is rejected outright (`DuplicateOperationError`) -- the row already
exists, so a caller that resubmits after a timeout or an ambiguous response
can never cause the underlying work to run twice. An operation transitions
`recorded -> running -> succeeded | failed`; attempting to mark an
already-`running` operation running again raises
`OperationAlreadyRunningError` -- the concrete fail-closed behavior for "a
process crashed after a provider request had possibly already been
transmitted": the stuck `running` row is never silently re-entered, and a
caller must inspect it and start a genuinely new operation to proceed.

### 7. Execution and its guardrails

`review/execution.py::execute_review_action()` is the single entry point
for every mutation:

1. Projects a fresh `ReviewCase` and checks the caller's `expected_version`
   against it (`ReviewError` on mismatch -- the same optimistic-concurrency
   discipline as every other store in this codebase).
2. Checks the requested action is in `eligible_actions`
   (`InvalidReviewActionError` otherwise).
3. For `verification_only_retry`/`local_continuation`/`frontier_continuation`,
   requires the caller's `expected_worktree_fingerprint` to match a
   freshly recomputed one (`WorktreeChangedError` otherwise) -- this is the
   literal "revalidate the worktree fingerprint... before continuing"
   requirement, implemented as a real optimistic check, not documentation.
4. For the two continuation actions, checks `additional_turns` against
   `max_additional_turns_per_continuation` and the continuation-count ceiling
   (`ContinuationCeilingExceededError`).
5. Creates the operation record, marks it `running`, dispatches to the
   action-specific handler, and marks `succeeded`/`failed` -- any exception
   during the handler is recorded on the operation before propagating, so a
   failed continuation never leaves the operation ledger ambiguous.

`abandon` cleans up the worktree only if one exists (fixing the CLI
`rollback` gap the inventory found) and transitions to `ROLLED_BACK`.
`verification_only_retry` re-runs configured verification with no model
call at all and, under `STRICT`, recomputes acceptance coverage the same
way `_one_shot_complete_or_gap` does. `local_continuation` /
`frontier_continuation` build the continuation package, recompile context
fresh, resume the `BoundedAgentSession`, and drive the same
`IMPLEMENTING -> PATCH_READY -> VERIFYING -> COMPLETE` or `IMPLEMENTING ->
ESCALATION_REQUIRED -> HUMAN_REVIEW_REQUIRED` edges
`_complete_agent_workflow`/`_require_human_after_agent` already used, under
new `review_*_continuation_*` event types (every one of which includes a
`reason` string, so a subsequent `ReviewCase` always has an accurate,
current stop-reason text without needing the original, now-stale report).

### 8. CLI

```
apoapsis review list
apoapsis review inspect <task-id>
apoapsis review abandon <task-id> --expected-version N --operation-id ID
apoapsis review retry-verification <task-id> --expected-version N --expected-fingerprint F --operation-id ID
apoapsis review continue-local <task-id> --expected-version N --expected-fingerprint F --operation-id ID --additional-turns N
apoapsis review continue-frontier <task-id> --expected-version N --expected-fingerprint F --operation-id ID --additional-turns N
```

Every mutating command requires an explicit `--operation-id`; there is no
default or auto-generated value, so reusing one is always the caller's
deliberate choice (the idempotency guarantee above).

## Rejected alternatives

- **Let a continuation redirect which agent continues** (e.g., resume with
  frontier after a local-only stop with no frontier session). Rejected:
  every continuation now provably resumes session state that actually
  exists; inventing a fresh-frontier-launch path would need its own escalation
  package construction and review-parity testing this milestone doesn't have
  room for. See Non-goals.
- **Reset budgets per continuation instead of accumulating them.** Rejected
  outright by the requirement ("never reset an earlier budget") and by
  `BoundedAgentSession.resume()`'s own construction: seeded counters are
  already cumulative, so ceilings must be too, or the arithmetic breaks.
- **Auto-generate `operation_id` inside the CLI/service.** Rejected: the
  entire crash-safety guarantee depends on the *caller* being able to
  deliberately reuse the same id after an ambiguous failure; an
  auto-generated id on every invocation would make that impossible.
- **Rewrite `report.json` after every continuation.** Rejected as
  disproportionate scope: `report.json` remains a snapshot of the original
  stop; `ReviewCase` instead reads live audit artifacts (`agent-session.json`,
  the event log) for anything that changes after a continuation. Documented
  explicitly as a simplification, not silently papered over.

## Non-goals

- Does not add a subscription-backed or hosted-API provider adapter.
- Does not change one-shot mode's own execution path at all --
  `verification_only_retry` is a new, separate review-driven flow, not a
  modification to `VerticalSliceRunner.run()`'s one-shot branch.
- Does not let a continuation switch which agent (local vs. frontier)
  resumes, or launch a fresh frontier session from a local-only stop.
- Does not wire context-measurement instrumentation into continuation model
  calls -- call/response/telemetry audit records are still complete; only
  the context-window-utilization measurement artifact is skipped for
  continuation-driven calls.
- Does not add a UI surface -- that is Commit C2, tracked separately.
- Does not change `workflow/states.py`'s `ALLOWED_TRANSITIONS` at all; every
  edge used here already existed.

## Consequences

A task stopped at `HUMAN_REVIEW_REQUIRED` for any of the five known reasons
now has a real, deterministic, harness-computed set of next actions instead
of dead state-machine capacity. A human can abandon a task that never had a
worktree (the CLI `rollback` gap is fixed), retry verification alone, or
authorize a bounded amount of additional local or frontier agent work that
resumes exactly where the prior session stopped -- never resetting spent
budget, never letting the model itself grant more turns, choose a
transition, or claim completion. Every continuation's exact inputs are
captured immutably before any model call, and duplicate or crash-ambiguous
operation submissions are rejected rather than silently repeated.

**Commit C2 update:** Commit C2 added a Human Review queue and case-detail
view to the existing ADR 0014 local UI, with two-step confirmation for every
mutating action, optimistic-version and worktree-fingerprint conflict
handling, and a background worker (outside the HTTP request path) that
performs continuation work asynchronously so a browser disconnect can never
cancel, duplicate, or repeat an authorized operation.
