# ADR 0021: Review/resume integrity hardening

- Status: Accepted
- Date: 2026-07-19

## Context

A focused review of ADR 0020's implementation (Commits C1/C2) found real
integrity gaps between what the deterministic authority boundary was
*documented* to guarantee and what the code actually enforced. None of these
change the design ADR 0020 describes; all of them are corrections to its
implementation. Specifically:

1. **Stale in-memory state crossed the submission/execution boundary.**
   `ReviewWorker`'s queue carried a fully-built `ReviewCase` (and a
   separately-threaded `budget`) captured at *submission* time.
   `run_review_operation` used that object directly with no re-check at
   *execution* time. Between a browser click and the worker actually
   dequeuing the job -- an unbounded delay under load -- the worktree could
   change, the task could move, or a concurrent operation could exist, and
   none of it would be noticed.
2. **The operation record was not self-sufficient.** `ReviewOperationRecord`
   never persisted `expected_worktree_fingerprint`, so a worker restart (or
   any caller reloading only the record) could not reconstruct what the
   operation was actually authorized against.
3. **No limit on concurrent operations per task.** Two browser tabs, or a
   retried request racing a still-queued original, could both create
   distinct `operation_id`s for the same task -- `DuplicateOperationError`
   only ever caught a literal `operation_id` collision, never two different
   operations targeting the same task at once.
4. **Provider construction happened before `mark_running`.** `ReviewWorker
   ._execute` built `local_coder_provider`/`frontier_coder_provider` from
   configuration *before* calling `run_review_operation` (which is where
   `mark_running` lived). A bad provider configuration raised before the
   operation ever left `RECORDED` -- permanently, since nothing ever moved
   it to a terminal status.
5. **No recovery path existed at all.** `OperationAlreadyRunningError`
   correctly refused to silently re-enter a stuck `RUNNING` operation, but
   nothing could ever move it anywhere else either. A task that crashed
   mid-continuation (moved to `IMPLEMENTING`, never reached `COMPLETE` or
   `HUMAN_REVIEW_REQUIRED`) was permanently unreachable by every review
   command, which require `HUMAN_REVIEW_REQUIRED`.
6. **`_execute_abandon` deleted the worktree before checking the version.**
   A stale abandon request would destroy the worktree and only then
   discover, via the version-checked `task_store.transition`, that it
   shouldn't have run at all.
7. **`classify_stop_reason` could silently use stale history.** It scanned
   newest-to-oldest for the first *recognized* `HUMAN_REVIEW_REQUIRED`
   event, skipping over an unrecognized newest one to find an older,
   recognized one -- misclassifying the task's actual current stop reason.
8. **`ReviewCase.current_diff` used a plain `git diff`.** Untracked text
   files (a normal byproduct of an applied patch, per ADR 0017) were
   invisible to a reviewer, even though the same worktree fingerprint
   `ReviewCase` shows is already sensitive to them.
9. **Verification/acceptance evidence never advanced past the original
   stop.** `report.json` is written once, at the first stop; `ReviewCase`
   read only that snapshot forever, so a `verification_only_retry` or
   continuation that produced completely new results left the UI/CLI
   showing stale, sometimes-empty original evidence.
10. **Verification-retry artifacts were not tied to their operation.** They
    were named by a locally-scanned sequence counter, not the operation
    that produced them, making them hard to correlate after the fact.

Nothing here changes `workflow/states.py`; every fix below uses transition
edges that already existed.

## Decision

### 1. The operation record is now fully self-sufficient

`ReviewOperationRecord` gained `expected_worktree_fingerprint`. The
`review_operations` table gained the matching column. `ReviewWorker`'s
queue now carries only an `operation_id` (`str`); `run_review_operation`
reloads the task id, action, expected version, expected fingerprint, and
authorized budget entirely from the durable record. Nothing is ever passed
from submission time to execution time except that one id.

### 2. Every precondition is rechecked immediately before doing anything

A single helper, `_validate_operation_preconditions`, is now called twice:
once by `prepare_review_operation` (against caller-supplied expectations,
before recording), and once by `run_review_operation` (against the
*recorded* expectations, against a *freshly re-projected* `ReviewCase`,
immediately before dispatching to any action handler). The check covers
task version, action eligibility, worktree fingerprint, and continuation
budget/ceilings, in that order, and raises on the first mismatch --
`run_review_operation` marks the operation `FAILED` with a clear message
rather than executing anything.

