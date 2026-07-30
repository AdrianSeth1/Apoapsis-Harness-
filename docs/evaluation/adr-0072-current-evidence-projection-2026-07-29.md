# ADR 0072 evidence record: current-evidence projection

Date: 2026-07-29

This record separates three kinds of evidence, and the separation is the
point. Nothing below is a live-model claim.

| Evidence class | Present here |
| --- | --- |
| Live local inference | Only as the **input** that motivated the change (the preserved Crisis Atlas run). No new live local run was performed for ADR 0072. |
| Live hosted inference | None. Not run. |
| Fake-provider / deterministic unit | All verification results below. |
| Live browser | None for this change. |

## Motivating live-local evidence (pre-existing, not produced by this change)

From the preserved evaluation project
`.apoapsis-eval/crisis-atlas-slice-wrap-2026-07-29`:

| Item | Value |
| --- | --- |
| Discovery session | `DISC-571F85289FB8` |
| Approved corrected plan | `PLAN-E1B90639E58D` |
| Slice 4 task | `TASK-5494B387C75F90D0FDE114A7` |
| Final integrated commit | `9b9fccbae2b1502a0aadbb583544360624569202` |
| Local model | `qwen3.6-27b`, Q4_K_M, reasoning disabled, 32,768-token context |
| Hosted inference | Not run |

Slice 4 stopped at `human_review_required` carrying a failed verification. A
hash-bound manual-frontier patch was applied, its verification passed, and the
task reached a persisted `COMPLETE`. The plan then delivered. The delivered
`delivery.json` and the generated whole-project frontier review handoff both
reported the pre-repair `human_review_required` outcome and its failed run,
because `architect.delivery` read each task's one-time `report.json`.

That observation is live local evidence. Everything below is not.

## Deterministic verification performed for this change

Environment: Python 3.14.5, pydantic 2.13.4, Windows, repository working tree
at `C:\Users\aryam\local harness`.

| Command | Result |
| --- | --- |
| `python -m unittest tests.test_current_evidence_projection` | 21 tests, **OK** |
| `python -m unittest tests.test_architect_slice` | 32 tests, **OK** (includes the 4 new `DeliveryCurrentEvidenceTests`) |
| `python -m unittest tests.test_review tests.test_review_ui tests.test_review_execution tests.test_review_hardening tests.test_review_frontier_stage tests.test_manual_frontier tests.test_manual_frontier_ui tests.test_cli tests.test_execution_ui tests.test_architect_slice_ui tests.test_current_evidence_projection tests.test_acceptance_coverage` | 213 tests in 379.6s, **2 failures** (both pre-existing, see below) |
| `python -m unittest tests.test_ui tests.test_ui_copy_and_accessibility tests.test_intake_ui tests.test_discovery_ui tests.test_verification tests.test_verification_contract tests.test_evaluation` | 173 tests in 103.2s, **OK** |
| `python -m compileall -q src tests` | exit 0 |
| `git -c core.whitespace=blank-at-eol,space-before-tab,cr-at-eol diff --check` | exit 0 |
| `git diff --check` (plain) | noisy, as previously documented for this checkout's mixed CRLF/LF working-tree state |
| `python -m unittest discover -s tests -v` | **not completed** — started twice, stopped partway at the owner's explicit request |

### The two failures

```
FAIL: tests.test_acceptance_coverage.AcceptanceCoverageTests
      .test_stale_worktree_digest_result_does_not_prove_current_code
FAIL: tests.test_acceptance_coverage.AcceptanceCoverageTests
      .test_untracked_new_file_creation_invalidates_earlier_proof
```

Both assert `report.outcome == HUMAN_REVIEW_REQUIRED` on the `FinalTaskReport`
returned directly by `run_vertical_slice`, and both observe
`TaskOutcome.COMPLETE`. That value is produced by
`workflow/vertical_slice.py::_finalize_report`, which this change does not
touch; the current-evidence projection is not in that code path.

These are the same two cases `HANDOFF.md` already lists by name under "Known
limitations and active risks", identified on 2026-07-26 and confirmed then to
be independent of ADR 0063 by reproduction in a neutralized scratch copy. They
are pre-existing and remain undiagnosed.

**What was not done:** a fresh stash-and-rerun to re-confirm these two against
an unmodified tree in this session. The reasoning above is a code-path
argument plus the existing documented inventory, not a new controlled
reproduction. Treat it as such.

## What the new tests actually prove

`tests/test_current_evidence_projection.py` builds every fixture from
persisted state and on-disk artifacts, using the real
`SQLiteTaskStore.transition` API so each state sequence is one the workflow
engine would permit. No provider, fake or otherwise, is involved.

Covered:

* the untouched first stop, where `report.json` is correctly current;
* local continuation, Local Power sandbox continuation, frontier continuation,
  and fresh frontier stage completions superseding a `human_review_required`
  report;
* manual-frontier repair completion — the literal Crisis Atlas shape;
* a verification retry that remains `HUMAN_REVIEW_REQUIRED`, reporting the
  retry's own commands rather than the report's;
* missing, malformed, and unidentifiable newer evidence, each asserted against
  a deliberately **passing** stale report so that a silent fallback would look
  like success rather than an obvious error;
* an unmapped decisive event and a continuation completion with no preceding
  started event, both failing closed;
* `report.json` byte-identical before and after projection;
* acceptance coverage recomputed from the immutable result's own
  `required`/`acceptance` flags, including non-acceptance and skipped commands;
* coverage carried on a stop event payload taking precedence.

`tests/test_architect_slice.py::DeliveryCurrentEvidenceTests` exercises real
git worktrees and a real plan/slice lifecycle with a fake model provider, and
asserts:

* `delivery.verification_summary[0]`'s outcome, per-command results,
  generation, and sources all equal a freshly computed projection for the same
  task — one evidence generation, not two;
* the frontier handoff carries the provenance and the retitled
  "Per-slice verification history" section;
* delivery refuses a persistently `COMPLETE` slice whose `report.json` is
  deleted (`integrity=missing`) or corrupted (`integrity=malformed`), leaves
  the plan `APPROVED`, and writes no `delivery.json`;
* `project_slice_status` and `delivery.json` agree on the generation.

## What this change does not establish

* It does not repair any Crisis Atlas **product** defect. The delivered
  application still serves 404 at `/`, still says `Offline Mode`, still uses
  in-memory sample data, and still ships the seed README. Those follow from a
  verification contract that cannot distinguish a same-origin API call from an
  external resource — remediation slice B — and from the absent integrated
  gate and operability contract, slices C and D.
* It does not add a whole-project integrated verification gate. Delivery now
  *labels* per-slice history honestly and refuses unevidenced slices; it still
  does not run the plan's `whole_project_verification_commands` against the
  integrated worktree.
* It has not been exercised against the preserved Crisis Atlas evaluation
  project. The regression scenario in
  `docs/handoff-2026-07-29-crisis-atlas-remediation.md` has not been re-run.
* No default, ceiling, context window, or authority boundary was changed.
