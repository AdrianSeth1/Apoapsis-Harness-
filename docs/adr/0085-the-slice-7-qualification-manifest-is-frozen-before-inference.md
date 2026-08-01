# ADR 0085: the Slice 7 qualification manifest is frozen before inference

Date: 2026-07-30

Status: accepted. Implements Slice 7 Phase 1. Supersedes nothing; it makes the
handoff's release gates executable.

## Context

The handoff states the release gates in prose. Prose gates are read after the
run, and a bound chosen after seeing which cases regressed is not a bound. The
programme has already been bitten twice by rules that were correct on paper and
unenforced in code: a pinned percentage that did not describe the trigger, and a
"clean suite" claim that had never been observed on the supported interpreter.

Phase 1 therefore writes the experiment down while the outcome is unknown, and
makes the parts that could be quietly bent into type errors instead.

## Decision

**The manifest is immutable and content-addressed.** Every model is
`frozen=True`; the digest is taken over the canonical JSON. Fixing an experiment
defect means issuing a new manifest with a new digest and restarting the
affected pairs — visible by construction rather than by discipline.

`manifest_commit` is recorded but **excluded from the digest**, and required to
differ from `source_under_test_commit`. Hashing a manifest into the tree it
describes would make the source hash self-referential, and committing the
artifact would change the artifact.

**Two scorecards, and no way to combine them.** There is no field, method, or
helper anywhere in `qualification.manifest` that returns a single number
summarising both — a test asserts the absence by scanning the module's own
symbols. `ProposalScore` refuses construction when `repair_applied` is true, so
a repair cannot retroactively improve the model's proposal score at the point of
data entry rather than in review. Final delivered quality is a third, separate
kind.

**Token accounting is the ADR 0082 rule as a shape.** `TokenAccounting`
validates that the residual is exactly aggregate minus exposed, so folding it in
or counting a result aggregate as another call fails validation.

**Abstention is never a pass.** `CaseVerdict` distinguishes ties, regressions,
`NOT_MEASURABLE`, `MISSING_EVIDENCE`, `UNCLASSIFIED_TRUNCATION`,
`INFRASTRUCTURE_FAILURE`, and `INCOMPARABLE`. Only `SANDBOX_BETTER` and `TIE`
return `passed_for_gate`. A required case absent from the verdict map blocks
too — omission is not a pass.

**No arithmetic across cases exists.** `evaluate_gate` loops over required cases
and collects blockers. There is deliberately no mean, sum, or ratio spanning
cases, so an aggregate improvement cannot offset a regression even in principle.

**A mismatch is `INCOMPARABLE`, and nothing is substituted.** `check_pair`
returns the mismatched field names, so the refusal names its own cause.
`cold_start` is a controlled variable: a warm sandbox against a cold control
measures the cache, not the harness.

**Negative controls need their mapped detector.** `allowed_secondary_detectors`
is empty by default and must be frozen explicitly. Detection by an unrelated
check tells you nothing about whether the layer meant to catch it works.

**Capture placeholders fail closed.** Hashes that can only be taken from live
artifacts carry a recognisable placeholder, `unresolved_hashes()` lists them,
and `ready_for_inference()` is false while any remain. Eight are outstanding.

## Consequences

Phase 2 cannot start until the eight live captures are taken. That is the
intended cost: an unbound controlled variable is exactly how two runs come to be
called a pair when they are not.

The corpus implies **24 paired executions (48 arm-runs)** before negative
controls and before cold/warm repetition. That is a large amount of local GPU
time, and the manifest does not pretend otherwise.

Phase 0's repairs are recorded in the manifest with
`counts_as_capability_sandbox_win = False`, and the field raises if set true.
Counting a calibration fix toward the defect-detection claim would be counting
the ruler as a measurement.
