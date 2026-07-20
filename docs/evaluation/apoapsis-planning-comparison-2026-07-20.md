# Planning comparison: monolithic vs. plan-then-slices — 2026-07-20

Three live monolithic attempts and three live planned (plan-then-slices)
attempts at the same task, against the same installed local model, using
the deterministic comparison framework built in Commit D4a (ADR 0028). The
plan was produced in a genuinely separate model session (Gemini 3.1 Pro),
which received only the exported `PlannerRequestPackage` — this harness
never generated it. No manual repair was performed between attempts. Every
audit artifact was preserved under `.apoapsis-eval/` (gitignored, not
committed; paths listed below).

```powershell
py -3 -m apoapsis eval-planning download-service-v2 --plan-id PLAN-A7CF3D97FF91 --expected-plan-version 3 --planned-project-root .apoapsis-eval\planned-v2-project   --planner-model "gemini-3.1-pro" --output-dir .apoapsis-eval\d4b-attempt-1
py -3 -m apoapsis eval-planning download-service-v2 --plan-id PLAN-1F66CDCC7DEE --expected-plan-version 3 --planned-project-root .apoapsis-eval\planned-v2-project-2 --planner-model "gemini-3.1-pro" --output-dir .apoapsis-eval\d4b-attempt-2
py -3 -m apoapsis eval-planning download-service-v2 --plan-id PLAN-FEDC720EB442 --expected-plan-version 3 --planned-project-root .apoapsis-eval\planned-v2-project-3 --planner-model "gemini-3.1-pro" --output-dir .apoapsis-eval\d4b-attempt-3
.\STOP_APOAPSIS.cmd
```

(Attempt 1's monolithic run itself first ran under a since-fixed operator
error — an untracked `response.json` file left the planned-condition
project's parent repository dirty, correctly failing the existing
`require_clean_parent_repository` guard before any slice's worktree was
created. The already-completed monolithic result was reused rather than
re-run; only the planned side was retried after cleaning the directory.
This was an artifact of manual attempt setup, not a framework or model
issue, and is disclosed here for completeness.)

Model: `qwen3-coder-next:q4_K_M`, native loopback Ollama,
`context_window_tokens = 65536`, `think = false`, `temperature = 0.0`.
Route: `local_only`. `completion_policy = strict` for both conditions
(ADR 0028's documented deviation from `apoapsis eval`'s normally-forced
`baseline`). Local agent budget: `max_turns = 12`, `max_patch_attempts = 6`
(monolithic config) / project-default (planned config, effectively
unconstrained relative to what was used), `max_verification_runs = 6`.
Scenario: `download-service-v2` v1.0 (ADR 0028). Planner: Gemini 3.1 Pro,
manual-subscription-paste, tokens/cost recorded unmeasured (no API
telemetry exists for a manually-pasted session).

A genuine plan-quality problem was found and corrected before running any
live attempt: the first plan response Gemini produced left `HC-NO-DEPS`
unrepresented by any slice (a real, blocking `validate_plan` error) and set
no slice's `acceptance_criterion_ids` at all (not blocking, but would have
left `STRICT` completion ungated for two of the three slices). Both were
fixed by asking the same external session for a corrected response — this
harness never authored the fix itself. The corrected plan validated cleanly
(zero findings) and was approved before any attempt ran.

## Headline result: 0/6 completions; a consistent model-logic failure, not a mechanism, specification, or oracle problem

**Every one of the six live attempts — three monolithic, three planned —
stopped at `HUMAN_REVIEW_REQUIRED` after exhausting its full 12-turn
budget, having called a verification command zero times.** The planned
condition never advanced past `SLICE-JOBS-001` (the first, dependency-free
slice) in any of the three attempts; `SLICE-DOWNLOADER-002` and
`SLICE-SERVICE-003` were never attempted. No held-out oracle ever ran (it
only runs after a real `COMPLETE`), so false success is `unmeasured`, not
zero — there were no claimed successes for it to evaluate.

Inspecting the actual turn sequences (`agent-turn-NNN.json`) shows the same
pattern in all six sessions: the model reads the target file, makes
**exactly one** edit (`replace_text`, occasionally preceded by a rejected
`propose_patch`), and then spends every remaining turn re-issuing
`read_file` against files it had already read — never calling `run_check`
or `submit_for_verification` even once. This is a genuine, repeatable
**model-logic failure**: a behavioral loop specific to this model under
this task/fixture, not a bug in the harness. Every mechanical part of the
system worked exactly as designed:

- Turn/patch/verification budgets were counted and enforced correctly.
- The escalation-required → Human Review classification fired correctly
  every time (`agent_stop_reason: "agent turn budget exhausted after 12
  turns"`).
- The planned condition's dependency-evidence and git-merge machinery was
  never even reached, because it never needed to be: `SLICE-JOBS-001`
  never completed, so nothing merged and nothing downstream was ever
  packaged — exactly the "stop advancing on the first non-`COMPLETE`
  slice, no auto-repair" behavior ADR 0028 requires.
- The oracle-absence check passed before every attempt; the oracle itself
  correctly never ran (`held_out_oracle` is `null` on every planned
  result, `not_run` on every monolithic one).

## Per-attempt results

