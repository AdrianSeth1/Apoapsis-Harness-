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

**Done, 2026-07-20.** Both the fail-closed path and the success path are now
live-proven. Docker's fail-closed path was already live-proven; the success
path was proven under explicit `LIVE DOCKER AUTHORIZED` authorization (image
`python:3.12-slim`): pulled once, pinned to its exact digest
(`sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`),
`apoapsis doctor` and the five gated live-Docker tests all passed for real
against a genuine Docker Desktop engine (network denial, read-only isolation,
worktree-copy mutation detection, verified timeout removal, trivial pass), no
container left running afterward. Follow ADR 0009 and the Docker instructions
in `HANDOFF.md` to reproduce. Full evidence:
`docs/evaluation/apoapsis-d5a-live-docker-evidence-2026-07-20.md`.

Earlier the same day, a read-only D5a inventory found Docker CLI installed
(29.5.2) but Docker Desktop's engine not responding, and hardened
`apoapsis doctor`'s sandbox diagnostics with their first deterministic test
coverage -- see ADR 0009's D5a amendment and
`tests/test_doctor.py::DoctorVerificationBackendTests`.

### 4. Add hosted-frontier evidence only when desired

When real API credentials and pricing are configured, run paired identical
local-first and direct-frontier lanes. Preserve the complete comparison and
aggregate artifacts. Until then, hosted rescue and savings must remain
`unmeasured`; subscription access must not be represented as API access.

D5b (2026-07-20, ADR 0030) added a required, hard-enforced aggregate spend
ceiling for any such run: `apoapsis eval --lane frontier --max-hosted-spend-
usd <AMOUNT>` refuses outright without the flag, refuses before any lane
starts if the configured worst-case allowance exceeds it, and stops the
whole invocation if real spend ever breaches it mid-run. `apoapsis doctor`
now also warns when a configured hosted model's pricing is left at $0. No
live hosted call has been made -- this is readiness/infrastructure only,
built and tested exclusively against fake providers. See ADR 0030.

### 5. Use and review the first local application slice

Run `apoapsis ui` from an initialized project. The offline black/orange/purple
interface now shows real project, task, specification, event, report,
evaluation, and model-configuration data. Specification approval is live and
uses the same optimistic transition/event record as the CLI. Opening the UI does
not load or prompt a model; Doctor runs only when explicitly selected.

Natural-language model-assisted intake (New Task), post-approval execution
orchestration (Control room), and review/resume choices (Human Review queue)
are all live from the browser now, each behind the same durable, crash-safe
operation ledgers and two-step confirmations the CLI uses. Only plan-slice
execution and native desktop packaging remain intentionally unavailable; use
the CLI for those until their own deterministic application services are
built.

### Done -- manual subscription-based frontier coding handoff (ADR 0031)

Added a second, distinct path for reaching a frontier model from a stopped
`HUMAN_REVIEW_REQUIRED` task, alongside (never replacing) the existing
automated API frontier path: `apoapsis frontier-manual export/import/
inspect/approve/apply/status`. Export writes an immutable, hashed package
(bound to task id/version, worktree fingerprint, approved specification/
constraints, current diff, relevant failure evidence, the verification
catalog, and the exact response JSON schema) plus a self-contained
`FRONTIER-CODING-HANDOFF-<package_id>.md` the user uploads by hand to their
own ChatGPT/Claude subscription session -- no website automation, no stored
or reused subscription credential, ever. Import rechecks task version,
eligibility, worktree fingerprint, package hash, active-operation conflicts,
response size, patch parsing, and patch policy, then creates a preview
only. Applying requires two explicit steps (approve, then a real
`MANUAL_FRONTIER_HANDOFF` review operation, reusing the existing
`ReviewOperationStore`/lease/recovery machinery unchanged) and only a
passing verification run (through the same `VerificationRunner` every
other path uses) ever reaches `COMPLETE`. A failed apply is eligible for a
small, deterministic, configurable number of repair rounds
(`[manual_frontier] max_repair_rounds`, default 2) using the real failure
evidence -- never an unbounded conversation. 22 new deterministic tests
(`tests/test_manual_frontier.py`); full suite 623/623 passing. See ADR 0031
and `HANDOFF.md`'s "Manual subscription-based frontier coding handoff"
section for full detail.

