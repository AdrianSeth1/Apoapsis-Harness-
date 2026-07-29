# ADR 0070: Failure evidence reaches the Local Power repair model

- Status: Accepted
- Date: 2026-07-27

## Context

Live task `TASK-E01762481075` re-ran the Focus Orbit build with ADR 0069's
changes in place and `web-product-integrity` configured as a required,
acceptance-designated command. The harness behaved correctly throughout:

- 6 initial turns, 3 writes, 2 verification runs.
- The model ran `unit-tests`; it passed. It re-requested the identical check
  without changing a file; the harness refused it (ADR 0069). The model
  returned `finish`.
- Because only `unit-tests` had passed and not every required command, the
  controller correctly did **not** terminate at that point.
- Final verification ran both required commands. `web-product-integrity`
  found four unresolved element ids and thirteen dead style rules.
- Outcome: `HUMAN_REVIEW_REQUIRED`. No false `COMPLETE`. `main` stayed clean.

That is the intended behavior, and it is a real improvement: the same model
producing the same broken product now stops safely instead of reporting
success. But the generated application was still broken, and the repair
continuation did nothing about it.

The continuation is where the defect is. `RVOP-A6A7D5BCB9C14F7B93483C8E`
correctly carried the normalized `web-product-integrity` failure. The Local
Power prompt built from it did not. Inspecting `call-007-request.json`
confirms it: the string `web-product-integrity` appears only as a name in
`CONFIGURED_VERIFICATION_COMMANDS_JSON`, and none of the check's actual
output — no `ERROR app.js [mode-focus]`, no `unresolved` — appears anywhere
in the 52 KB prompt. The task directory contains no
`local-power-verification-failure-*.json` at all, because `LocalPowerSession`
never normalized a failure in the first place.

So the model was shown: a session history in which one check had been run and
had passed, a list of configured command names, and no failure output. It
re-ran `unit-tests` and reported that every check had passed. Given what it
could see, that was a reasonable inference. Apoapsis rejected completion
again, correctly, and no repair occurred — and none could have.

Three smaller gaps sit behind the same failure:

- The prompt never distinguished "passed for this code" from "passed earlier,
  before the code changed" from "never run". `SESSION_HISTORY_JSON` shows a
  passing check with no indication that a later edit invalidated it.
- Nothing told the model that re-running a currently-passing check cannot
  advance the task. ADR 0069 made the harness refuse such a request; it did
  not make the reason legible in advance.
- `finish` was accepted from a model that had neither attempted a repair nor
  run the outstanding check.

`BoundedAgentSession` has normalized failures into its own prompt since ADR
0011. The sandbox path was written without it and nobody noticed, because
until ADR 0069 introduced a check that could realistically fail while another
passed, the situation barely arose.

## Decision

### 1. Normalize every failing check and put it in the prompt

`LocalPowerSession._verify` now runs `FailureNormalizer` on any result that
did not pass, writes `{prefix}verification-failure-NNN.json`, and adds the
normalized output to the observation ledger as
`<verification:COMMAND_NAME>` evidence, so it renders inside
`REPOSITORY_EVIDENCE` on every subsequent turn.

An acceptance-designated command's failure counts even when the aggregate
status stays `PASSED` because the command is `required = false` (ADR 0018) —
that is exactly the contract shape ADR 0069 encourages, so it must not be the
shape whose failures go unreported.

Evidence carries `TransmissionPolicy.LOCAL_ONLY`, matching every other
sandbox observation. The strict loop marks its failure evidence
`CLOUD_ALLOWED`; the sandbox path is local-model-only by construction, and
widening transmission policy is not this ADR's decision to make.

### 2. Seed a resumed session with what failed before it

`LocalPowerSession.resume` now calls `_seed_prior_failure_evidence` over the
prior stage's verification results. A continuation exists *because* something
failed, so beginning one with no record of the failure is the single thing it
must not do.

Only the most recent unresolved failure per command is seeded, walking results
newest-first and skipping any command that a later result shows passing. An
older failure for a since-fixed command is noise, and noise in a repair prompt
is worse than silence.

### 3. State the verification position explicitly

Two new prompt blocks:

- `VERIFICATION_STATE_JSON` — per configured command: `required`,
  `acceptance_designated`, and a four-valued `state`
  (`passing_for_current_code`, `failed_for_current_code`,
  `passed_earlier_but_the_code_has_changed_since`, `never_run`).
