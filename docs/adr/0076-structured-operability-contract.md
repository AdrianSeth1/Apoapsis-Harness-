# ADR 0076: The operability contract is structured, and delivery checks it

- Status: Accepted
- Date: 2026-07-29

## Context

Crisis Atlas remediation slice D. The approved plan named a launch command
and required a README. Neither was proven at delivery.

What the delivered ZIP's `APOAPSIS-USING-THE-FINISHED-PROJECT.md` said:

> 1. Read `README.md`; it is the project's primary usage guide.

That line was produced by checking whether the string `"README.md"` appeared
in the archive's file inventory. The README was still the seed README. No
command had ever started the product — `python -m api.server` returned 404 at
`/`. The guide could not have known either way, and its confident phrasing
made an unverified inference read as a documented fact.

Three separate gaps:

**The launch path was prose.** `PlanDeliveryContract
.launch_or_usage_instructions` is a free-text field. Apoapsis never executes
it, correctly — executing planner-authored prose would hand a model shell
authority through the back door — but that left the canonical launch path with
nothing behind it at all.

**Required artifacts were never compared to the delivered tree.** ADR 0074
made plan validation check that some slice was *responsible* for each
`required_artifacts` entry. Nothing checked the artifact was actually shipped.

**The usage guide inferred from filenames.** `package.json` present meant "this
is a Node project"; `README.md` present meant "read it first". Reasonable
guesses, presented as the project's documented path.

The handoff also asks for "no seed, placeholder, demo-only, or offline-mode
behavior where the plan requires the real backend". That is not statically
detectable, and detecting it would need exactly the prose/keyword inference
barred from gates. This ADR does not pretend otherwise; see Decision 4 for the
structural lever it uses instead.

## Decision

### 1. Two structured fields make the launch claim checkable

`PlanDeliveryContract` gains:

```python
launch_verification_command: str = ""   # name of a configured command
launch_not_runnable_reason: str = ""    # explicit owner-written excuse
```

`launch_verification_command` names a **configured verification command**,
never a shell string. The canonical launch path therefore stays an
owner-approved structured command the harness already knows how to execute
safely and inside the existing backend/timeout/environment boundary. Prose
fields are unchanged and still never executed.

Validation requires **exactly one** of the two:

| Code | Condition |
| --- | --- |
| `MISSING_LAUNCH_CONTRACT` | Neither is set. |
| `AMBIGUOUS_LAUNCH_CONTRACT` | Both are set. |
| `UNKNOWN_VERIFICATION_COMMAND` | The named command is not configured. |
| `LAUNCH_COMMAND_NOT_WHOLE_PROJECT` | The command is not in `whole_project_verification_commands`, so it would never run against the integrated project. |

The escape hatch is deliberate and deliberately costly: an owner who cannot
launch the product must write down why, which turns an unmeasured launch from
silence into a visible statement that travels into the delivery record and the
ZIP.

### 2. Documentation must be identified and owned

| Code | Condition |
| --- | --- |
| `MISSING_PRIMARY_DOCUMENTATION` | `primary_documentation_path` is empty. |
| `UNSAFE_PRIMARY_DOCUMENTATION_PATH` | It is not a safe repository-relative path. |
| `UNASSIGNED_DELIVERY_ARTIFACT` | It is in no slice's `suggested_paths`, so no slice is responsible for writing or updating it. |

The third is the one that matters for Crisis Atlas: naming a README nobody is
responsible for updating is how a seed README survives to delivery.

### 3. Delivery compares the contract to the delivered tree

`assess_delivered_operability` produces a `DeliveredOperability` record from
the plan's structured contract and the `git ls-tree` inventory of the
integrated commit. A `required_artifacts` entry or the
`primary_documentation_path` missing from that inventory raises
`SlicePackagingError`: the plan stays `APPROVED`, no ZIP, no `delivery.json`.

`PlanDelivery` gains an `operability` field carrying, separately:

* `primary_documentation_present` — the artifact is in the tree;
* `launch_measured` — the plan named a launch command *and* that command runs
  against the integrated project, which delivery has separately proven passed
  (ADR 0074), so a launch command reaching here has genuinely been exercised
  at the delivered commit; and
* `launch_unmeasured_reason` — the owner's explicit excuse.

Three states the old guide collapsed into one reassuring sentence.

### 4. The usage guide renders the contract, and labels what it infers