**Not done in this milestone**: a local UI surface for this path (CLI/
service only so far). The exact next UI seam is a Human Review case-detail
action alongside `authorize_frontier_stage`, following the identical
two-step-confirmation/background-worker/polling pattern ADR 0020 Commit C2
already established.

### Done -- local-first Architect Mode discovery and frontier planning handoff (ADR 0032)

Added a bounded workflow in front of Architect Mode planning: the user
enters an idea, a configured local model may propose up to a small,
harness-enforced number of clarification questions, the user answers
verbatim, and the local model may propose an `IdeaBrief` (`extra="forbid"`,
no status field) that only the user can approve. Only after that approval
does Apoapsis build an immutable `FrontierPlanningRequestPackage` (approved
brief, verbatim Q&A, active hard constraints, verification catalog,
Architect Mode ceilings, the complete plan/response schemas) and send it to
a frontier model over either an explicitly configured, spend-ceilinged API
transport (`apoapsis discover call-api`, reusing ADR 0030's
`SpendCeilingModelProvider` unmodified) or a manual subscription transport
(`apoapsis discover import-manual-response`, one self-contained
`FRONTIER-PLANNING-HANDOFF.md`, never automating a website, tokens/cost
always `unmeasured`). The frontier stage may return a bounded set of
clarification questions (capped at `[discovery] max_frontier_clarification_
rounds`, default 2) or a complete plan; a returned plan calls
`SQLitePlanStore.create_plan()` directly -- the exact function
`architect.importer.import_planner_response()` already calls -- so it
becomes an entirely ordinary plan the existing, completely unmodified
`apoapsis plan validate`/`apoapsis plan approve` commands work on
unchanged. 20 new deterministic tests (`tests/test_discovery.py`); full
suite 643/643 passing. See ADR 0032 and `HANDOFF.md`'s "Local-first
discovery and frontier planning handoff" section for full detail.

**Not done in this milestone**: a local UI surface for this path (CLI/
service only so far, mirroring ADR 0031's own current scope). The exact
next UI seam is described in ADR 0032's Non-goals.

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

### Done — Phase D2a: durable post-approval task execution (ADR 0024)

Inventoried `VerticalSliceRunner.run()`'s phases directly from the code,
then split it at the `SPEC_APPROVED` boundary into a shared
`_run_from_approved()` continuation (research, context compilation,
routing, worktree creation, the selected coding stage with escalation,
verification, reporting -- moved verbatim, not reimplemented) plus a new
public `execute_approved_task(task_id)` entry point. The full existing
test suite (418 tests) passed unchanged against the refactor before any
new code was added, confirming `apoapsis run` and one-shot mode are
byte-for-byte preserved. Added `src/apoapsis/execution/operation_*`
(`schema`/`store`/`service`/`recovery`/`worker`), structurally mirroring
the review/intake operation ledgers exactly: `ExecutionOperationRecord`
carries the task id, the task version and repository HEAD observed at
preparation time, and status; `prepare_execution_operation()` requires
`SPEC_APPROVED` at an exact version and captures HEAD; `run_execution_
operation()` marks `RUNNING` before provider construction, rechecks
task state/version *and* repository HEAD (catching a parent-repository
commit landing in the gap between approval and a queued operation's
actual start), then calls `execute_approved_task()`. The operation is
marked `SUCCEEDED` for *any* deterministic task outcome (`COMPLETE`,
`FAILED`, or `HUMAN_REVIEW_REQUIRED` are all legitimate) -- only a crash
marks the operation itself `FAILED`. Crash recovery mirrors review/intake
exactly and, critically, never touches the task's worktree: a stale
`RUNNING` operation becomes `AMBIGUOUS`, and a task stuck anywhere between
`SPEC_APPROVED` and a terminal state is returned to `HUMAN_REVIEW_REQUIRED`
(every intermediate state already has that edge) for inspection/abandon
through the existing, unmodified review machinery. CLI: `apoapsis execute
start/inspect/recover`. New ADR 0024. 15 new tests in `tests/
test_execution_operations.py`; full suite 433/433 passing.

### Done — Phase D2b: control-room UI (ADR 0024)

`ApoapsisUIService.task_detail()` gained three read-only fields:
`execution_preview` (route/models/budgets/completion-policy/sandbox,
computed with the exact same `select_agent_route()` the real service
uses), `active_execution_operation` (via `find_active_for_task()` -- the
primary reconnect mechanism, needing no `sessionStorage`, unlike review/
intake's operation panels), and `recent_agent_turns` (the last 20 turn
records parsed directly from the `agent-turn-*.json`/`frontier-agent-
turn-*.json` files the bounded agent already writes incrementally --
genuine live progress while an operation is still `RUNNING`).
`_available_actions()` now returns `["start_execution"]` at
`SPEC_APPROVED`. `submit_execution_operation()`/`execution_operation_
status()` mirror the review/intake service methods exactly; `POST
/api/tasks/<id>/execute` / `GET /api/execution/operations/<operation-id>`
sit behind the same session/origin checks as every other route. The
control room (`#/task/<id>/control`) adds a two-step "Start coding"
confirmation showing the full preview, background submission with
automatic reconnect, a live tool-action feed, a usage/telemetry panel
once a report exists, and a direct link into the existing, unmodified
Human Review case detail view when a task stops there.

Live-verified end to end in a real browser against a real local Ollama
model: "Start coding" showed an accurate preview and ran a real bounded
local-agent session that stopped for human review with an accurate error
message; the new "Open the Human Review case" link correctly opened the
real case detail view. **This live check found and fixed a second real,
pre-existing bug** the deterministic suite could never have caught (it
never executes `app.js`): two unrelated functions were both named
`reviewView`, so the top-level `#/review/<task-id>` route always executed
the wrong one and crashed -- Human Review navigation had been silently
broken since whichever earlier change introduced the second definition.
Fixed by renaming the task-page sub-tab placeholder to
`taskReviewTabView(detail)`. 19 new tests in `tests/test_execution_ui.py`;
one existing `tests/test_ui.py` assertion updated to reflect
`start_execution` correctly appearing at `SPEC_APPROVED`. Full suite:
452/452 passing.

### Done — Phase H3: operation lease and recovery integrity (ADR 0025)

Auditing all three durable operation ledgers (review/ADR 0020-0021,
intake/ADR 0023, execution/ADR 0024) against real crash scenarios found
two shared weaknesses: a `RECORDED` operation that crashed before ever
being enqueued sat forever until an unrelated new submission happened to
lazily construct that operation type's worker (whose own first-construction
recovery scan then raced the caller's own explicit enqueue, double-
scheduling it); and `RUNNING` staleness was judged purely by a last-write
timestamp plus a fixed window (e.g. 15 minutes), so a genuinely healthy,
long-running agent session looked indistinguishable from a crashed one.