| Attempt | Condition | Turns | Patch attempts | Verify runs | Rejected requests | Outcome | Input/output tokens | Latency s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Monolithic | 12 | 1 | 0 | 1 | `HUMAN_REVIEW_REQUIRED` (turn budget exhausted) | 93,750 / 2,855 | 243.7 |
| 1 | Planned — Slice A | 12 | 2 | 0 | 1 | `HUMAN_REVIEW_REQUIRED` (turn budget exhausted) | 82,544 / 1,241 | 190.4 |
| 2 | Monolithic | 12 | 1 | 0 | 1 | `HUMAN_REVIEW_REQUIRED` (turn budget exhausted) | 90,611 / 1,618 | 213.5 |
| 2 | Planned — Slice A | 12 | 1 | 0 | 0 | `HUMAN_REVIEW_REQUIRED` (turn budget exhausted) | 81,661 / 535 | 171.8 |
| 3 | Monolithic | 12 | 1 | 0 | 1 | `HUMAN_REVIEW_REQUIRED` (turn budget exhausted) | 91,174 / 1,665 | 214.0 |
| 3 | Planned — Slice A | 12 | 2 | 0 | 1 | `HUMAN_REVIEW_REQUIRED` (turn budget exhausted) | 82,636 / 1,211 | 191.8 |

`SLICE-DOWNLOADER-002` and `SLICE-SERVICE-003`: 0 attempts across all three
planned runs (never reached).

## Aggregate (`summarize_planning_comparisons`, deterministic, no model call)

| Metric | Monolithic | Planned |
| --- | --- | --- |
| True completion (`COMPLETE` + oracle passed) | 0/3 | 0/3 |
| False success | unmeasured (no claimed success reached the oracle) | unmeasured (no fully-complete attempt reached the oracle) |
| Human Review | 3/3 | 3/3 |
| Policy-rejected patches | 0/3 patch attempts | 0/5 patch attempts |
| Verification runs (median) | 0 | 0 |
| Latency s (median) | 214.0 | 191.8 (Slice A only; downstream slices never ran) |
| Input tokens (median) | 91,174 | 82,544 |
| Output tokens (median) | 1,665 | 1,211 |
| Estimated cost | $0.00 (local model, zero configured per-token pricing) | $0.00 |

Integration failure: unmeasured (no planned attempt ever completed all
three slices, so the held-out oracle never had anything to evaluate).
`per_slice`: `SLICE-JOBS-001` 0/3 completion, 3/3 Human Review.

## Failure-mode classification

- **Mechanism failure**: none observed. Every harness mechanism (turn
  budget, patch/verification accounting, escalation classification,
  dependency-evidence gating, oracle withholding) behaved exactly as
  designed across all six attempts.
- **Specification failure**: one real instance, caught and corrected
  *before* any live attempt ran (missing `HC-NO-DEPS` representation and
  missing `acceptance_criterion_ids` links) — see above. Not present in
  the plan actually executed.
- **Retrieval failure**: none observed; the model successfully read the
  files it needed (confirmed by the turn-by-turn `read_file` sequence)
  before getting stuck.
- **Model-logic failure**: the dominant and only observed failure mode.
  The model made one edit, then repeatedly re-read already-read files
  instead of ever running a check, in six independent sessions across two
  different execution shapes (one large task, one small dependency-free
  slice). This looks like a genuine behavioral limitation of
  `qwen3-coder-next:q4_K_M` under this harness's prompt/tool structure for
  this specific task, not noise.
- **Slice-integration failure**: not observed and not measurable this
  round — no planned attempt ever got two slices to `COMPLETE`, so there
  is nothing for `integration_failure` to detect yet.
- **Oracle failure**: none. The oracle never ran (correctly, since nothing
  reached real `COMPLETE`), and its absence from every agent-visible copy
  was verified before every attempt.

## What this evidence does and does not support

**Does not support**: any claim that Architect Mode/planning improves or
worsens outcomes versus a monolithic request. Both conditions failed
identically, for the identical underlying reason (the coding model never
attempted verification), which tells us nothing about whether decomposing
the task into slices helps *once a model actually verifies its own work*.

**Does not support**: any completion-rate or false-success-rate claim
beyond "0/3 observed for both conditions this round" — six attempts is far
too small a sample, and the identical root cause across all six means this
round measured one model-behavior question, not six independent
observations of the harness or the plan.

**Does support**: the planning-comparison framework itself (ADR 0028)
worked correctly end to end against a genuinely externally-produced,
independently-corrected plan and a real local model, including a live
plan-quality defect being caught before any model call was made.

## Next steps (not taken here)

- Investigate the model's read-loop behavior directly (larger turn budget,
  a different prompt structure, or a different local model) before
  re-running this comparison — six attempts against a model that never
  once calls a verification tool cannot distinguish "planning helps" from
  "planning doesn't help," and re-running with the same behavior would
  only reproduce the same non-result.
- Once at least some attempts reach real `COMPLETE` in both conditions,
  rerun this exact comparison to get a first measurable true-completion/
  false-success/integration-failure signal.

## Artifacts (local, gitignored, not committed)

- `.apoapsis-eval/planned-v2-project{,-2,-3}/` — the three approved-plan
  project directories (plan/slice/task/operation stores, full audit trail).
- `.apoapsis-eval/d4b-attempt-{1,2,3}/` — `planning-comparison.json`/`.md`
  per attempt, plus the fresh monolithic fixture copy and its full audit
  trail for each attempt.
