# ADR 0074: Delivery verifies the integrated project, and plans must be internally consistent

- Status: Accepted
- Date: 2026-07-29

## Context

Crisis Atlas remediation slice C. Two related gaps, both visible in
`PLAN-E1B90639E58D` (2026-07-29).

### Gap one: nothing ever ran against the combined result

Each slice is verified in isolation, in its own worktree, at the time it
runs. `prepare_plan_delivery` then checked that every task state was
`COMPLETE` and that the commits formed an integrated ancestry chain, and
shipped. The plan's own
`verification_strategy.whole_project_verification_commands` were validated
as *names* at plan-validation time and never executed.

So Crisis Atlas delivered four green slices and a product that did not work.
`python -m api.server` served `/incidents` and returned 404 at `/`; the
dashboard said `Offline Mode`; `app.js` used in-memory sample data and never
called the backend; a browser-created incident vanished on reload. No
individual slice check could have caught any of it, because no individual
slice was wrong. The defect lived *between* the slices, which was precisely
the region nothing was looking at.

The delivered `delivery.json` then presented per-slice verification history
under a heading that read like proof of the whole. ADR 0072 retitled that
section honestly; it did not add the missing evidence.

### Gap two: a plan could contradict itself and nobody noticed

The same plan required a browser-to-local-API integration *and* configured a
`verify-web-product` check that, before ADR 0073, forbade the mechanism.
Both statements were true. Neither was machine-readable alongside the other,
because the requirement lived in an `IntegrationContract`'s prose
(`interface`, `data_flow`) and the prohibition lived in a command's `argv`.

ADR 0073 removed that specific contradiction by narrowing what
`--forbid-external-resources` means. It did not make the *class* of
contradiction detectable. A plan can still require an integration and
configure `--forbid-runtime-network-apis`, and the model will again resolve
the impossibility the only way it can: by deleting the integration.

## Decision

### 1. A harness-owned final verification operation

`apoapsis.architect.final_verification` runs the plan's whole-project
contract against the integrated result. `prepare_plan_delivery` calls it
after resolving the integrated commit and worktree, and **before** the
archive is written or `mark_executed` is called.

The operation:

1. resolves the exact integrated commit, branch, and worktree (the caller
   has already done this and passes them in);
2. captures the worktree fingerprint **before** running, because a
   verification command may leave byproducts behind (ADR 0063) and a
   fingerprint taken afterwards would describe the tree the check produced
   rather than the tree it examined;
3. runs only the commands the approved plan named, against a
   `VerificationConfig` narrowed to that subset;
4. computes whole-plan acceptance coverage from the immutable result;
5. persists `final-project-verification.json` in the plan audit directory
   regardless of outcome, so a refusal leaves evidence of why; and
6. returns a record whose `is_sufficient_for_delivery` is the single gate.

Authority is unchanged. The commands come from the approved plan and must
resolve to configured `VerificationCommand` entries. No model chooses,
reorders, edits, adds, or waives one. The operation never transitions a
plan, never commits, and never merges.

**Required is forced for this run.** `VerificationRunner` executes the whole
configured command set for every task, so a genuine whole-project check --
one asserting that two slices agree -- necessarily fails inside the isolated
worktree of any slice that ran before its counterpart existed. The workable
configuration is therefore `required = false`, so early slices are not failed
by a check that could not yet succeed. But `VerificationResult` only
aggregates to `FAILED` on a *required* command, so honouring that flag at
delivery would let the gate pass on a failing integrated project. Naming a
command in `whole_project_verification_commands` is the owner's statement
that it must pass before delivery, and that statement governs here.

### 2. Binding, staleness, and fail-closed

The record carries `final_commit` and `worktree_fingerprint`. A record is
reused only when it passed **and** matches both. A stale binding, a
malformed artifact, or any non-passing status causes a fresh run rather than
a refusal, so an owner who fixes the integrated project and retries is not
made to delete a file by hand. A malformed artifact reads as absent; it can
never cause a pass, because the fresh run is what decides.

`FinalVerificationStatus` distinguishes four outcomes, because they need
different instructions:

| Status | Meaning |
| --- | --- |
| `passed` | Every named command passed at the integrated commit. |
| `failed` | The integrated project failed. |
| `not_configured` | The plan named no whole-project command, so nothing has ever run against the combined result. |
| `commands_unavailable` | The plan's named commands are not configured in this project, so the approved contract cannot be executed. |