New, deliberately shared module `src/apoapsis/operations/lease.py` (the
one intentional exception to this codebase's usual review/intake/execution
mirroring convention): atomic `claim_lease()`/`renew_lease()`/
`release_lease()`/`expire_lease_to_ambiguous()`, each a single guarded
`UPDATE ... WHERE` -- never read-then-write -- plus `LeaseHeartbeat`, a
daemon-thread ticker renewing on a fixed wall-clock interval independent of
how long the underlying model call actually takes. All three operation
records gained `lease_owner_id`/`lease_expires_at` (additive migration).
`mark_running()` now requires `owner_id` and claims the lease;
`mark_succeeded`/`mark_failed`/`mark_pending_approval` require the same
`owner_id` and raise `LeaseLostError` if a different owner or recovery
already won the row. Recovery reads each `RUNNING` record's own
`lease_expires_at` (injectable `now=` for deterministic tests) instead of
a fixed `running_expiry` window -- a long-but-healthy operation, renewed by
its own heartbeat, survives arbitrarily many former staleness windows; a
lease that stops renewing is reclaimed and marked `AMBIGUOUS` exactly once,
with the original owner permanently locked out afterward. A legacy
`RUNNING` row with `NULL` lease columns is treated as unconditionally
expired -- fail closed.

`ApoapsisUIService.start_background_workers()` is new; `create_ui_server()`
calls it immediately after construction, eagerly starting all three
operation workers (each running its own startup recovery pass) before the
HTTP server accepts a request -- closing both the "recovery never runs
until an unrelated submission" gap and the duplicate-enqueue race
structurally, since the worker now always already exists by the time any
record is prepared. `apoapsis review/intake/execute recover` stays
report-only by default; a new `--resume-recorded` flag makes running
reclaimed work an explicit, opt-in, foreground CLI action.

