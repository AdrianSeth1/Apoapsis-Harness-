# ADR 0074 evidence record: final integrated verification and plan cross-consistency

Date: 2026-07-29

| Evidence class | Present here |
| --- | --- |
| Live local inference | Only as the **input** that motivated the change (the preserved Crisis Atlas run). No new live local run was performed. |
| Live hosted inference | None. Not run. |
| Deterministic unit | All results below. Tests drive real `git` worktrees, real checkpoint commits, real `VerificationRunner` subprocess execution, and a fake model provider for the coding stage. |
| Hand-run CLI | None for this ADR. |
| Live browser | None. |

## Motivating live-local evidence (pre-existing)

Crisis Atlas (`PLAN-E1B90639E58D`, `qwen3.6-27b` at 32K, 2026-07-29). All four
slices reached `COMPLETE`, each slice's configured checks passed, plan
delivery succeeded, and the delivered commit
`9b9fccbae2b1502a0aadbb583544360624569202` was a functioning backend plus a
separate UI prototype that never called it: 404 at `/`, `Offline Mode`,
in-memory sample data, an incident vanishing on reload, an empty backend list
after browser creation.

No individual slice check could have caught any of it, because no individual
slice was wrong. `prepare_plan_delivery` checked task states and commit
ancestry and never executed the plan's own
`whole_project_verification_commands`.

The same plan also required a browser-to-local-API integration while
configuring a check that forbade the mechanism. Both statements were true;
neither was machine-readable alongside the other.

That is live local evidence. Everything below is not.

## Deterministic verification

Environment: Python 3.14.5, pydantic 2.13.4, Windows.

| Command | Result |
| --- | --- |
| `python -m unittest tests.test_architect_validation` | 41 tests, **OK** (20 before this change) |
| `python -m unittest tests.test_architect_slice` | 41 tests in 155.4s, **OK** (32 before this change) |
| `python -m unittest tests.test_planning_evaluation` | 10 tests in 45.7s, **OK** |
| Twelve-module touched run (see below) | 287 tests in 247.3s, **OK** |
| `python -m compileall -q src tests` | exit 0 |
| `python -m unittest discover -s tests` | 1118 tests in 1050.6s, **7 failures, 2 errors, 12 skips** — exactly the documented pre-existing inventory |
| `git -c core.whitespace=blank-at-eol,space-before-tab,cr-at-eol diff --check` | exit 0, zero output |

### The full-suite run, and the blocker it surfaced

The first full-suite run at this tree reported **12** failures. Five were new,
all in `test_diagnostic_probe`, and all with the same traceback: its
`_single_slice_plan` fixture named no whole-project verification command, so
ADR 0074's `MISSING_WHOLE_PROJECT_VERIFICATION` rejected it at
`_approve_plan`.

That fixture now declares `whole_project_verification_commands=["v2-jobs-tests"]`.
Fixing it also un-masked the module's *pre-existing* failure
(`test_summary_reports_the_read_loop_when_the_model_never_verifies`), which had
begun failing earlier at plan approval rather than at its own
`first_no_progress_turn` assertion — so for one run the baseline defect was
hidden behind a new one.

The second full run reports 7 failures and 2 errors, matching the documented
inventory exactly:

| Module | Count |
| --- | --- |
| `test_acceptance_coverage` | 2 failures |
| `test_desktop_import` | 3 failures |
| `test_desktop_reference` | 1 failure |
| `test_diagnostic_probe` | 1 failure |
| `test_desktop_home` | 1 error |
| `test_desktop_registry` | 1 error |

Three fixtures in total needed the ADR 0074 migration: `architect_helpers
.make_plan`, `test_planning_evaluation._v2_plan`, and
`test_diagnostic_probe._single_slice_plan`. Two were found by targeted runs;
the third only by the full suite.

Twelve-module run: `test_architect_slice test_architect_validation
test_architect_slice_ui test_architect_store test_architect_cli test_ui
test_cli test_schemas test_planning_evaluation test_verification
test_verification_contract test_current_evidence_projection`.

### Fixtures changed by the new validation rule

`MISSING_WHOLE_PROJECT_VERIFICATION` made three existing fixtures invalid,
which is the intended consequence rather than a regression:

* `tests/architect_helpers.py::make_plan` now declares
  `whole_project_verification_commands=["unit-tests"]` by default. This also
  means every pre-existing delivery test now exercises the new
  final-verification gate rather than routing around it.
* `tests/test_planning_evaluation.py::_v2_plan` now declares
  `["v2-service-tests"]`. That is the correct command for that fixture:
  `AC-SVC` is explicitly about the *integrated* service, which is precisely
  the claim per-slice verification cannot support.
* `tests/test_diagnostic_probe.py::_single_slice_plan` now declares
  `["v2-jobs-tests"]`, the only command that fixture has. Found by the full
  suite, not by any targeted run.

Four `test_planning_evaluation` cases and five `test_diagnostic_probe` cases
failed for exactly this reason and pass after the fixture changes.

### Explicitly out of scope

The two pre-existing `test_acceptance_coverage` failures
(`test_stale_worktree_digest_result_does_not_prove_current_code`,
`test_untracked_new_file_creation_invalidates_earlier_proof`) were **not**
addressed. They assert on `_finalize_report`'s return value in
`workflow/vertical_slice.py`, which this change does not touch. The full-suite
run confirms they are still exactly two failures and nothing more. They remain
in `HANDOFF.md`'s known-limitations inventory, undiagnosed.