### 3. Only one active operation per task

`ReviewOperationStore.create()` now checks -- inside the same `BEGIN
IMMEDIATE` transaction as the insert, closing the check-then-act race --
whether the task already has a `RECORDED` or `RUNNING` operation, raising
the new `ActiveOperationExistsError` if so. `DuplicateOperationError` is
now reserved specifically for reusing an `operation_id` whose row already
exists in a *terminal* status (`SUCCEEDED`/`FAILED`/`AMBIGUOUS`); a literal
resubmission of the same id for a still-active task is caught by the
active-operation check first, since the row is already `RECORDED`.

### 4. `RUNNING` is entered before any potentially failing setup

`run_review_operation` calls `operation_store.mark_running(operation_id)`
as its very first action -- before re-projecting the `ReviewCase`, before
building a model provider, before anything that can raise. Provider
construction (`_build_provider`, moved from `worker.py` into
`execution.py`) now happens inside `run_review_operation`, after the
precondition recheck, immediately before dispatch. Every exception from
that point on is caught by one `try`/`except` that marks the operation
`FAILED` before re-raising -- an operation can no longer be left `RECORDED`
forever by a setup failure.

### 5. Explicit crash recovery (`review/recovery.py`)

`recover_stale_operations(task_store, operation_store, *, running_expiry)`:

- **`RECORDED` operations are always safe to reclaim.** By construction
  (point 4), nothing is ever transmitted before `mark_running`, so a
  `RECORDED` row found during a scan -- whether because the process
  restarted and lost its in-memory queue, or it simply hasn't been
  dequeued yet -- is reported as reclaimable. `ReviewWorker` runs this scan
  once at startup and re-submits every reclaimed id to its own queue.
- **`RUNNING` operations older than `running_expiry` become `AMBIGUOUS`.**
  A new terminal `ReviewOperationStatus.AMBIGUOUS` value means "we do not
  know whether a model call was transmitted before the owning process
  died" -- never automatically repeated, but no longer stuck forever
  either. Freshly-`RUNNING` operations within the expiry window are left
  alone; they might still be legitimately in progress.
- **A task left outside `HUMAN_REVIEW_REQUIRED` by an ambiguous operation
  is returned there** through whatever permitted transition edge already
  exists from its current state (all of them already did), with a new
  `review_operation_recovery_requires_human` event whose payload names the
  operation and the state it was recovered from -- explicitly making no
  claim about whether the interrupted call succeeded. A task already at a
  terminal state, or already back at `HUMAN_REVIEW_REQUIRED`, is left
  alone.

`apoapsis review recover` runs this explicitly from the CLI, for operators
who never start `apoapsis ui` (where `ReviewWorker`'s own startup scan
would otherwise be the only trigger).

### 6. Abandon transitions before it deletes

`_execute_abandon` now calls the version-checked `task_store.transition(...,
WorkflowState.ROLLED_BACK, ...)` **first**, and only cleans up the worktree
afterward, if that succeeds. A stale abandon request fails its version
check and the worktree is never touched. (If the transition succeeds but
the subsequent worktree cleanup itself fails -- a separate, much rarer
failure -- the task is left correctly `ROLLED_BACK` with a leftover
worktree, a recoverable state, not a corrupted one.)

### 7. Stop classification decides on the newest event alone

`classify_stop_reason` now returns `(StopReasonKind, WorkflowEvent | None)`
and returns as soon as it finds the newest `HUMAN_REVIEW_REQUIRED`
transition, whether or not its `event_type` is recognized -- an
unrecognized newest event now classifies as `StopReasonKind.UNKNOWN`
(only `inspect_only`/`abandon` eligible) rather than silently falling back
to an older, recognized stop reason from earlier in the task's history.

### 8. Shared bounded diff/inspection machinery

`ReviewCase.current_diff` is now built from `RepositoryInspector.diff()`
(the same machinery `agent/inspection.py` and the bounded agent loop
already use), not a plain `git diff`. Permitted untracked text files now
appear with full content; untracked binary files and symlinks appear as
the same path-only, bytes-never-rendered placeholders the rest of the
codebase already uses (ADR 0017) -- consistent with what the worktree
fingerprint shown alongside the diff is already sensitive to.

