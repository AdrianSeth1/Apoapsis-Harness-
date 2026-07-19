# ADR 0023: Durable model-assisted new-task intake

- Status: Accepted
- Date: 2026-07-19

## Context

`apoapsis run` already does model-assisted specification extraction, but
it does so synchronously, inline, inside one long-lived CLI process --
`VerticalSliceRunner.run()` blocks on the extraction call, then blocks
again on an in-process `approve()` callback, then (if approved) keeps
running the rest of the task end to end. None of that is reusable from
the local UI, whose entire architecture (ADR 0014) is built around HTTP
handlers that must return immediately and typed operations a background
worker executes -- exactly the pattern ADR 0020/0021 already built for
human-review resume. Priority C's own roadmap (`NEXT_STEPS.md`) named this
the highest-value remaining gap: "the UI still cannot originate a
natural-language task and durably carry it through model-assisted
extraction, approval, and new-task execution."

This milestone builds the first half of that: a user types a natural-
language request in the browser, the harness durably records an intake
operation before making any model call, extraction (with its existing one
bounded correction attempt) runs on a background worker, and the browser
can safely disconnect and reconnect by polling the same operation id. The
result is a validated candidate specification sitting at `SPEC_DRAFTED`,
approved through the exact same optimistic-version transition
(`ApoapsisUIService.approve_specification` / `apoapsis approve`) that
already exists and is not touched here. **This phase deliberately stops
there** -- it does not execute the approved task. That is explicitly the
next, separate milestone (Priority C step 3), because it needs its own
durable worker/authority design for the full bounded-agent/one-shot
pipeline, not a natural extension of intake's much narrower scope.

## Decision

### A new, small, mirrored package: `src/apoapsis/intake/`

Structurally, `intake/` mirrors `review/` closely, reusing the exact same
crash-safety discipline ADR 0020/0021 already proved out, rather than
inventing a second one:

- `intake/schema.py`: `IntakeOperationStatus` (`RECORDED`, `RUNNING`,
  `PENDING_SPECIFICATION_APPROVAL`, `FAILED`, `AMBIGUOUS`) and
  `IntakeOperationRecord` (operation id, allocated task id, the exact
  verbatim request text, its sha256 hash, the task version observed at
  creation, the provider role, status, timestamps, and accumulated audit
  artifact locations).
- `intake/store.py`: `IntakeOperationStore`, a SQLite ledger
  (`.apoapsis/intake-operations.db`) with the identical guarantees as
  `review.store.ReviewOperationStore`: a caller-supplied `operation_id` can
  never be resubmitted for an active operation
  (`ActiveIntakeOperationExistsError`) or a terminal one
  (`DuplicateIntakeOperationError`), both checked atomically inside the
  same `BEGIN IMMEDIATE` transaction as the insert; a `RUNNING` operation
  can never be silently re-entered
  (`IntakeOperationAlreadyRunningError`) -- only explicit recovery ever
  moves it forward, into the terminal `AMBIGUOUS` status.
- `intake/execution.py`: `prepare_intake_operation()` /
  `run_intake_operation()` / `execute_intake_operation()`, mirroring
  `review.execution`'s exact three-function shape (fast synchronous
  prepare; the actual work, reloading everything from the durable record;
  a synchronous convenience wrapper for the CLI).
- `intake/recovery.py`: `recover_stale_intake_operations()`, mirroring
  `review.recovery.recover_stale_operations()`.
- `intake/worker.py`: `IntakeWorker`, mirroring `review.worker
  .ReviewWorker` -- a background thread whose queue carries only an
  `operation_id`, running one recovery pass at startup.

None of `workflow/states.py`, `review/`, or `VerticalSliceRunner`'s own
execution path changed. `intake/` is purely additive.

### Deterministic, stable id allocation, before any model call

`prepare_intake_operation()`:

1. Allocates `task_id` with the exact same convention every other
   task-creation path already uses: `f"TASK-{uuid.uuid4().hex[:12].upper()}"`
   (`_task()`/`_run_vertical_slice()` in `cli/app.py`,
   `VerticalSliceRunner.run()`).
2. Creates the task's row immediately, at `INTAKE`, with a preliminary
   `TaskSpecification` whose `objective.text` is the exact, verbatim
   request text -- the same pattern `VerticalSliceRunner.run()` already
   uses for its own preliminary specification. This is the durable,
   canonical home for the exact request text; nothing paraphrases or
   truncates it before this point.
3. Computes the request's sha256 and creates the `IntakeOperationRecord`
   (status `RECORDED`) with the task id, the observed task version, the
   exact request text (redundantly, since the operation record must be
   independently readable without joining against the task store), its
   hash, and the provider role that will draft the specification
   (`ModelRole.FRONTIER_IMPLEMENTATION` -- the same role `DRAFT_SPECIFICATION`
   calls already use).

