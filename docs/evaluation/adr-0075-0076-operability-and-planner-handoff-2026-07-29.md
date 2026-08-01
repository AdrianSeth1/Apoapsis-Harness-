# ADR 0075 + 0076 evidence record: planner handoff and operability contract

Date: 2026-07-29

| Evidence class | Present here |
| --- | --- |
| Live local inference | Only as the **input** that motivated the change (the preserved Crisis Atlas run). No new live local run was performed. |
| Live hosted inference | None. Not run. |
| Deterministic unit | All results below. The delivery tests drive real `git` worktrees, real checkpoint commits, real `VerificationRunner` subprocess execution, and a fake model provider for the coding stage. |
| Live browser | None. |

## Motivating live-local evidence (pre-existing)

Two observations from Crisis Atlas (`PLAN-E1B90639E58D`, 2026-07-29):

**ADR 0075.** The plan required a browser-to-local-API integration and
configured a check that forbade the mechanism. ADR 0074 made that detectable
via `IntegrationContract.runtime_boundary`, but nothing asked a planner to
populate the field, and the ADR 0066 literal example rendered it as
`"runtime_boundary": "unspecified"` — the one value that disables the check,
formatted like an answer rather than a placeholder.

**ADR 0076.** The delivered `APOAPSIS-USING-THE-FINISHED-PROJECT.md` said
"Read `README.md`; it is the project's primary usage guide." That line came
from checking whether the string `README.md` appeared in the archive
inventory. The README was still the seed README, `python -m api.server`
returned 404 at `/`, and no command had ever started the product.

Both are live local evidence. Everything below is not.

## Deterministic verification

Environment: Python 3.14.5, pydantic 2.13.4, Windows.

| Command | Result |
| --- | --- |
| `python -m unittest tests.test_schemas` | 19 tests, **OK** |
| Six-module discovery run: `test_discovery test_discovery_ui test_schemas test_research_units test_provider_and_specification test_intake` | 126 tests, **OK** |
| `python -m unittest tests.test_architect_validation` | 69 tests, **OK** (41 before ADR 0076, 20 before ADR 0074) |
| Seven-module run: `test_architect_slice test_planning_evaluation test_diagnostic_probe test_architect_slice_ui test_architect_cli test_schemas test_discovery` | 144 tests in 266.6s, **1 failure** — the documented pre-existing `test_diagnostic_probe` baseline case, failing at its own `first_no_progress_turn` assertion |
| `python -m unittest discover -s tests` | 1154 tests in 1073.2s, **7 failures, 2 errors, 12 skips** — the documented pre-existing inventory, unchanged, with 36 more tests than the ADR 0072-0074 run and no new failure |
| `python -m compileall -q src tests` | exit 0 |
| `git -c core.whitespace=blank-at-eol,space-before-tab,cr-at-eol diff --check` | exit 0, zero output |

## ADR 0075: what the tests actually pin

The enum placeholder change affects every enum in the handoff, so the tests
assert the *rule* rather than one field's output:

* the `runtime_boundary` placeholder listing every `RuntimeBoundary` member
  and never equalling `unspecified`;
* the placeholder being rendered from the enum itself, asserted against
  `RiskLevel` by reconstructing the expected string from the enum — so a new
  member cannot be missing from the handoff without this test failing;
* a true `Literal` (`schema_version`) still rendering its single constant, so
  the Literal and enum branches stay distinct; and
* the response `kind` enum showing both variants, which is what the
  surrounding prose already said.

The handoff assertions check the generated Markdown for the field name in the
literal shape, the dedicated section heading, all six values documented, the
`--forbid-runtime-network-apis` example, the phrase `rejects the plan`, the
warning against using `unspecified` to avoid deciding, and the binding quality
requirement naming the field.

### Honest limit

ADR 0075 raises the odds a planner populates the field. It cannot guarantee
it. A plan whose contracts are all `unspecified` still validates with the
contradiction check inert, deliberately — inventing a boundary for a planner
that did not assert one is the prose inference ADR 0074 exists to avoid. No
live evidence yet says whether the prose is sufficient in practice.

## ADR 0076: what the tests actually pin

Validation, in `OperabilityContractTests` and
`NetworkedIntegrationNeedsEndToEndProofTests`: missing, unsafe, and unassigned
documentation paths; neither launch field, both launch fields, an unconfigured
launch command, a launch command absent from the whole-project list, a
compliant whole-project launch command, and the explicit-reason-alone case
producing no findings at all; then a networked contract with no scenario, a
scenario proven by a non-acceptance command, an acceptance-designated
whole-project scenario satisfying it, an `in_process` contract needing nothing,
and the check staying silent without command `argv`.

Delivery, in `DeliveredOperabilityTests`, against real worktrees:

* a required artifact absent from the integrated commit blocks delivery, the
  error names the artifact, the plan stays `APPROVED`, and no `delivery.json`
  is written;
* an unexercised launch is recorded as unexercised with the owner's reason
  carried into the ZIP guide, and the old filename heuristics appear demoted
  under a heading labelling them as inference;
* an exercised launch names the command that ran and renders the plan's own
  install, launch, test, and readiness text; and
* the record round-trips through `delivery.json`.

The exercised-launch test uses a `launch-smoke` command configured
`required=False` — the realistic configuration, since the whole configured set
runs for every slice — and relies on ADR 0074 forcing `required=True` for the
final run. That coupling is load-bearing and now has a test that would fail if
it were removed.

### Fixture migration

Three fixtures needed the ADR 0076 change, all in `tests/architect_helpers.py`:
`make_slice` now lists `README.md` in default `suggested_paths`; `make_plan`
names a `primary_documentation_path` and an explicit
`launch_not_runnable_reason`. Two `SlicePackagingTests` cases supply their own
`suggested_paths`, so `make_plan` falls back to a path those slices really do
claim rather than forcing every such test to restate a delivery contract it
does not care about. Stating that a library-change fixture has no launchable
entry point is more honest than inventing a launch command for it.

### Honest limits

* **Nothing reads the README's content.** A slice is responsible for it and
  its presence in the delivered tree is checked. Whether it describes the
  shipped behaviour is a judgement no static check makes.
* **Seed data, demo-only paths, and offline-mode fallbacks stay undetectable.**
  `INTEGRATION_WITHOUT_END_TO_END_PROOF` forces a behavioural acceptance
  command to *exist* for a networked contract; it does not detect the smell,
  and a badly written acceptance command still proves nothing.
* **`launch_measured` means the command ran and passed at the delivered
  commit.** It does not mean the product is usable, and the guide says so.

## What neither ADR does

* Repairs the Crisis Atlas product. Both make its defects reportable or
  preventable; neither rebuilds the integration.
* Changes the authority boundary. No model chooses, proposes, or influences a
  validation finding, a launch command, or an operability field. The launch
  command is a name resolved against owner-configured commands, and Apoapsis
  still never executes planner-authored prose.
* Re-runs the twelve-point Crisis Atlas regression scenario.