### 9. Fresh verification/acceptance evidence

`build_review_case` now selects which evidence source is authoritative
using the *same* newest-event classification `classify_stop_reason` already
computed, so the choice is never independent of (or inconsistent with)
`stop_reason_kind`/`stop_reason_text`:

- A verification-retry stop (`review_verification_retry_incomplete`/
  `_failed`) reads the specific `review-verification-retry-<operation_id>
  .json` artifact the retry that produced this exact stop wrote, and the
  acceptance coverage embedded in that same event's payload.
- A local/frontier continuation stop reads that session's own
  `verification_results`/`acceptance_coverage` (already the full,
  cumulative history across every continuation of that session).
- Anything else (the original, never-continued stop) still reads the
  original `report.json` snapshot, which is accurate for a stop that has
  never been acted on.

### 10. Verification-retry artifacts are tied to their operation id

`review-verification-retry-<operation_id>.json` replaces the old
`review-verification-retry-<sequence>.json` naming; every
`review_verification_retry_*` event payload now also carries the
`operation_id` that produced it, so `build_review_case` can locate the
exact artifact deterministically rather than scanning and guessing at
"latest by sequence number."

## Corrections to ADR 0020 and HANDOFF.md

ADR 0020's own text was an accurate description of the *intended* design;
the gaps above were implementation bugs, not misrepresentations, so ADR
0020 is not rewritten -- an errata note there points here. HANDOFF.md's
Commit C2 description previously stated a worker crash "is left `RUNNING`
in the durable store exactly like a CLI crash would leave it -- the same
documented fail-closed behavior, not a new failure mode," which was true
narrowly (nothing *repeated* it) but overstated what a caller could
actually *do* about it -- there was no recovery path, and "start a
genuinely new operation" was not actually possible while the stuck
operation silently blocked nothing (no per-task limit existed yet) or,
after this ADR, would have been correctly blocked with no way to clear it.
HANDOFF.md is corrected directly (a living document, not decision history)
to describe the recovery path this ADR adds.

## Tests

New `tests/test_review_hardening.py` covers: a worktree change between
`prepare_review_operation` and `run_review_operation` (queue-delay
simulation) being rejected; a task-version change in the same window being
rejected; the fixed abandon ordering (worktree never deleted when the
version check fails); two operations for the same task being rejected;
`_build_provider` raising after `mark_running` still reaching `FAILED`; an
unrecognized newest stop event producing `UNKNOWN` with only
`inspect_only`/`abandon` eligible even when an older event is recognized;
an untracked text file appearing in `current_diff`; a verification retry's
results/coverage overriding a stale (here, empty) original report snapshot;
and three `recover_stale_operations` scenarios (a `RECORDED` operation
reclaimed without ever running, a `RUNNING` operation stale before any
workflow transition producing only an `AMBIGUOUS` operation with no task
transition, and a `RUNNING` operation stale after a workflow transition
correctly returning the task to `HUMAN_REVIEW_REQUIRED`). Existing
`tests/test_review.py`/`test_review_execution.py`/`test_review_ui.py` were
updated for the new `classify_stop_reason` signature and the
active-operation-per-task/duplicate-operation distinction; all continue to
pass.

## Non-goals

- Does not add a periodic background recovery sweep -- recovery runs at
  `ReviewWorker` startup and via the explicit `apoapsis review recover`
  CLI command only, not on a timer.
- Does not persist or expose a configurable `running_expiry` in
  `.apoapsis/config.toml`; it is a parameter of
  `recover_stale_operations()` with a sensible default, not yet a product
  setting.
- Does not change `workflow/states.py`, one-shot mode's execution path, or
  the five stop-reason scenarios ADR 0020 identified.

## Consequences

A review operation's authorization is now checked against live state
immediately before it does anything irreversible, not just at submission
time -- closing a real gap between a fast, synchronous accept and a
possibly much later, asynchronous execution. A crashed worker process no
longer leaves operations or tasks permanently stuck: `RECORDED` work is
safely retried, `RUNNING` work that went stale is marked ambiguous and
surfaced for review, and a task stranded outside `HUMAN_REVIEW_REQUIRED` is
returned there without ever claiming what actually happened to the
interrupted call. Reviewers now see the same untracked-file-aware diff the
agent loop itself sees, and the freshest verification/acceptance evidence
available, not a stale first snapshot.
