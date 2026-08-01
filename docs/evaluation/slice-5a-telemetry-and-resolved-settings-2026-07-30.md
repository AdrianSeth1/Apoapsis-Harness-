# Slice 5A task 4: resolved settings capture and per-call decomposition

Date: 2026-07-30. **No model call was made.** This is instrumentation and one
diagnosis performed against evidence already on disk.

Scope is Slice 5A task 4 only: the telemetry that every later 5A experiment
compares against, and the two Slice 5C items carried into this stage. Tasks 1,
2, 3, 5, and 6 of Slice 5A are untouched, deliberately — each of them changes
behaviour that this telemetry measures, and landing them alongside it would
produce numbers describing no particular configuration.

## Verdict

| Item | Result |
|---|---|
| `resolved_from_cli` root cause | **DIAGNOSED — two causes, one previously unrecorded** |
| Resolved native context capture | **IMPLEMENTED and RUN — all fields unresolved, `resolved_from_cli` stays `False`** |
| `NativeContextPin.auto_compact_threshold = 0.85` | **CORRECT AS A PERCENTAGE, WRONG AS A TRIGGER — real auto threshold is 32,536, not 55,706** |
| Pinned threshold ladder | **MEASURED by executing the CLI's own `computeThresholds`** |
| `qualify.py` trigger | **CORRECTED — now refuses to synthesise a trigger from a percentage** |
| Superseding decision | **ADR 0082** |
| Per-internal-call decomposition | **IMPLEMENTED** |
| The 53,397 figure | **RECLASSIFIED — it is a session aggregate, not a call** |
| The unattributed residual | **PERSISTED AND TERMINALLY UNEXPLAINED** |
| Deterministic coverage | 20 new tests, all passing |
| Workcell/telemetry suites | 159 passed, 1 skipped |
| Full deterministic suite | **NOT RUN — see "What was not verified"** |
| Two `test_acceptance_coverage` failures | **PRE-EXISTING at `d50ddf2`, reproduced on a clean worktree** |

## `resolved_from_cli` had two causes, and the record named one

Slice 5C recorded that `context.autoCompactThreshold` "was never read back from
resolved CLI settings". That is true and it is not the whole fault.

`.apoapsis-eval/slice5c-2026-07-30/qwen-settings-yolo.json` is the settings
document the run installed into the workcell. It contains `selectedAuthType`,
`security`, `telemetry`, `usageStatisticsEnabled`, `providerProtocol`, `tools`,
and `modelProviders`. It contains **no `context` block and no
`model.chatCompression` block at all.**

So there was never a configured value to read back. The run compacted against
whatever the pinned CLI build's own default constant is, and the `0.85` in
`NativeContextPin` was Apoapsis's *belief* about that constant — asserted in a
docstring as "the resolved default for the pinned 0.21.1" and never once
compared against the CLI.

The two causes need different fixes and only the first is done here:

1. **Not read back.** Closed by `parse_native_context` and
   `native_context_pin_from_resolved` in `workcell/pin_capture.py`, wired into
   the conformance capture in `cli/app.py`.
2. **Not set.** Open, and deliberately not fixed in this task. Writing
   `context.autoCompactThreshold: 0.85` into the settings file would make the
   value resolvable immediately — and if the CLI's real default is not 0.85, it
   would also silently change the compaction behaviour of every subsequent run
   while appearing to be a telemetry fix. That is a behaviour change and it
   needs an owner decision and a measured before/after, not a quiet edit inside
   an instrumentation task. **The capture must run first and report what the
   CLI actually answers.** If the observed value is not 0.85, the pin's default
   is wrong and the Slice 5C compaction observations were recorded against a
   threshold nobody knew.

### The capture was run, and it resolves nothing

Run against the pinned `apoapsis-qwen-workcell:0.21.1` image with
`--network none`, **no model call, and no setting written**. Evidence:
`.apoapsis-eval/slice5a-2026-07-30/native-context-capture.json`.

All three fields come back `resolved: false`. `loadSettings` is exported from
`chunks/chunk-EIF3FXTB.js` and resolves fine; the settings simply carry no
`context` or `model.chatCompression` block, and **no chunk in the bundle exports
`DEFAULT_AUTO_COMPACT_THRESHOLD` or `AUTO_COMPACT_THRESHOLD`** for the fallback
to read. So `native_context_pin_from_resolved` returns a default pin and
`resolved_from_cli` stays `False` — which is the correct outcome, not a failure
of the capture.

