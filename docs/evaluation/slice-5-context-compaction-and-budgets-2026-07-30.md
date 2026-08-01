# Slice 5: task kernel, state capsule, two-tier compaction, and real budgets

Date: 2026-07-30  
Evidence class: **deterministic only.** 38 new tests, 255 in the focused set.
No model calls, no container. This slice changes how context and budgets are
managed; it does not yet run a session through them.

## Four Crisis Atlas findings, addressed

| Finding | Response |
| --- | --- |
| The prompt supplied volume instead of navigation | A small, stable `TaskKernel`; files stay on disk, retrieved just in time |
| Replay cost 8× the input tokens (2,080,801 vs 258,632) | A `StateCapsule` of facts, not the transcript they came from |
| The control died at 65,536 with no compaction; Slice 2D reached 88.6% and still fired none | Proactive two-tier compaction at a measured threshold |
| "The budget described protocol turns, not engineering work" | Wall time, process time, tokens, and no-progress detection; the call ceiling is an emergency stop |

## The kernel refuses to be volatile

Prefix caching only helps when the prefix is byte-identical between calls, and
the way that breaks is invisible: the run still works, every answer is still
right, and the cache-hit rate quietly goes to zero. Slice 0's efficiency gate
would then report the token cost as a property of the harness rather than as a
bug.

So `TaskKernel` **rejects** a timestamp, a UUID, an `MRQ-` request id, or an
elapsed-time phrase at construction, and renders its lists sorted so a
reordering does not change the digest. `check_prefix_stability` reports the
call at which a prefix first moved.

## The capsule is honest about what it is

`StateCapsule` carries what the handoff lists — unresolved obligations, the
interface ledger, changed paths, the delta summary, the fingerprint, witnesses
already observed, latest failures, refused and no-progress actions, and the
model's own notes.

Three details are deliberate:

- **Model notes are rendered under "Advisory ... may be wrong."** They are the
  one part of the document the model wrote, and the only part that is belief
  rather than observation.
- **A witness from an older fingerprint is rendered "not current evidence."**
  Carrying it forward is useful history; letting it read as current is the ADR
  0072 failure.
- **Refused and no-progress actions are carried forward.** A fresh context that
  immediately retries what already failed is how the Slice 2C sandbox arm
  reached nine identical calls before its own loop detection halted it.

A test asserts the capsule has no `transcript`, `raw_output`, or `logs` field.
Once a log's facts are in the capsule, replaying it is the 8× mistake.

## Compaction fires before the cliff, in two tiers

`CompactionPolicy.should_compact(58_038)` is **true** at the default 0.70
threshold — that is Slice 2D's own near-boundary reading, 88.6% of the window,
which fired no compaction event at the time. So is the unrestricted control's
fatal 64,409-token prompt.

That is a statement about the policy, not a claim about what the model would
then have done. Whether compaction at 70% keeps a run coherent is the
near-boundary rerun still owed.

`DEFAULT_COMPACTION_THRESHOLD = 0.70` matches Qwen Code's own default and is
recorded as **the first experiment point**, not an Apoapsis constant; the
handoff wants 60/70/80 compared on the corpus. A `target` at or above the
`threshold` is refused at construction, because a session that compacts, lands
just under the line, and compacts again next turn has a policy bug rather than
a busy history.

Tier one is mechanical: drop old reasoning, replace old tool outputs with
artifact pointers. Tier two is semantic and only *requested* — it costs a model
call and it summarises, so the caller performs it deliberately.

Two safety properties hold throughout:

- **The capsule is never dropped.** It is what compaction exists to preserve.
- **Output with nowhere to spill is kept, not dropped.** Discarding it would
  make the only record of what a command printed vanish. Likewise
  `bound_observation` refuses to truncate when it has no spill directory, and
  `BoundedObservation` rejects `truncated=True` with no artifact pointer at
  construction.

Truncation keeps the head *and* the tail, because a command's first lines say
what it did and its last lines say how it ended.

## Budgets measure work, not turns

Twelve Local Power turns and sixty-two control cycles were both "the budget",
and neither described what the owner was paying for. The primary ceilings are
wall clock, in-workcell process time, input and output tokens, and no-progress
detection. Wall time and process time are tracked separately, because "the
session took 30 minutes" and "25 of those were the test suite" need different
fixes.

`ProgressTracker` defines progress as **a changed worktree fingerprint** — not
a turn occurring, and not the model's account of itself. `SessionBudget`
refuses an `emergency_call_ceiling` below 50, because setting it low would
recreate the turn-count budget by the back door.

Breaches carry guidance only when the agent can act on them: a no-progress stop
tells it to try something different; a wall-clock expiry says nothing, because
it is not the agent's to repair.

## Honest limitations

- **Nothing is wired in.** `run_checkpoint` does not yet build a kernel,
  maintain a capsule, compact, or enforce a budget. This slice is the
  machinery; the session loop that uses it is not written. That is the same
  gap review found in Slice 4, and I am flagging it before it is found.
- **The near-boundary rerun is still owed.** The policy would have fired at
  58,038 tokens. Whether the run then continues coherently — the slice's own
  exit criterion — needs a live run, and the CLI limit mismatch stays
  *causally consistent* with the Crisis Atlas rollover rather than proven.
- **Token counts are estimates supplied by the caller.** `HistorySegment
  .estimated_tokens` is not measured here; a caller that estimates badly
  compacts at the wrong time. Binding it to provider-reported usage is
  outstanding.
- **No prompt-evaluation or cache telemetry yet.** The exit criterion asks that
  telemetry prove whether the stable prefix helped. `check_prefix_stability`
  proves the prefix *was* stable; it cannot show the cache hit.
- **Semantic compaction is not implemented**, only requested. The tier that
  costs a model call is the caller's.
- `relay.py` still cannot be imported on Windows (Slice 2A defect, unfixed).

## Verification

`compileall` clean. `tests/test_workcell_context.py` 38 tests. Focused set —
context, checkpoint, acceptance, admission, agent profile, workcell, paired
scoring — **255 passing** (1 skipped where symlinks are not creatable).
`git diff --check` clean.