New `tests/test_operation_lease.py` (22 tests: the shared primitives
directly, `LeaseHeartbeat` on injected millisecond intervals, and one
shared parametrized base class proving review/intake/execution all use
identical semantics). `tests/test_execution_ui.py` gained a test proving
`start_background_workers()` alone reclaims and completes a stranded
operation with no submission call. `tests/test_intake_cli.py` gained two
tests for `--resume-recorded`. Full suite: 478/478 passing, 6 intentional
skips. See ADR 0025 and `HANDOFF.md`'s "Operation lease and recovery
integrity" section for full detail.

### Done — Phase H4: immutable execution authorization and truthful live UI (ADR 0026)

The two-step "Start coding" confirmation showed a preview but nothing tied
the shown preview to the executed reality: the task, specification,
repository state, or configuration could drift between preview and
confirmation (or between recording an operation and a worker actually
running it) and the operation would still run as if nothing changed.
Separately, two real bugs existed: `_run_from_approved()` compiles initial
context from the parent checkout while the task worktree is created from
clean HEAD, so an uncommitted parent change could make the two silently
disagree; and the control room's poll loop only refreshed persisted events/
turns once an operation reached a terminal status, leaving the live-progress
view frozen for the entire RUNNING duration.

New `src/apoapsis/execution/authorization.py`:
`build_execution_authorization_package()` deterministically computes what a
confirmation would authorize (task/spec hash, full parent-repository
fingerprint, effective-config hash, predicted route, provider/model names,
budgets, completion policy, verification backend/catalog/hash, authority
rules, and a `package_sha256` excluding `operation_id`/`generated_at`) --
called identically from the UI preview, `prepare_execution_operation()`
(which persists the hash), and `run_execution_operation()` (which
recomputes and rejects on mismatch before any provider construction).
`VerificationCommand.environment` values are never serialized or hashed in
raw form. New `src/apoapsis/repository/readiness.py`:
`require_clean_parent_repository()` fails closed (never stashes, resets, or
commits automatically) when the parent repository is dirty, called before
`_build_providers()`. `app.js`'s poll loop now refreshes live progress on
every tick while RUNNING, and turn ordering is fixed to real execution order
(local before frontier, not alphabetical).

**A live-browser pass against a real local Ollama model caught a real gap
the deterministic suite alone did not**: the backend authorization checks
were written and tested first, but `app.js`'s confirmation button never
actually sent the new required field, so every real "Start coding" click
would have failed with a 400. Fixed before this phase closed, with a
bundled-asset regression test guarding against it recurring silently. The
full pass (New Task → approval → Start coding → live running progress →
Human Review navigation) succeeded end to end.

New `tests/test_execution_authorization.py` (11 tests) and
`tests/test_app_js_regression.py` (4 tests: duplicate-declaration and
route-dispatch static checks always run; Node syntax/boot smoke tests skip
cleanly when Node is unavailable). `tests/test_execution_ui.py` gained
preview-vs-confirm drift, turn-ordering, and bundled-asset-field tests. Full
suite: 497/497 passing, 6 intentional skips. See ADR 0026 and `HANDOFF.md`'s
"Immutable execution authorization and truthful live UI" section for full
detail.

### Done — Phase D3a: approved-plan to single-slice execution (ADR 0027)

ADR 0019 gave Apoapsis a planning foundation that deliberately executed
nothing; ADR 0024 gave it a durable execution service for already-approved
tasks. This phase is the bridge: turning one explicitly selected, approved
slice into a real, running task through the existing D2 service, with zero
duplicated routing/context/worktree/agent/patch/verification/escalation/
reporting logic.

New `src/apoapsis/architect/slice_schema.py`/`slice_package.py`/
`slice_store.py`/`slice_service.py`. `build_plan_slice_execution_package()`
deterministically compiles what approving one slice would authorize (no
model call, no task yet): requires the plan `APPROVED` at the exact expected
version, confirms the plan's originating request package still matches the
current repository, revalidates against *current* configuration, proves
every dependency slice, copies the slice's exact inherited hard constraints/
acceptance criteria verbatim, and hashes the result with a deterministic
derived-task id so repackaging an unchanged slice reproduces the same hash.

