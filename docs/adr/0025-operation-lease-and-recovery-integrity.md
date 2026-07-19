# ADR 0025: Operation lease and recovery integrity

- Status: Accepted
- Date: 2026-07-19

## Context

ADR 0020/0021 (review), ADR 0023 (intake), and ADR 0024 (execution) each
built a durable, crash-safe operation ledger with the same shape:
`RECORDED -> RUNNING -> {terminal} | AMBIGUOUS (recovery only)`. Auditing
all three against real crash scenarios surfaced two weaknesses shared by
all of them, not specific to any one subsystem:

1. **The duplicate-enqueue / never-recovered window.** A process can durably
   record an operation (`RECORDED`) and then crash before it is ever
   enqueued to a worker. Recovery already detected this correctly
   (`report.reclaimed_operation_ids`), but nothing actually *ran* the
   reclaimed operation until an unrelated new submission happened to
   lazily construct that operation type's worker for the first time --
   and that same lazy-construction path had its own bug: preparing a
   `RECORDED` row and then, on the very first submission, constructing a
   worker whose constructor immediately runs its own startup recovery
   scan (which would discover the row just prepared and enqueue it) right
   before the caller's own explicit `.submit(operation_id)` call enqueued
   it a second time.
2. **`RUNNING` had no notion of a live owner, only a last-write timestamp.**
   Recovery classified a `RUNNING` operation as crashed purely by how long
   it had been running (`updated_at` plus a fixed `running_expiry`
   window, e.g. 15 minutes). A real agent session or one-shot repair
   attempt that legitimately runs longer than that window looks
   indistinguishable from a crashed one -- recovery would move a perfectly
   healthy operation to `AMBIGUOUS`, discarding real progress. There was
   also no mechanism preventing a *different* process from also believing
   it owned the same operation.

Both problems have the same root cause: `RUNNING` was a status, not a
claim. This ADR gives all three operation ledgers one coherent, shared
lease/recovery discipline that fixes both, rather than three independently
patched half-fixes.

## Decision

### A shared, table-name-parameterized lease module

`src/apoapsis/operations/lease.py` is new, genuinely shared code (a
deliberate, explicitly-scoped exception to this codebase's usual
review/intake/execution mirroring convention, because a subtle
concurrency bug in a lease implementation has the same shape and the same
consequences in all three subsystems -- getting it right once and reusing
it is safer than maintaining three independently-drifting copies). Every
function operates on a caller-supplied table name and an already-open
`sqlite3.Connection`; each store keeps owning its own transaction
boundaries exactly as before.

- `claim_lease()`: a single atomic `UPDATE ... WHERE operation_id = ? AND
  status = ?` that transitions `RECORDED -> RUNNING` while writing a fresh
  `lease_owner_id` (a random `LEASE-<uuid4>` string, never reused across
  attempts) and `lease_expires_at` deadline in the same statement. Returns
  whether exactly one row matched -- never a separate read-then-write, so
  two callers racing to claim the same row can never both succeed.
- `renew_lease()`: extends the deadline, gated on `status = running AND
  lease_owner_id = <caller's>`. Succeeds even if the previous deadline has
  technically already passed, as long as nobody else has acted on the row
  yet -- deliberately, so a heartbeat tick that fires a little late does
  not cost a healthy operation its lease.
- `release_lease()`: the only way to reach a terminal status from
  `RUNNING`, gated the same way -- only the owning lease may mark success
  or failure.
- `expire_lease_to_ambiguous()`: the only way recovery may move a row to
  `AMBIGUOUS`, gated on `status = running AND (lease_expires_at IS NULL OR
  lease_expires_at < now)` -- checked in the same statement as the write,
  so there is no race between "recovery decided this looks stale" and
  "the owner renews it a moment later." A row with `NULL` lease columns
  (a legacy row written before this migration) is unconditionally treated
  as expired -- fail closed, since there is no signal at all to prove such
  a row's owner is still alive.
- `ensure_lease_columns()`: an additive migration (`PRAGMA table_info`
  then `ALTER TABLE ... ADD COLUMN` only for columns not already present),
  safe to call on every store construction against both a brand-new and a
  pre-existing database. Existing terminal records remain fully readable;
  existing `RUNNING` records simply gain `NULL` lease columns.
