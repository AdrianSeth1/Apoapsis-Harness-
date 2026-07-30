# ADR 0077 paired scorer and frozen Crisis Atlas arms

Date: 2026-07-30  
Evidence class: deterministic rescore of two existing dated records. **No new
inference, no provider call, no container, no browser.**

## Question

Can the two existing Crisis Atlas arms be rescored, without model calls, under
the scoring rules
`docs/handoff-2026-07-30-qwen-baseline-preserving-superiority.md` requires — two
separate scorecards and four separately reported release gates?

And does that rescore reproduce the handoff's central claim, that Slice 2 is
*both* a model proposal miss and a harness detection miss?

## What was built

| Component | Purpose |
| --- | --- |
| `apoapsis/models/ceilings.py` | `CeilingStopReason`, `CeilingEvent`, `classify_ceiling_stop_reason`, and `partition_failures`, so a context or output limit is never counted as a model reasoning failure. |
| `apoapsis/evaluation/paired.py` | `ModelProposalScorecard`, `HarnessDetectionScorecard`, `PairedRunManifest`, `BaselineCapability`, per-case verdicts, and four independent `GateResult`s. No combined score field. |
| `apoapsis/evaluation/crisis_atlas_facts.py` | Both arms frozen as `PairedArmRecord`s, plus `CRISIS_ATLAS_SLICE_FACTS` attributing each slice's misses by owner. |
| `apoapsis/evaluation/paired_report.py` | `paired.json` and `paired.md`. |
| `apoapsis eval-paired` | Rescores a corpus; with no arguments it rescores the frozen arms. |

Every figure in the frozen records traces to
`crisis-atlas-qwen-cli-control-2026-07-30.md` or
`crisis-atlas-64k-codex-frontier-trial-2026-07-30.md`. Nothing was inferred.
Where a record does not contain a figure, the field is left unrecorded and the
metric is `UNMEASURED` with a written reason.

## Result 1: the ceiling classifier reproduces both published diagnoses

The two evaluation records diagnosed their truncations by hand. The classifier
now reaches the same conclusions from the numbers alone:

| Observed call | Input | Output | Context | Output cap | Classification |
| --- | ---: | ---: | ---: | ---: | --- |
| Control README write | 64,409 | 1,127 | 65,536 | 16,384 | `INPUT_CONTEXT_EXHAUSTED` |
| Control next request | — | — | 65,536 | — | `PROVIDER_ERROR_AFTER_ROLLOVER` |
| Sliced call 1/9 | 13,562 | 8,192 | 65,536 | 8,192 | `OUTPUT_CEILING_TRUNCATION` |
| Sliced call 1/11 | 15,551 | 8,192 | 65,536 | 8,192 | `OUTPUT_CEILING_TRUNCATION` |

The first row is the one that matters. Prompt plus completion consumed the whole
window while the output cap was nowhere near reached, so charging it to the
output cap would have justified raising a limit that was never hit. The control
record said exactly this in prose; it is now a computed label.

## Result 2: the historical arms are not a matched pair

`apoapsis eval-paired` with no arguments returns:

| Verdict | Observed value |
| --- | --- |
| Proposal | `INCOMPARABLE` |
| Delivered | `INCOMPARABLE` |
| Capability preservation gate | **`FAILED`** |
| Proposal non-inferiority gate | `UNMEASURED` |
| Delivered superiority gate | `UNMEASURED` |
| Efficiency gate | `UNMEASURED` |
| Median control input tokens | 2,080,801 |
| Median candidate input tokens | 258,632 |
| `recommended_for_default` | `false` |

Two of these were corrected against the first run rather than predicted.

**Capability preservation fails rather than abstaining.** Both records observe
all eight capabilities, so the gate is decidable even though the arms are not a
matched pair: the sliced arm dropped the persistent shell, arbitrary sandbox
commands, the self-directed test/debug loop, and multi-file change without JSON
serialization. Capability is a property of the interface, not of the pairing.

**Efficiency initially reported `passed`, and that was a defect.** The sliced
arm's 258,632 median input tokens really are far below the control's 2,080,801,
but a token median across arms that never shared their controlled variables
measures nothing — and printing "efficiency passed" beside two incomparable runs
is precisely the cheap-arm-looks-good failure this module exists to prevent. The
gate now applies the same disqualification the other two do.

