# Apoapsis STRICT live evaluation — 2026-07-18

Three fresh, identical, live `local-strict` attempts against the controlled
`download-service` fixture, using the configured Qwen3-Coder-Next Q4 local
model at the 64k context profile. No research model, no hosted provider.
Each attempt used its own output directory and every audit artifact was
preserved; no attempt was manually repaired, altered, or rerun.

```powershell
.\START_APOAPSIS.cmd
py -3 -m apoapsis eval download-service --lane local-strict --context-profile 64k --output-dir .apoapsis-eval/local-strict-1
py -3 -m apoapsis eval download-service --lane local-strict --context-profile 64k --output-dir .apoapsis-eval/local-strict-2
py -3 -m apoapsis eval download-service --lane local-strict --context-profile 64k --output-dir .apoapsis-eval/local-strict-3
.\STOP_APOAPSIS.cmd
```

Model: `qwen3-coder-next:q4_K_M` (79.7B, Q4_K_M), native loopback Ollama,
`context_window_tokens = 65536`, `think = false`, `temperature = 0.0`.
Route: `local_only` (forced by the `local-strict` lane overlay).
`completion_policy = strict` (forced by the lane, regardless of this
repository's own `.apoapsis/config.toml`). Verification commands used for
this run (configured locally, not committed — `.apoapsis/config.toml` is
gitignored):

```toml
[[verification.commands]]
name = "unit-tests"
category = "tests"
description = "Runs the project's full test suite."
argv = ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
timeout_seconds = 120
required = true

[[verification.commands]]
name = "resumable-acceptance-check"
category = "acceptance"
description = "Model-visible resumable-download acceptance checks: an interrupted download resumes from the persisted byte offset, and a server that ignores the Range header has its stale partial data replaced rather than appended to."
argv = ["python", "-m", "unittest", "tests.test_resumable_visible_acceptance", "-v"]
timeout_seconds = 60
required = false
acceptance = true
```

## Per-attempt results

| Attempt | Spec extraction | Proposed AC mapping | Context compiled | Files retrieved | Turns | Patch attempts | Verify runs | Dev check (`unit-tests`) | Acceptance check | Rejected requests | Strict outcome | Held-out oracle | Input/output tokens | Latency s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Succeeded | AC-1, AC-2 → `resumable-acceptance-check` | Yes | 7 (same set every call) | 12 | 2 (1 rejected, 1 applied) | 1 | Never run | Ran once: 1/2 sub-tests failed | 7 of 13 | `HUMAN_REVIEW_REQUIRED` (turn budget exhausted) | `not_run` (never reached COMPLETE) | 66,339 / 1,808 | 175.1 |
| 2 | **Failed** (Pydantic validation error) | n/a | No | 0 | 0 | 0 | 0 | n/a | n/a | 0 | `FAILED` | `not_run` | 1,672 / 976 | 25.3 |
| 3 | Succeeded | AC-DOWNLOAD-RESUME, AC-STALE-PARTIAL-OVERWRITE → `resumable-acceptance-check` | Yes | 7 (same set every call) | 12 | 1 (applied) | 1 | Never run | Ran once: 1/2 sub-tests failed | 8 of 13 | `HUMAN_REVIEW_REQUIRED` (turn budget exhausted) | `not_run` (never reached COMPLETE) | 61,479 / 1,180 | 150.1 |

Full audit trees (every `agent-turn-*.json`, `call-*-request/response/
telemetry.json`, `verification-*.json`, `held-out-oracle.json`,
`report.json`) are preserved under
`.apoapsis-eval/local-strict-{1,2,3}/local-strict/download-service/.apoapsis/tasks/<task-id>/`
(gitignored, not committed — this document is the durable record). Task
IDs: attempt 1 `TASK-6F6D947D6B4C` (run `EVAL-A937FDB4CB37`), attempt 2
`TASK-944DA9C8F73F` (run `EVAL-AC2102C47D8D`), attempt 3
`TASK-5FF10CCCF2B4` (run `EVAL-E374AC8A7EE8`). Peak context-window
utilization was ~3.3% (attempt 1) and ~3.1% (attempt 3) of the 64k budget —
context size was never close to the limiting factor. Cached input tokens
were 0 in all three (expected: each call's content differs enough that the
provider never reports a cache hit). Estimated cost: $0.00 (loopback
Ollama, zero configured pricing).

## What actually happened

### Attempt 1 and attempt 3: the acceptance mechanism worked exactly as designed, up to a harness gap that then stalled repair

In **both** attempts that reached context compilation, the model:

1. Read `downloader.py` (and, in attempt 1, `jobs.py`).
2. Extracted a specification whose acceptance criteria were **genuinely
   mapped to the real catalog** — `AC-1`/`AC-2` (attempt 1) and
   `AC-DOWNLOAD-RESUME`/`AC-STALE-PARTIAL-OVERWRITE` (attempt 3), both
   pointing `verification_method` at `resumable-acceptance-check`, the
   exact acceptance-designated command from `ACCEPTANCE_COMMAND_CATALOG`.
   Nothing injected or rewrote this — it is the model's own proposal,
   approved by the controlled evaluation's auto-approval step exactly like
   any other lane (ADR 0015/0016 requirement 5).
3. Applied a real fix via `replace_text`, adding a conditional `Range`
   header and branching on the response status code.
4. Ran `resumable-acceptance-check` **once**. One of its two sub-tests
   failed in both attempts. The turn's recorded summary read
   `"deterministic verification passed"` — **misleading**, because
   `resumable-acceptance-check` is `required = false` (an intentional
   design choice: acceptance-only commands should not gate ordinary
   development verification). `_record_verification()`'s summary logic
   only reports a failure when a *required* command in the checked set
   fails; since the one command checked here was optional, the aggregate
   read as "passed" even though its own sub-test had just failed.
5. Spent every remaining turn (6 in attempt 1, 8 in attempt 3) trying to
   `run_check("resumable-acceptance-check")` again on an unchanged diff,
   rejected every time with `"identical verification already ran for the
   current diff; change the code or inspect the recorded failure"` — until
   the turn budget was exhausted and the task stopped at
   `HUMAN_REVIEW_REQUIRED`.

The model never ran `unit-tests` (the required development command) in
either attempt. Because `_check_completion()` only computes real acceptance
coverage once the aggregate verification result is `PASSED` *and* every
required command has passed at the current fingerprint, the model never
saw the one piece of evidence specifically designed for this situation —
the `EV-ACCEPTANCE-GAP` entry naming exactly which criterion is unproven and
why. It saw a falsely reassuring "passed" message instead, and had no
signal telling it to look closer or make another edit.

**The actual code was subtly, differently wrong in each attempt** — both
close, both wrong in the return-value arithmetic the original 1.0 evidence
already flagged as the hard part of this task:

- Attempt 1 correctly branched on `response.status_code == 206` for
  append-vs-overwrite, but returned only `downloaded` (bytes written in
  *this* call) instead of the cumulative total (`offset + downloaded`) —
  the job store's persisted offset was updated correctly, but the
  function's own return value was not.
- Attempt 3 correctly sent the conditional header and correctly reset to
  `"wb"` on a `200` response, but for the resume (`206`) branch it
  re-derived the byte count by re-opening and re-reading the destination
  file (`existing_offset + len(handle.read())`) instead of just using
  `existing_offset` — double-counting bytes already on disk.

**Manual post-hoc check** (performed by the report author outside the
harness's own pipeline, purely to confirm the above analysis — the formal,
automated held-out oracle correctly stayed `not_run` in both attempts,
exactly as designed, since neither task reached `COMPLETE`): copying the
real held-out `tests/test_resumable_acceptance.py` into each attempt's
final worktree and running it directly confirms both bugs are genuine and
generalize — attempt 1's code fails the oracle's resume test with
`5 != 11`, attempt 3's with `17 != 11`. Both attempts' range-ignoring-server
sub-test passes the oracle too, consistent with the visible test. This is
not part of either attempt's recorded `held_out_oracle` field (which
remains the faithful `not_run` result) — it is independent confirmation
that the visible acceptance test and the held-out oracle are measuring the
same real property, and that the model's remaining bug was real, not an
artifact of the visible test's specific data.

