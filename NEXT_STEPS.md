# Apoapsis next steps

This is the practical roadmap after the completed Apoapsis 1.0 implementation.
`HANDOFF.md` remains the canonical architecture and project-status record;
`AGENTS.md` remains mandatory instructions for coding models.
The owner-oriented explanation of how the pieces fit together, including the
held-out oracle, is `docs/architecture-explained.md`.

## For the owner

### 1. Use the new local-model controls

On Windows, double-click:

- `START_APOAPSIS.cmd` to start/check Ollama and warm the configured coding
  model for 30 minutes at its configured context size.
- `STOP_APOAPSIS.cmd` when finished. It explicitly unloads every configured
  local Ollama model, including the research model, and releases model RAM/VRAM.

The shared Ollama service stays running after Stop; it is lightweight and may be
used by other applications. Stop never touches hosted providers. To warm the
research model too, run:

```powershell
.\START_APOAPSIS.cmd --include-research
```

Loading both large models simultaneously may exceed available GPU/RAM even when
one model alone has context headroom. Leave Research Mode lazy unless it is
needed. Set `APOAPSIS_NO_PAUSE=1` when invoking either file from automation.

### 2. Collect the missing context-profile evidence

The framework is complete; the most valuable missing result is whether more
context actually helps this model on the same task. Start with 64k and 128k,
using the same task, model, quantization, generation settings, and lane:

```powershell
apoapsis eval download-service --lane local --context-profile 64k --output-dir .apoapsis-eval/profile-64k-1
apoapsis eval download-service --lane local --context-profile 128k --output-dir .apoapsis-eval/profile-128k-1
apoapsis eval-aggregate .apoapsis-eval/profile-64k-1/comparison.json .apoapsis-eval/profile-128k-1/comparison.json --output-dir .apoapsis-eval/profile-comparison-1
```

Repeat each profile at least three times before drawing a model-quality
conclusion. Compare completion and held-out-oracle results first; then context
tokens, attribution, cache telemetry, latency, and resource pressure. Do not
assume 256k is better merely because the model reports support for it.

### 3. Prove the sandbox success path

Docker's fail-closed path is live-proven; the success path still needs a pinned
local image and one explicitly authorized run. Follow ADR 0009 and the Docker
instructions in `HANDOFF.md`. Do not enable a silent host fallback.

### 4. Add hosted-frontier evidence only when desired

When real API credentials and pricing are configured, run paired identical
local-first and direct-frontier lanes. Preserve the complete comparison and
aggregate artifacts. Until then, hosted rescue and savings must remain
`unmeasured`; subscription access must not be represented as API access.

### 5. Use and review the first local application slice

Run `apoapsis ui` from an initialized project. The offline black/orange/purple
interface now shows real project, task, specification, event, report,
evaluation, and model-configuration data. Specification approval is live and
uses the same optimistic transition/event record as the CLI. Opening the UI does
not load or prompt a model; Doctor runs only when explicitly selected.

Natural-language model-assisted intake, execution orchestration, review/resume
choices, and native desktop packaging remain intentionally unavailable. Use the
CLI for those operations until the deterministic application services below are
built.

## For future coding agents

Read `AGENTS.md`, then all of `HANDOFF.md`, before making changes. Check the Git
status and preserve `substrate-v0.1` and all user work.

### Priority A — evidence before more retrieval machinery

1. Run repeated 64k/128k local evaluations on identical conditions.
2. Aggregate the persisted reports without model calls.
3. Record observed results in a new dated evaluation document.
4. Diagnose any quality difference from the audited context and action history.
5. Do not add embeddings, learned ranking, or model-selected context unless the
   deterministic lexical/symbol/import/test/diff path fails repeatably and the
   evidence identifies why.

Stop after publishing the evidence and ask for review before changing retrieval
architecture.

### Done — verification sufficiency and acceptance coverage (ADR 0015)

The 1.0 profile evidence above showed configured verification passing was not
proof of product correctness (4 of 5 completions had a failed held-out
oracle). This milestone added a real, product-level notion of "proven" that
composes with the existing bounded-agent/one-shot/escalation machinery
without touching retrieval, context compilation, or the held-out oracle:

