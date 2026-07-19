# ADR 0024: Durable post-approval task execution

- Status: Accepted
- Date: 2026-07-19

## Context

ADR 0023 closed half of the "type an idea into the app" gap: a user can
now durably draft and approve a specification from the browser. But
nothing yet turns an approved specification into a running task from the
app -- `VerticalSliceRunner.run()` is the only code path that drives a
task from `SPEC_APPROVED` through completion, and it is a single, long,
synchronous method that blocks the calling process until the task
finishes. That is fine for a foreground CLI process; it cannot be called
from an HTTP request handler, which must return immediately.

## Inventory: `VerticalSliceRunner.run()`'s phases

Before refactoring anything, the exact phases `run()` drives were
enumerated directly from the code (`src/apoapsis/workflow/vertical_slice.py`):

1. Allocate `task_id`, create the task row at `INTAKE` with a preliminary
   specification, create the audit directory.
2. Draft a specification (one model call, one bounded correction attempt
   per ADR 0018), validate it, transition `INTAKE -> SPEC_DRAFTED`.
3. Call the caller-supplied `approve()` callback. Reject ->
   `HUMAN_REVIEW_REQUIRED`. Accept -> transition `SPEC_DRAFTED ->
   SPEC_APPROVED`, write `approved-specification.json`.
4. **Everything from here on is what this ADR extracts:** optional
   Research Mode -> `REPOSITORY_ANALYZED`; context compilation ->
   `CONTEXT_COMPILED`; deterministic routing (`select_agent_route`) ->
   `ROUTED`, or straight to `HUMAN_REVIEW_REQUIRED` if the route requires
   a human; worktree creation -> `IMPLEMENTING`; the selected coding stage
   (one-shot unified-diff + one repair, or the bounded local/frontier
   agent with escalation) -> `PATCH_READY` -> `VERIFYING` ->
   `COMPLETE`/`HUMAN_REVIEW_REQUIRED`/`FAILED`; `_finalize_report()` writes
   `report.json`.

Phases 1-3 are exactly ADR 0023's intake operation and are untouched.
Phase 4 -- research through reporting -- is the "post-SPEC_APPROVED
execution spine" this ADR makes durable and resumable, reused rather than
reimplemented.

## Decision

### Extraction: one shared continuation, two entry points, zero duplication

`VerticalSliceRunner.run()`'s body is split at the `SPEC_APPROVED`
transition into a new private method, `_run_from_approved(task_id,
specification, *, approved_version)`, containing phase 4 verbatim (moved,
not rewritten -- the only substantive change was recomputing the
repository HEAD locally, since the original code read a `head` local
variable captured earlier in the same method before the split; this is
byte-identical in `run()`'s case, since nothing changes the repository
between drafting and this point in a normal synchronous call).

`run()` now ends with `return self._run_from_approved(task_id,
specification, approved_version=approved.version)` immediately after
writing `approved-specification.json`. Actually -- `_run_from_approved`
writes that file itself, as its first action, so it is genuinely shared
rather than duplicated between the two call sites.

A new public method, `execute_approved_task(task_id: str) ->
FinalTaskReport`, is the entry point the new durable execution service
uses: it loads the task from the store, requires it to already be at
`SPEC_APPROVED` (raising otherwise), and calls the exact same
`_run_from_approved`. No routing, context, worktree, agent, patch,
verification, escalation, or reporting logic exists in two places.
`workflow/states.py` did not change; every transition `_run_from_approved`
drives already existed.

**Regression proof:** the full existing deterministic suite (418 tests
before this change, spanning `test_vertical_slice.py`,
`test_specification_correction.py`, `test_agent_loop.py`,
`test_evaluation.py`, `test_acceptance_coverage.py`,
`test_context_measurement_integration.py`, and every other suite) passes
unchanged against the refactored code, confirming `apoapsis run` and
one-shot mode are byte-for-byte behavior-preserved.

### A new, small, mirrored package: `src/apoapsis/execution/operation_*`

Structurally, the new modules mirror `review/` and `intake/` closely,
reusing the exact same crash-safety discipline rather than inventing a
third one (added under the existing `apoapsis.execution` namespace,
alongside `worktree.py`/`backend.py`, since these are also about
executing a task -- not a new top-level package):

