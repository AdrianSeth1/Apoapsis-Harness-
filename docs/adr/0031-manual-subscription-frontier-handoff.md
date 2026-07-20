# ADR 0031: Manual subscription-based frontier coding handoff

- Status: Accepted
- Date: 2026-07-20

## Context

ADR 0006/0022 give a stopped, local-only task a way to reach a frontier
model, but only through a configured, authenticated `[models.frontier_coder]`
API provider. Many owners have a ChatGPT or Claude *subscription* -- web
chat access -- with no separate API credential and no wish to pay for one.
Apoapsis must not automate either website (a terms-of-service and reliability
problem) and must never store or reuse a subscription session as if it were
an API credential. What it can do is package a task's state deterministically
enough that a human can paste it into their own chat session by hand and
paste one bounded answer back -- without ever letting that pasted answer
claim authority Apoapsis itself must retain (routing, verification,
completion, workflow state).

This milestone adds exactly that path as a second, distinct option alongside
the existing automated API frontier path -- never replacing it, never
touching `AUTHORIZE_FRONTIER_STAGE`/`FRONTIER_CONTINUATION`, and never
weakening the non-negotiable authority boundary in `HANDOFF.md`.

## Decision

### A new, distinct review action: `MANUAL_FRONTIER_HANDOFF`

`ReviewActionKind.MANUAL_FRONTIER_HANDOFF` is added alongside the existing
actions, offered under exactly the same two stop reasons the automated
frontier actions are offered under (`LOCAL_AGENT_ESCALATION_UNAVAILABLE`,
`FRONTIER_AGENT_EXHAUSTED`) plus, for a bounded repair round after a failed
manual apply, `VERIFICATION_FAILED`. Unlike `AUTHORIZE_FRONTIER_STAGE`/
`FRONTIER_CONTINUATION`, eligibility is **never** gated on
`frontier_available` (a configured API credential) -- the whole point of
this path is to work without one. It **is** gated on a new, small,
deterministic ceiling: `ReviewCase.manual_frontier_rounds_used` (counted from
a specific, recognized event type, never inferred) versus
`config.manual_frontier.max_repair_rounds` (default `2`). Once the ceiling is
reached, the action is removed from `eligible_actions` exactly like
`LOCAL_CONTINUATION`/`FRONTIER_CONTINUATION` are removed once
`max_continuations_per_task` is reached -- the same, already-proven pattern,
not a new one.

### Reuses the existing operation ledger -- no weaker parallel authority path

Rather than inventing a second operation/lease/recovery mechanism,
`MANUAL_FRONTIER_HANDOFF` is executed entirely through the existing
`review.execution`/`ReviewOperationStore` machinery (ADR 0020/0021/0025):
`ReviewOperationRecord` gains one new, optional, additive field,
`manual_frontier_preview_id` (existing rows unaffected), and
`_WORKTREE_CHECKED_ACTIONS` gains this action so it gets the same fresh
task-version/worktree-fingerprint recheck and one-active-operation-per-task
guarantee as every other review action. This means: only one manual-frontier
apply (or any other review operation) can ever be in flight for a task at
once; a crash mid-apply is recovered exactly like a crashed continuation
(`review.recovery.recover_stale_operations`, unchanged); and the CLI/UI
two-step-confirmation, background-worker, and polling patterns all apply
unchanged. `review.execution.run_review_operation`'s dispatch for this
action imports `manual_frontier.apply.execute_manual_frontier_apply` lazily,
inside the branch only, to avoid a module-load-time circular import
(`manual_frontier` depends on `review`; `review.execution` must not depend on
`manual_frontier` at import time) -- the same technique `ui/server.py`
already uses for its own heavier lazy import.

### The immutable, hashed handoff package

`manual_frontier.package.build_manual_frontier_handoff_package()`
deterministically builds a `ManualFrontierHandoffPackage` from the exact same
`ReviewCase` projection every other review action uses -- no parallel
evidence format. It is bound to:

- the exact task id and version, and the exact worktree fingerprint
  (`repository.fingerprint.compute_worktree_fingerprint`, ADR 0017) and
  repository HEAD commit;
- the approved `TaskSpecification` and its active hard constraints, verbatim;
- the current diff (`ReviewCase.current_diff`, the same bounded synthetic-diff
  representation used everywhere else, ADR 0017 -- never a plain `git diff`);
- relevant normalized verification failures (`ReviewCase.normalized_failures`
  -- the same evidence the automated escalation package already includes);
- the configured verification catalog (name/category/description/
  required/acceptance-designated only -- **never** a command's
  `environment` dict, which could hold literal secret values for one
  command's run, exactly the concern ADR 0026's `_safe_config_payload`
  already identified for the automated-execution authorization package);
- the exact JSON schema of `ManualFrontierResponseEnvelope`, so the pasted
  answer can be schema-checked deterministically; and
- a fixed, human-legible list of authority rules.

It excludes secrets (no raw command environment values, no credentials --
there are none to include on this path), unrelated files (only the bounded
evidence above, the same set `ReviewCase`/the escalation package already
expose), held-out oracle content (the oracle is never part of any context
compiled for a model, on any path, unchanged), and audit-only private data
(no internal operation ids, lease state, or other bookkeeping).

`package_sha256` is computed over the full payload excluding `package_id`
and `generated_at` -- the same exclude-the-fresh-identifiers convention ADR
0026's `ExecutionAuthorizationPackage` already established -- so
re-deriving the same package from the same `ReviewCase`/specification input
reproduces the same hash, and any on-disk tampering
(`manual_frontier.package.verify_package_integrity`) is detectable before
the package is ever trusted again.

Two artifacts are written to the task's existing audit directory (no new
storage location): the canonical JSON package, and a single, self-contained
`FRONTIER-CODING-HANDOFF-<package_id>.md` -- everything needed to produce
one valid response is embedded in that one file; nothing external is
referenced. The user uploads this file to their own ChatGPT/Claude
subscription session by hand.

### The strict response envelope

`ManualFrontierResponseEnvelope` (`model_config = ConfigDict(extra="forbid")`)
is the only shape Apoapsis will ever accept back:
`schema_version`/`package_id`/`package_sha256`/`task_id`/`task_version`/
`patch`/`summary`. There is deliberately no status/completion field, no
command-selection field, and no budget field of any kind -- `extra="forbid"`
means any additional field (a `"status": "complete"` a model might be
tempted to add) is rejected outright at parse time, before any other check
runs. The response asks for **one complete, bounded patch**, not an
interactive shell or tool-call loop: there is no mechanism anywhere in this
path for a second request, a follow-up question, or a request for more
context -- the whole document handed to the model in
`FRONTIER-CODING-HANDOFF-<package_id>.md` is everything it ever gets.

### Import creates a preview only; two-step approval gates apply

`manual_frontier.importer.import_manual_frontier_response()` performs, in
order, and fails closed on the first violation:

1. **Task version and eligibility.** A fresh `ReviewCase` is built; the task
   must be at `HUMAN_REVIEW_REQUIRED` with `MANUAL_FRONTIER_HANDOFF`
   currently in `eligible_actions` (this alone enforces the repair-round
   ceiling, since ineligibility after the ceiling is reached is computed the
   same way `LOCAL_CONTINUATION` becomes ineligible after its own ceiling).
2. **Active-operation conflict.** No other review operation (of any kind --
   not just another manual-frontier one) may currently be `RECORDED`/
   `RUNNING` for this task.
3. **Package integrity, task version, and worktree fingerprint.** The
   referenced package is reloaded from disk and its own hash re-verified; its
   recorded `task_id`/`task_version`/`worktree_fingerprint` must match the
   task's *current* state exactly -- a stale package (the task moved on, or
   the worktree changed) is rejected with a message asking for a fresh
   export, never silently accepted.
4. **Response size**, checked in raw bytes **before** any JSON parsing
   (`config.manual_frontier.max_response_bytes`, default 2 MB) -- a caller
   cannot exhaust memory with an oversized paste before schema validation
   even runs.