### Attempt 2: a pre-existing specification-extraction reliability failure, unrelated to the new mechanism

Attempt 2 never reached context compilation. Extraction raised:

```
SpecificationExtractionError: frontier specification is invalid: 3 validation errors for TaskSpecification
hard_constraints.0.verification_method
  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]
hard_constraints.1.verification_method
  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]
hard_constraints.2.verification_method
  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]
```

The model set `verification_method: null` on all three extracted hard
constraints. `HardConstraint.verification_method` is a **required**
free-text string field (`str = Field(min_length=1)`), unrelated to the new,
optional, catalog-validated `AcceptanceCriterion.verification_method` this
milestone added. This is the same specification-extraction reliability
class already flagged as a separate, not-yet-investigated concern in the
1.0 profile evidence (one of six 128k runs also failed at drafting). It is
not evidence about the acceptance mechanism, the fixture, or retrieval —
extraction failed before any of those were reached.

## Aggregate distinctions

| Metric | Value | Denominator |
| --- | --- | --- |
| Strict workflow completion rate | 0/3 (0%) | 3 attempts |
| Held-out correctness rate (formal, automated) | Unmeasured | 0 valid oracle runs (oracle only runs after `COMPLETE`; never reached) |
| False-success rate among strict completions | Unmeasured | 0 completions (no denominator, not zero) |
| Human-review rate | 2/3 (67%) | 3 attempts |
| Specification failure rate | 1/3 (33%) | 3 attempts |
| Retrieval-related failures | 0/3 | Both attempts that reached context compilation retrieved the identical, correct 7-file set every call |

