# ADR 0079: Completion is readiness, and evidence is controller-owned

- Status: Accepted
- Date: 2026-07-30
- Supersedes the completion rule of ADR 0069. That ADR remains decision
  history and its anti-redundancy reasoning stands; only its termination
  condition is replaced.
- Builds on ADR 0077 (workcell boundary) and ADR 0078 (envelope conformance).

## Context

ADR 0069 ended a session as soon as every configured verification command had
passed for the current worktree fingerprint. The reasoning was sound — a model
re-running an unchanged check wastes calls — and the rule was wrong in a way
that only showed up under a specific shape of failure.

Crisis Atlas Slice 2 is that shape. The slice asked for `IncidentService`,
`ExportService`, and tests. Qwen's first response said it would implement both
services, then proposed a single partial file at `services/incident_service.py`
— the wrong package path — with no export service and no tests. Apoapsis
applied it, ran the inherited checks, observed green, and terminated the
session `COMPLETE`.

The inherited tests stayed green **precisely because they never imported the
new file**. Greenness was evidence that nothing had changed, and it was read as
evidence that everything had. The model made an incomplete first edit; the
harness converted it into a final result, and never gave the model a turn in
which it could have noticed its own omission.

Slice 4 of the baseline-preserving handoff built the decision kernel for the
replacement: `SliceAcceptanceContract`, the new-component rule, versioned
structured witnesses. Review correctly found it **operationally incomplete** —
the production tree had no caller for `evaluate_checkpoint` and no emitter for
`StructuredWitness`, so the rule was enforceable in principle and unenforced in
practice. This ADR records the decision the completed integration implements.

## Decision

### 1. Completion is readiness against a contract compiled before spend

Every approved slice compiles to a `SliceAcceptanceContract` **before the first
model call**, from fields the planner already fills in and the owner already
approved: declared paths, declared symbols, acceptance criteria, integration
contracts, verification commands.

Before, because a contract written afterwards is written by someone who has
already seen what the model produced.

The contract refuses two shapes at construction: a criterion no obligation
could prove, and an obligation naming nothing that could discharge it. Both
would be unsatisfiable in a way nobody notices until delivery.

### 2. Configured commands cannot reach a completion decision

`evaluate_checkpoint` takes an admission result and a readiness report. It
takes **no command results at all**, and a test asserts its signature.
Greenness is one input to `evaluate_slice_readiness`, several layers down,
weighed against obligations.

Its outcomes are `COMPLETE`, `CONTINUE`, `CANDIDATE_REFUSED`, and
`HUMAN_REVIEW_REQUIRED`. **`CONTINUE` is the one that did not exist**: admitted
work, obligations outstanding, and the agent gets another turn to finish its
own stated plan.

### 3. Evidence is a structured witness, and the controller produces it

A configured command's *name* is not evidence. `StructuredWitness` records what
a run did: the process launched, its readiness condition, the address it
actually bound, the routes exercised with methods and normalised assertions,
mutations and the reads that followed them, cleanup, coverage, artifact hashes,
and the criteria it claims. `validate_witness` refuses eight shapes, including
a witness whose only content is its name and an exit code.

**Coverage is derived from an artifact the controller produced and hashed.**
`emit_test_witness` deletes the coverage artifact before the run, tells the
command where to write it, then reads and hashes that file itself;
`CoverageObservation.source_artifact_sha256` records which file the numbers came
from. A coverage claim arriving as text is never accepted — not because a model
would necessarily lie, but because a claim cannot be distinguished from a
mistake, a stale run, or a different tree.

Emitters fail closed. A run that produced no artifact yields no witness, rather
than a witness with an empty coverage section, because an empty section is
indistinguishable from one that found nothing.

### 4. The rule is about changed behaviour, not changed files

Slice 4's new-component rule looked at added files. Crisis Atlas Slice 3's
unreachable export routes lived in a **modified** file, which a file-level rule
cannot see: the file was already covered, and the addition inside it was not.

The unit is now a `BehaviourUnit` — a whole added production file, a new
top-level symbol inside a modified one, or a new route literal — each with a
line range, checked against line-level coverage. Routes are additionally
satisfied by a launch or behavioural witness that *called* them, which no
coverage tool reports.

Python symbols come from `ast` and are exact. Routes come from a narrow regex
over routing-looking lines and are marked `heuristic=True`; a route it invents
is a false obligation the owner can see and delete, which is the safe direction
for a heuristic to err.

### 5. The checkpoint loop is the caller

