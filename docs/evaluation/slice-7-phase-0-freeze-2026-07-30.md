# Slice 7 Phase 0: the freeze did not pass

Date: 2026-07-30. **No live inference was run.** Phase 0 is a gate and it failed.

## Verdict

| Phase 0 item | Result |
|---|---|
| 1. Commit Slice 5A/6 code, ADRs, tests, docs | **DONE — `f68827e`.** `CON` and `tools/slice_e_bind_plan.js` left untracked |
| 2. Run suites on supported Python 3.11+ | **DONE — Python 3.12.13** |
| 3. Nine `enterContext` failures | **RESOLVED — interpreter-only.** Zero occurrences on 3.12 |
| 4. Two `test_acceptance_coverage` cases | **NOT interpreter-only. They fail on supported Python at the frozen baseline** |
| 5. No live inference if the tree adds an unexplained failure | **Tree adds none — but the baseline carries six** |

**Phase 0 outcome: STOP.** Proceeding to Phase 1 is deferred pending an owner
decision described below.

## The numbers

Same container image, same interpreter, same command.

| Tree | Result |
|---|---|
| `d50ddf2` (frozen baseline, clean worktree) | **6 failed, 1546 passed**, 11 skipped |
| `f68827e` (this work) | **6 failed, 1625 passed**, 11 skipped |

**Identical failure set.** This work adds 79 passing tests and no new failures,
so the literal Phase 0.5 clause — "no live inference if the *current tree adds*
an unexplained deterministic failure" — is satisfied.

That is not the same as the baseline being sound.

## The six failures, and why they matter

```
tests/test_acceptance_coverage.py::test_stale_worktree_digest_result_does_not_prove_current_code
tests/test_acceptance_coverage.py::test_untracked_new_file_creation_invalidates_earlier_proof
tests/test_desktop_import.py::test_absolute_destination_directory_is_rejected
tests/test_desktop_import.py::test_apoapsis_directory_is_excluded
tests/test_desktop_import.py::test_git_directory_is_excluded
tests/test_diagnostic_probe.py::test_summary_reports_the_read_loop_when_the_model_never_verifies
```

All six reproduce on a clean `git worktree` at `d50ddf2` with none of this
work present. They are pre-existing.

The `desktop_import` three were checked for a platform artifact and are not one
— they fail identically on Linux/3.12 in the container **and** on Windows/3.12
natively:

- `AssertionError: ImportSafetyError not raised` — an absolute destination
  directory is accepted when it should be rejected.
- `AssertionError: 'new' != 'skipped_excluded'` (twice) — `.git` and
  `.apoapsis` directories are **not excluded** from import.

These are safety rules that are not firing. A `.git` directory copied through
an import path is the kind of thing the containment work exists to prevent, and
whatever the eventual diagnosis, "the exclusion test fails" is not a cosmetic
state to run a qualification on top of.

The two `acceptance_coverage` failures are the stale-worktree-digest and
untracked-new-file cases — both about *evidence going stale*, which is the exact
property Slice 6 was built to guarantee and Slice 7 would be measuring.

## A second finding: the suite has no green platform

The full deterministic suite **cannot run on Windows at all**.
`tests/test_workcell_relay.py` fails at collection with
`module 'socketserver' has no attribute 'ThreadingUnixStreamServer'` — the relay
tests are Unix-only, and collection is interrupted before anything else runs.

So the supported configuration is Linux with Python 3.11+, and the repository
does not say so. More importantly: the release gate reads *"the deterministic
suite must add no failures"*, and it is being evaluated against a baseline that
has never been observed green in any single environment. The `d50ddf2` commit
message's clean-suite claim does not hold on the supported interpreter.

## Why this stops the phase rather than being noted in passing

Phases 3–5 consume many hours of live GPU time across eight task families, three
seeds, two arms, cold and warm, plus ten negative controls. The Phase 6 decision
then turns on per-case comparisons.

Running that on a baseline with six unexplained failures — three of them safety
checks, two of them about stale evidence — would produce a qualification whose
own release gate cannot be evaluated honestly. If a case then regressed, there
would be no way to separate a Capability Sandbox defect from a defect the
baseline already had.

The Phase 0 instruction exists to prevent exactly this, and its intent is
clearer than its literal wording: the frozen baseline is supposed to be sound
before inference is spent against it.

## What is needed before Phase 1

1. **Diagnose the three `desktop_import` failures.** Either the safety rules
   regressed, or the tests encode a rule the code deliberately stopped
   enforcing. Either answer is fine; neither is currently written down.
2. **Diagnose the two `acceptance_coverage` failures**, which sit on the
   stale-evidence property Slice 7 would be measuring.
3. **Diagnose `test_diagnostic_probe`** — `AssertionError: unexpectedly None`.
4. **Record the supported platform** as Linux + Python 3.11+, since the suite
   cannot be run to completion anywhere else.
5. Re-run Phase 0, then proceed to the qualification manifest.

**Owner decision available:** if these six are judged out of scope for the
Capability Sandbox claim, Phase 1 can proceed with them recorded as a known,
frozen, arm-independent baseline defect set — they are constant across both
arms, so a per-case comparison is still meaningful. That is a legitimate call.
It is not one this task should make silently, because it weakens the "no new
failures" gate to "no new failures beyond these six", and that weakening should
be explicit in the qualification manifest rather than discovered later.

## What was committed

`f68827e` — Slice 5A minimal profile and Slice 6 authoritative repair
checkpoints: ADRs 0082, 0083, 0084; `call_decomposition`, `diagnostics`,
`plan_checkpoint`, `runtime_profile`; three new test modules; canonical docs
updated. `CON` and `tools/slice_e_bind_plan.js` preserved untracked.
