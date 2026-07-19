# ADR 0019: Architect Mode planning foundation

- Status: Accepted
- Date: 2026-07-19

## Context

Apoapsis's local coding model is deliberately bounded: a small, verifiable
task, a fixed budget of turns/patches/verifications, and a deterministic
completion gate (ADR 0015-0018). That is the right shape for one focused
change. It is the wrong shape for a large idea that has not yet been broken
into small pieces -- asking the local model to both *design* an architecture
and *implement* it in one bounded loop reintroduces exactly the
open-ended-agency risk the rest of this project has spent nine ADRs
eliminating.

Architect Mode exists to let a stronger model (Claude, Codex, Fabel, or any
other frontier model the user already has access to -- manually, with no new
subscription-backed provider adapter) design an architecture and decompose a
large idea into small, independently verifiable **implementation slices**
sized for the local coding model's existing bounded-agent loop. It is not an
autonomous agent swarm, it does not gain workflow authority, and it does not
execute anything. It produces a plan. A human reviews and approves the plan.
Executing an approved slice is deliberately out of scope for this commit and
for Commit B2 -- see Non-goals.

## Decision

### 1. Typed schemas (`src/apoapsis/architect/schema.py`)

`ArchitectureDecision`, `ImplementationSlice` (with the full field set: title,
concrete objective, explicit exclusions, dependency slice IDs, inherited hard-
constraint IDs, acceptance-criterion IDs, advisory suggested paths/symbols,
context/search seeds, verification-command names, integration assumptions,
interface contracts, risk level, local-model-fit rationale, stop/escalation
conditions, and a concise work brief), `ArchitecturePlan` (the planner's whole
proposal: idea text, architecture summary, decisions, hard constraints and
acceptance criteria -- reusing `specification.schema.HardConstraint` /
`AcceptanceCriterion` exactly rather than inventing parallel types -- and
slices), `PlannerRequestPackage`, `PlannerResponseEnvelope`,
`PlanValidationFinding` / `PlanValidationResult`, `PlanEvent`, and `PlanRecord`
with a versioned `PlanStatus` (`proposed`, `validated`, `approved`,
`superseded`, `executed`).

**Typed schema vs. deterministic validation is a deliberate split.**
`ArchitecturePlan` only enforces per-field shape (non-empty strings, ID
patterns) at the Pydantic level -- it does not reject a dependency cycle,
duplicate ID, or ceiling violation at construction time. That is
`validate_plan`'s job (below), and it must be possible to *import and inspect*
an invalid plan with concrete findings, not just have it bounce.