The ZIP guide now opens with `## Install, launch, and test`, which states in
its first line whether launch was exercised, by which command, or why not; then
renders the plan's own `install_instructions`,
`launch_or_usage_instructions`, `test_instructions`, and `readiness_checks`
verbatim; then says plainly that Apoapsis reproduces these and does not
execute prose.

The old filename heuristics survive under `## If you need more than the above`,
explicitly labelled as inferred from filenames rather than taken from the plan.
Demoting them rather than deleting them keeps something useful for a plan whose
delivery contract is thin, without letting a guess impersonate documentation.

### 5. A networked integration needs behavioural proof

`INTEGRATION_WITHOUT_END_TO_END_PROOF` (ERROR): an `IntegrationContract`
whose `runtime_boundary` is `same_origin_http` or `cross_origin_http`, and no
`end_to_end_scenario` proven by a command that is both acceptance-designated
and run against the integrated project.

This is the structural answer to "no offline-mode behaviour". The harness
cannot detect seed data, a demo-only path, or an offline fallback — that needs
the inference this codebase bars from gates. What it can do is refuse to let a
contract that crosses an origin at runtime exist with nothing but static
evidence behind it, which forces the owner to configure a command that would
notice. Crisis Atlas had no such command; that is why nothing caught
`Offline Mode`.

Like ADR 0074's contradiction check, this needs `configured_commands` for the
`acceptance` flags and is silent without them.

## Consequences

### Migration

**A plan must now name `primary_documentation_path` and exactly one of the two
launch fields.** Plans approved earlier and not yet delivered must be revised
and re-approved — the same standing consequence ADR 0074 introduced, for the
same reason: there is no evidence to substitute.

**Delivery can now fail on a missing artifact** after passing every
verification gate. The error names the artifact.

**`PlanDelivery` gains a required `operability` field.** A record written
before this change lacks it.

Three test fixtures needed updating, all in the same direction: the default
`make_slice` now lists `README.md` in `suggested_paths` and the default
`make_plan` delivery contract names it as primary documentation with an
explicit "library change; nothing to launch" reason. Stating that a fixture has
no launchable entry point is more honest than inventing a launch command for it.

### What this does not do

It does not prove the README's *content* is current. Nothing here reads the
file. A slice is now responsible for it and its presence is checked; whether it
describes the shipped behaviour is a judgement no static check makes.

It does not detect seed data, demo-only paths, or offline-mode fallbacks. It
makes a plan that requires a networked integration configure a command that
could catch them, and it makes an unexercised launch visible. That is the limit
of what a harness that executes only owner-approved structured commands can
honestly claim.

It does not execute prose, and it does not give the launch command any
authority an ordinary verification command lacks.

### Rejected alternatives

**Execute `launch_or_usage_instructions` as a shell command.** The handoff
rules this out and it is right to: it is planner-authored text, and running it
would hand a model shell authority through the delivery path. Naming a
configured command keeps the owner in the loop.

**Infer the entry point from `runtime_design.entry_points` or file names.**
This is what produced the misleading guide. Filenames are a fallback now,
labelled as such.

**Make the launch command mandatory with no excuse field.** Would make library
plans, data pipelines, and anything without a long-running process
undeliverable. The excuse must be written down, which is the actual
requirement.

**Read the README to check it mentions install/launch/test/persistence.** A
keyword check on documentation content is the prose inference barred from
gates, and it would be trivially satisfied by a README listing the words.

## Verification

`tests/test_architect_validation.py`:

* `OperabilityContractTests` — missing, unsafe, and unassigned documentation
  paths; neither launch field, both launch fields, an unconfigured launch
  command, a launch command absent from the whole-project list, a compliant
  whole-project launch command, and the explicit-reason-alone case producing no
  findings at all.
* `NetworkedIntegrationNeedsEndToEndProofTests` — a networked contract with no
  scenario, a scenario proven by a non-acceptance command, an
  acceptance-designated whole-project scenario satisfying it, an `in_process`
  contract needing nothing, and the check staying silent without command argv.

`tests/test_architect_slice.py::DeliveredOperabilityTests` — a missing required
artifact blocking delivery with the plan left `APPROVED` and no
`delivery.json`; an unexercised launch recorded as unexercised with the reason
carried into the ZIP guide and the filename heuristics demoted and labelled; an
exercised launch naming the command that ran, with the plan's own install,
launch, test, and readiness text rendered; and the record round-tripping
through `delivery.json`.