### The pinned 0.85 is a percentage, and it is not the trigger

Reading the pinned bundle directly — these are its own constants and its own
arithmetic, not a reimplementation:

```
getAutoCompactThreshold() {
  const threshold = this.autoCompactThreshold;
  if (typeof threshold === "number" && threshold > 0 && threshold <= 1) return threshold;
  return void 0;                     // no 0.85 fallback at this layer
}

computeThresholds(window, pct) {
  effectivePct    = clamp(pct ?? DEFAULT_PCT, 0, 1)   // DEFAULT_PCT = 0.85
  effectiveWindow = window - SUMMARY_RESERVE          // SUMMARY_RESERVE = 20_000
  proportional    = effectivePct * window
  absoluteCeiling = effectiveWindow - AUTOCOMPACT_BUFFER   // 13_000
  auto            = min(proportional, absoluteCeiling)
}
```

At the pinned 65,536-token window:

| Quantity | Value |
|---|---:|
| `proportional` (0.85 x 65,536) | 55,705.6 |
| `effectiveWindow` | 45,536 |
| `absoluteCeiling` | 32,536 |
| **`auto` (the actual trigger)** | **32,536** |
| `warn` | 12,536 |
| `hard` | 42,536 |
| Effective ratio | **0.4965** |

So `DEFAULT_PCT` really is 0.85 and the pin's value is right *as a percentage*.
But the proportional term never wins at this window — the absolute ceiling does —
and the run compacted at roughly **half** its context window, not 85% of it.

`tools/slice5c/qualify.py` computes `trigger = native.auto_compact_threshold *
limit`, i.e. 55,706, which is **1.71x the real auto threshold**. Slice 5C still
observed three compaction events, because the real trigger fires *earlier* than
the one it was watching for — the benign direction, and the reason this went
unnoticed. Any statement keyed to "0.85 of the window" is nonetheless wrong, and
`NativeContextPin`'s docstring claim that 0.85 is "the resolved default for the
pinned 0.21.1" is true only of the percentage, not of the behaviour.

This is exactly the divergence the pin's own docstring warned about — "a second,
subtly different model of when compaction happens... the two would diverge
without anyone noticing". They did, and the mechanism was not duplication of the
ladder but **substitution of a single number for it**.

### The ladder is now measured, not derived

`computeThresholds` is exported from the pinned bundle, so it is executed rather
than reimplemented. Three probes, each a measurement:

| Probe | Recovers |
|---|---|
| `(65_536, undefined)` | the authoritative warn/auto/hard for this run |
| `(1_000_000, undefined)` | the built-in percentage — at that width the proportional term governs, so `auto / window` **is** `DEFAULT_PCT` |
| `(65_536, 1.0)` | the auto-compaction buffer — at `pct = 1` the ceiling must govern, so `effectiveWindow - auto` is the buffer |

Result, from
`.apoapsis-eval/slice5a-2026-07-30/threshold-ladder.json`:

| Quantity | Value | Provenance |
|---|---:|---|
| Configured percentage | unset | settings, observed |
| Built-in percentage | 0.85 | wide-window probe |
| Summary reserve | 20,000 | `window - effectiveWindow` |
| Auto-compaction buffer | 13,000 | `pct = 1` probe |
| Effective window | 45,536 | returned |
| **Effective auto trigger** | **32,536** | returned |
| warn / auto / hard | 12,536 / 32,536 / 42,536 | returned |
| Governing term | absolute ceiling | derived |
| Effective ratio | 0.4965 | `auto / window` |
| Source chunk SHA-256 | `634214ec...81c5c` | proves which algorithm answered |

Nothing is scraped from bundle text. A release that changes a constant moves
these numbers automatically; one that renames a constant cannot silently defeat
the capture, because nothing matches on a name. `parse_threshold_ladder` refuses
rather than defaulting if the wide probe fails to land on the proportional term,
so a percentage is never reported that was not measured.

`WorkcellPin.threshold_ladder` carries it, `PIN_SCHEMA_VERSION` goes to **1.2**,
and manifests written before the ladder are not comparable with ones written
after — the honest outcome, since the earlier runs did not know the window they
compacted at.

`tools/slice5c/qualify.py` now takes its trigger from the ladder and **raises**
when none is pinned, rather than falling back to `pct * limit`. A fallback there
would reproduce the withdrawn 55,706 under a new name while looking like a
reasonable default. The decision is recorded in **ADR 0082**, which supersedes
only the threshold-modelling portion of ADR 0081.