All of this is synchronous, fast, and deterministic -- no model call, no
worktree, no external process -- so it is exactly as safe to call directly
from an HTTP handler as `prepare_review_operation()` already is.
`operation_id` reuse fails with `ActiveIntakeOperationExistsError`/
`DuplicateIntakeOperationError` from the *second* call's operation-store
insert; the task row the first, successful call already created is left in
place at `INTAKE`, inspectable and harmless (a documented, accepted trade-
off: task creation and operation-record creation are two separate SQLite
databases, so true cross-database atomicity is not available anywhere in
this codebase -- the same is true of `review/`'s worktree mutations versus
its own operation ledger).

### Execution-time recheck, mirroring ADR 0021 exactly

`run_intake_operation()` takes only `operation_id`. It marks the operation
`RUNNING` before anything else -- including provider construction -- so a
bad provider configuration or any other preflight failure reaches a
deterministic terminal status instead of leaving the operation `RECORDED`
forever. It then re-fetches the task and rechecks, immediately before
doing anything else: the task still exists, is still at `INTAKE`, and its
version still matches what was observed at `prepare_intake_operation()`
time. A mismatch raises and is treated as an ordinary infra failure (caught
by the generic handler below, operation marked `FAILED`, exception
re-raised) -- exactly the queue-delay/stale-state discipline ADR 0021 built
for review operations, applied here even though, in the default new-task
flow, nothing else can plausibly race a task_id that was only just
randomly allocated.

### Reusing the specification-extraction machinery exactly, not duplicating it

`run_intake_operation()` calls, unmodified: `SpecificationExtractor.build_
prompt()`/`build_correction_prompt()`/`parse()` -- the same class
`VerticalSliceRunner` uses, given the same `config.verification.commands`
(the deterministic `ACCEPTANCE_COMMAND_CATALOG` construction, ADR 0016) and
subject to the exact same exact-verbatim-substring hard-constraint check
and acceptance-catalog-membership check `parse()` already enforces for
every other extraction call site. On a first-parse failure, it makes
exactly one bounded correction call (mirroring ADR 0018's contract to the
letter: the exact validation errors and the model's own prior response are
embedded, no coercion, no second correction) before letting a second
failure stop the operation deterministically.

The request/context/response/telemetry audit discipline is reproduced by a
small, dedicated `_perform_intake_model_call()` (`intake/execution.py`),
writing the identical `call-<NNN>-context.json` / `call-<NNN>-request.json`
/ `call-<NNN>-response.json` / `call-<NNN>-telemetry.json` shapes
`VerticalSliceRunner._model_call()` writes, using the same
`ModelRequest`/`ModelResponse`/`ConstraintCoverage` schemas and the same
`TaskAuditStore.write_call_package()`/`write_call_result()` methods. This
mirrors -- rather than literally shares a function object with --
`VerticalSliceRunner._model_call()`, following the same established
pattern `review.execution._ContinuationModelCaller` already uses for
continuation model calls outside `VerticalSliceRunner`: the discipline
(audit ordering, file shapes, telemetry-on-failure) is identical and
independently tested; the implementation is a small, purpose-built
mirror, not a shared abstraction reached across module boundaries.
Provider construction (`_build_provider`) is the same small,
per-subsystem dispatch already duplicated in `review/execution.py`,
`cli/app.py`, and `doctor.py` -- adding a fourth copy here follows the
codebase's own existing convention rather than introducing a new one.

On success, `run_intake_operation()` calls the *same*
`SQLiteTaskStore.update_specification()`/`transition()` methods
`VerticalSliceRunner.run()` calls, transitioning `INTAKE -> SPEC_DRAFTED`
(a pre-existing edge; `workflow/states.py` did not change) with a new
`intake_specification_drafted` event, and writes the same
`approved-specification-candidate.json` artifact shape. The operation is
then marked `PENDING_SPECIFICATION_APPROVAL`. **Nothing here approves the
specification or executes anything past this point** -- `apoapsis approve`
/ `ApoapsisUIService.approve_specification()` (unmodified, already covered
by existing tests) is the one and only approval path, exactly as it
already is for tasks created by `apoapsis run`, `apoapsis task`, or now
`apoapsis intake submit`.

### Bounded failure is a deterministic outcome, not a crash

If the one bounded correction attempt also fails to parse, this is treated
exactly as `VerticalSliceRunner._handle_failure()` already treats it: the
task transitions to `FAILED` (via the pre-existing `INTAKE -> FAILED` edge)
with an `intake_extraction_failed` event, and `run_intake_operation()`
returns a normal `IntakeOperationRecord` with status `FAILED` -- it does
**not** raise `SpecificationExtractionError` to its caller. This matters
for the CLI seam: a synchronous `apoapsis intake submit` call should print
a clean, structured failure result (exactly like `apoapsis run` does today
for the same underlying bounded-failure scenario), not an uncaught Python
traceback. A second, generic `except Exception` around the same call marks
the operation `FAILED` and *does* re-raise -- this is the genuinely
unexpected-crash path (a bad provider configuration, an IO error), where
propagating is correct and matches `review.execution.run_review_operation`
's own behavior.

### Explicit crash recovery, reusing review's own abandon path