**A real design subtlety was caught by the test suite itself, not assumed
away**: proving a dependency slice's work has actually landed can't just
check the dependency's task reached COMPLETE, since Apoapsis never auto-
merges or auto-commits a worktree. A first attempt used git ancestry against
`WorktreeManager.describe()`'s `base_commit` field -- which turned out to
mean the worktree's *current* HEAD, not its original creation-time base,
making the check trivially always true regardless of whether anything was
ever committed. Fixed by reading the dependency's *true* original base
commit from its own `ExecutionOperationRecord.expected_repository_head`
instead. A human must commit, then merge, a completed dependency's work
through their own ordinary git workflow before a dependent slice can be
packaged -- Apoapsis proves this happened, never does it itself. This also
means there is no shared "plan workspace" to build or to accidentally
damage when abandoning one slice: every slice gets its own, completely
independent, unmodified D2 worktree.

Slice status (`PACKAGED`/`APPROVED` are the only two values actually
persisted) is otherwise always a live projection from the derived task's
real, current workflow state -- never a second, independently-drifting
copy of it, closing off an entire class of staleness bugs before they could
exist. CLI: `apoapsis plan slice list/inspect/package/approve/status/start`.

New `tests/test_architect_slice.py` (17 tests) covering package determinism,
revalidation/repository-identity/stale-version rejection, the full
dependency-evidence matrix, exact constraint/criterion propagation,
advisory-path freedom, duplicate-approval/start rejection, and status
projection from real task state. Full suite: 514/514 passing, 6 intentional
skips. See ADR 0027 and `HANDOFF.md`'s "Approved-plan to single-slice
execution" section for full detail.

### Done — Phase D3b: the Plans UI slice experience (ADR 0027)

Adds the browser surface on top of D3a with zero new execution, routing, or
completion logic: `plan_detail()` and a new `plan_slice_detail()` compose
live per-slice status, the latest immutable package, and the derived task's
own state from the exact service functions D3a already built and tested.
The Implementation Slices tab shows real status; Inspect renders the
immutable package preview (exact inherited constraints/criteria,
verification commands, dependency evidence, advisory hints) behind a
"Package this slice" action, then a two-step Approve action mirroring ADR
0026's own preview/confirm discipline. Once approved, the view links to the
derived task's existing, unmodified control room, changes view, and report
view rather than duplicating any of that machinery -- no "Run all" button,
no scheduler, anywhere in this surface.

New `tests/test_architect_slice_ui.py` (12 tests). Full suite: 526/526
passing, 6 intentional skips. Verified live in a real browser end to end:
Plans list -> plan detail -> Implementation Slices tab -> Inspect -> package
preview -> two-step approve -> derived-task links -> the existing control
room correctly recognizing the slice-derived task at `SPEC_APPROVED` with
its normal "Start coding" action present, untouched. See ADR 0027's D3b
addendum and `HANDOFF.md`'s "Plans UI slice experience" section for full
detail.

### Done — Phase D4a: planning comparison framework (ADR 0028)

Builds the deterministic monolithic-versus-planned comparison framework
Priority C item 4 (below) calls for, and the fixture it needs. A new,
physically separate `examples/download-service-v2/` fixture (an extension
of the ADR 0012 fixture family, not a second unrelated one -- the original
`download-service` scenario's files, checks, oracle, and historical
evaluation path stay byte-for-byte untouched) has a real 3-slice dependency
DAG (job-record bookkeeping, resilient downloading, and an integrating
service that depends on both) with per-slice dev/acceptance checks and a
held-out cross-slice oracle covering adversarial cases none of them see
alone.

Both conditions use `STRICT` completion policy -- a documented deviation
from every other evaluation lane's forced `BASELINE` -- so each slice's own
inherited acceptance criterion gates it on exactly its own command, never
on an unrelated slice's not-yet-implemented one. Building this end to end
surfaced and fixed a real, previously-latent bug in ADR 0027's own "one
active slice per plan" check, which incorrectly blocked approving a second
slice forever rather than only while the first was genuinely still
running -- a live product user would have hit the same bug sequentially
working through any multi-slice plan.

`run_monolithic_condition()`/`run_planned_condition()` (new
`evaluation/planning_harness.py`) compare a single-shot attempt against the
exact, unmodified D3a slice-execution functions, advanced automatically
only inside this evaluation-only module and gated on an already-approved
fixed plan -- auto-advance is never reachable from the product CLI/UI.
`summarize_planning_comparisons()` computes true completion, false
success, per-slice outcomes, resource totals, and cross-slice integration
failure from persisted reports only. New `apoapsis eval-planning` CLI
command. New `tests/test_planning_evaluation.py` (10 tests). Full suite:
536/536 passing, 6 intentional skips.