5. **Schema validation and self-consistency.** The response must parse as
   JSON, validate against the strict envelope (any extra field, including an
   attempted status/completion claim, is rejected here), and must echo back
   this exact package's id, hash, task id, and task version -- a response
   produced against a *different* package (or a stale, previously-answered
   one) is rejected as a hash mismatch, not silently applied.
6. **Patch parsing and policy.** The patch is parsed by the same
   `UnifiedDiffParser` and validated by the same `PatchPolicyValidator`
   every other patch path uses (file count, size, protected paths, binary/
   symlink rejection, dependency/verification-config protection) -- no
   separate, weaker patch acceptance logic.

Only on success is a `ManualFrontierPreviewRecord` created (its own small
SQLite ledger, `.apoapsis/manual-frontier-previews.db`) at status
`PREVIEWED`. **Nothing here touches the worktree or mutates task state.** A
fresh import for the same task marks any prior `PREVIEWED`/`APPROVED`
preview `SUPERSEDED`, so an older, unapplied preview can never be approved
or applied after a newer one exists.

Applying the patch requires two distinct, explicit steps:

1. `manual_frontier.approve.approve_manual_frontier_preview()` -- records
   that the user reviewed the previewed patch and intends to apply it.
   Rechecks the preview's own captured task version/fingerprint against
   current state and that the action is still eligible; never mutates
   anything itself.
2. Submitting a real `MANUAL_FRONTIER_HANDOFF` review operation (CLI
   `frontier-manual apply`, going through `execute_review_action`/
   `prepare_review_operation`/`run_review_operation` exactly like every
   other review action) -- this is what actually applies the patch. Its
   handler (`manual_frontier.apply.execute_manual_frontier_apply`)
   independently re-checks preview status (`APPROVED`, not merely
   `PREVIEWED`), task version, worktree fingerprint, and package integrity
   *again*, immediately before touching the worktree -- never trusting that
   nothing changed between approve and apply, the same discipline ADR 0021
   already established for every other review action.

### Apoapsis applies the patch; only the verifier completes the task

`execute_manual_frontier_apply()` re-validates patch policy against
*current* configuration (which may have changed since import), applies it
with the same `GitPatchApplier` every other path uses, transitions
`IMPLEMENTING -> PATCH_READY -> VERIFYING` (existing edges, no
`workflow/states.py` change), and runs the project's configured
`VerificationRunner` -- the same runner, same commands, same `STRICT`
acceptance-coverage check (ADR 0015/0016/0017) as every other completion
path. **Only a passing verification result (and, under `STRICT`, satisfied
acceptance coverage) reaches `COMPLETE`.** Nothing in the response envelope
-- there being no status/completion field for it to abuse -- can cause a
different outcome. A failing result returns the task to
`HUMAN_REVIEW_REQUIRED` via a new, recognized event type,
`manual_frontier_apply_verification_failed`
(`review.classify._EVENT_TYPE_STOP_REASON`, classified as
`StopReasonKind.VERIFICATION_FAILED`), which is exactly how
`ReviewCase.manual_frontier_rounds_used` is counted -- one round per failed
apply, never for a successful one (there is nothing left to repair once the
task reaches `COMPLETE`).

If patch application itself fails unexpectedly (e.g. `git apply` rejects a
patch that passed policy but not application -- rare, since the preview's
patch was already applied once conceptually as text, not yet against a
possibly-drifted tree), the exception propagates, the operation is marked
`FAILED`, and the task is left at `IMPLEMENTING` until an explicit
`apoapsis review recover` pass returns it to `HUMAN_REVIEW_REQUIRED` -- the
same accepted, already-documented behavior every other review action's
mid-operation exception produces (ADR 0021/0025), not a new gap introduced
here.

### Operator-declared provenance; tokens and cost stay unmeasured