Only `passed` permits delivery. On anything else `prepare_plan_delivery`
raises `SlicePackagingError`: the plan stays `APPROVED`, no ZIP is written,
no `delivery.json` is recorded, and the error names the status, the reason,
and the artifact path.

Acceptance coverage is reported but is deliberately **not** a second gate. A
criterion can only reach `FAILED` because a command failed, which already
fails `status`; blocking on `UNPROVEN` would refuse delivery for plans whose
criteria are simply unmapped, a configuration gap the contract assessment
(ADR 0069) already reports in its own voice.

### 3. Two evidence sections, structurally apart

`PlanDelivery` gains `final_project_verification` alongside the existing
`verification_summary`, and its `schema_version` moves to `1.1`.

* `verification_summary` — per-slice history. Each entry is scoped to one
  task and carries no commit or fingerprint binding, which is exactly why it
  cannot stand in for the other.
* `final_project_verification` — the plan's own contract, executed once,
  bound to the integrated commit and fingerprint.

The frontier review handoff gains a `## Final integrated-project
verification` section listing the commands, the commit, the fingerprint, the
per-command results, and — pointedly — which acceptance criteria the
integrated run did *not* prove, so a reviewing model directs attention at the
unproven part instead of assuming the green result covered everything.

`APOAPSIS-USING-THE-FINISHED-PROJECT.md` gains a "What was actually
verified" section naming the commands that ran against the delivered commit
and the criteria they did not establish.

### 4. Structured plan cross-consistency

New `validate_plan` findings, all ERROR, all reading **structured fields
only**:

| Code | Condition |
| --- | --- |
| `MISSING_WHOLE_PROJECT_VERIFICATION` | `whole_project_verification_commands` is empty. |
| `UNASSIGNED_INTEGRATION_CONTRACT` | A contract no slice references. |
| `UNASSIGNED_DELIVERY_ARTIFACT` | A `required_artifacts` entry in no slice's `suggested_paths`. |
| `UNVERIFIED_END_TO_END_SCENARIO` | A scenario with no command, or proven only by a command that is not a whole-project command. |
| `INTEGRATION_FORBIDDEN_BY_VERIFICATION` | A contract's declared runtime boundary is forbidden by a governing command's flags. |

`MISSING_WHOLE_PROJECT_VERIFICATION` is symmetric with the existing
`MISSING_VERIFICATION_INTENT` for a slice, and an ERROR for the same reason:
a plan that can never produce evidence about its own integrated result is
unfalsifiable, delivery refuses it anyway, and approving one only defers the
refusal to the point where all the work is already done.

`UNVERIFIED_END_TO_END_SCENARIO` insists a scenario be proven by a
whole-project command specifically. A scenario spans more than one slice by
definition, so a command that only ever runs inside one slice's isolated
worktree cannot prove it.

### 5. `IntegrationContract.runtime_boundary`

The contradiction check needs the mechanism to be structured, so
`IntegrationContract` gains:

```python
class RuntimeBoundary(StrEnum):
    UNSPECIFIED = "unspecified"      # default; asserts nothing
    IN_PROCESS = "in_process"
    SAME_ORIGIN_HTTP = "same_origin_http"
    CROSS_ORIGIN_HTTP = "cross_origin_http"
    FILESYSTEM = "filesystem"
    SUBPROCESS = "subprocess"
```

Contradictions are then a lookup, not an inference:

| Command flag | Forbids |
| --- | --- |
| `--forbid-runtime-network-apis` | `same_origin_http`, `cross_origin_http` |
| `--forbid-external-resources` | `cross_origin_http` only |

These are Apoapsis's own flags with documented semantics (ADR 0073), so
reading them is a lookup of the harness's own options rather than a guess
about an arbitrary command's behaviour — the inference this codebase refuses
to make and still refuses.

The governing command set for a contract is the whole-project commands plus
the `verification_commands` of every slice referencing it. `UNSPECIFIED`
produces no finding: a planner that did not fill the field in is not
asserting a mechanism, and inventing one for it would be exactly the prose
inference the field exists to replace.