**`ArchitecturePlan` has no status, approval, or execution field.** Every
schema in this module extends `StrictModel` (`extra="forbid"`), so a planner
response that tries to smuggle `"status": "approved"` or an `"execute_now"`
flag fails validation outright rather than being silently accepted or
ignored -- this is requirement "a plan cannot mark itself approved or
executed" enforced structurally, the same way the rest of the codebase
refuses to let a model self-grant authority (ADR 0012's held-out oracle,
ADR 0015/0016's acceptance coverage). `PlanValidationResult.valid` is itself
guarded by a model validator: it can never be `True` while an error finding
is present, or `False` while none is -- the flag cannot drift from the
findings that justify it.

### 2. Deterministic validation (`src/apoapsis/architect/validation.py`)

`validate_plan()` returns a list of `PlanValidationFinding` (never raises for
content problems) covering: unique IDs (decisions, slices, hard constraints,
acceptance criteria); no dependency cycles (DFS cycle detection over the
slice graph); no missing dependencies; no unknown constraint/criterion
references; no invented verification-command names (checked against the
*current* `[verification.commands]` configuration, passed in by the caller,
never trusted from the plan itself); every active hard constraint
represented in at least one slice; every slice names at least one
verification command (executable verification intent); configurable ceilings
(`ArchitectPlanCeilings` in `config.py`: max slices, max dependency depth,
max suggested paths per slice, max constraint/criterion references per
slice, max work-brief length); and repository-relative, non-escaping
suggested paths (no absolute paths, no Windows drive letters, no `..`
segments).

"Planner prose never grants shell/filesystem/workflow/retry/completion
authority" is satisfied structurally rather than by text-scanning free-form
fields (consistent with how the rest of the codebase treats hard-constraint
and specification text -- nothing elsewhere greps prose for forbidden
phrases either): `ArchitecturePlan` has no field capable of granting any of
that authority, `verification_commands` are names checked against
configuration, and `suggested_paths` are checked for escape, not executed.

### 3. Reproducible planner package (`src/apoapsis/architect/package.py`)

`build_planner_request_package()` builds a `PlannerRequestPackage`
containing: the idea's exact wording (`idea_text`, on a
`str_strip_whitespace=False` model, mirroring `HardConstraint
.verbatim_source`'s existing verbatim-preservation pattern); repository
identity and current Git state (`GitRepository.snapshot()`, unchanged);
deterministic architecture/context evidence with provenance -- reusing
`ContextCompiler`/`ContextPackage`/`ContextEvidence` exactly (via a
throwaway, never-persisted `TaskSpecification` whose objective is the idea
text, purely to drive the existing retrieval pipeline) rather than inventing
a second evidence format; HANDOFF/ADR references (a live directory listing
of `docs/adr/*.md` plus `HANDOFF.md`, so it can never go stale); the
configured verification/acceptance-command catalog (`VerificationCatalogEntry`
-- name/category/description/acceptance-designated only, mirroring
`specification.extractor`'s existing acceptance-catalog shape exactly, never
argv or environment); `ArchitecturePlan.model_json_schema()`; a fixed
`PLAN_AUTHORITY_RULES` prose block stating the boundary explicitly; and a
`package_sha256` derived the same way `ContextPackage.context_sha256` is
(canonical JSON, sorted keys, SHA-256, embedded back into the model with a
validator that rejects a mismatch). `apoapsis plan export` writes this
package to `.apoapsis/plan-packages/<package_id>/request-package.json`
*before* printing it to stdout -- the mechanism by which a human copies it
to Claude/Codex/Fabel/etc. -- so it is always an immutable record of exactly
what left Apoapsis, never a reconstruction.

### 4. Manual, subscription-friendly CLI workflow

```
apoapsis plan export "<idea>"
apoapsis plan import <response.json>
apoapsis plan validate <plan-id>
apoapsis plan inspect <plan-id>
apoapsis plan approve <plan-id> --expected-version <n>
```

`plan export` requires no credentials and calls no provider -- the exported
package is designed to be pasted into any chat-based frontier model by hand.
`plan import` reads a `PlannerResponseEnvelope` (`package_id`,
`request_package_sha256`, and the `plan` itself) from a file the user saves
after getting a response back, and creates a brand-new `PlanRecord` at
`PlanStatus.PROPOSED`, version 1. No API credentials and no subscription-
backed provider adapter were added -- this is deliberately the same
non-goal ADR 0008 already recorded for `claude_code_cli`/`codex_cli`-style
adapters, still deferred, not built here.

### 5. Persistence and audit

`SQLitePlanStore` (`src/apoapsis/architect/store.py`) mirrors
`workflow.engine.SQLiteTaskStore`'s schema and optimistic-concurrency
discipline exactly, in its own database file (`.apoapsis/architect-plans.db`)
so this milestone never touches the existing task store: `BEGIN IMMEDIATE`
transactions, an `expected_version` pre-check plus a belt-and-suspenders
`WHERE version = ?` on every `UPDATE`, and `ConcurrentPlanTransitionError` on
either mismatch. `create_plan` starts a plan at `PROPOSED`/version 1.
`record_validation` moves to `VALIDATED` only when the result is valid,
otherwise stays at `PROPOSED`, and is only callable from `PROPOSED` or
`VALIDATED` (validating an `APPROVED` plan raises
`InvalidPlanTransitionError` -- validation is frozen once approved).
`approve_plan` requires `VALIDATED` status *and* re-checks the stored
validation result is itself valid, raising `PlanActionError` otherwise --
approval can never be reached from an invalid or unvalidated plan.
`create_revision` bumps the version, resets to `PROPOSED`, clears the
validation result, and is allowed from any status including `APPROVED` --
**this is how "never overwrite an approved plan" is satisfied**: the
approved version's immutable audit snapshot
(`.apoapsis/plans/<plan_id>/plan-v<n>.json`, written by the caller before
`create_revision` runs) is never touched; only the store's current-version
pointer advances, exactly as `SQLiteTaskStore.transition` never deletes a
prior workflow event. `PlanAuditStore` (`src/apoapsis/architect/audit.py`)
writes the imported response, each version's plan snapshot, each version's
validation result, and the approval event with the same atomic
tempfile-then-`fsync`-then-`os.replace` discipline as `audit.store
.TaskAuditStore._write`.

`import_planner_response()` (`src/apoapsis/architect/importer.py`) is the one
integrity gate between an exported package and an imported plan: it loads
the stored package by `package_id` and rejects the import outright
(`PlanImportError`) if the response's `request_package_sha256` does not
match the package's own `package_sha256` exactly, or if no package with that
ID was ever exported.

### 6. Tests

`tests/test_architect_validation.py` (schema/validation unit tests: valid
plans, cycles, missing dependencies, duplicate IDs, unknown verification
commands, missing verification intent, unknown constraint/criterion
references, unrepresented hard constraints, path escapes, every ceiling, the
`valid`/findings consistency guard, and two direct attempts to smuggle an
authority field into plan JSON), `tests/test_architect_store.py`
(`SQLitePlanStore` transitions: version-1 creation, validation moving to/
staying off `VALIDATED`, stale-version rejection on validation/approval/
revision, approval requiring `VALIDATED` and a valid result, validating an
already-approved plan being rejected, revision preserving event history
without losing prior versions, and store-reopen persistence), and
`tests/test_architect_cli.py` (the full CLI lifecycle export -> import ->
validate -> approve -> inspect; the package being written to disk before the
CLI returns; a stale approval version being rejected; response/package hash
mismatch; import without a prior export; and verbatim idea-text
preservation through import).

## Rejected alternatives

- **Let the planner's JSON include its own approval/status field and just
  ignore it.** Rejected: silently ignoring an authority-claiming field is a
  worse failure mode than rejecting the response outright, and `extra=
  "forbid"` was already the project's standard authority-boundary
  discipline everywhere else.
- **Validate at Pydantic-construction time (reject cycles/duplicate IDs by
  raising).** Rejected: an invalid plan still needs to be stored and
  inspectable with concrete findings a human (or a future correction pass)
  can act on, not bounced before it can even be looked at.
- **Reuse `SQLiteTaskStore` for plans by adding a `plans` table.** Rejected:
  plans and tasks have different lifecycles (versioned revisions after
  approval; no shared foreign key), and mixing them risks the exact kind of
  accidental coupling ADR 0001's substrate was designed to avoid. A second,
  small, separately-migrated store is simpler to reason about.
- **Build a `claude_code_cli`/hosted-API adapter now so export/import could
  be automatic.** Rejected per ADR 0008's existing non-goal; still deferred.

## Non-goals

- Does not execute any slice. There is no code path anywhere in this commit
  that turns an approved `ImplementationSlice` into a running task -- that is
  explicitly future work, gated on its own design and review.
- Does not add a hosted or subscription-backed provider adapter; `plan
  export`/`import` are manual, copy-paste-friendly, and require no
  credentials.
- Does not change `workflow/`, `agent/`, or `vertical_slice.py` in any way;
  Architect Mode is entirely additive and orthogonal to the existing
  task-execution state machine.
- Does not add a multi-plan-merge, dependency-graph auto-scheduling, or
  automatic re-planning-on-failure mechanism.
- Not yet a UI surface -- that is Commit B2, tracked separately in this same
  ADR's Consequences once implemented.

## Consequences

A human with access to any strong model can turn a large idea into a
reviewed, versioned, dependency-ordered set of small implementation slices
the existing bounded local-agent loop is already sized for, without granting
that model shell, filesystem, workflow, retry, or completion authority at any
point, and without adding a new provider integration or credential surface.
Every plan's full lifecycle -- the exact package that was exported, the exact
response that was imported, every validation result, and the approval event
-- is preserved as an immutable audit record. Approving a plan is inert: it
records a harness-owned status transition and nothing else executes as a
result, exactly as intended for this commit.

**Commit B2 update:** Commit B2 added a read-only, capability-protected Plans
surface to the existing ADR 0014 local operator interface (`ApoapsisUIService`
plan-listing/detail/approve methods, `GET /api/plans`, `GET /api/plans/<id>`,
`POST /api/plans/<id>/approve`, and static Plans index/detail views reusing
the existing black/orange/purple design language). It added no new authority:
the UI's approve action calls the exact same `SQLitePlanStore.approve_plan`
optimistic-version transition the CLI uses, and the detail view states
explicitly, visibly, that approving a plan does not execute any slice.
