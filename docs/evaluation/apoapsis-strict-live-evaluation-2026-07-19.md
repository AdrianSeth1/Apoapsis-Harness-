# Apoapsis STRICT live evaluation, round 2 — 2026-07-19

Three fresh, identical, live `local-strict` attempts against the controlled
`download-service` fixture, run immediately after Phase A (ADR 0018) fixed
the two gaps the first round
(`docs/evaluation/apoapsis-strict-live-evaluation-2026-07-18.md`) found.
Same model, same profile, same fixture, same task text as round 1. No
research model, no hosted provider, no manual repair between attempts,
every audit artifact preserved.

```powershell
.\START_APOAPSIS.cmd
py -3 -m apoapsis eval download-service --lane local-strict --context-profile 64k --output-dir .apoapsis-eval/local-strict-4
py -3 -m apoapsis eval download-service --lane local-strict --context-profile 64k --output-dir .apoapsis-eval/local-strict-5
py -3 -m apoapsis eval download-service --lane local-strict --context-profile 64k --output-dir .apoapsis-eval/local-strict-6
.\STOP_APOAPSIS.cmd
```

Model: `qwen3-coder-next:q4_K_M` (79.7B, Q4_K_M), native loopback Ollama,
`context_window_tokens = 65536`, `think = false`, `temperature = 0.0`.
Route: `local_only`. `completion_policy = strict`. Same
`.apoapsis/config.toml` verification commands as round 1 (`unit-tests`,
required; `resumable-acceptance-check`, `acceptance = true`) --
unchanged, gitignored, not committed.

## Headline result: the first genuine, independently-confirmed success

**Attempt 5 reached `COMPLETE`, and the held-out oracle passed.** This is
the first true success across both rounds of live evaluation (6 attempts
total). The model produced a fully correct fix, confirmed by two
independent checks using different data: the visible
`resumable-acceptance-check` (which gated its own completion) and the
held-out `tests/test_resumable_acceptance.py` (which never entered its
context and was only run by the harness after `COMPLETE`).

## Per-attempt results

| Attempt | Spec extraction | Proposed AC mapping | Turns | Patch attempts | Verify runs | Acceptance check | Rejected requests | Strict outcome | Held-out oracle | Input/output tokens | Latency s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 4 | Succeeded (no correction needed) | AC-RESUMABLE-DOWNLOAD → `resumable-acceptance-check` | 12 | 4 | 2 | Failed once (accurate summary + evidence), then `unit-tests` also failed | 3 of 13 | `HUMAN_REVIEW_REQUIRED` (turn budget exhausted) | `not_run` | 67,700 / 2,519 | 187.8 |
| 5 | Succeeded (no correction needed) | AC-RESUMABLE-DOWNLOAD → `resumable-acceptance-check` | 8 | 1 | 2 | Passed both times it ran | 3 of 9 | **`COMPLETE`** | **`passed`** | 39,968 / 1,234 | 107.2 |
| 6 | Succeeded (no correction needed) | AC-001, AC-002 → `resumable-acceptance-check` | 12 | 2 | 1 | Failed once (accurate summary + evidence), model then stalled | 0 of 13 | `HUMAN_REVIEW_REQUIRED` (turn budget exhausted) | `not_run` | 67,296 / 1,389 | 162.9 |

Task IDs: attempt 4 `TASK-7F68ED2DBCA3` (run `EVAL-E58349D77309`), attempt 5
`TASK-092ECCC4FAE9` (run `EVAL-CD87ED1AA915`), attempt 6
`TASK-0749D25521C7` (run `EVAL-DF1514D2C982`). Peak context-window
utilization stayed low across all three (3.3%-4.4% of the 64k budget) --
context size remains nowhere near the limiting factor. No specification
required the one bounded correction attempt this round (all three
extracted validly on the first try) -- round 1 had one outright
specification failure (1/3); this round had zero, but three attempts each
is far too small a sample to call that a reliability improvement.
Full audit trees are preserved under
`.apoapsis-eval/local-strict-{4,5,6}/local-strict/download-service/.apoapsis/tasks/<task-id>/`
(gitignored, not committed -- this document is the durable record).

## What actually happened

### Attempt 5: a genuine, fully correct fix

1. The model read `downloader.py`, applied a real fix via `replace_text`
   on its very first edit, and ran `resumable-acceptance-check`. Both
   sub-tests passed, and this time the turn summary correctly said
   `"deterministic verification passed"` -- because it actually had.
2. It then tried to re-run the identical check three more times (still
   correctly rejected each time as an unchanged duplicate -- that
   protection is unaffected by ADR 0018), before switching to
   `submit_for_verification`, which ran `unit-tests` and
   `resumable-acceptance-check` together. Both passed at the same
   worktree fingerprint, satisfying the one active acceptance criterion
   (`AC-RESUMABLE-DOWNLOAD`), and the task reached `COMPLETE` in 8 turns.
3. The final code:

   ```python
   def download(self, url: str, destination: Path) -> int:
       start_offset = self.jobs.get_offset(url)
       headers = {"Range": f"bytes={start_offset}-"} if start_offset > 0 else {}
       response = self.transport.get(url, headers=headers)
       destination.parent.mkdir(parents=True, exist_ok=True)
       if response.status_code == 200 and start_offset > 0:
           downloaded = 0
           with destination.open("wb") as handle:
               for chunk in response.iter_chunks():
                   handle.write(chunk)
                   downloaded += len(chunk)
                   self.jobs.set_offset(url, downloaded)
       else:
           downloaded = start_offset
           with destination.open("ab") as handle:
               for chunk in response.iter_chunks():
                   handle.write(chunk)
                   downloaded += len(chunk)
                   self.jobs.set_offset(url, downloaded)
       return downloaded
   ```

   This is the first live attempt (of six, across both rounds) to get the
   return-value arithmetic right in the resume branch (`downloaded =
   start_offset`, correctly seeding the cumulative total) -- exactly the
   class of bug every prior attempt got wrong. The held-out oracle
   independently confirmed it: `held_out_oracle.status == "passed"`.