**Still not done, deliberately:** `context.autoCompactThreshold` is not written
into the installed settings. That would make the configured percentage
resolvable and would also move the proportional term, changing behaviour. It
needs a measured before/after and an owner decision.

### The capture fails closed

`ResolvedNativeContext` carries a per-field `resolved` flag alongside each
value, and `native_context_pin_from_resolved` returns a **default** pin with
`resolved_from_cli = False` unless every field resolved. A partial capture
therefore degrades to "not checked" rather than to a pin that carries plausible
numbers under the authority of an observation. This is the ADR 0069 shape
applied to a measurement rather than to a verification result, and it is
covered by `test_an_unresolved_capture_degrades_to_defaults_not_to_resolved`.

The node script reports each field's provenance in the CLI's own terms —
`settings:context.autoCompactThreshold` when configured,
`cli_default:<SYMBOL>` when read from the CLI's exported default, and an
explicit unresolved marker otherwise. Default-export names are looked up from a
candidate list; a CLI release that renames one produces an unresolved field, not
a guess.

## The 53,397 figure: evidence retained, description corrected

### A prior claim in this document was wrong

An earlier revision of this record stated the raw evidence was gone and the
anomaly was "permanently undiagnosable". **That was wrong.** The complete
evidence directory was on the Docker Desktop VM disk at
`/mnt/docker-desktop-disk/data/apoapsis-slice5c-2026-07-30/evidence/`, which is
not reachable from the analysis environment and which this task concluded was
absent after finding only five files under `.apoapsis-eval/`. The owner located
it. All 21 files, including every `stage7-*.json`, are now copied to
`.apoapsis-eval/slice5c-2026-07-30/evidence/` on the host and are durable.

The error is worth naming precisely: an unreachable path was reported as a
non-existent one, and "undiagnosable" was asserted from a failed lookup rather
than from a search. It resolved in the direction that closed an open item most
cheaply.

### 53,397 is not a call

`stage7-perturbed-1.json` contains four events. Exactly one `assistant` message
carries usage, and the 53,397 figure is the **`result` event — the CLI's own
session aggregate**:

| Quantity | input | output | cached |
|---|---:|---:|---:|
| `result` aggregate | 53,397 | 475 | 26,487 |
| Exposed `assistant` message | 22,433 | 24 | 19,742 |
| **Unattributed residual** | **30,964** | **451** | **6,745** |

There was never a "second internal call" in the stream. There was a total, one
visible component, and a difference between them. One turn, exit 0, `ARM1`, no
visible tool activity.

### The residual is in every invocation

Running the new decomposition over all six retained stage-7 records:

| Invocation | exposed | aggregate | residual (in/out/cached) |
|---|---:|---:|---|
| stable-0 | 22,431 | 33,427 | 10,996 / 96 / 7,793 |
| stable-1 | 22,431 | 33,427 | 10,996 / 195 / 7,793 |
| stable-2 | 22,431 | 33,427 | 10,996 / 193 / 7,793 |
| perturbed-0 | 22,433 | 33,431 | 10,998 / 175 / 7,793 |
| **perturbed-1** | 22,433 | **53,397** | **30,964 / 451 / 6,745** |
| perturbed-2 | 22,433 | 33,431 | 10,998 / 171 / 7,793 |

So the residual is **structural**: the CLI spends provider tokens on traffic it
never emits an envelope for, in every invocation, at a tightly grouped ~10,997
input tokens in five of six. Only its size in `perturbed-1` deviates — 2.82x the
cohort median. Its cached count is also *lower* there (6,745 against a constant
7,793) despite far more input. **No cause is inferred for any of this.** The
event stream does not contain the evidence that would justify one.

### What changed in the instrument

The `result` event is modelled as a session aggregate, deliberately not as one
of `calls`. Counting it alongside the message it totals compares a sum with its
own component — the same category error the Slice 5C recomputation had to undo
when a max across all messages compared two different prompts. Keeping them
apart is what makes `residual = aggregate - exposed` computable at all.

`ResidualStatus` distinguishes `no_aggregate` (nothing to reconcile against, not
the same as zero), `fully_attributed`, `unattributed_residual`, and
`over_attributed` (exposed exceeds the total, which should be impossible and is
reported rather than clamped).

