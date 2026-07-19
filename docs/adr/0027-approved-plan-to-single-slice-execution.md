# ADR 0027: Approved-plan to single-slice execution

- Status: Accepted
- Date: 2026-07-19

## Context

ADR 0019 gave Apoapsis a planning foundation: a strong external model can
decompose a large idea into small, independently verifiable
`ImplementationSlice`s inside an `ArchitecturePlan`, a human reviews and
approves that plan, and nothing executes as a result -- approval was
explicitly inert. ADR 0024 separately gave Apoapsis a durable,
crash-safe way to execute an already-approved task. This ADR is the
bridge the first two deliberately left unbuilt: turning one explicitly
selected, approved slice into a real, running task through the existing
D2 execution service, without duplicating any of its routing, context,
worktree, agent, patch, verification, escalation, or reporting logic, and
without ever executing more than the one slice a human explicitly chose.

## Decision

### One coherent bridge, three new modules, zero duplicated execution logic

`src/apoapsis/architect/slice_schema.py`, `slice_package.py`,
`slice_store.py`, and `slice_service.py` are new. Nothing in
`workflow/`, `agent/`, `execution/operation_service.py`, or
`WorktreeManager` changed to support this -- "start this slice" is, at
the moment it actually runs, indistinguishable from starting any other
approved task through the unmodified `execute_execution_operation()`/
`ApoapsisUIService.submit_execution_operation()` paths ADR 0024 already
built.

### `PlanSliceExecutionPackage`: the same immutable-package discipline, applied to a slice

`build_plan_slice_execution_package()` deterministically compiles exactly
what approving one slice would authorize -- no model call, no repository
mutation, no task created yet. It:

1. Requires the plan to be `APPROVED` at exactly the caller-supplied
   `expected_plan_version` (`SlicePackagingError` otherwise).
2. Loads the plan's originating `PlannerRequestPackage` by `package_id`
   and confirms its `repository.root` still matches the current
   repository's root -- a plan built against a different repository (or
   whose package can no longer be found) is rejected, asking for
   re-planning rather than guessing compatibility.
3. **Revalidates the plan against current configuration** -- the exact
   same `validate_plan()` ADR 0019 already built, called again with the
   *current* `[verification.commands]` catalog and ceilings, never
   trusting a validation result recorded at approval time. A plan that
   was valid when approved but no longer is (a referenced verification
   command was renamed or removed, for example) cannot have a slice
   packaged from it.
4. **Proves every dependency slice, never trusting status alone.** See
   below -- this is the part of the spec that most needed a genuine
   design decision.
5. Copies the exact `HardConstraint`/`AcceptanceCriterion` objects the
   slice inherits from the plan's own records (never re-derived,
   reworded, or weakened) into a real, freshly-compiled
   `TaskSpecification` -- fails closed (`SlicePackagingError`) if a
   referenced ID cannot be recovered exactly, a defense-in-depth check
   that is provably unreachable through the normal approved-plan
   lifecycle (validation already guarantees every reference resolves
   before approval is even possible) but is exercised directly in tests
   rather than removed, since it is the actual mechanism providing that
   guarantee.
6. Captures the full parent-repository fingerprint
   (`compute_worktree_fingerprint`, ADR 0017/0026's own convention -- not
   `git rev-parse HEAD` alone) for audit.
7. Computes `package_sha256` the same way ADR 0026's
   `ExecutionAuthorizationPackage` does: canonical JSON, excluding
   `generated_at` and `package_id` (a fresh id chosen per packaging
   attempt, not content). The derived task's own id is *also*
   deterministic -- `sha256(f"{plan_id}:{slice_id}:{plan_version}")`,
   never a random UUID -- so repackaging the same (plan, slice, plan
   version) before approval reproduces the exact same package hash,
   exactly like ADR 0026 requires of its own preview/confirm cycle. A
   real plan revision (a different `plan_version`) gets a genuinely
   different derived task id, never colliding with a stale, superseded
   package's.