- `LeaseHeartbeat`: a daemon-thread ticker that calls a caller-supplied
  `renew` callable on a fixed wall-clock interval, independent of how long
  the underlying model call or agent turn actually takes. If a renewal
  ever returns `False` (or raises), it flags `lease_lost` and stops --
  the caller's `finally` block always stops the heartbeat regardless of
  outcome.

### Every store adopts the same lease-gated method shape

`ReviewOperationRecord`/`IntakeOperationRecord`/`ExecutionOperationRecord`
each gained `lease_owner_id: str | None` and `lease_expires_at: datetime |
None`. Each store's `mark_running()` now requires `owner_id` and claims
the lease; its success/failure-marking methods (`mark_succeeded`,
`mark_failed`, `mark_pending_approval`) now require the same `owner_id`
and release the lease atomically -- raising `LeaseLostError` if the row is
no longer owned by that lease (recovery already won, or, defensively, a
different owner somehow holds it). A row still at `RECORDED` (never
claimed by anyone) instead raises the store's own domain error
(`ReviewError`/`IntakeError`/`ExecutionOperationError`) -- an invalid
lifecycle transition, not a lease race, so callers can distinguish "you
called this out of order" from "you lost a real race."

Recovery (`recover_stale_operations` / `recover_stale_intake_operations` /
`recover_stale_execution_operations`) no longer takes a `running_expiry:
timedelta`; it takes an injectable `now: datetime | None = None` and reads
each `RUNNING` record's own `lease_expires_at` directly, skipping the
(now rare) SQL call entirely for a record whose lease has not expired,
and calling `mark_ambiguous(..., now=moment)` -- which itself re-checks
expiry atomically -- for the rest. `mark_ambiguous()`'s only caller is
recovery; it is never part of ordinary execution.

Each of the three `run_*_operation()` functions
(`review.execution.run_review_operation`, `intake.execution.run_intake_
operation`, `execution.operation_service.run_execution_operation`) now:
generates a fresh `owner_id = new_owner_id()`; claims the lease via
`mark_running(operation_id, owner_id=owner_id)`; constructs and starts a
`LeaseHeartbeat` wrapping `store.renew_lease(operation_id, owner_id=
owner_id)`; wraps its existing body in `try/finally: heartbeat.stop()`
(intake's nested bounded-correction-retry body required re-indenting one
level deeper to fit inside this wrapper, without changing its retry
semantics at all); and passes `owner_id` to every terminal
`mark_succeeded`/`mark_failed`/`mark_pending_approval` call.

### Eager background-worker startup closes both the recovery-never-runs gap and the duplicate-enqueue race

`ApoapsisUIService.start_background_workers()` is new: it eagerly
constructs all three operation workers (`_worker()`, `_intake_worker_
instance()`, `_execution_worker_instance()`), each of which runs its own
`_recover_at_startup()` pass inside its constructor. `create_ui_server()`
now calls this once, immediately after constructing the service, before
returning the HTTP server -- so a stranded `RECORDED` operation from a
crashed previous process is reclaimed and queued the moment `apoapsis ui`
starts, never only when an unrelated new submission happens to lazily
construct that worker for the first time.

This structurally closes the duplicate-enqueue window too: by the time
any `submit_*_operation()` call ever runs, the relevant worker already
exists (constructed by `start_background_workers()`), so `_worker_
instance()` just returns the cached instance without re-running
`_recover_at_startup()` -- there is no longer a "prepare the record, then
lazily construct a worker that also discovers it" race, because
construction always happens before any record exists to discover. Each
worker is still constructed at most once (`self._execution_worker is
None` guard, unchanged), and `start_background_workers()` is idempotent
and safe to call more than once.

### `--resume-recorded`: recovering data is not the same as authorizing a model to run

`apoapsis review/intake/execute recover` remained report-only by default
(the existing, unmodified behavior) -- listing reclaimed/ambiguous
operation ids and tasks returned to review is a read, not an action, and
should never silently trigger model calls. A new explicit
`--resume-recorded` flag makes running recovered work an opt-in,
foreground CLI action: for every reclaimed `RECORDED` operation id, the
CLI process itself calls the corresponding `run_*_operation()`
synchronously and collects the results into `result["resumed"]`. This
mirrors `apoapsis execute start`'s existing convention of running
synchronously in the CLI process. The background workers reclaim and
queue `RECORDED` operations on their own at startup (per the previous
section) regardless of this flag -- the flag only governs the CLI's own
`recover` command, which a human might run against a project whose UI
server is not currently running.