- Three named verification layers (development, user-approved acceptance,
  held-out evaluation oracle) and a deterministic
  `AcceptanceCoverage`/`compute_acceptance_coverage()` record per criterion
  (`src/apoapsis/workflow/acceptance.py`).
- An opt-in `CompletionPolicy.STRICT` (default remains `BASELINE`, preserving
  today's held-out false-success comparability) that gates `COMPLETE` on
  every active acceptance criterion being Proven by a configured,
  user-approved acceptance-designated command -- never by a model's own
  claim.
- Ten deterministic fake-provider scenarios
  (`tests/test_acceptance_coverage.py`) covering unmapped/mapped/failing-
  then-passing criteria, a model's ineffective mapping attempt, two
  different valid tool sequences, multi-turn repair, fail-closed rejection
  of an unknown command, the oracle/workflow import-graph separation,
  baseline-unaffected behavior, and composition with frontier escalation.
- Report and UI surfacing of per-criterion status, configured agent budgets
  versus actual usage, and frontier availability/escalation state.

Not done, and the natural next evaluation once this is reviewed: map a real
`verification_method` onto the download-service fixture's acceptance
criteria and run `STRICT` against a real local/frontier model to see whether
it can productively repair toward the mapped acceptance command rather than
merely toward ordinary verification passing. Specification-extraction
reliability (the one 128k drafting failure noted above) remains a separate,
not-yet-investigated task.

### Done — corrective follow-up: acceptance catalog, stale-proof fix, strict default (ADR 0016)

A review of the ADR 0015 milestone above found three defects before any live
strict evaluation should run, all now fixed:

- Specification extraction now receives a deterministic
  `ACCEPTANCE_COMMAND_CATALOG` (name/category/description/
  `acceptance_designated`) built from real `[verification.commands]`
  configuration on every call. A model may propose
  `AcceptanceCriterion.verification_method` only from that catalog;
  extraction rejects anything else. The UI specification view now shows the
  proposed mapping so approval is informed.
- `compute_acceptance_coverage()` now consumes a `dict[str,
  VerificationStatus]` scoped to the current worktree digest, not a flat
  "ever passed" set. Never executed, executed-and-failed, and
  executed-and-passed are three distinct states; a result recorded against
  an earlier digest can never prove the current one. Proven by both a
  direct unit-test class and two integration tests that pass a mapped
  command, edit the worktree, and confirm the criterion reverts to Unproven
  until re-verified at the new digest.
- `apoapsis init` now writes `completion_policy = "strict"` -- the
  practical default for ordinary product runs -- with its default command
  marked `acceptance = true` at the time. (**Superseded below**: ADR 0017
  reversed the auto-grant; a fresh project's command stays
  `acceptance = false` until the owner explicitly opts in.) Every
  `apoapsis eval` lane explicitly forces `BASELINE` regardless of the
  caller's real project config, recorded on every persisted report and in
  the comparison Markdown, so false-success measurement stays comparable.

The held-out download-service oracle was deliberately left untouched and
was not turned into the visible acceptance check for that fixture -- doing
so is explicitly still future evaluation work, described immediately above
this section, requiring three distinct, separately-scoped checks: the
existing agent-visible development tests, a new user-approved acceptance
check the agent may run and repair toward, and the existing held-out oracle
that stays invisible to the agent. See ADR 0016. The full pre-existing test
suite (197 tests) was unaffected by these corrections; 13 new tests were
added (210 total).

### Done — proof-integrity hardening: worktree fingerprint, explicit acceptance designation (ADR 0017)

A further review found two more issues before a live strict evaluation
should run, both fixed:

- `BoundedAgentSession`'s verification-state digest was `git diff
  HEAD`-only and blind to **untracked files** -- the ordinary result of a
  patch that creates a new file without `git add`ing it (`git apply` never
  stages). A model could create or edit a new file and an earlier
  verification/acceptance-proof result would still look current. Replaced
  with `src/apoapsis/repository/fingerprint.py`'s
  `compute_worktree_fingerprint()`: HEAD identity, the canonical tracked
  diff, and sorted permitted untracked paths with exact content hashes and
  type/mode (symlinks hashed by target text, never dereferenced; binaries
  hashed by raw bytes, never decoded). Used everywhere verification
  caching, command results, and acceptance proof are scoped. `inspect_diff`
  now also represents permitted untracked files as bounded synthetic diffs
  so a model can see the same state being fingerprinted, with binary/
  symlink content failing closed to a path-only placeholder.
- `apoapsis init`'s auto-grant of `acceptance = true` (added by the ADR
  0016 section above) was reversed: acceptance designation must be an
  explicit owner decision, so the generated command now stays
  `acceptance = false` with inline setup guidance. `apoapsis doctor` and
  the UI overview both warn when `STRICT` has no acceptance-designated
  command, and separately when `BASELINE` is selected at all -- reported
  facts only, no config file is ever rewritten automatically.

The full pre-existing test suite (210 tests) was unaffected; 17 new tests
were added (227 total, 6 intentional skips -- 2 new ones for symlink
creation being unsupported on this Windows machine). See ADR 0017.

### Done — first controlled STRICT live evaluation

Added an opt-in `local-strict` evaluation lane (`--lane local-strict`,
excluded from the default lane set, forcing `completion_policy = STRICT`
regardless of the caller's real project config) and a second,
model-visible `tests/test_resumable_visible_acceptance.py` in the
`download-service` fixture -- distinct data and test/class names from the
held-out `tests/test_resumable_acceptance.py`, proven through its own
specifically named `resumable-acceptance-check` acceptance-designated
command. 244 tests total (full suite unaffected; 17 new).

Then ran three fresh, identical, live `local-strict` attempts
(Qwen3-Coder-Next Q4, 64k profile, no manual repair between attempts,
every audit artifact preserved). Result: **0/3 reached `COMPLETE`** (2
`HUMAN_REVIEW_REQUIRED`, 1 specification-extraction failure unrelated to
the new mechanism). Full detail:
`docs/evaluation/apoapsis-strict-live-evaluation-2026-07-18.md`.

**Not an architecture fix, and not done here, per instruction** -- but the
single highest-priority next step before any further live `local-strict`
evaluation: the run surfaced a genuine, narrowly-scoped harness gap. A
failing verification command that is `acceptance = true` but
`required = false` produces neither informative failure evidence
(`_verify()`'s trigger in `agent/session.py` keys on the aggregate
`VerificationResult.status`, which only reflects *required* failures) nor
an accurate turn summary (`_record_verification()`'s same `required`-only
check). In both live attempts that reached the mechanism, the model
proposed a genuinely correct acceptance-catalog mapping and got close to a
correct fix, but never saw that its one remaining, narrow return-value bug
had actually failed the acceptance check -- it saw "deterministic
verification passed" and spent its whole remaining budget re-running an
unchanged, already-run check. Fixing this (treating `acceptance = true`
the same as `required = true` for failure-evidence and summary purposes,
without changing what counts as a required *development*-gating failure)
is a small, well-scoped, already-diagnosed change -- the natural next step
before re-running `local-strict` to get a first real completion-rate
measurement. Do not begin it, or any further live evaluation, without
explicit direction.

### Done — Phase A: made the strict experiment fair (ADR 0018)

Fixed exactly the two gaps identified above, both deterministically tested,
neither yet re-validated live:

1. **Acceptance-designated command failures now produce real evidence.**
   `VerificationCommandResult.acceptance` carries the flag into the
   immutable result record. `FailureNormalizer.extract()`,
   `BoundedAgentSession._verify()`'s failure-evidence trigger, and
   `_record_verification()`'s turn summary all widen from `required` to
   `required or acceptance`. A failing optional acceptance command now
   always gets a real normalized-failure record, an accurate turn summary,
   and (once a required command also passes at the same fingerprint)
   failed acceptance coverage -- and the model can edit and retry within
   its existing budgets. `VerificationRunner`'s aggregate status
   computation is unchanged: an optional acceptance command's failure
   still never becomes a required development gate. An unchanged duplicate
   check is still rejected, but only after the original evidence was
   already produced.
2. **Exactly one bounded specification-extraction correction attempt.**
   When the first response fails schema/Pydantic/verbatim/catalog
   validation, `VerticalSliceRunner.run()` persists the failure and makes
   one more model call with `SpecificationExtractor
   .build_correction_prompt()` -- the exact validation errors, the
   model's own prior response, and the same schema/catalog/rules as the
   original prompt. If that also fails, the task stops deterministically
   at `FAILED`; there is never a second correction, and nothing coerces,
   nulls, or weakens validation to force success.

Full suite: 258 tests (up from 244; 14 new across
`tests/test_verification.py`, `tests/test_acceptance_coverage.py`,
`tests/test_specification_correction.py`, and
`tests/test_provider_and_specification.py`). See ADR 0018.

### Done — re-ran `local-strict` live under the Phase A fix: first genuine success

Three more fresh Qwen3-Coder-Next Q4 attempts at 64k, identical conditions
to round 1, no manual repair. Result:
**1/3 reached `COMPLETE`, and the held-out oracle independently confirmed
it correct** -- the first genuine true success across both rounds (6
attempts total). The model's return-value arithmetic was finally right in
the resume branch. The other two attempts both received accurate failure
evidence (never happened in round 1) and made real further edits -- one
ran the required `unit-tests` command for the first time in either round
-- but ran out of their 12-turn budget before finishing; a manual
post-hoc check confirmed their remaining bugs were genuine, not harness
artifacts. Zero specification failures this round (one in round 1) -- too
small a sample to call either a rate. No retrieval issue in either round.
Full detail: `docs/evaluation/apoapsis-strict-live-evaluation-2026-07-19.md`.

**Still unmeasured**: a reliable completion/false-success rate (one
success in three attempts is not a rate); whether more turn budget, a
different budget shape, or a more capable model would close the gap for
the two attempts that ran out of turns; specification-extraction
reliability under the one-correction-attempt fix (never exercised live
this round, since all three extracted validly on the first try). Do not
begin another live evaluation without explicit direction.

### Done — Phase B1: Architect Mode planning foundation (ADR 0019)

Deterministic plan schemas (`ArchitecturePlan`, `ImplementationSlice`,
`ArchitectureDecision`, `PlannerRequestPackage`, `PlannerResponseEnvelope`,
`PlanValidationResult`, `PlanRecord`/`PlanStatus`), a standalone
`validate_plan()` covering unique IDs, dependency cycles/missing
dependencies, unknown constraint/criterion references, invented
verification-command names, unrepresented active hard constraints, missing
verification intent, configurable ceilings, and repository-escaping paths;
a reproducible `PlannerRequestPackage` builder reusing
`ContextCompiler`/`GitRepository` and the existing verification catalog; a
`SQLitePlanStore` mirroring `SQLiteTaskStore`'s optimistic-versioning
discipline in its own database; atomic-write plan audit artifacts; a
package/response hash-integrity importer; and the
`apoapsis plan export/import/validate/inspect/approve` CLI group -- manual,
credential-free, usable with any external model by hand. 36 new deterministic
tests (`tests/test_architect_validation.py`, `tests/test_architect_store.py`,
`tests/test_architect_cli.py`); full suite 294/294 passing. Architect Mode
does not execute any slice and does not touch `workflow/`, `agent/`, or
`vertical_slice.py`.

### Done — Phase B2: Plans surface on the local UI (ADR 0014 + ADR 0019)

Extended `ApoapsisUIService` with `plans()`, `plan_detail()`, and
`approve_plan()` -- read-only listing/detail plus the one deterministic,
optimistic-version-checked mutation, exactly mirroring the existing
specification-approval pattern. `ui/server.py` added `GET /api/plans`,
`GET /api/plans/<id>`, and `POST /api/plans/<id>/approve` behind the same
capability-token/origin checks as every other route. `ui/static/app.js`
added a Plans index and a two-tab plan-detail view (Overview: idea,
architecture summary, decisions, validation findings, package/provenance,
audit artifacts; Implementation slices: dependency-ordered slice cards with
objective, exclusions, dependencies, inherited constraints/criteria,
verification commands, suggested paths, risk, local-model-fit rationale,
and stop conditions) using only existing `.pill`/`.card`/`.constraint`/
`.metric` CSS classes -- no new design system. Status pills render the real
`PlanStatus` value (`proposed`/`validated`/`approved`/`superseded`/
`executed`), and the approval bar states explicitly that approving a plan
does not execute any slice. Added no slice-execution UI, no background task
execution, no upload endpoint, and no provider call from any HTTP handler.
11 new tests in `tests/test_ui.py` (service-level plan listing/detail/
approval, an explicit "approval touches no tracked file" check, and
server-level session/origin/version-conflict coverage for the new routes);
full suite 305/305 passing.

### Done — Phase C1: deterministic human review and resume (ADR 0020)

A full inventory of every code path reaching `HUMAN_REVIEW_REQUIRED` found
exactly five stop reasons (specification rejected, routing requires human,
one-shot acceptance-coverage-incomplete, local-agent escalation unavailable,
frontier-agent exhausted) and confirmed `ALLOWED_TRANSITIONS` already had
real outgoing edges from `HUMAN_REVIEW_REQUIRED` that nothing ever used.
New `src/apoapsis/review/` package: `ReviewCase` (a deterministic projection
of stop reason, current diff/worktree fingerprint, consumed/configured
budgets, and harness-computed eligible actions -- never a model's claim);
`BoundedAgentSession.resume()` (`agent/session.py`) to continue a bounded
agent session without resetting prior turns/observations/verification
state; an idempotent, crash-safe `ReviewOperationStore` (duplicate
`operation_id` rejected outright; an operation stuck `running` can never be
silently re-entered); an immutable `ReviewContinuationPackage` written
before any resumed model call; and `execute_review_action()`, which checks
optimistic task version, eligible action, worktree-fingerprint match, and
continuation ceilings before doing anything. Five actions:
`inspect_only`/`abandon`/`verification_only_retry`/`local_continuation`/
`frontier_continuation` -- eligibility computed fresh every time (frontier
availability re-checked against current config, not the stale routing
decision). CLI: `apoapsis review list/inspect/abandon/retry-verification/
continue-local/continue-frontier`. 35 new tests
(`tests/test_review.py`, `tests/test_review_execution.py`,
`tests/test_review_cli.py`) covering every stop scenario, stale versions,
changed worktrees, unavailable frontier, exhausted ceilings, duplicate/
crash-ambiguous operations, counter preservation, continuation-package
auditing, successful continuation, continued failure, and forbidden model
authority claims; full suite 340/340 passing. `workflow/states.py` was not
changed at all -- every transition edge used already existed. Does not
change one-shot's own execution path, does not let a continuation switch
which agent resumes, and does not launch a fresh frontier session from a
local-only stop with no frontier session yet.

**Next (Phase C2, tracked separately):** a Human Review queue and case-detail
view on the existing local UI, with two-step confirmation for every
mutating action, optimistic-version/worktree-fingerprint conflict handling,
and a background worker outside the HTTP request path so a browser
disconnect can never cancel, duplicate, or repeat an authorized operation.

### Done — Phase C2: Human Review UI (ADR 0014 + ADR 0020)

Extended `ApoapsisUIService` with `review_cases()`, `review_case_detail()`,
`submit_review_operation()`, and `review_operation_status()`.
`submit_review_operation()` performs every fast, synchronous check
(`review.execution.prepare_review_operation()`: optimistic task version,
eligible action, worktree-fingerprint match, continuation ceilings) and
durably records the operation before handing it to `review.worker
.ReviewWorker` -- a background thread that calls
`review.execution.run_review_operation()` (the actual model call,
verification run, or worktree cleanup) entirely outside the HTTP request.
`ui/server.py` added `GET /api/reviews`, `GET /api/reviews/<id>`,
`POST /api/reviews/<id>/operations` (returns `202 Accepted` immediately,
never blocks on the actual work), and
`GET /api/reviews/<id>/operations/<operation-id>` for polling -- all behind
the same capability-token/origin checks as every other route.
`ui/static/app.js` added a Human Review queue (`#/reviews`) and case-detail
view (`#/review/<task-id>`): exact stop reason, current diff, active
constraints, verification/acceptance results, consumed/configured budgets,
models used, audit locations, and only the actions the service actually
declares eligible. Every mutating action requires two-step confirmation;
continuation actions additionally prompt for an authorized
`additional_turns` value bounded by the configured ceiling. The browser
persists the in-flight `operation_id` in `sessionStorage` and resumes
polling it on reconnect (page reload) instead of resubmitting -- a browser
disconnect can never cancel, duplicate, or repeat an authorized operation,
since submission is already durably recorded before the worker ever runs
it. 18 new tests in `tests/test_review_ui.py` (service-level listing/detail/
submission, background-worker execution via a patched fake provider,
duplicate-operation replay rejection, reconnect via a fresh service
instance reading the same persisted operation, and server-level session/
origin/version/duplicate-conflict coverage); full suite 358/358 passing.

### Done — Phase H1: review/resume integrity hardening (ADR 0021)

A focused integrity review of ADR 0020's implementation found real gaps
between documented and enforced guarantees: `ReviewWorker`'s queue carried
a `ReviewCase` captured at submission time with no recheck at execution
time; no limit existed on concurrent operations per task; provider
construction could raise before `mark_running`, leaving an operation
`RECORDED` forever; no crash-recovery path existed at all; `_execute_abandon`
deleted the worktree before its version check; `classify_stop_reason` could
silently fall back to a stale, older recognized stop reason; `ReviewCase
.current_diff` used a plain `git diff` (missing untracked files); and
verification/acceptance evidence never advanced past the original,
never-updated `report.json` snapshot. All fixed without touching
`workflow/states.py` or any of ADR 0020's five stop-reason scenarios:
`ReviewOperationRecord` now persists `expected_worktree_fingerprint`;
`run_review_operation()` takes only an `operation_id`, marks it `RUNNING`
first, then freshly re-projects and re-validates a `ReviewCase` against the
durably recorded expectations before doing anything; `ReviewOperationStore
.create()` atomically rejects a second active operation per task
(`ActiveOperationExistsError`); a new `review.recovery.recover_stale_
operations()` reclaims never-started operations, marks stale `RUNNING` ones
the new terminal `AMBIGUOUS` status, and returns stranded tasks to
`HUMAN_REVIEW_REQUIRED` without claiming what happened -- run automatically
at `ReviewWorker` startup and explicitly via `apoapsis review recover`;
abandon now transitions before cleanup; stop classification decides on the
newest `HUMAN_REVIEW_REQUIRED` event alone; `ReviewCase.current_diff` uses
the shared `RepositoryInspector.diff()`; and fresh evidence is selected
using the same newest-event classification already computed. New ADR 0021;
errata appended to ADR 0020; HANDOFF's overstated crash-behavior claim
corrected. 12 new tests in `tests/test_review_hardening.py`, plus updates to
existing review tests for the new `classify_stop_reason` signature and the
duplicate/active-operation distinction; full suite 373/373 passing.

### Done — Phase H2: explicit fresh-frontier authorization (ADR 0022)

Added a distinct review action, `AUTHORIZE_FRONTIER_STAGE`, alongside (not
overloading) `FRONTIER_CONTINUATION`: the latter only ever resumes a
frontier session that already exists; the former starts a *fresh* frontier
stage from a local-only stop once a human explicitly approves it, using the
full configured `frontier_agent` budget with no partial-turns override, and
is only ever offered once per task -- once a frontier session exists, only
`FRONTIER_CONTINUATION` is offered from then on. `frontier_available`/
`frontier_model`/`frontier_stage_exists` on `ReviewCase` are always computed
against the *current* config and worktree, never cached from the original
stop, so adding `[models.frontier_coder]` after a local-only stop makes the
action eligible immediately. Extracted the previously-inline escalation-
package-construction logic out of `VerticalSliceRunner._run_frontier_
escalation` into `workflow/escalation.py`'s `build_local_to_frontier_
escalation()`, now shared byte-for-byte by both the automatic in-process
escalation path and this new human-authorized one (verified via the
existing, unmodified `test_agent_loop`/`test_vertical_slice`/
`test_evaluation` suites, 62 tests). `AUTHORIZE_FRONTIER_STAGE` reuses ADR
0021's execution-time precondition recheck/worktree-fingerprint check/
one-active-operation-per-task guarantee unchanged. CLI:
`apoapsis review authorize-frontier-stage` (no turns/budget flag). UI:
confirmation panel shows the exact configured frontier model and turn/
patch/verification-run ceiling before confirming; never launches
automatically. New ADR 0022. 8 new tests in
`tests/test_review_frontier_stage.py` (eligibility transitions,
unavailable-frontier rejection, stale-worktree/duplicate-operation
rejection, successful and budget-exhausted stage runs) plus a new
background-worker/UI test in `tests/test_review_ui.py`; full suite passing.

### Done — Phase D1a: durable model-assisted new-task intake (ADR 0023)

Added `src/apoapsis/intake/` (`schema.py`/`store.py`/`execution.py`/
`recovery.py`/`worker.py`), structurally mirroring `review/`'s crash-safety
ledger exactly: `IntakeOperationRecord`/`IntakeOperationStore`
(`.apoapsis/intake-operations.db`) with the same
active-operation-per-task/duplicate-operation-id/never-silently-re-enter-
RUNNING guarantees as `ReviewOperationStore`. `prepare_intake_operation()`
allocates a deterministic `task_id`, creates the task row at `INTAKE` with
the exact verbatim request text, and durably records the operation --
synchronous, fast, model-call-free, HTTP-handler-safe. `run_intake_
operation()` takes only `operation_id`, marks it `RUNNING` before provider
construction, rechecks the task's identity/state/version fresh, then calls
the *unmodified* `SpecificationExtractor` with its existing one-bounded-
correction-attempt contract (ADR 0018) -- no second specification-
extraction implementation. A double failure stops deterministically at
`FAILED` (returned, not raised); any other exception marks the operation
`FAILED` and re-raises. Success reaches `SPEC_DRAFTED` through the
pre-existing edge, using the same `update_specification()`/`transition()`
calls `VerticalSliceRunner.run()` already uses --
`workflow/states.py` did not change, and approval still only ever uses the
existing, unmodified `apoapsis approve` / `ApoapsisUIService.approve_
specification()` transition. `recover_stale_intake_operations()` mirrors
`review.recovery` exactly, returning a stranded `INTAKE` task to
`HUMAN_REVIEW_REQUIRED` through an event `review.classify` doesn't
recognize (correctly `UNKNOWN`, `inspect_only`/`abandon` only) -- a
genuine reuse of the existing, unmodified review/abandon machinery for
crash recovery, not a new capability. CLI: `apoapsis intake
submit/inspect/recover` -- a full seam usable without `apoapsis ui`. New
ADR 0023. 20 new tests across `tests/test_intake.py`/
`tests/test_intake_cli.py`; full suite 403/403 passing, confirming
`apoapsis run`/`apoapsis task` and every existing suite are unaffected
(only additive changes).

### Done — Phase D1b: New Task UI screen (ADR 0023)

Replaced the prior disabled `#/new` placeholder with a real screen:
`ApoapsisUIService.submit_intake_operation()`/`intake_operation_status()`
mirror the review-operation service methods exactly (validate, durably
record via `prepare_intake_operation()`, hand off to a lazily-constructed
`IntakeWorker`, return immediately -- the service itself never calls a
model); `POST /api/intake/operations` / `GET /api/intake/operations/
<operation-id>` sit behind the same session/origin checks as every other
route. The New Task screen submits a natural-language request, persists
the operation id in `sessionStorage` (mirroring the review operation's
reconnect pattern), and polls through `recorded`/`running` to either
`pending_specification_approval` or `failed`/`ambiguous`. Once drafted, it
shows the provider role, extraction-attempt count, and audit-artifact
list, then links straight to the existing, unmodified task detail page for
the full candidate review and two-step approval -- no second approval
implementation was added. 15 new tests in `tests/test_intake_ui.py`
(service, HTTP security, duplicate/reconnect, ambiguous-operation
visibility, bundled-asset sanity); full suite 418/418 passing. Live-
verified end to end against a real local Ollama model in a browser: a
typed request reached `SPEC_DRAFTED` and was approved to `SPEC_APPROVED`,
confirmed via `apoapsis inspect`'s event log -- this live check caught a
real bug (`INTAKE_OPERATION_STAGE` referenced before being defined) that
the deterministic suite had not, since it never executes `app.js`; fixed
before commit.

### Priority C — extend the accepted application shell (ADR 0014)

The application now has local/offline assets, a capability-protected loopback
API, real task/report/environment/evaluation/plan/review views, optimistic
specification and plan approval, durable Human Review operations with
bounded continuation, crash recovery, explicit fresh-frontier-stage
authorization, and durable new-task intake (CLI, service, and UI). It can
control existing work safely and durably draft and approve a new task's
specification, start to finish, from the browser. The highest-value
remaining product gap is that it still cannot *execute* a new task after
approval.

Continue in this order:

1. Done (ADR 0023): durable, resumable model-assisted task intake, stopping
   at `SPEC_DRAFTED`, with both a CLI/service seam (Commit D1a) and a New
   Task UI screen (Commit D1b) reusing the existing, unmodified
   specification-approval action.
2. After optimistic specification approval, launch the already-approved task
   through a durable worker. Project progress from workflow events/operation
   records; browser code must not infer state, run a CLI subprocess, or own a
   provider. A disconnect must not grant, cancel, duplicate, or repeat work.
3. Then add an approved-plan-to-single-slice bridge under its own ADR. Compile
   one explicitly selected ready slice into an immutable execution package,
   recheck the plan/repository/dependency fingerprints, obtain explicit user
   approval, and run the normal bounded agent protocol. Suggested paths and
   symbols remain advisory; never auto-start the next slice or add an autonomous
   scheduler.
4. Evaluate one substantial task monolithically versus plan-then-slices under
   identical model/settings. Compare held-out true success, false success,
   turns, patch/verification attempts, transmitted context, latency, and hosted
   calls/cost before claiming Architect Mode improves outcomes.
5. Only then choose a packaged native wrapper for the proven loopback surface.

Keep `src/apoapsis/ui/application.py` as the authority boundary. Browser code
must not call providers, construct CLI commands, parse files into invented
state, or decide verification/completion.

### Priority D — operational proof and packaging

- Run the live-gated Docker success-path test with a pinned local image.
- Exercise `START_APOAPSIS.cmd` and `STOP_APOAPSIS.cmd` on the supported Windows
  setup; keep model endpoints loopback-only.
- Decide how a future native wrapper locates Python, Git, ripgrep, Ollama, and
  Docker without weakening `apoapsis doctor` or silently installing software.
- Add packaging only after resumable intake and review commands prove the
  application service; do not hide prerequisites or auto-download models/images.

## Always preserve these boundaries

- Models propose typed actions or patches; they never receive direct shell,
  filesystem, Git, network, workflow, retry, verification, completion, or audit
  authority.
- Verification and the held-out oracle are separate; oracle failures never
  become repair context.
- Hosted spend requires explicit provider configuration.
- Start/Stop manages only configured loopback Ollama models.
- No autonomous agent swarms, general-purpose work automation, or decorative UI
  that obscures whether a result was verified.