`validate_plan` gains an optional `configured_commands` parameter carrying
full command objects. It defaults to `None`, so a caller with only names
keeps working and simply does not get these checks — absent information is
not evidence of a contradiction.

**ADR 0073's keyword-based criterion warning is deliberately absent from all
of this.** It reads prose, it produces false positives by design, and it
stays advisory. Nothing here gates on it.

## Consequences

### Migration

**`prepare_plan_delivery` now requires `verification_config`.** Every caller
passes it; it is a parameter rather than something read from disk inside so
the caller's already-loaded configuration governs.

**A plan with no `whole_project_verification_commands` is now invalid.** New
plans cannot be approved without one. A plan approved before this change and
not yet delivered must be revised and re-approved — there is no evidence to
substitute, and pretending otherwise is the failure being fixed.

**Preparing a delivery now executes commands and can take real time.** The
browser action may run as long as the configured whole-project commands do,
and can refuse a plan whose slices are all `COMPLETE`.

**`PlanDelivery.schema_version` is `1.1`.** A `1.0` record on disk predates
the final-verification field.

### Behaviour changes an operator will see

* Delivery can fail on a plan whose every slice is green. The error names
  the status, the reason, and the artifact path.
* `delivery.json`, the frontier handoff, and the ZIP's usage guide each
  distinguish per-slice history from integrated verification, and each name
  the criteria the integrated run did not prove.
* Plan validation raises new errors for unassigned contracts and artifacts,
  unverifiable end-to-end scenarios, and self-contradicting plans.

### What this does not do

It does not add the operability contract — launch path, README currency,
reachable entry point (slice D). It does not repair the Crisis Atlas
product. It does not make the whole-project run a *behavioral* check: what
it proves is exactly what the configured commands prove, and ADR 0069/0073
evidence-strength reporting applies unchanged.

It does not change the authority boundary. Every command executed here is
owner-approved, named in an approved plan, and resolved against configured
entries.

### Rejected alternatives

**Run the full configured command set at delivery.** The plan's
whole-project contract is an owner decision recorded at approval time.
Quietly running more than it asked for would make the record describe
something the owner never approved.

**Make the final verification a durable operation with its own store.**
`prepare_plan_delivery` is itself synchronous and not a durable operation;
adding a store for one step inside it would add a lifecycle to reason about
without adding a guarantee. The persisted, commit-bound artifact provides
the auditability that mattered.

**Gate on acceptance coverage as well as command status.** Would refuse
delivery for unmapped criteria, which is a configuration gap ADR 0069
already reports. Two gates for one fact, with the second one firing on
something the first never claimed to cover.

**Detect the integration contradiction from `interface`/`data_flow` prose.**
This is the inference the user explicitly ruled out and the codebase already
refuses elsewhere. A structured `runtime_boundary` costs one optional,
defaulted enum field and makes the check exact.

**Honour `required = false` for whole-project commands.** Would make the
gate unenforceable for exactly the checks it exists to enforce; see the
reasoning under Decision 1.

## Verification

`tests/test_architect_slice.py::FinalIntegratedVerificationTests` — nine
cases against real git worktrees and a real plan/slice lifecycle:

* two slices each passing their own verification while the integrated
  project fails, delivery blocked, plan left `APPROVED`, no ZIP, no
  `delivery.json`, failure record persisted with the unproven criterion;
* the control case where the second slice actually wires itself to the
  first, delivery proceeding and the plan reaching `EXECUTED`;
* commit/branch/worktree/fingerprint binding, and `matches()` rejecting a
  different commit or a different fingerprint;
* a planted passing record for another commit being re-run rather than
  reused;
* a malformed artifact causing a fresh run;
* a whole-project command missing from configuration blocking with
  `commands_unavailable`;
* a plan naming no whole-project command never being sufficient; and
* the two evidence sections staying structurally apart in `delivery.json`,
  the frontier handoff, and the ZIP usage guide, plus a round trip through
  `delivery.json`.

`tests/test_architect_validation.py` — `PlanCrossConsistencyTests` and
`IntegrationVersusVerificationContradictionTests` cover each new finding
code, the same-origin-versus-`--forbid-external-resources` combination that
must *not* fire (a drift detector between this ADR and 0073), every
non-networked boundary, `UNSPECIFIED` asserting nothing, and the
contradiction check staying silent when no command `argv` is supplied.