Never serializes a credential: the package inherits `_safe_config_
payload`'s discipline implicitly by never touching raw verification-
command `environment` values at all -- it only ever reads command
*names* from the catalog, exactly as ADR 0019's own `VerificationCatalog
Entry` already did.

### Dependency readiness: proof, not a status flag -- and a real subtlety in what "proof" means here

The hardest genuine design question in this milestone: slice B depending
on slice A means B should only be packageable once A's work has actually
landed in the repository state B would start from. Apoapsis has never
auto-merged, auto-committed, or auto-cleaned up a worktree (ADR 0024's
explicit, repeated non-goal, unchanged here) -- so there is no shared
"plan workspace" to chain slices through, and building one would have
been a much larger, separately-reviewable change. Instead:

- A dependency slice_id is satisfied only if its own `PlanSliceExecution
  Record` has a `task_id`, that task's **real, current** workflow state
  (read fresh from `SQLiteTaskStore`, never from this store's own
  persisted `status` field -- see below) is `COMPLETE`, and the
  dependency's worktree branch is a **git-ancestor of the current
  repository HEAD**.
- **The subtlety a first implementation attempt got wrong, caught by its
  own test suite**: `WorktreeManager.create()`'s worktree branch is
  created once, at the task's start, from a captured base commit --
  Apoapsis never commits to it afterward. A branch whose tip still
  equals its own creation-time base commit is *trivially* an ancestor of
  anything descended from that base, regardless of whether the task's
  real changes were ever committed or merged -- so a naive "is an
  ancestor of HEAD" check is a silent no-op that always reports
  "satisfied" the moment a task reaches `COMPLETE`, exactly the failure
  mode the requirements explicitly warned against ("never claim a
  dependency satisfied merely because another isolated worktree reached
  COMPLETE"). The fix: the dependency's *true* original base commit is
  read back from its own `ExecutionOperationRecord.expected_repository_
  head` (captured once, at `prepare_execution_operation` time, ADR
  0024) -- not from `WorktreeManager.describe()`'s own `base_commit`
  field, which turned out to mean something different (the worktree's
  *current* HEAD, always trivially equal to itself) than its name
  suggested at the call site that mattered. Only once the branch's tip
  genuinely differs from that true original base -- meaning a human has
  actually committed the dependency's finished work inside its worktree
  -- does the ancestry check become a meaningful proof at all. This
  means a human must **commit, then merge** a completed dependency slice
  through their own, ordinary git workflow before a dependent slice can
  be packaged; Apoapsis proves this happened, but never does it itself.
- This design was chosen specifically because it closes the hazard the
  spec named directly: abandoning one slice never touches a shared
  workspace with other slices' completed work, because there is no
  shared workspace -- every slice's derived task gets its own,
  completely independent, unmodified D2 worktree, and abandoning it
  through the existing, unmodified Human Review machinery affects only
  that one task.

### `PlanSliceExecutionRecord`: only the harness-controlled half is ever stored

`PlanSliceExecutionStore` (its own SQLite file, `plan-slice-executions
.db`, mirroring `SQLitePlanStore`'s own separate-database convention) is
deliberately narrow: it only ever writes `PACKAGED` and `APPROVED` --
the two transitions entirely under the harness's own control, before any
task exists. `RUNNING`/`COMPLETE`/`HUMAN_REVIEW`/`FAILED` are **never**
separately persisted; they are computed live, on every read
(`project_slice_status()`), from the derived task's own real, current
`WorkflowState` and (before a task exists) from live dependency-evidence
computation. This was a deliberate correction during design: an earlier
draft considered persisting the full status lifecycle on this store's
own row, which would have required a synchronization mechanism to keep
it truthful as the background execution worker (ADR 0024/0025) advances
the task asynchronously -- a second, independently-drifting copy of the
truth is exactly the failure mode "real per-slice readiness and
execution status ... from persisted facts only" (D3b's own requirement)
rules out. Reading it live instead makes drift structurally impossible.

At most one record per plan may be `APPROVED` at a time
(`ActiveSliceExecutionExistsError`, checked atomically in the same
transaction as approval) -- only one slice of a given plan is ever
actively authorized to run.

### Approval and execution remain genuinely separate actions

`package_slice()` performs no model call, no task creation, no
repository mutation -- purely deterministic compilation, safe to call
repeatedly before approval (a fresh package simply replaces the prior
one at `PACKAGED`; re-packaging an already-approved slice is rejected).
`approve_slice()` is the one explicit human action that creates the
derived task -- through the exact same `INTAKE -> SPEC_DRAFTED ->
SPEC_APPROVED` transitions every other task-creation path already uses,
no new workflow edge added -- and records the slice as `APPROVED`, but
never starts it. `start_slice()` is a separate, later, explicit action
that does nothing but look up the derived task and call the existing,
unmodified `execute_execution_operation()` -- it contains no routing,
context, worktree, agent, patch, or verification logic of its own.
Nothing here ever automatically starts a next slice, merges, commits, or
marks the whole plan `EXECUTED`.

### CLI

```
apoapsis plan slice list <plan-id>
apoapsis plan slice inspect <plan-id> <slice-id>
apoapsis plan slice package <plan-id> <slice-id> --expected-plan-version N
apoapsis plan slice approve <plan-id> <slice-id> --expected-package-sha256 HASH
apoapsis plan slice status <plan-id> <slice-id>
apoapsis plan slice start <plan-id> <slice-id> [--operation-id ID]
```

## Tests

New `tests/test_architect_slice.py` (17 tests, deterministic fake
providers, the `download-service` fixture): package determinism and
operation-id/package-id independence (repeated packaging of an
unmodified slice reproduces the same hash); exact inherited hard-
constraint/acceptance-criterion propagation; stale plan version and
unapproved-plan rejection; changed-repository rejection (a request
package rebuilt with a different repository root, reconstructed rather
than hand-edited so only the repository-identity check under test is
exercised, not the package's own independent self-consistency
validator); a defense-in-depth unit test of the missing-constraint fail-
closed path (proven unreachable through the real approval gate, and
proven correct directly); advisory-path/symbol freedom (the derived
`TaskSpecification` carries no field that could restrict which paths a
bounded agent may touch); the full dependency-evidence matrix (never
packaged, complete-but-not-committed, complete-and-committed-but-not-
merged, and genuinely satisfied after a real commit-then-merge); package-
hash-mismatch rejection at approval; duplicate-approval and duplicate-
start rejection (`ActiveSliceExecutionExistsError`); successful
completion and Human-Review-stop status projection, both read from the
real task state; and an explicit proof that approving/starting one slice
never approves, packages, or starts a dependent slice automatically.

Full suite: 514 tests, 0 failures, 6 intentional skips.
`python -m compileall -q src tests` and `git diff --check` both clean.

## Non-goals

- Does not build a shared, persistent "plan workspace" or any worktree-
  chaining mechanism across slices -- deliberately, per the dependency-
  proof design above.
- Does not auto-merge, auto-commit, or auto-clean-up any worktree,
  dependency or otherwise -- unchanged from ADR 0024.
- Does not wire advisory `suggested_paths`/`suggested_symbols`/
  `context_seeds` into context compilation. They are preserved verbatim
  in the package for human/model visibility (`ContextCompiler.compile()`
  already exposes `preferred_paths`/`extra_queries` parameters that would
  be the natural extension point), but plumbing them through the shared
  `_run_from_approved` continuation used by every execution path is a
  separate, disclosed, deferred change -- not required by "hints, never
  an allowlist," which this milestone already satisfies structurally
  (the derived `TaskSpecification` has no field that could restrict
  anything).
- Does not auto-start a next slice, auto-approve a plan revision, or mark
  a plan `EXECUTED` -- a plan's own `PlanStatus.EXECUTED` value exists in
  ADR 0019's schema but nothing in this milestone ever sets it.
- Commit D3a itself added no UI surface -- see the D3b addendum below.

## Addendum: Commit D3b -- the Plans UI slice experience

Everything above is reachable only through `apoapsis plan slice ...`. This
addendum adds the browser surface, with zero new execution, routing, or
completion logic of its own -- every service function it calls is the exact
one D3a already built and tested.

`ApoapsisUIService.plan_detail()` now includes a `slices` array (each entry
`{slice_id, status, record}`, from the same `project_slice_status()` D3a
already exposes to the CLI) so the Plans list and the Implementation Slices
tab show real, live per-slice status without any new persisted field.
`plan_slice_detail(plan_id, slice_id)` composes the slice definition, its
live status, its latest package (via the new public
`read_latest_slice_package()`, `None` before packaging), and the derived
task's own `available_actions`/links once one exists.
`package_plan_slice()`/`approve_plan_slice()` are thin service wrappers
around `package_slice()`/`approve_slice()` -- no new validation, no new
state, no new error type beyond re-exporting D3a's own exceptions to the
HTTP layer.

`ui/server.py` adds `GET /api/plans/<plan_id>/slices/<slice_id>` and
`POST .../package` and `.../approve`, inserted **before** the existing
generic `/api/plans/` and `.../approve` routes, since a slice-approve URL
also ends in `/approve` and would otherwise be caught by the plan-level
handler first. Handlers only validate the request body, call the one
matching service function, and translate its typed exceptions to status
codes (400 for packaging/lookup errors, 409 for a package-hash mismatch at
approval) -- no direct model, command, or Git call, no invented state, no
routing or completion decision, matching every other handler in this file.

The UI's only genuinely new surface is the slice list, an immutable package
preview (exact inherited constraints/criteria, verification commands,
repository fingerprint, dependency evidence, advisory hints -- all read
verbatim off the package, nothing re-derived in JavaScript), and a two-step
Approve action (`intent -> confirm`, mirroring ADR 0026's own preview/confirm
discipline for "Start coding"). Once a slice is approved, its derived task
is deliberately indistinguishable from any other task: D3b renders links to
that task's existing, completely unmodified control room, changes/
verification view, and report/audit view rather than duplicating any of
that machinery. There is no "Run all" button and no scheduler anywhere in
this surface -- starting a slice's derived task still requires the same
explicit "Start coding" confirmation every other task requires.

New `tests/test_architect_slice_ui.py` (12 tests): live slice-status
projection on `plan_detail`/`plan_slice_detail` before and after packaging;
package-then-approve through the service layer creating a real derived task
at `SPEC_APPROVED`; package-hash-mismatch rejection at approval; dependency
reasons surfaced before packaging; the full HTTP lifecycle (unauthenticated
-> 401, package -> approve -> plan detail reflects `approved`); hash-
mismatch over HTTP -> 409; unknown slice -> 400; missing request fields ->
400; and a bundled-asset guard confirming `app.js` actually ships the new
slice-action hooks. Full suite: 526 tests, 0 failures, 6 intentional skips.
`python -m compileall -q src tests` and `git diff --check` both clean.

Verified live in a real browser against a disposable project: plan list ->
plan detail (slice status renders) -> Implementation Slices tab -> Inspect
-> package preview -> two-step approve -> derived-task links -> the
existing, unmodified control room recognizing the slice-derived task at
`SPEC_APPROVED` with its real specification-approval history and its normal
"Start coding" action present and untouched.

## Consequences

An approved plan's slices can now become real, running, durably tracked
tasks -- one explicitly selected slice at a time, through the exact same
crash-safe, lease-protected, authorization-hashed execution service every
other task uses, with its exact inherited constraints and acceptance
criteria preserved verbatim from the approved plan. Dependency ordering
is enforced by genuine git-ancestry proof of committed-and-merged work,
not by trusting a status flag -- closing a real gap a naive
implementation would have silently left open. Approving a slice remains
as inert with respect to execution as approving a plan already was;
starting it is always a separate, later, explicit action; and nothing
here ever chains beyond the one slice a human chose.