`ManualFrontierPreviewRecord.declared_model_name` is required, non-empty,
free text the operator types (e.g. `"claude-opus-4.6-web"`) -- Apoapsis never
infers, defaults, or verifies which model actually produced a response; it
is provenance, not a security claim. No token count, cache statistic, or
cost is ever recorded for this path, and none is ever displayed as `0` --
there is no telemetry to measure on a manual paste, so it stays absent
(consistent with `HANDOFF.md`'s existing rule that hosted savings/rescue
stay `unmeasured`, never a fabricated zero, until real evidence exists).

### CLI: export / inspect / import / approve / apply / status

```
apoapsis frontier-manual export TASK-ID
apoapsis frontier-manual import TASK-ID --package-id ID --response FILE \
  --declared-model-name NAME --preview-id ID
apoapsis frontier-manual inspect TASK-ID (--package-id ID | --preview-id ID)
apoapsis frontier-manual approve TASK-ID --preview-id ID --expected-version N
apoapsis frontier-manual apply TASK-ID --preview-id ID --expected-version N \
  --expected-fingerprint F --operation-id ID
apoapsis frontier-manual status TASK-ID
```

`apply` is the only mutating command and requires the same
`--expected-version`/`--expected-fingerprint`/`--operation-id` triple every
other mutating review command requires. No new UI surface is added in this
milestone -- see "Non-goals" below.

## Non-goals

- Does not automate ChatGPT's or Claude's website in any way -- no browser
  driver, no session cookie, no scraping. The user manually uploads a file
  and manually pastes back a response.
- Does not store, reuse, or infer subscription credentials. Apoapsis never
  authenticates to either service.
- Does not add a local UI surface for this path in this milestone (CLI/
  service only, mirroring how ADR 0019/0020 shipped their CLI/service seam
  before a later UI commit). The exact next UI seam, if desired: a Human
  Review case-detail action alongside `authorize_frontier_stage`, following
  the identical two-step-confirmation/background-worker/polling pattern
  ADR 0020 Commit C2 already established -- `ApoapsisUIService` would need
  `import_manual_frontier_response`/`approve_manual_frontier_preview`
  read/write methods and one new route pair, with `submit_review_operation`
  already handling the `apply` step unchanged, since it is a normal review
  operation.
- Does not change `AUTHORIZE_FRONTIER_STAGE`, `FRONTIER_CONTINUATION`, or
  any behavior of the existing automated API frontier path.
- Does not change `workflow/states.py` -- every transition this path uses
  already existed.
- Does not add an unbounded repair conversation -- `max_repair_rounds`
  (default 2) is small, configurable, and deterministically enforced the
  same way continuation ceilings already are.

## Tests

New `tests/test_manual_frontier.py` (22 tests, all deterministic,
fake-provider-free -- this path makes no model call of its own):
package-hash determinism and identifier exclusion, tampered-package
integrity detection, response-schema extra-field rejection (a model cannot
smuggle a completion claim), Markdown/JSON artifact writing and reload;
preview-store approve/apply/supersede transitions and double-approval
rejection; importer coverage for stale task version, stale worktree
fingerprint, malformed JSON, an extra field, oversized response, package-hash
mismatch, malformed patch, patch-policy rejection, and a stale/replayed
response against a task that already left `HUMAN_REVIEW_REQUIRED`; and
apply-path coverage for verifier-owned successful completion, a failing
verification correctly returning to human review while remaining eligible
for another round, the repair-round ceiling removing eligibility once
reached, a concurrent active review operation being rejected, and an
unapproved preview being rejected at apply time. `tests/test_review_execution
.py` gained one updated assertion (`MANUAL_FRONTIER_HANDOFF` is now correctly
present in an unconfigured-frontier scenario's eligible-action set). Full
suite: 623 tests, 0 failures, 10 intentional skips.

## Consequences

An owner with only a ChatGPT/Claude subscription -- no API credential -- now
has a real, bounded, auditable way to get frontier-model help on a task that
stopped locally, without Apoapsis ever automating a website, storing a
credential it was never given, or letting a pasted response claim any
authority beyond proposing one patch. The existing automated API frontier
path is completely unchanged and remains the preferred path once real API
access is configured.
