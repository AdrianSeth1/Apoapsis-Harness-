# ADR 0028: Planning comparison framework (Phase D4a)

- Status: Accepted
- Date: 2026-07-19

## Context

ADR 0019 built Architect Mode; ADR 0027 built the bridge from an approved
plan's one selected slice into the existing durable execution service. Both
are deliberately silent on the actual product question this whole
direction exists to answer: does decomposing a task into a reviewed plan
and executing it slice by slice produce better real outcomes than asking
the bounded local agent to do the same task in one shot? Every existing
evaluation lane (ADR 0012) compares *execution routes* for one monolithic
task; none compares *monolithic versus planned* for the same task. This
milestone builds the deterministic comparison framework and fixture that
question needs. It deliberately does **not** run it against a live model
yet -- that is Commit D4b, gated on this commit's review and on a
genuinely independent, externally-produced plan (see Non-goals).

## Decision

### An extension of the ADR 0012 fixture family, not a second, unrelated fixture

`examples/download-service-v2/` is a new, physically separate directory,
not a second scenario folded into `examples/download-service/`. This is a
deliberate choice, not laziness: `download-tests`/`unit-tests` in every
existing evaluation lane run `python -m unittest discover -s tests` against
`examples/download-service`, and every existing fake-provider test in
`tests/test_evaluation.py` copies that exact directory with no exclusions
beyond the held-out oracle. Adding new test files with new import targets
directly into that same `tests/` folder would be swept into every existing
lane's `download-tests` run and change its pass/fail behavior for reasons
unrelated to what those lanes ever asked a model to do -- silently
redefining the original benchmark, exactly what was ruled out. A second,
physically independent directory costs nothing and keeps the original
resumable-download scenario's fixture, visible checks, held-out oracle, and
every historical evaluation result byte-for-byte reproducible. It is still
squarely the same fixture *family* ADR 0012 committed to: same domain
(a download service), same three-layer verification discipline (development
checks, model-visible acceptance checks, a held-out oracle), same
`prepare_fixture_repository`/`assert_oracle_withheld` machinery, reused
unchanged.

### A real dependency DAG, not just three independent features

- **Slice A** (`jobs.py`): durable job-record bookkeeping -- attempt count,
  transferred bytes, an expected checksum, a lifecycle state, and failure
  information. No dependencies.
- **Slice B** (`downloader.py`): a resilient downloader -- `Range`-based
  resume, deterministic retry with backoff (an injectable `sleep`, never a
  real one), and a structured progress callback. No dependencies.
- **Slice C** (`service.py`): integrates A and B -- persists progress and
  attempt state through a real download, verifies the downloaded content's
  SHA-256 before reporting completion, and leaves a consistent failure
  state otherwise. Depends on **both** A and B.