## The central test: every slice green, integrated project broken

`FinalIntegratedVerificationTests` constructs the Crisis Atlas shape rather
than asserting it abstractly.

Configuration has two commands. `unit-tests` is the per-slice check and
passes for either slice on its own. `integration-check` asserts that *both*
slices' contributions are present together:

```python
"import pathlib, sys;"
"downloader = pathlib.Path('src/download_service/downloader.py').read_text();"
"marker = pathlib.Path('integration.txt');"
"ok = 'get_offset' in downloader and marker.is_file() and "
"'SLICE-2 wired itself' in marker.read_text();"
"sys.exit(0 if ok else 1)"
```

The plan names `integration-check` as its sole whole-project command and maps
`AC-1` to it via an `AcceptanceProofObligation`.

**Failing arm.** `SLICE-1` lands the downloader change; `SLICE-2` lands an
unrelated file. Both tasks reach persisted `COMPLETE` (asserted). Delivery
raises, naming `integration-check` and stating that per-slice history is not
evidence about the combined result. The plan stays `APPROVED`,
`load_plan_delivery` returns `None`, and the persisted record is `failed`
with `AC-1` unproven.

**Control arm.** The same plan, with `SLICE-2` actually contributing
`integration.txt`. Delivery proceeds, the record is `passed`, no criterion is
unproven, and the plan reaches `EXECUTED`. This is what establishes the gate
blocks on the integration defect rather than on something incidental to the
fixture.

### A wrinkle worth recording

`VerificationRunner` executes the *whole* configured command set for every
task, so a genuine integration check fails inside the worktree of any slice
whose counterpart does not exist yet — the first attempt at this test failed
with `SLICE-1: dependency task state is 'FAILED', not COMPLETE`.

The workable configuration is `required = false` for such a command. But
`VerificationResult` only aggregates to `FAILED` on a *required* command, so
honouring that flag at delivery would have let the gate pass on a failing
integrated project — silently, and in exactly the scenario the ADR exists
for. `run_final_project_verification` therefore forces `required = True` for
the final run. Naming a command in `whole_project_verification_commands` is
the owner's statement that it must pass before delivery.

This was found by running the test, not by reading the code.

## Binding, staleness, and fail-closed coverage

| Case | Asserted behaviour |
| --- | --- |
| Commit/branch/worktree/fingerprint binding | Record fields equal the delivery's; fingerprint is 64 hex chars |
| `matches()` with a different commit | `False` |
| `matches()` with a different fingerprint | `False` |
| Planted passing record for another commit | Re-run, not reused; result is for the real commit and real task |
| Malformed artifact | `load_final_project_verification` returns `None`; delivery re-runs and passes |
| Whole-project command absent from configuration | `commands_unavailable`, delivery blocked, plan `APPROVED`, `missing_command_names == ["integration-check"]` |
| Plan naming no whole-project command | `not_configured`, `is_sufficient_for_delivery` `False`, no result, `AC-1` unproven |

The two evidence sections are asserted structurally apart: per-slice entries
are keyed by `slice_id`/`task_id` and carry no `final_commit` or
`worktree_fingerprint` field, while `final_project_verification` carries both
and its command results are exactly `["integration-check"]`. The frontier
handoff contains both headings and the fingerprint; the ZIP usage guide
contains a "What was actually verified" section naming the command.

## Plan cross-consistency coverage

`PlanCrossConsistencyTests` and
`IntegrationVersusVerificationContradictionTests` cover each new finding
code, plus:

* **`same_origin_http` + `--forbid-external-resources` must NOT fire.** This
  is a deliberate drift detector between ADR 0073 and ADR 0074: if it ever
  starts failing, the narrowed flag semantics and the contradiction table
  have diverged.
* `cross_origin_http` + `--forbid-external-resources` does fire.
* A whole-project command's flags govern the contradiction too, not only the
  slice's own command.
* `in_process`, `filesystem`, and `subprocess` boundaries are never
  contradicted.
* `unspecified` asserts nothing and produces no finding.
* With no `configured_commands` supplied, the contradiction check is silent —
  absent information is not evidence of a contradiction.

## What this change does not establish

* It does not repair the Crisis Atlas product. The delivered application
  still serves 404 at `/`, still reports `Offline Mode`, and still ships the
  seed README. This makes the defect *detectable at delivery*; it does not
  rebuild the integration.
* It is not a behavioral check. What a whole-project run proves is exactly
  what the configured commands prove, and ADR 0069/0073 evidence-strength
  reporting applies unchanged. A plan whose whole-project command is a static
  file check still delivers on static evidence.
* The contradiction check only fires when a planner populates
  `IntegrationContract.runtime_boundary`. Planning and discovery prompts do
  not yet ask for it, so coverage currently depends on the planner
  volunteering it. Recorded in `NEXT_STEPS.md` Priority 2.
* Operability obligations remain unenforced (slice D): nothing proves a
  launch command works or that `README.md` matches it.
* The twelve-point Crisis Atlas regression scenario has not been re-run.
* No default, ceiling, context window, or authority boundary was changed.
  Every command executed by the new operation is owner-approved, named in an
  approved plan, and resolved against configured entries.
