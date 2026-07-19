# Apoapsis next steps

This is the practical roadmap after the completed Apoapsis 1.0 implementation.
`HANDOFF.md` remains the canonical architecture and project-status record;
`AGENTS.md` remains mandatory instructions for coding models.

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

**Not yet done: re-running `local-strict` live under these fixes.** That
is the natural next step -- three fresh Qwen3-Coder-Next Q4 attempts at
64k, identical conditions, comparing specification-correction behavior,
acceptance-failure evidence, whether the model edits after seeing the real
error, strict completion, held-out correctness, false success, human
review, turns/verification attempts/tokens/latency against the first
run. Do not begin it without explicit direction.

### Priority B — review and resume experience

The highest-value product gap is a polished continuation path for tasks that end
in human review. Build this as deterministic control-plane functionality:

- list stopped tasks and their exact stop reason;
- show active constraints, current diff, policy findings, verification failures,
  budgets consumed, and remaining authorized options;
- allow explicit user choices such as retry with a configured remaining budget,
  authorize a configured frontier stage, abandon, or inspect only;
- append every decision to the existing audit/event record;
- never let a model choose a transition, command, retry ceiling, or completion.

Add an ADR before changing workflow/resume semantics. Cover each branch with a
fake provider and keep the one-shot baseline intact.

### Priority C — extend the accepted application shell (ADR 0014)

The first UI slice is complete: local/offline assets, a capability-protected
loopback API, real read-only task/report/environment/evaluation views, and
optimistic specification approval have deterministic integration and visual
coverage. CLI and UI approval produce the same persisted transition record.

Continue in this order:

1. Extract model-assisted task intake into a resumable application service. Do
   not keep a CLI input callback or HTTP request open while a model is running.
2. Persist a pending-approval operation and reconnect it to the existing
   specification extractor, provider telemetry, audit package, and exact
   verbatim-constraint checks.
3. Implement the explicit review/resume options from Priority B as typed service
   commands with optimistic versions and allowed-transition validation.
4. Add task execution progress through persisted events or a durable operation
   record; browser disconnects must not grant, cancel, or repeat authority.
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