`flag_residual_anomalies` is a **set-level** function over matched invocations,
because a residual present in every run of a controlled set is a property of the
CLI and flagging it per-invocation would fire on all six. Deviation is only
meaningful against a cohort.

Per-call `flag_anomalies` is retained for streams that do expose multiple calls,
attributing each outlier to `follows_compaction`, `follows_ceiling_stop`,
`first_call`, or the terminal `unexplained`.

**The measured cache benefit is unaffected.** The 2,173-token result was taken
on the first exposed message, which is unchanged; a regression test asserts it
against the retained evidence.

### Binding token-accounting rules for the paired scorer

Task 6 is **not started**. These four rules constrain it when it is, and they
follow from the evidence above rather than from preference:

1. **Total token cost comes from CLI session aggregates.** They are the only
   quantity that includes the residual, and the residual is real spend.
2. **Per-call and cache comparisons come from exposed provider messages.** Only
   those carry a prompt whose prefix can be controlled; the 2,173-token result
   is valid precisely because it was taken there.
3. **The unattributed residual is reported separately, always** — never folded
   into either of the above.
4. **The aggregate is never counted as another call, and its cost is never
   omitted.** Counting it as a call compares a sum with its own component;
   dropping it understates real spend by about a third. Both errors have
   already occurred in this programme, in opposite directions.

### Status and exit condition

The residual is **persisted and terminally unexplained** — not undiagnosable,
and not closed. It closes when a run under instrumentation that can see inside
the aggregate attributes those tokens to something observed. Nothing in the
current event stream can do that, so closing it requires either a CLI that emits
envelopes for its internal traffic or relay-side per-request accounting that
reconciles against the aggregate independently. **That is a scoping input for
Slice 5A task 5, not a finding this task can supply.** Until then, no Slice 5A
benchmark depending on per-call input accounting is settled, because roughly a
third of the input tokens in a controlled invocation are unattributed by
construction.

## What was not verified

**The full deterministic suite did not run.** The repository requires Python
3.11 or later (`apoapsis.specification.schema` imports `enum.StrEnum`,
`apoapsis.config` imports `tomllib`); only 3.10 was available in this session's
environment, and neither a newer interpreter nor a package index carrying one
was reachable.

The 20 new tests in `tests/test_workcell_slice5a_telemetry.py` were run and pass
under a 3.10 compatibility shim applied outside the repository, as did
`test_workcell_slice2c`, `test_workcell_relay`, and `test_paired_scoring` —
159 passed, 1 skipped. That is the suite neighbourhood this task changes, and it
is still weaker evidence than the whole suite: it proves the new logic, not the
absence of regressions elsewhere. `compileall` passes on every changed file; it
has not been run across `src tests` entire.

`tests/test_workcell_session.py` reports nine failures under the shim, all
`AttributeError: 'CoordinatorTests' object has no attribute 'enterContext'`.
`unittest.TestCase.enterContext` was added in **Python 3.11**, so these are a
direct consequence of the interpreter and not a regression. They are named here
rather than filtered out, because a suite run that quietly drops its failures is
the thing this record exists to avoid.

### Two pre-existing failures, and they are not this task's

`tests/test_acceptance_coverage.py` reports two failures under the shim:
`test_stale_worktree_digest_result_does_not_prove_current_code` and
`test_untracked_new_file_creation_invalidates_earlier_proof`. Both were
reproduced on a **clean `git worktree` at `d50ddf2` with none of this task's
changes present**, so they are not caused by this work.

They are not thereby dismissed. Both are worktree-digest tests that spawn `git`
in temporary directories, so an environmental cause is likely — but the
canonical snapshot at `d50ddf2` claims a clean deterministic suite, and under
this environment it is not clean. Whichever way that resolves it is a finding:
either the environment invalidates these two tests, in which case the suite is
less portable than the snapshot implies, or they are genuinely failing at HEAD
and the Slice 5C commit's clean-suite claim is wrong. **This task does not
resolve it and does not assume the flattering answer.**

`python -m compileall -q src tests` and the CRLF-aware `git diff --check` remain
outstanding on an interpreter that can run them.

**Required before this work is trusted:** run the full deterministic suite,
`compileall`, and `git diff --check` on Python 3.11+ on the owner's machine, and
check the two `test_acceptance_coverage` failures there. If they pass on 3.11+,
they were environmental. If they fail, they belong to `d50ddf2`, not here.