### Attempts 4 and 6: the fix works -- real evidence, real edits -- but neither finished in budget

Both attempts show exactly the behavior ADR 0018 was meant to produce:

- Attempt 4's turn 7: `"resumable-acceptance-check failed with exit code
  1"` (not "passed"). The model responded with `inspect_diff` (turn 8),
  another `replace_text` edit (turn 9), then ran the required `unit-tests`
  command for the first time in either round of live evaluation (turn
  10) -- which also failed, with full accurate evidence. It made one more
  edit attempt (turn 12, rejected as a no-op: `"replace_text did not
  change the file"`) before the turn budget ran out. Four total patch
  attempts, two real verification runs -- both qualitatively new
  behaviors compared to round 1, where the model never ran `unit-tests`
  at all and never got past its first, misleadingly-labeled check.
- Attempt 6's turn 4: the same accurate failure summary, followed by
  `inspect_diff` and a second `replace_text` edit (turn 6) that partially
  addressed the bug -- it computed a file-size value (`initial_size`) that
  was never actually used in the final `return downloaded` statement, so
  the underlying bug (missing cumulative offset) remained. The model then
  spent its remaining six turns issuing `inspect_diff` repeatedly without
  editing or re-verifying, and the turn budget exhausted with the fix
  still incomplete and never re-checked.

**Manual post-hoc check** (by the report author, outside the harness's own
pipeline; the formal `held_out_oracle` correctly stayed `not_run` in both,
since neither reached `COMPLETE`): copying the real held-out test into
each attempt's final worktree confirms both remaining bugs are genuine --
attempt 4 fails the oracle's resume test `5 != 11`; attempt 6 fails it the
same way, `5 != 11`. Both attempts' range-ignoring-server behavior already
passes both the visible test and the oracle. Neither attempt was harmed by
a harness gap this round -- both saw exactly the evidence ADR 0018 added;
the remaining gap in both is the model's own repair completeness within a
12-turn budget.

## Comparing rounds 1 and 2

| Metric | Round 1 (2026-07-18) | Round 2 (2026-07-19) |
| --- | --- | --- |
| Strict workflow completion rate | 0/3 (0%) | 1/3 (33%) |
| Held-out correctness rate (formal) | Unmeasured (0 valid oracle runs) | 1/1 valid oracle run passed (attempt 5); still too small to call a rate |
| False-success rate among strict completions | Unmeasured (0 completions) | 0/1 (the one completion was independently confirmed correct) |
| Human-review rate | 2/3 (67%) | 2/3 (67%) |
| Specification failure rate | 1/3 (33%) | 0/3 (0%) |
| Model ran the required `unit-tests` command at all | 0/2 attempts that reached the mechanism | 2/2 attempts that reached the mechanism |
| Model received an accurate failure summary from a failing acceptance check | 0/2 | 2/2 (both non-completing attempts) |
| Rejected tool requests (of total calls) | 7/13, 8/13 | 3/13, 0/9, 0/13 |
| Retrieval-related failures | 0/3 | 0/3 -- identical, correct 7-file set every call, every attempt |

With samples this small (three attempts per round, six total), none of
these deltas are a statistically reliable completion-rate measurement.
What changed qualitatively is unambiguous: in round 1, the model was
**structurally prevented** from ever learning its fix was wrong once it
ran the one command it had mapped -- it saw "passed," and its remaining
turns were entirely consumed by a rejected, unchanged re-check. In round
2, all three attempts saw accurate, evidence-bearing failure summaries
when their fix was actually wrong, and two of three responded with real
further edits and (in one case) the required command's own real failure
evidence too. The one attempt that also managed a fully correct fix within
budget reached a genuine, independently-confirmed `COMPLETE`. The two that
did not were budget- and model-repair-limited, not harness-evidence
limited -- a materially different, and more diagnosable, failure mode than
round 1's.

## What this evidence does not show

- It does not establish a reliable completion rate for `STRICT` -- one
  completion in three attempts, from a single small round, is not a rate.
- It does not show specification-extraction reliability is fixed --
  zero failures in three attempts is consistent with the prior 1-in-6
  rate; the sample is too small to distinguish luck from improvement, and
  the one bounded correction attempt (ADR 0018) was never exercised live
  this round.
- It does not show a retrieval problem of any kind, in either round.
- It does not mean 12 turns is definitely insufficient budget -- attempt 5
  completed in 8; attempts 4 and 6 had real, evidence-driven activity
  filling most of their 12, but also each spent turns on dead ends
  (a rejected no-op edit; repeated `inspect_diff` with no further action).
  Whether more turns, or a more capable model, would have closed the gap
  is unmeasured here.

## Audit locations

`.apoapsis-eval/local-strict-4/`, `.apoapsis-eval/local-strict-5/`,
`.apoapsis-eval/local-strict-6/` (all gitignored, not committed) each
contain `comparison.json`/`comparison.md` and the full per-task audit tree
under `local-strict/download-service/.apoapsis/tasks/<task-id>/`, including
every `agent-turn-*.json`, provider call/response/telemetry record,
verification result, and `held-out-oracle.json`.
