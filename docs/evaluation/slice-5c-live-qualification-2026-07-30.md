# Slice 5C: the live Option B qualification

Date: 2026-07-30. One live run through the controller-owned relay, plus one
recomputation of its raw evidence. **No model call was repeated to obtain the
corrected numbers.**

Provenance: `.apoapsis-eval/slice5c-2026-07-30/provenance.json` records the
source commit, the Dockerfile, the build command, both image ids, the complete
controller mount set and the container argv. The workcell's own argv comes from
`WorkcellController.build_create_argv` and is recorded in
`evidence/workcell-config.json`. The build refuses to run with a dirty `src/`.

## Verdict

| Claim | Result |
|---|---|
| Containment | **PASSED** — 22 probes, 0 breaches, 0 unproven |
| No direct upstream from the workcell | **PASSED** — `socket.gaierror: Temporary failure in name resolution` |
| Relay readiness | **READY** — 3 relay requests observed |
| Execution profile, fresh `-p` | `permission_mode=yolo`, 26 tools |
| Execution profile, after `--resume` | **`yolo`, 26 tools — preserved** |
| Native compaction | **NATIVE_COMPACTION_OBSERVED** — 3 events |
| Post-compaction continuation | **VERIFIED** — dependent edit, controller-run tests |
| Efficiency | **MEASURED — 2,173 tokens** |

## What each stage showed

**Containment and mediation.** Every model turn produced non-zero relay
traffic, so nothing reached the model outside the controller's path. The
workcell could not resolve the upstream at all: there is no DNS in the netns,
which is a stronger negative than a refused connection.

**`--resume` preserves the execution profile.** This is the assumption ADR
0081 rests on and it had only ever been established for a fresh `-p`. Session
`4e0de664-da45-4559-83d3-2683ac430e0a` came back at `yolo` with the same 26
tools, no `computer_use__*`, no tool-search surface.

**Native compaction fired three times**, observed as the CLI's own events, not
inferred. Qwen managed its own history exactly as Option B assumes.

**The dependent edit survived the boundary.** `multiply` was written against
the `subtract` added before compaction, both asserted, and the tests were run
*by the controller* rather than believed from the model's report.

**Cache: 2,173 tokens.** Three calls per arm. The stable arm reused a
byte-identical prefix; the perturbed arm changed one early value per call.

| Arm | input_tokens | cached_input_tokens |
|---|---|---|
| stable, call 0 | 22,431 | 19,742 |
| stable, call 1 | 22,431 | **21,915** |
| stable, call 2 | 22,431 | **21,915** |
| perturbed, call 0 | 22,433 | 19,742 |
| perturbed, call 1 | 22,433 | 19,742 |
| perturbed, call 2 | 22,433 | 19,742 |

The stable prefix earns 2,173 additional cached input tokens from the second
call onward. The perturbed arm never moves off its cold-start value. This is
the first efficiency number in the whole programme that is a measurement
rather than an abstention.

## Two corrections applied to the run's own output

Both are recomputations of raw bytes already on disk (`tools/slice5c/recompute.py`).

**1. The cache result was falsely reported `NOT_MEASURABLE`.** Stage 7 read
`cached_input_tokens` directly off the provider message. Qwen Code emits
`cache_read_input_tokens`. `_flatten_usage` has known both spellings since
Slice 2, and Stage 7 simply was not using it — so a measurable result was
recorded as absent telemetry.

This is the failure mode this codebase treats as worse than a plain bug:
**absence of a reading reported as absence of the thing.** It is the same
shape as ADR 0069's green-suite completion. `tests/test_workcell_session.py`
now pins the spelling, including that a missing field stays `None` rather than
becoming `0` — zero would read as "measured, and the cache did nothing", which
is a different claim.

**2. A `3.0005` utilisation figure is withdrawn.** Stage 5 divided 196,639
reported input tokens by the 65,536 window. That total is *aggregate usage
across every internal call Qwen made during the turn*, not one prompt's
occupancy of the context window, so the ratio described nothing — and a
utilisation above 1.0 should have been the tell. No ratio replaces it. The
three observed compaction events are direct evidence and never needed one.

A related subtlety in the cache measurement: a single `qwen -p` invocation
makes several internal calls, and only the **first** carries the perturbed
prefix. Its `input_tokens` is constant within each arm (22,431 / 22,433),
which is what makes the comparison controlled. An earlier recomputation took a
max across all messages and landed on a later call — 27,535 → 29,708 — whose
delta was also 2,173 by coincidence. Same answer, wrong quantity; the record
measures the first provider message and says so.

## Withdrawn on 2026-07-30 by Slice 5A task 4

**Withdrawn: that this run watched the correct predicted compaction trigger.**
Stage 5 computed its target as `auto_compact_threshold * limit` = 0.85 x 65,536
= 55,706. Executing the pinned CLI's own exported `computeThresholds` shows it
returns `min(pct * window, effectiveWindow - AUTOCOMPACT_BUFFER)`, and at this
window the ceiling governs: the real trigger is **32,536 tokens, 49.65% of the
window**. The prediction was 1.71x too high. See ADR 0082 and
`docs/evaluation/slice-5a-telemetry-and-resolved-settings-2026-07-30.md`.

**Not withdrawn — the context-safety result stands entire.** Compaction was
observed as the CLI's own events, three times, never inferred from a token
count; the post-compaction dependent edit was verified by the controller
running the tests rather than believed from the model's report. Neither claim
ever depended on the predicted trigger. It is precisely because the result
rested on observed events that a wrong prediction did not invalidate it — and
precisely why the prediction went unchallenged: the real threshold fires
*earlier* than the predicted one, so compaction happened sooner than stage 5
expected rather than not at all.

The efficiency result is likewise unaffected: 2,173 tokens was measured on the
first exposed provider message and has nothing to do with the threshold.

## Corrected on 2026-07-30: the 53,397 figure

An earlier revision of this section described "an anomalous 53,397-token second
internal call". **There was no second call.** 53,397 is the `result` event —
the CLI's own session aggregate. The invocation exposed exactly one
usage-bearing `assistant` message, at 22,433. The unexplained quantity is a
**30,964-token unattributed residual** (451 output, 6,745 cached).

The same residual is present in all six stage-7 invocations, grouped at ~10,997
input tokens in five of them, so it is structural: the CLI spends roughly a
third of each invocation's input on provider traffic it emits no envelope for.
Only this invocation's residual deviates, at 2.82x the cohort median, with
*lower* cached tokens despite far more input. No cause is inferred; the event
stream does not contain one. Status: **persisted and terminally unexplained.**

The evidence is retained at
`.apoapsis-eval/slice5c-2026-07-30/evidence/`, copied from the Docker Desktop
VM disk.

## Still outstanding

- `context.autoCompactThreshold` was **not** read back from resolved CLI
  settings, so `NativeContextPin.resolved_from_cli` is `False`. The capture has
  since been run: all fields resolve to unset, because the installed settings
  write no `context` block and the bundle exports no default-threshold symbol.
  0.85 is confirmed as `DEFAULT_PCT` but is a percentage, **not** the trigger.
- The 2,173-token benefit is one workload at one prefix size on one server. It
  establishes that the mechanism works and is observable here; it does not
  establish a general saving.