**Did visible acceptance feedback improve the previously observed 1/6
true-success result?** Not in the literal completion sense — this round
produced 0/3 strict completions, and with samples this small (1/6 vs 0/3)
no rate comparison is statistically meaningful either way. But the
*qualitative* picture changed substantially and informatively:

- The model **did** discover and correctly use the new acceptance-command
  catalog in both attempts that reached it — proposing sensible, real
  criterion-to-command mappings unprompted, exactly the behavior ADR
  0015/0016 was designed to make possible. This did not happen by
  construction or injection; it is the model's own proposal in both cases.
- The model's code was **much closer to correct** than the earlier
  1/6 baseline's typical failures — both attempts got the harder
  "server-ignores-Range, discard stale data" branch right, and both had
  only a narrow, specific return-value arithmetic bug remaining, in the
  exact class of subtlety the original evidence already named as the hard
  part of this task.
- The model never got a **real chance to fix that remaining bug**, because
  a harness gap discovered during this run — not previously known —
  misrepresented a failing, `required = false` acceptance check as
  "deterministic verification passed," so the model neither saw an
  informative failure nor the designed `EV-ACCEPTANCE-GAP` coverage-gap
  evidence, and burned its remaining budget re-running an unchanged check.

This is a genuine, disclosed harness finding, not a retrieval or context
problem, and per instruction it is **not** being fixed in this milestone:
`_record_verification()`'s and `VerificationRunner`'s aggregate-status logic
in `src/apoapsis/agent/session.py` and `src/apoapsis/verification/runner.py`
treat "required" as the sole criterion for whether a failing command
produces informative failure evidence and an accurate turn summary. An
acceptance-designated (`acceptance = true`) command that is not `required`
can fail without the model ever seeing a genuine failure signal from that
specific check, unless a *separate*, required command also fails or passes
at the same worktree fingerprint. A narrowly scoped future fix would treat
"acceptance-designated" the same as "required" for the purpose of
triggering `_add_failure_evidence()` and an accurate turn summary — without
changing what counts as a required *development*-gating failure. That is a
future, separate decision, not made here.

## What this evidence does not show

- It does not show the acceptance mechanism is unable to produce genuine
  completions — no attempt got a fair chance to iterate after its one
  informative-evidence gap.
- It does not show a retrieval problem of any kind; the same correct file
  set was found every time, consistent with all prior evidence.
- It does not establish a reliable completion or false-success *rate* for
  `STRICT` with real acceptance mappings — three attempts, one of which
  never reached the mechanism at all, is not enough to generalize from.
- It does not indicate the visible acceptance test or the held-out oracle
  measure different things — the manual post-hoc check found both
  attempts' bugs generalized to the independent oracle, exactly as
  intended.

## Audit locations

`.apoapsis-eval/local-strict-1/`, `.apoapsis-eval/local-strict-2/`,
`.apoapsis-eval/local-strict-3/` (all gitignored, not committed) each
contain `comparison.json`/`comparison.md` and the full per-task audit tree
under `local-strict/download-service/.apoapsis/tasks/<task-id>/`, including
every `agent-turn-*.json`, provider call/response/telemetry record,
verification result, and `held-out-oracle.json`.