`run_checkpoint` runs the ordered sequence: freeze and compute the delta, admit
or refuse atomically, emit witnesses **against the admitted snapshot**,
evaluate readiness, decide.

Witnesses are emitted against the snapshot rather than the workcell so a
command can never be observed running over a file the policy refused. And an
emitter failure cannot coexist with `COMPLETE`: if emission failed, the
evidence readiness accepted cannot be current, so the loop downgrades rather
than trusting the coincidence.

## Consequences

### Migration

**A slice with no acceptance criteria will not compile a contract**, and
therefore cannot complete under this rule. Plans whose slices carry no
`acceptance_criterion_ids` must be revised — the same standing consequence ADR
0074 and 0076 introduced, for the same reason: there is no evidence to
substitute.

**A verification command that emits no structured witness proves nothing.**
Existing configured commands still run and still gate, but their passing is now
necessary rather than sufficient, and a command that cannot be wrapped into a
witness leaves its obligations unproved.

**`ReadinessBlock.NEW_COMPONENT_UNEXERCISED` is renamed**
`CHANGED_BEHAVIOUR_UNEXERCISED`, and `SliceReadinessReport
.unexercised_new_components` becomes `unexercised_behaviour`, carrying
`unit_id`s rather than paths.

### What this does not do

It does not make the harness able to tell a good implementation from a bad one.
It makes the harness able to tell an *unproven* one from a proven one, which is
a narrower and more achievable claim.

It does not validate coverage independently. `collection_method` and
`source_artifact_sha256` force the wrapper to say how it measured and from
which file; nothing here re-derives coverage from first principles. A wrapper
that writes a false artifact produces a witness that validates.

It does not replace the legacy Local Power loop, which still terminates under
ADR 0069. The two coexist until the Capability Sandbox becomes the default.

### Rejected alternatives

**Keep green-test termination and add a "did the model do what it said" check.**
That requires reading the model's stated plan and comparing it to its output —
prose inference of exactly the kind this codebase bars from gates.

**Accept the model's own coverage report.** It is usually right, and there is
no way to tell when it is not. The whole point of the Crisis Atlas record is
that a confident report and a true one were indistinguishable.

**Require every symbol in a new file to be individually covered.** Stricter
than the handoff asks, and it would let an untested helper function block a
slice whose actual behaviour is proven.

**Make forbidden classes or the readiness rule configurable.** A configuration
that could switch off the new-component rule would make it advisory, and
advisory is what ADR 0069 already was.

## Amendment, 2026-07-30 (Slice 4C)

Two authority defects in the first integration, both corrected before Slice 5.

**Advisory plan metadata must not become a completion gate.**
`ImplementationSlice` documents its own cross-references as "advisory proposals
from the planner". The compiler read `suggested_symbols` and
`integration_contract_ids` and made each a mandatory obligation marked
intentionally unmeasured — turning a suggestion into a gate no evidence could
open, and routing every slice declaring a symbol to permanent human review.
The compiler now ignores both. Interface and integration obligations exist only
from the owner-approved `required_interfaces` and
`required_integration_routes`.

**Required-command success is derived, never supplied.** `passed_commands` was
a caller parameter, which could describe a different tree or an earlier turn —
the stale-evidence problem this ADR refuses everywhere else. It is removed from
`evaluate_slice_readiness` and `run_checkpoint`; a command counts as passed only
when a *usable* witness reports it, so a stale witness cannot open the gate.

To keep real interface obligations expressible, `AcceptanceObligation` gains
`required_symbols` and `required_routes`, discharged from
`CoverageObservation.observed_symbols` and from a witness's actual HTTP
exchanges respectively — observation, not assertion.

## Verification

`tests/test_workcell_acceptance.py` — the decision kernel: witness validation
including `COMMAND_NAME_ONLY`, staleness, uncollected coverage, uncleaned
launches, and mutations never read back; contract construction refusing orphan
criteria and undischargeable obligations; the Crisis Atlas Slice 2 proposal
evaluating not-ready on three independent blocks while its required command
passed; `evaluate_checkpoint`'s four outcomes and its signature.

`tests/test_workcell_checkpoint.py` — the integration: coverage provenance from
a hashed controller artifact, a missing artifact emitting nothing, a stale
artifact refused, a failing run claiming no criteria, launch cleanup running
even when a probe raises, changed behaviour at file/symbol/route granularity,
contract compilation from an approved plan, and
`CrisisAtlasTwoTurnTests::test_partial_then_finished` — the partial proposal
receiving `CONTINUE`, the next turn finishing it, and only then `COMPLETE`,
through `run_checkpoint` rather than a hand-built report.