## Tests

New `tests/test_operation_lease.py` (22 tests):

- `LeaseModuleTests`: the shared primitives tested directly against a
  minimal ad hoc table -- only one of two racing claims wins; renewal
  succeeds only for the owning owner and succeeds even past a technically
  expired deadline when uncontested; release succeeds only for the owning
  owner; `expire_lease_to_ambiguous` fails before genuine expiry and
  succeeds after; a legacy row with `NULL` lease columns is
  unconditionally expired regardless of `now`; `ensure_lease_columns` is
  additive and idempotent.
- `LeaseHeartbeatTests`: a heartbeat renews repeatedly on its own short
  injected interval; it flags `lease_lost` and stops when its `renew`
  callable returns `False` or raises -- using millisecond intervals, never
  sleeping for a real lease duration.
- `ReviewLeaseSemanticsTests` / `IntakeLeaseSemanticsTests` /
  `ExecutionLeaseSemanticsTests` (one shared base class, parametrized per
  store, proving all three use identical semantics): a long healthy
  operation, renewed every few simulated minutes via an injected `now`,
  survives recovery checks that sail past several former 15-minute
  boundaries and remains `RUNNING`; a lease that stops being renewed is
  correctly marked `AMBIGUOUS` once genuinely expired; the original owner
  is rejected with `LeaseLostError` if it tries to report a result after
  recovery has already won; a second claim attempt on an already-claimed
  `RECORDED` operation is rejected and never touches the first claim's
  lease.

`tests/test_execution_ui.py` gained
`test_start_background_workers_reclaims_stranded_recorded_operation`,
proving a `RECORDED` operation prepared directly (simulating a crash
before any worker ever saw it) is picked up and run to completion purely
by `start_background_workers()`, with no call to `submit_execution_
operation()` at all.

`tests/test_intake_cli.py` gained two tests for the new flag: `intake
recover` without `--resume-recorded` reports the reclaimed id but leaves
the operation exactly at `RECORDED`, untouched; with `--resume-recorded`
(and a patched fake provider) the same operation is actually run to
`pending_specification_approval` and appears in `result["resumed"]`.

Five pre-existing test files needed mechanical updates for the new
required `owner_id` keyword argument and the `now=`-based recovery
signature (`test_review.py`, `test_review_hardening.py`, `test_review_
execution.py`, `test_intake.py`, `test_intake_ui.py`, `test_execution_
operations.py`, `test_execution_ui.py`). Two tests that previously poked
a since-removed private `_transition()` method to simulate an
out-of-band status write now use a new, explicit test helper,
`tests.helpers.force_operation_status()`, that does the same thing via a
direct SQL `UPDATE` -- clearly a test-only mechanism, not a store method.

Full suite: 478 tests, 0 failures, 6 intentional skips.
`python -m compileall -q src tests` and `git diff --check` both clean.

## Non-goals

- Does not change any workflow state machine transitions
  (`workflow/states.py` untouched).
- Does not add a new operation type or change what `RECORDED`/`RUNNING`/
  terminal statuses mean for any of the three subsystems -- only how
  `RUNNING` is owned and how staleness is detected.
- Does not wire lease renewal into `apoapsis run`'s synchronous, one-shot
  CLI path -- that path has no background worker or crash-recovery
  concept to begin with and is unaffected.
- Does not change `apoapsis execute start`'s existing synchronous
  behavior, only how `--resume-recorded` reuses the same underlying
  `run_*_operation()` functions.

## Consequences

Review, intake, and execution operations now share one lease/recovery
discipline instead of three copies of the same idea at different levels
of correctness. A `RUNNING` operation is a live claim with a real owner
and a renewed deadline, not a timestamp guess -- a long-but-healthy agent
session survives arbitrarily many former staleness windows as long as its
own process is alive, while a genuinely crashed process's lease expires
and is reclaimed exactly once, atomically, with the original owner locked
out of ever overwriting the resulting `AMBIGUOUS` status. A stranded
`RECORDED` operation from a crashed process is reclaimed and actually run
the moment the UI server starts, with no unrelated second task required
to notice it, and the CLI's own `recover` command can now optionally do
the same explicitly and synchronously via `--resume-recorded` without
ever doing so silently.