- `execution/operation_schema.py`: `ExecutionOperationStatus`
  (`RECORDED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `AMBIGUOUS`) and
  `ExecutionOperationRecord` (operation id, task id, the task version and
  repository HEAD observed at preparation time, status, timestamps,
  result summary, error, and the resulting `report.json` path once
  known).
- `execution/operation_store.py`: `ExecutionOperationStore`, a SQLite
  ledger (`.apoapsis/execution-operations.db`) with the identical
  guarantees as `ReviewOperationStore`/`IntakeOperationStore`: a
  caller-supplied `operation_id` can never be resubmitted for an active
  operation (`ActiveExecutionOperationExistsError`) or a terminal one
  (`DuplicateExecutionOperationError`), both checked atomically inside
  the same `BEGIN IMMEDIATE` transaction as the insert; a `RUNNING`
  operation can never be silently re-entered
  (`ExecutionOperationAlreadyRunningError`).
- `execution/operation_service.py`: `prepare_execution_operation()` /
  `run_execution_operation()` / `execute_execution_operation()`, mirroring
  `review.execution`'s/`intake.execution`'s three-function shape.
- `execution/operation_recovery.py`: `recover_stale_execution_operations()`.
- `execution/operation_worker.py`: `ExecutionWorker`.

### `SUCCEEDED` means the operation concluded deterministically, not that the task completed

Unlike intake (which only ever has one "good" outcome, `SPEC_DRAFTED`),
task execution has several legitimate, deterministic outcomes:
`COMPLETE`, `FAILED` (e.g. the one-shot repair budget exhausted, or a
bounded specification issue), and `HUMAN_REVIEW_REQUIRED` (routing
requires a human, escalation exhausted, acceptance coverage incomplete).
Mirroring `review.execution`'s own convention (`_execute_authorize_
frontier_stage` marks its operation `SUCCEEDED` even when the frontier
stage stopped rather than completed), `run_execution_operation()` marks
the *operation* `SUCCEEDED` as soon as `execute_approved_task()` returns
any `FinalTaskReport` at all, regardless of `report.outcome` -- the task's
own outcome is read from the task record/`report.json`, not the operation
status. Only an operation-level exception (raised *before or instead of*
`execute_approved_task()` returning -- a stale precondition, a provider
construction failure, or any other crash) marks the *operation* `FAILED`.

### Preparation: version, repository HEAD, and the eligibility gate

`prepare_execution_operation()` requires: the task exists and is at
`SPEC_APPROVED`; its version matches the caller-supplied
`expected_version` exactly (`StaleExecutionStartError` otherwise -- the
same optimistic-concurrency discipline every other store in this codebase
already uses). It then captures the current repository HEAD
(`git rev-parse HEAD`, the same value `WorktreeManager.create()` already
uses as its base ref) and durably records the operation, including that
HEAD, before anything else runs.

`run_execution_operation()` takes only `operation_id`, marks it `RUNNING`
before provider construction (mirroring review/intake's "mark running
before anything that can fail" discipline exactly), then re-fetches the
task and rechecks its state, version, *and* the current repository HEAD
against what was recorded at preparation time -- a parent-repository
commit landing between approval and a queued operation's actual start
(the literal "worktree-creation crash window" scenario) is caught here,
before `WorktreeManager.create()` is ever called, rather than silently
building a worktree against an unexpected base.

### Provider construction and Research Mode scope

Providers are built the same small, per-subsystem way `cli/app.py`'s
`_build_agent_providers()` and `review/execution.py`'s `_build_provider()`
already do (a fourth small copy, following this codebase's own existing
convention rather than introducing a new one) -- `config.models.frontier`
for the one-shot/specification role, `config.models.local_coder` (falling
back to `frontier`) for the local coding agent, and
`config.models.frontier_coder` (optional) for the frontier coding agent.
Full local/frontier routing, budgets, tool actions, escalation packages,
verification, strict acceptance coverage, telemetry, and final reports
are all preserved exactly, because they are the same, unmodified
`_run_from_approved` code.

**Non-goal, explicitly scoped out:** Research Mode is not wired into the
durable execution service in this phase. A task started through
`run_execution_operation()` always runs with `research_engine=None` /
`ResearchMode.OFF`, regardless of `config.research.default_mode`. This is
a disclosed, narrow scope limit -- Research Mode's fetch-process lifecycle
needs its own careful integration into a background-worker context, which
is not required by this milestone. `apoapsis run` remains the only path
that exercises Research Mode.

### Explicit crash recovery, reusing review's own abandon path

`recover_stale_execution_operations()` mirrors the review/intake recovery
functions exactly. A stale `RUNNING` operation always becomes `AMBIGUOUS`
and is never automatically repeated. Critically, **the task's worktree
(if one was created) is never touched by recovery** -- only an explicit,
separate `abandon` action ever cleans one up, and that action already
exists, unmodified, in `review/execution.py`. Every state between
`SPEC_APPROVED` and a terminal state already has a permitted
`HUMAN_REVIEW_REQUIRED` transition edge (`workflow/states.py`, unchanged),
so recovery can return a task stuck in *any* execution phase there,
through a new `execution_operation_recovery_requires_human` event that
`review.classify` does not recognize (correctly classifying as `UNKNOWN`,
offering `inspect_only`/`abandon` only) -- an operator can then inspect
the exact diff/worktree the crash left behind and abandon it through the
existing, completely unmodified review machinery.

### CLI/service seam

```
apoapsis execute start <task-id> --expected-version N --operation-id ID
apoapsis execute inspect <operation-id>
apoapsis execute recover
```

`start` runs `execute_execution_operation()` synchronously (prepare, then
run, in one call) -- a foreground CLI process blocking until the task
finishes is the existing, accepted convention (`apoapsis run`).

## Tests

New `tests/test_execution_operations.py` covers, all with deterministic
fake providers: a full local-agent completion and a one-shot completion
both reached through `execute_approved_task()`; local-then-frontier
escalation; a route that requires human review before any worktree
exists; a bounded local agent exhausting its budget with no frontier
configured; every replay/stale scenario
(`ActiveExecutionOperationExistsError`, `DuplicateExecutionOperationError`,
a stale task version, a repository HEAD that changed between prepare and
run); a provider-construction failure reaching `FAILED` rather than
leaving the operation `RECORDED` forever; all three
`recover_stale_execution_operations()` scenarios (a `RECORDED` operation
reclaimed, a `RUNNING` operation stale while still mid-execution correctly
returning the task to `HUMAN_REVIEW_REQUIRED` with its worktree left
in place, and one stale after already reaching a terminal state producing
only an `AMBIGUOUS` operation with no further task transition); and that
`apoapsis run`'s and one-shot mode's own existing suites are entirely
unaffected (no shared code was modified beyond the mechanical extraction,
only added).

## Non-goals

- Does not add plan-slice execution -- that is Phase D3's own, separately
  reviewed milestone.
- Does not wire Research Mode into the durable execution service (see
  above).
- Does not automatically commit, merge, or clean up a successful
  worktree -- unchanged from today's behavior.
- Does not change `workflow/states.py`, one-shot mode's own execution
  path, or any existing `review/`/`intake/` behavior.

## Consequences

An already-approved task can now be started as a durable, crash-safe
operation instead of only through a blocking `apoapsis run` process --
the operation is recorded before anything happens, marked running before
any provider call or worktree mutation, and rechecked fresh against the
task's actual current state and the repository's actual current HEAD
immediately before doing anything irreversible. A crashed process no
longer leaves an execution operation ambiguous forever: it is marked
`AMBIGUOUS` and the task is returned to `HUMAN_REVIEW_REQUIRED` with its
worktree exactly as the crash left it, inspectable and abandonable
through the existing review machinery. Nothing about local/frontier
routing, budgets, escalation, verification, or reporting changed -- it is
the same, single, well-tested implementation `apoapsis run` always used.

## Commit D2b: the control-room UI

`ApoapsisUIService.task_detail()` now returns three additional, read-only
fields, computed entirely from persisted facts or deterministic,
side-effect-free config reads -- never a model call, never invented by
browser code:

- `execution_preview`: computed with the exact same `select_agent_route()`
  the real execution service uses (not a separate guess), showing the
  predicted route, execution mode, completion policy, sandbox
  (`verification.backend`), configured verification commands, local/
  frontier models, and local/frontier turn-patch-verification budgets --
  everything the two-step "Start coding" confirmation needs, before
  anything runs.
- `active_execution_operation`: `ExecutionOperationStore.find_active_for
  _task()` for this task, if any. This is the primary reconnect mechanism
  -- unlike the review/intake operation panels (which rely on a
  `sessionStorage`-persisted operation id), the control room discovers an
  in-progress execution purely from server-side state, so *any* browser
  tab loading the task page picks up live polling automatically, with no
  client-side storage required.
- `recent_agent_turns`: the last 20 `AgentTurnRecord`s (local and
  frontier, merged, `observation_ledger` excluded to keep the payload
  small), parsed directly from the `agent-turn-*.json`/`frontier-agent-
  turn-*.json` files `BoundedAgentSession._record()` already writes
  incrementally, one per turn, as execution progresses -- genuine live
  progress, not a synthetic status message, visible while an operation is
  still `RUNNING`.

`_available_actions()` now returns `["start_execution"]` for a task at
`SPEC_APPROVED` (previously `[]`), mirroring `["approve_specification"]`
at `SPEC_DRAFTED`. `submit_execution_operation()` / `execution_operation
_status()` mirror `submit_review_operation()`/`submit_intake_operation()`
exactly: validate and durably record via `prepare_execution_operation()`,
hand off to a lazily-constructed `ExecutionWorker`, return immediately --
the service itself never calls a model, creates a worktree, or runs a
command. `POST /api/tasks/<id>/execute` / `GET /api/execution/operations/
<operation-id>` sit behind the same session/origin checks as every other
route.

The control room (`#/task/<id>/control`, `src/apoapsis/ui/static/app.js`)
adds: a "Start coding" action shown only when `available_actions` offers
it and no operation is already active for the task; a two-step
confirmation panel rendering `execution_preview` in full (route, models,
budgets, completion policy, sandbox, verification commands) before
anything runs; background submission that returns immediately and begins
`sessionStorage`-independent polling (seeded from `active_execution_
operation` on load, so a reload or a second tab reconnects automatically);
a live progress feed built directly from `recent_agent_turns` (tool
actions and rejections, most recent first); and, once a report exists,
a usage/telemetry panel (tokens, estimated cost, latency, audit-artifact
count) drawn from the same `FinalTaskReport` the CLI has always produced.
`COMPLETE`, `FAILED`, and `AMBIGUOUS` are terminal, inspectable operation
states rendered with the same pill styling used elsewhere; a task that
reaches `HUMAN_REVIEW_REQUIRED` shows a direct link into the existing,
unmodified Human Review case detail view.

**A genuine, pre-existing bug was found and fixed during this commit's
live browser verification, not by the deterministic suite:** `app.js` had
two unrelated functions both named `reviewView` -- one for the top-level
`#/review/<task-id>` route (`if (!store.review) return loadingView();
return reviewDetailView(store.review);`), and one for the task page's
static "Review" sub-tab (a placeholder taking a `detail` parameter).
JavaScript's last-function-declaration-wins semantics meant every call to
the zero-argument top-level route handler actually executed the
sub-tab's body instead, crashing on `detail.task` (`detail` being
`undefined`) -- so clicking into a Human Review case from *anywhere* in
the app has been broken since whichever earlier change introduced the
second definition, invisibly, because no existing test executes `app.js`
in a JS engine. Live-clicking the new "Open the Human Review case" link
surfaced it immediately. Fixed by renaming the task-sub-tab placeholder
to `taskReviewTabView(detail)` and updating its one call site; the
top-level `reviewView()` route handler (and the real Human Review
experience it renders) is unaffected and was verified working, end to
end, against a real local Ollama model afterward.

New tests: `tests/test_execution_ui.py` (19 tests: service/HTTP security,
duplicate/reconnect, ambiguous-operation visibility, `task_detail`'s new
fields, bundled-asset sanity); one existing assertion in `tests/test_ui.py`
was updated (`SPEC_APPROVED`'s `available_actions` is now
`["start_execution"]`, not `[]`) to reflect the new, correct behavior.
Full suite: 452 tests, 0 failures, 6 intentional skips. Live-verified in a
real browser against a real local Ollama model: "Start coding" reached a
real routing decision and ran a real bounded local-agent session that
stopped for human review with an accurate, real error message, and the
new "Open the Human Review case" link correctly opened the real case
detail view (after the bug above was fixed).