The disqualifying findings are `MATCHED_MANIFEST_UNRECORDED`, principally:

* the sliced arm's **seed commit was never written down** — the trial record
  names four verified checkpoints and no starting commit; and
* its **output cap changed mid-run** (8,192 for Slice 1, 16,384 for Slices 2-4),
  so a single recorded value would misreport half the run.

Several other controlled variables — sampling seed, server flag hash, verifier
version, worktree fingerprint — are absent from both records.

This is the correct answer and it is worth stating plainly: **the two Crisis
Atlas arms cannot be used to claim either mode is better.** They differ in
protocol, one had Codex inside its loop, and their controlled variables were
never bound. The scorer refusing to produce a verdict is the feature.

## Result 3: Slice 2 carries both labels permanently

`CRISIS_ATLAS_SLICE_FACTS` attributes each slice:

| Slice | Reported | Proposal miss | Detection miss |
| ---: | --- | :-: | :-: |
| 1 domain/persistence | `HUMAN_REVIEW_REQUIRED` | yes | **no** |
| 2 services/exports | `COMPLETE` in one call | yes | **yes** |
| 3 HTTP API | `COMPLETE` in one call | yes | yes |
| 4 dashboard/integration | `COMPLETE` after five calls | yes | yes |

Slice 1 is deliberately *not* a detection miss: the harness refused to complete
it. Charging it one would inflate the harness's fault and misdirect the repair.

The sliced arm's detection scorecard records `false_complete_count = 3` and
`missing_evidence_accepted_as_complete = 1`. Its
`obligations_implemented_before_repair` is `0/4`, because every slice needed
Codex before it became a verified checkpoint — and Codex's repair is recorded on
the *delivered* outcome only, so it can never improve the local model's proposal
score.

## Result 4: what each arm lost

Capability observations, per the two records:

| Capability | Control | Sliced Local Power |
| --- | --- | --- |
| Persistent shell | provided | **absent** |
| Repository-wide inspection | provided | provided |
| Ordinary file editing | provided | provided |
| Arbitrary sandbox commands | provided | **absent** |
| Self-directed test/debug loop | provided | **absent** (ADR 0069 terminated on green) |
| Multi-file change without JSON serialization | provided | **absent** (ADR 0071 still serializes) |
| Persistent working directory | provided | provided |
| Context continuation or compaction | **absent** | provided |

The control's single missing capability is the one that ended it: it had no
compaction, so the evaluator — not the agent — started the continuation.

## Honest limitations

- This is a rescore of prose records, not a re-run. It inherits every gap in the
  original two documents, and several metrics are `UNMEASURED` as a result.
- `defects_detected`, `criteria_with_current_state_evidence`, and
  `structured_witness_coverage` are `UNMEASURED` for both arms. Nothing in the
  new code measures them yet; that is handoff slice 4.
- Repair distance in files and lines is `None`, not zero, for both arms. Zero
  would read as "no repair was needed", which is the opposite of the truth.
- Codex token use during the sliced arm's repairs was never recorded, so
  `frontier_repair_cost_usd` is `0.0` and the comparison emits
  `FRONTIER_REPAIR_NOT_ITEMIZED` on any matched pair with a frontier actor.
- `tests/test_paired_scoring.py` (47 cases) and `tests/test_cli.py` (10 cases)
  pass, and `python -m compileall -q src tests` passes. The **full** deterministic
  suite is still deferred to the end of the handoff's slice sequence.
- Those runs used CPython 3.10 with a sandbox-local `sitecustomize.py` shim
  supplying `enum.StrEnum`, `tomllib`, and `datetime.UTC`, because no 3.11+
  interpreter was installable in the evaluation environment. The shim is not in
  the repository. Re-run on the project's real 3.12 interpreter before treating
  this as a clean result.
- No workcell exists. ADR 0077 is a decided boundary, not implemented
  execution.

## Conclusion

The old arms are now replayable without inference, the ceiling conditions the
two records diagnosed by hand are computed labels, and Slice 2's double
attribution is fixed in code rather than in a paragraph.

The first thing the new scorer does with real evidence is refuse to declare a
winner. That is the intended behaviour, and it is a better starting position
than the single number that let a cheaper, worse arm look acceptable.