`recover_stale_intake_operations()` mirrors `review.recovery
.recover_stale_operations()` exactly: a `RECORDED` operation is always
safe to reclaim (nothing was ever transmitted, since `mark_running` is
unconditionally the first action `run_intake_operation()` takes); a
`RUNNING` operation stale beyond `running_expiry` becomes the terminal,
inspectable `AMBIGUOUS` status and is never automatically repeated.

If the stale operation's task is still stuck at `INTAKE` (the crash
happened before the extraction call ever returned, or before its result
was recorded), the task is moved to `HUMAN_REVIEW_REQUIRED` through the
pre-existing `INTAKE -> HUMAN_REVIEW_REQUIRED` edge, with an
`intake_operation_recovery_requires_human` event that makes no claim about
whether the interrupted call succeeded. Because that event type is not in
`review.classify`'s recognized-event table, `classify_stop_reason()`
correctly classifies it as `StopReasonKind.UNKNOWN` (ADR 0021's newest-
event-only rule), offering exactly `inspect_only`/`abandon` -- **this is a
genuine reuse, not a new capability**: an operator can inspect and
`apoapsis review abandon` a stranded intake task through the existing,
completely unmodified review machinery, which already handles a task with
no worktree at all (the same shape `specification_not_approved`/
`deterministic_route_requires_human` stops already have). If the task had
already reached `SPEC_DRAFTED`/`FAILED` before the crash -- only the
operation's own bookkeeping call never completed -- it is left exactly
where it is; no outcome is inferred or retroactively granted to the
operation itself, which still becomes `AMBIGUOUS`.

### CLI/service seam

```
apoapsis intake submit "<natural language request>" --operation-id ID
apoapsis intake inspect <operation-id>
apoapsis intake recover
```

`submit` runs `execute_intake_operation()` synchronously (prepare, then
run, in one call) -- there is no CLI-side worker; a foreground CLI process
blocking until the model responds is the existing, accepted convention
(`apoapsis run`, `apoapsis review continue-local`). This seam works fully
without `apoapsis ui` running, matching the explicit requirement that
intake operations be creatable, inspectable, and recoverable independent
of the UI.

## Tests

New `tests/test_intake.py` covers, all with deterministic fake providers:
successful extraction reaching `SPEC_DRAFTED` with both a clean first
parse and a first-response bounded correction; a double parse failure
stopping deterministically at `FAILED` with a scripted third response
never consumed (the retry ceiling); exact verbatim hard-constraint
preservation and acceptance-catalog rejection surviving into the
correction path unchanged; a caller-supplied `operation_id` resubmission
being rejected once terminal, and a second operation for the same task
being rejected while one is still active; simultaneous operations
targeting one task at the store level; a provider-construction failure
reaching `FAILED` rather than leaving the operation `RECORDED` forever; a
task mutated between `prepare_intake_operation()` and
`run_intake_operation()` being rejected (the queue-delay/stale-state
scenario); all three `recover_stale_intake_operations()` scenarios (a
`RECORDED` operation reclaimed without ever running, a `RUNNING` operation
gone stale while still at `INTAKE` correctly returning the task to
`HUMAN_REVIEW_REQUIRED`, and one gone stale after already reaching
`SPEC_DRAFTED` producing only an `AMBIGUOUS` operation with no further task
transition); telemetry/audit-file ordering (context, then request, then
response, then telemetry, matching `VerticalSliceRunner`'s own ordering);
and that `apoapsis run`/`apoapsis task`/existing `tests/test_vertical_
slice.py`/`tests/test_specification_correction.py` suites are entirely
unaffected (no shared code was modified, only added).

## Non-goals

- Does not execute the approved task. Approval still only reaches
  `SPEC_APPROVED`; nothing here starts routing, context compilation, or a
  coding agent. That is Priority C's next, separately reviewed milestone.
- Does not add a second specification-approval implementation. `apoapsis
  approve` / `ApoapsisUIService.approve_specification()` are reused,
  completely unmodified.
- Does not change `workflow/states.py`, `VerticalSliceRunner`, or any
  existing `review/` behavior.
- Does not add cross-database atomicity between task creation and
  intake-operation creation; the narrow, documented trade-off (an orphaned
  `INTAKE` task on `operation_id` reuse) is accepted rather than solved
  with a more complex two-phase commit.
- Does not add a configurable ceiling or `[intake]` config section --
  there is exactly one bounded correction attempt, matching ADR 0018's
  existing, non-configurable contract precisely.

## Consequences

A user can now type a natural-language request into the local UI, safely
close the tab or lose the connection while extraction (and its one bounded
correction attempt) runs on a background worker, and reconnect later by
polling the same operation id to see the validated candidate specification
-- all without a single line of duplicated specification-extraction,
audit, or crash-recovery logic; every one of those disciplines is reused
exactly as `apoapsis run` and `review/` already implement and test them.
The harness still never lets a model choose a workflow transition, expand
its own budget, or claim completion -- extraction only ever proposes a
candidate; the harness alone validates it, persists it, and requires an
explicit, separate, already-existing approval step before it takes effect.