Each slice has its own agent-visible development test
(`tests/test_jobs_contract.py`, `tests/test_resilient_downloader.py`) and
Slice C additionally has a model-visible integration acceptance test
(`tests/test_service_integration_visible.py`). A held-out cross-slice
oracle (`tests/test_v2_holdout_acceptance.py`, excluded from every
agent-visible copy exactly like ADR 0012's) exercises adversarial
combinations none of the per-slice checks see together: existing partial
data with `Range` honored or ignored, a transient failure followed by a
retry, correct byte accounting across a resume-and-retry, progress that
never double-counts a chunk, matching and mismatching checksums, a
corrupted file never reported complete, correct final job status/error,
and restart/recovery consistency across a fresh `DownloadService` instance
over the same durable `JobStore`.

### Why `STRICT` completion policy, for both conditions -- a documented deviation

Every existing evaluation lane (`evaluation/lanes.py`) explicitly forces
`CompletionPolicy.BASELINE`, because those lanes measure false success
against a single, blanket "did configured verification pass" signal that
must stay comparable across runs. This scenario cannot use that signal at
all for its planned condition: each slice executes in its own isolated
worktree, and a single shared verification catalog with multiple
`required = true` commands would block a slice on another slice's
not-yet-implemented file. `download_service_v2_config()` instead marks all
three commands `acceptance = true` and maps each slice's *own* inherited
`AcceptanceCriterion.verification_method` to its own command; under
`STRICT` (ADR 0015-0018, already built, reused unchanged), a task's
completion gate is "every *active* acceptance criterion on *this task's own
specification* is proven" -- exactly and only the one command a given
slice's derived task actually needs, regardless of what else exists in the
shared catalog. The monolithic condition's specification carries all three
criteria, so it is held to the equivalent full bar. This is recorded
explicitly on every `FinalTaskReport.completion_policy` and stated here,
never silently inherited.

`VerticalSliceRunner.run()` independently requires at least one
`required = true` verification command to exist at all (a pre-existing
safety floor, unrelated to acceptance coverage). `v2-jobs-tests` (Slice A's
own command) is the one marked `required = true`: Slice A has no
dependencies and always runs first in this fixed DAG, so by the time any
other slice's isolated worktree is created, Slice A's fix is already merged
into its base and that command already passes there too -- satisfying the
floor without ever blocking an unrelated slice. A consequence worth stating
plainly: because the required-check floor only counts commands *actually
executed at the current worktree digest during the current session*
(`_all_required_checks_passed`), every slice's own session must re-run
`v2-jobs-tests` once, even when its own work is unrelated to `jobs.py` --
a small, harmless, and disclosed cost of sharing one verification catalog
across every slice of a plan.

### A real D3a bug, found and fixed by actually running three slices end to end

`PlanSliceExecutionStore.approve()` previously rejected approving *any*
slice of a plan once *any other* slice of that same plan had ever been
approved, forever -- not just while that other slice was still genuinely
running. This was invisible until now because no existing test ever
approved a second slice after a first one actually completed; every prior
dependency test stopped at proving `package_slice` correctly *blocks* an
unmet dependency. The root cause: this store deliberately never persists a
slice's status past `APPROVED` (ADR 0027 -- RUNNING/COMPLETE/HUMAN_REVIEW/
FAILED are always a live projection from the derived task's own state), so
a query purely over this table's own `status` column cannot tell a
still-running slice from one that finished long ago; "no other row is
`APPROVED`" is not stale, it is permanently wrong once any slice has ever
run. Fixed by moving the "at most one slice per plan active *at a time*"
check out of the store (which cannot answer it correctly) and into
`slice_service.approve_slice()`, which already has the task store and now
resolves every other `APPROVED` slice's *real, current* workflow state
before approving; only a slice whose task has not yet reached a terminal
state (`COMPLETE`/`HUMAN_REVIEW_REQUIRED`/`FAILED`/`ROLLED_BACK`) still
blocks. `ActiveSliceExecutionExistsError` is still raised for a genuine
concurrent conflict; `tests/test_architect_slice.py`'s existing duplicate-
approval test still passes unchanged, proving the *concurrent* case is
still caught. This is a correctness fix to ADR 0027's own mechanism, not a
concession made for this evaluation framework -- a live product user
sequentially approving slice B after slice A finished would have hit the
exact same bug.

### The comparison framework itself

New, parallel schemas (`evaluation/planning_schemas.py`) deliberately do
not touch `EvalLane`/`EvalLaneResult`/`EvalComparisonReport`/
`aggregate_evaluations` at all -- mixing a single-attempt lane result with
a multi-slice plan attempt into one schema risks exactly the kind of
accidental coupling ADR 0019 already declined for plans-versus-tasks.
`PlannerProvenance` records the planner model and method *separately from
the coding model*, and is always supplied by the caller: this framework
contains no code path that calls a planner or authors a plan itself --
`run_planned_condition()` requires an already-approved `plan_id`/
`plan_version` and only ever orchestrates the exact, unmodified
`package_slice`/`approve_slice`/`start_slice` functions ADR 0027 already
built and reviewed. A manually-pasted subscription planning session
records `planner_tokens_status = UNMEASURED` with a stated reason, never a
fabricated zero.

`run_monolithic_condition()` does not reuse `evaluation.harness.
run_eval_lane`: every lane forces `BASELINE`, and this scenario needs
`STRICT` for both conditions (above). It is a small, deliberately
duplicated wrapper around the same `VerticalSliceRunner` call, using the
caller's config exactly as given.

`run_planned_condition()` advances an approved plan's slices strictly in
topological order, one at a time, stopping immediately (no auto-repair, no
auto-advance past a stuck slice) the moment one fails to reach `COMPLETE`.
Auto-advance across slices exists **only** inside this evaluation-only
module, gated on the caller already holding an approved, fixed plan; it is
never reachable from `apoapsis plan slice ...` or the Plans UI, which
remain one-slice-at-a-time with no scheduler. After a slice reaches
`COMPLETE`, the driver commits its worktree and merges its branch into the
shared base via plain, deterministic git commands -- mirroring exactly
what ADR 0027 already requires a human to do before a dependent slice can
be packaged; this evaluation harness performs that same step deterministically
so a fixed comparison run can proceed without a human clicking through
each slice by hand. Once every slice completes, the held-out oracle runs
once against the final merged repository state, via a new
`run_held_out_oracle_against_worktree()` (extracted from the existing
`run_held_out_oracle()`'s worktree-scoped mechanics so both the single-task
and whole-plan call sites share one implementation). `integration_failure`
is `True` exactly when every slice individually reached `COMPLETE` but the
held-out oracle still failed -- proof that per-slice verification agreed
while the assembled system did not.

`evaluation/planning_aggregate.py`'s `summarize_planning_comparisons()`
computes, without any model call: end-to-end true completion (`COMPLETE`
and the held-out oracle passed) and false success for each condition;
per-slice completion and Human Review rates; calls, local/frontier turns,
patch and verification attempts, input/output/cached tokens, transmitted
context files/lines, latency, estimated cost, and policy rejections --
summed per attempt for the planned condition (one total per whole-plan run,
comparable to the monolithic condition's single total) and refuses to
silently mix comparisons from different `scenario_id`/`scenario_version`
values.

### CLI

```
apoapsis eval-planning download-service-v2 \
  --plan-id PLAN-... --expected-plan-version N \
  --planned-project-root <already-initialized-and-approved-project> \
  --planner-model "<free text>"
```

Requires a project directory where the fixed plan was already exported,
imported, validated, and approved via the existing `apoapsis plan ...`
commands, still at its untouched scenario baseline (no slice packaged or
started yet) -- this command never generates a plan itself. It creates one
fresh, byte-identical `download-service-v2` copy for the monolithic
condition, runs both conditions under identical model/config/completion
policy, and writes `planning-comparison.json`/`.md`.

## Tests

New `tests/test_planning_evaluation.py` (10 tests, deterministic fake
providers): the monolithic condition reaching `COMPLETE` with all three
acceptance criteria proven and the held-out oracle passing; oracle absence
from every agent-visible copy; all three slices completing in dependency
order with real git merges between them and the oracle passing; a
dependent slice's packaging still correctly blocked before its
dependencies merge (D3a's own mechanism, exercised through this new
caller); a slice stopping at Human Review halting the whole plan with no
auto-repair and no auto-advance; a genuine integration failure (every
slice individually `COMPLETE`, held-out oracle still `FAILED`) detected
from a bug invisible to a single-chunk per-slice dev test but caught by
the held-out oracle's multi-chunk case; and the aggregate summarizer's
true-completion/false-success/unmeasured-state formulas and its refusal to
mix scenario versions.

Full suite: 536 tests, 0 failures, 6 intentional skips.
`python -m compileall -q src tests` and `git diff --check` both clean.

## Non-goals

- No live model call anywhere in this commit. Every test uses a
  deterministic fake provider; nothing here is evidence about model
  quality. That is Commit D4b, and only after this commit is reviewed.
- Does not generate, validate, or approve a plan. `run_planned_condition`
  requires one already approved through the existing, unmodified
  `apoapsis plan export/import/validate/approve` workflow.
- Does not change any existing evaluation lane, schema, or aggregator.
  `EvalLane`/`EvalLaneResult`/`EvalComparisonReport`/`aggregate_evaluations`
  are untouched; historical evidence stays reproducible.
- Does not touch `examples/download-service`'s own files, tests, held-out
  oracle, or historical evaluation path.
- Not yet Phase D5 (Docker sandbox live proof, hosted-frontier comparison,
  packaging decision) -- explicitly gated on D1-D4 completing first.

## Consequences

A monolithic attempt and a plan-then-execute attempt at the same
three-slice task can now be run under byte-identical fixture copies,
identical model/config/completion-policy/verification backend, and
compared on true completion, false success, per-slice outcomes, resource
totals, and cross-slice integration failure -- entirely deterministically,
with zero live evidence claimed. A real, previously-latent bug in ADR
0027's own "one active slice per plan" invariant was found and fixed along
the way, strictly narrowing it from "ever approved" to "currently still
running," which a live product user sequentially working through a
multi-slice plan will also now benefit from. D4b (live local evidence
against a genuinely externally-produced plan) is deliberately deferred to
its own review.
