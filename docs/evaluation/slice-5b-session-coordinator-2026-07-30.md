# Slice 5B: the session coordinator, and three corrections of authority

Date: 2026-07-30. Deterministic evidence only. **No live session has run
through this code.**

## What was required

Review required Slice 5B before Slice 6, with three adjustments and seven exit
criteria. This record states plainly which are met and which are not.

## The three adjustments

**1. Volatility is provenance, not shape — done.** `TaskKernel` no longer
raises on timestamp-, UUID- or request-id-shaped text. The kernel is rendered
once by `persist_kernel`, written, hashed, and read back on every call via
`KernelArtifact.load_text`, which raises `KernelDriftError` if the bytes moved.
`scan_volatility` survives as advisory hints recorded on the artifact.

A test asserts that `Fix tenant 3f2b1c4d-…'s export` is now a legitimate
objective and is reported as a `uuid` hint rather than refused.

**2. Provider-reported tokens only — done.** `TokenLedger` separates reported
usage from local estimates. `CompactionPolicy.should_compact_reading` returns
`False` for an unreported reading; `evaluate_budget` lists the token ceilings in
`unenforced` and says `UNENFORCED` in the verdict detail rather than reading a
missing ledger as zero spend. `TokenLedger.estimate_error` keeps the estimate
useful for diagnosis without giving it a vote.

**3. Progress is authoritative state advancement — done.** `TurnObservation`
carries the worktree fingerprint, newly discharged obligation ids, and
controller-produced evidence artifact ids. Any of the three counts.
`ProgressTracker` remembers what it has already seen, so re-reporting an old
discharge is not progress. There is no field for the model's account of its own
turn.

## The seven exit criteria

| # | Criterion | Status |
|---|---|---|
| 1 | A live session coordinator owns kernel, capsule, budget and checkpoint loop | **Met in code, unexercised live.** `SessionCoordinator` owns all four and calls `run_checkpoint`. |
| 2 | Mechanical and semantic compaction both execute, or semantic failure stops explicitly | **Half met.** Mechanical executes. Semantic is a `SemanticCompactor` callable with **no production implementation**, so today the failure path is what executes: unconfigured, empty, or raising all stop with `COMPACTION_FAILED`. |
| 3 | The 58,038-token scenario triggers compaction before another oversized request | **Met deterministically.** A test drives a reported 58,038-token reading and asserts the transition order: `AWAITING_MODEL` → `COMPACTING` → `AWAITING_MODEL`. The second model call cannot occur before compaction is recorded. |
| 4 | Post-compaction Qwen retains the capsule and continues editing/testing | **NOT MET.** The capsule provably survives compaction and renders its obligations, advisory notes and no-progress actions. Whether a real Qwen picks up from it and keeps working is unmeasured, and no deterministic test can settle it. |
| 5 | Cached-input / prompt-evaluation telemetry shows whether the stable prefix helps | **NOT MET.** `cached_input_tokens` is plumbed from the CLI's usage events into `TokenLedger` and onto `SessionRecord`. `PrefixDrift` proves the prefix is *stable*. Nothing yet shows stability buys cache hits against this provider. **The efficiency claim does not exist.** |
| 6 | Budget breaches terminate through recorded state-machine outcomes | **Met.** Seven `SessionOutcome` values, every ending through `_stop`, every stop a `SessionTransition` with a reason. A test asserts the budget is checked *before* the model call, so no call is spent discovering the allowance was gone. |
| 7 | ADR 0080 records the policy and its evidence limitations | **Met.** `docs/adr/0080-context-authority-and-the-session-coordinator.md`. |

Three of seven met, one met deterministically, one half met, two not met. The
two unmet criteria are both live-run criteria, and both are exactly the claims
the slice exists to support.

## What this does not license

The context-safety claim and the efficiency claim are both unevidenced. Criteria
4 and 5 need a real workcell session against the pinned Qwen, and until that
run exists this slice has produced enforcement machinery with a scripted agent
behind it.

Also outstanding: `TurnResult.observation` is accepted but not routed through
`bound_observation`, so per-tool output budgets bind only where a caller applies
them; `relay.py` still cannot be imported on Windows.

## Verification

`python -m compileall -q src tests` clean. 192 tests across
`test_workcell_context`, `test_workcell_session`, `test_workcell_checkpoint`,
`test_workcell_acceptance`, `test_workcell_admission` and `test_paired_scoring`
pass on the host's real interpreter, 1 skipped. `git diff --check` clean.

The standing 12 pre-existing failures elsewhere in the suite are unchanged and
still reproduce at commit `0fb4e39`.