**No live model evidence anywhere in this commit** -- every test uses a
deterministic fake provider. Commit D4b (live local evidence against a
genuinely externally-produced plan, obtained by manually pasting the
exported planner package into a separate chat session) is deliberately
deferred to its own review. See ADR 0028 and `HANDOFF.md`'s "Planning
comparison framework" section for full detail.

### Done — Phase D4b: live evaluation evidence (2026-07-20)

Three monolithic and three planned live attempts against
`qwen3-coder-next:q4_K_M`, using a plan from a genuinely separate model
session (Gemini 3.1 Pro), corrected once (a real, blocking plan-quality
defect) before approval. **0/6 completions** -- every attempt, both
conditions, stopped at `HUMAN_REVIEW_REQUIRED` after exhausting its turn
budget having never once called a verification command (one edit, then a
`read_file` loop). A repeatable model-logic failure, not a harness,
specification, or oracle defect -- every mechanical part of the framework
behaved correctly. No completion-rate or Architect-Mode-advantage claim is
supported by this round; both conditions failed identically for the
identical reason. See `docs/evaluation/apoapsis-planning-comparison-
2026-07-20.md` for the full breakdown. Next: investigate the model's
read-loop behavior directly before re-running this comparison -- six
attempts against a model that never calls a verification tool cannot
distinguish "planning helps" from "planning doesn't help."

### Done — D4c: forensic diagnosis, diagnostic-probe infrastructure, and live evidence (ADR 0029, 2026-07-19/20)