- `OUTSTANDING_REQUIRED_COMMANDS_JSON` — the required commands with no passing
  result for the current worktree fingerprint, followed by one plain sentence
  naming them and pointing at their recorded output.

The four-valued state is the point. "Passed, but for code that has since
changed" and "passed, for this exact code" are different facts, and collapsing
them into "passed" is how a model comes to believe a stale result still
counts.

New completion rules tell the model that `VERIFICATION_STATE_JSON` is
authoritative over its own memory and over `SESSION_HISTORY_JSON`; that
re-running a currently-passing check is the same question against the same
code and therefore refused; and that a premature `finish` will be refused.

### 4. Refuse `finish` when nothing has been done about what is failing

`finish` is refused when all of the following hold:

- the sandbox contains at least one changed file (a session that built nothing
  has nothing to repair, and cannot complete anyway);
- some required command has no passing result for the current state;
- at least one of those commands has **no result at all** for the current
  state; and
- no file has been written or deleted since the last verification run.

The bar is deliberately low and mechanical. The model may finish as soon as it
has either changed something or actually run the outstanding command. It does
**not** have to succeed: running the check and failing is enough, because the
gate exists to stop a model ending the session without looking, not to demand
a repair it may be incapable of.

Refusals are capped at two. A model that insists it is finished after being
told twice will not be argued into repairing anything, and spending the rest
of its budget on the argument helps nobody — the harness runs its own final
verification either way, and the outcome will be human review.

### 5. Make the web check's output legible to the normalizer

`verify-web-product` now ends with `FAILED: web product integrity check -- N
error finding(s), M warning(s)` instead of a bare `FAIL`. `FailureNormalizer`
scans for `FAILED` when choosing a root error; `FAIL` matched nothing, so the
root error fell back to the last line of output. The full findings always
reached `relevant_error` regardless, but the summary line is what a reviewer
and the strict loop read first.

## Alternatives considered

**Recompile context on failure, as `BoundedAgentSession` does.** The strict
loop rebuilds its whole context package around the failure's locations. The
sandbox session has no `ContextCompiler` — it deliberately takes a compiled
package and only appends its own observations — and giving it one would be a
structural change well beyond this defect. Appending normalized failure
evidence to the observation ledger puts the same text in front of the model
through the existing seam.

**Refuse `finish` until the outstanding command actually passes.** Rejected.
It converts "the model cannot fix this" into "the model burns its entire
budget", and the harness already has a correct answer for an unfixable
failure: human review. The gate targets inattention, not incapacity.

**Say nothing and let the evidence speak.** Rejected. The evidence fix alone
would probably have been enough for this specific trial, but the four-valued
state costs almost nothing and removes an entire class of stale-result
reasoning that the transcript shows this model is prone to.

## Consequences

- A Local Power model in a repair continuation sees what failed, in the
  check's own words, with the files and lines it named.
- A stale pass can no longer be mistaken for a current one from the prompt.
- Ending a session while a required check has never been run now takes an
  explicit second and third attempt, and is recorded as refused each time.
- `local-power-verification-failure-NNN.json` joins the audit record.
- None of this makes the model better at implementing. It makes the
  information it needs available. Whether Laguna can act on it is the open
  question the next live rerun should answer, and this ADR makes no claim
  about it.

## Evidence

- Deterministic fake-provider coverage: `tests.test_local_power_session`
  (`FailureEvidenceAndRepairTests`, 11 tests) — failing checks reaching the
  next prompt, the audit artifact, no false evidence from a passing check,
  outstanding-command reporting, stale-pass labelling, the finish gate's
  refusal and both of its escape hatches, the refusal cap, the no-changes
  exemption, and a resumed session seeded from a prior failure. 66/66 pass
  with 1 expected skip.
- Live artifacts inspected: `TASK-E01762481075`'s
  `call-007-request.json` (confirms the failure output is absent from the
  continuation prompt), its task directory (confirms no normalized-failure
  artifact was ever written), and the second worktree.
- `verify-web-product` re-run against that worktree: 4 error findings, 13
  warnings, exit 1.
- **No live local-model rerun was performed for this ADR.** Everything here is
  fake-provider evidence.