A read-only forensic pass over all six preserved D4b turn/call artifacts
found the loop's exact shape (a byte-for-byte identical repeated
`read_file` action after the one accepted edit) and, critically, that it
is **absent from every other preserved live Qwen3-Coder-Next Q4 session**
(ten sessions across `local-strict-*`/`smoke-local`/`priority-a-64k*`/
`128k*`) -- the model reliably calls verification elsewhere, so this is
fixture/prompt-specific, not a general capability gap. Two independent,
non-causal issues were found and *deliberately not fixed* here (see ADR
0029's "Deferred follow-ups"): `search_repository`'s raw `[WinError 2]`
(no lexical fallback, unlike the context compiler's own; confirmed
unrelated to the loop by a clean natural control), and an unlabeled
stale/fresh evidence duplication after an edit (confirmed present in a
prior *successful* session too). Full detail:
`docs/evaluation/apoapsis-d4c-forensic-diagnosis-2026-07-19.md`.

Built evaluation-only infrastructure (`src/apoapsis/evaluation/
diagnostic_probe.py`/`diagnostic_probe_report.py`, new `apoapsis
eval-planning-probe` CLI command) to run a single already-approved,
dependency-free plan slice once, varying only one independent variable at
a time:

- **Probe 2** (`--prompt-condition progress_advisory`): the exact,
  unmodified production prompt plus one short, explicitly advisory (never
  action-forcing) note. Must run against the project's own configured
  coding model -- an alternate model is rejected.
- **Probe 3** (`--alternate-model NAME --authorize-alternate-model NAME`):
  a different, already-installed local coding model under the unchanged
  production prompt, with every other decoding/config setting inherited
  unchanged. Fails closed on three independent conditions before any
  provider is built: the alternate model must genuinely differ from the
  project's already-configured one, must be explicitly authorized by the
  caller, and must actually be installed right now.

A review pass after the first implementation found and fixed two real
gaps, both now covered by dedicated regression tests: (1) the one-
independent-variable invariant wasn't actually enforced anywhere --
`validate_single_independent_variable()` now rejects `progress_advisory`
paired with an alternate model, checked both by the orchestration
function itself (first statement, before any I/O) and independently by
the CLI on the raw arguments (before any filesystem access); and (2)
`first_no_progress_turn` could misfire on a legitimate post-edit reread
that added real new evidence, because its `(action, summary)` text is
identical to the pre-edit read (`summary` only encodes the path/line-
range, not file content) -- it now also requires `evidence_ids` to be
empty, with a regression test encoding the exact sequence (initial read
-> edit -> fresh reread with new evidence -> identical reread with none;
the fourth turn, not the third, is now correctly the first no-progress
turn). `--context-profile` was also removed from this command entirely
(it was a candidate for a second, unrecorded independent variable) --
this narrowly scoped probe always inherits the project's baseline
configuration, including context window, unchanged.

Touches product code only via two additive, default-safe constructor
parameters (`BoundedAgentSession`/`VerticalSliceRunner` both gained an
optional `agent_step_prompt_fn`, defaulting to exactly today's behavior)
-- proven inert when omitted by dedicated regression tests, not merely
asserted. `_AGENT_STEP_STATIC_PREFIX`, the action schema, retry budgets,
workflow transitions, and completion authority are all untouched; models
remain untrusted proposers and Apoapsis alone still validates/executes/
rejects every requested action.

28 tests in `tests/test_diagnostic_probe.py` (all deterministic fake
providers or pure functions). Full suite: 564/564 passing, 6 intentional
skips. See ADR 0029.

**Live evidence (2026-07-20).** Probe 2 and a production-condition
control have each been run once against `qwen3-coder-next:q4_K_M` on
`SLICE-JOBS-001`:

```powershell
# Probe 2 -- run 2026-07-20. 8 turns, one v2-jobs-tests run (passed),
# AC-JOBS-STATE proven, COMPLETE. 53,039 input / 876 output / 0 cached
# tokens, 151.4s. Artifact: .apoapsis-eval\d4c-probe2-output\diagnostic-probe.json
apoapsis eval-planning-probe download-service-v2 `
  --plan-id PLAN-51DCC9E12110 --expected-plan-version 3 `
  --planned-project-root .apoapsis-eval\d4c-probe2-project `
  --slice-id SLICE-JOBS-001 `
  --prompt-condition progress_advisory `
  --output-dir .apoapsis-eval\d4c-probe2-output

# Production-condition control -- run 2026-07-20, same model/slice, run
# through this same probe infrastructure. 5 turns, one v2-jobs-tests run
# (passed), COMPLETE. 31,965 input / 803 output / 0 cached tokens, 109.4s.
# Artifact: .apoapsis-eval\d4c-probe-control-output\diagnostic-probe.json
apoapsis eval-planning-probe download-service-v2 `
  --plan-id PLAN-BB5F0E22CF0F --expected-plan-version 3 `
  --planned-project-root .apoapsis-eval\d4c-probe-control-project `
  --slice-id SLICE-JOBS-001 `
  --prompt-condition production `
  --output-dir .apoapsis-eval\d4c-probe-control-output
```

Both escaped D4b's read loop and reached real `COMPLETE`. The production
control succeeded **without** the advisory prompt and in fewer turns, so
these two observations give no basis for changing the production prompt
or for attributing either success to the advisory note -- they do show
this model can solve and verify this slice, so D4b's read loop is not a
hard capability limitation. The contrast with D4b's 0/6 remains
unexplained. Covers only `SLICE-JOBS-001`; no completion rate,
reliability rate, or planning advantage is claimed. Full detail:
`docs/evaluation/apoapsis-d4c-forensic-diagnosis-2026-07-19.md`'s
live-evidence addendum.

**Probe 3 has not been run.** The exact proposed command, still gated on
your separate explicit authorization:

```powershell
# Probe 3 -- unchanged production prompt, a different already-installed
# local model (e.g. qwen3-coder:30b, already pulled on this machine),
# same slice. Requires --authorize-alternate-model to exactly match.
apoapsis eval-planning-probe download-service-v2 `
  --plan-id PLAN-... --expected-plan-version N `
  --planned-project-root .apoapsis-eval\d4c-probe3-project `
  --slice-id SLICE-JOBS-001 `
  --prompt-condition production `
  --alternate-model qwen3-coder:30b --authorize-alternate-model qwen3-coder:30b `
  --output-dir .apoapsis-eval\d4c-probe3
```

Do not run this command, re-run the full D4b comparison, or begin D5
without separate, explicit authorization. The two deferred defects
(`search_repository`'s `[WinError 2]`, unlabeled stale/fresh evidence
duplication) remain unresolved and were not touched by these runs.

### Priority C — extend the accepted application shell (ADR 0014)

The application now has local/offline assets, a capability-protected loopback
API, real task/report/environment/evaluation/plan/review views, optimistic
specification and plan approval, durable Human Review operations with
bounded continuation, crash recovery, explicit fresh-frontier-stage
authorization, durable new-task intake (CLI, service, and UI), and durable
post-approval execution (CLI, service, and UI). A user can now go from a
typed natural-language request to a completed (or Human-Review-stopped)
task entirely from the browser.

Continue in this order:

1. Done (ADR 0023): durable, resumable model-assisted task intake, stopping
   at `SPEC_DRAFTED`, with both a CLI/service seam (Commit D1a) and a New
   Task UI screen (Commit D1b) reusing the existing, unmodified
   specification-approval action.
2. Done (ADR 0024): durable post-approval execution, reusing
   `VerticalSliceRunner`'s existing implementation exactly, with both a
   CLI/service seam (Commit D2a) and a control-room UI screen (Commit D2b)
   projecting live progress from persisted workflow events/operation
   records -- browser code never infers state, runs a CLI subprocess, or
   owns a provider.
3. Done (ADR 0027): an approved-plan-to-single-slice bridge, with both a
   CLI/service seam (Commit D3a) and a Plans UI slice experience (Commit
   D3b). Compiles one explicitly selected ready slice into an immutable
   execution package, rechecks the plan/repository/dependency fingerprints
   (git-ancestry proof, not a trusted status flag), obtains explicit user
   approval, and hands off to the exact same durable execution service every
   other task uses. Suggested paths and symbols remain advisory; nothing
   auto-starts the next slice or adds an autonomous scheduler.
4. Done (ADR 0028, Commits D4a/D4b): the deterministic monolithic-versus-
   planned comparison framework, its `download-service-v2` fixture, and a
   first live evaluation round (2026-07-20, 0/6 completions -- a
   repeatable model-logic failure, not a harness defect; see
   `docs/evaluation/apoapsis-planning-comparison-2026-07-20.md`). Still no
   basis for a completion-rate or Architect-Mode-advantage claim: both
   conditions failed identically for the identical reason. Done (ADR
   0029, D4c): a forensic diagnosis of that failure (fixture/prompt-
   specific, not a general model-capability gap -- see
   `docs/evaluation/apoapsis-d4c-forensic-diagnosis-2026-07-19.md`),
   evaluation-only single-slice diagnostic-probe infrastructure
   (`apoapsis eval-planning-probe`), and two completed live observations
   (2026-07-20, `SLICE-JOBS-001`): a `progress_advisory` probe and an
   unmodified-production control, both `COMPLETE`, both escaping the read
   loop. The production control succeeded without the advisory prompt, so
   neither observation supports a prompt change or a causal claim; they
   do show the model can solve and verify this slice. The contrast with
   D4b's 0/6 remains unexplained. **Not yet done**: Probe 3 (alternate
   model, gated on explicit authorization) and re-running the full
   comparison, which stays blocked pending further investigation of that
   unexplained contrast.
5. Only then choose a packaged native wrapper for the proven loopback surface.

Keep `src/apoapsis/ui/application.py` as the authority boundary. Browser code
must not call providers, construct CLI commands, parse files into invented
state, or decide verification/completion.

### Priority D — operational proof and packaging

- Done (D5a, 2026-07-20): hardened `apoapsis doctor`'s Docker sandbox
  diagnostics to distinguish CLI-missing/engine-unreachable/image-absent/
  digest-mismatch/successful-self-test, gave that check its first
  deterministic test coverage, and restructured the gated live-Docker test
  into five focused assertions (network denial, read-only isolation,
  worktree-copy mutation detection, verified timeout removal, trivial
  pass). See ADR 0009's D5a amendment.
- Done (D5a live evidence, 2026-07-20, `LIVE DOCKER AUTHORIZED`): pulled
  the one authorized image (`python:3.12-slim`), resolved and pinned its
  exact digest
  (`sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`),
  configured a disposable project with `--pull=never` and that pinned
  digest, and ran `apoapsis doctor` plus the five gated live-Docker tests
  for real -- all passed against a genuine Docker Desktop engine (`29.5.2`,
  Linux/WSL2): real network-connect denial, a real blocked write outside
  `/workspace`, a real in-container mutation correctly caught by
  `finalize()`, and real timeout-triggered removal independently confirmed
  via `docker ps`. No container left running afterward; no other image
  pulled; no Docker Desktop setting changed. See ADR 0009's live-evidence
  addendum and
  `docs/evaluation/apoapsis-d5a-live-docker-evidence-2026-07-20.md`.
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
