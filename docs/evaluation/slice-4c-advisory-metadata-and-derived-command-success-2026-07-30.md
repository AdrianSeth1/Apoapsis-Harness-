# Slice 4C: advice is not a gate, and command success is derived

Date: 2026-07-30  
Evidence class: **deterministic only.** 10 new tests, 217 in the focused set.
Two authority defects in Slice 4B, both found by review.

## Defect 1: advisory plan metadata was promoted into a completion gate

`ImplementationSlice` says so in its own docstring:

> All cross-references (dependencies, constraint IDs, criterion IDs,
> verification command names) are **advisory proposals from the planner**.

The Slice 4B compiler read `suggested_symbols` and `integration_contract_ids`
and turned each into a mandatory obligation, which it then marked
*intentionally unmeasured* because no witness kind could discharge them. The
combined effect was worse than either half: a planner's suggestion silently
became a completion gate that **no evidence could ever open**, and every slice
declaring a symbol was routed to human review forever.

I reported this last turn as a limitation. It was a defect — the difference
being that a limitation is a thing the design does not yet do, and this was the
design doing something it should not.

**Fix.** The compiler ignores both advisory fields. Interfaces and integration
edges become obligations only from owner-approved inputs:

```python
compile_slice_contract(
    plan, slice_id,
    required_interfaces={"incident-service": ["incident.services.IncidentService"]},
    required_integration_routes={"INT-dashboard-api": ["/api/incidents"]},
)
```

Supply neither and no such obligation exists. Tests assert that
`suggested_symbols` and `integration_contract_ids` produce **zero** interface
and integration obligations, and that no obligation is born intentionally
unmeasured.

## Defect 2: command success was caller-supplied

`passed_commands: set[str]` was a parameter. Everything else in this design
refuses evidence that is not current, usable, and bound to the candidate
fingerprint — and then the required-command check accepted a bare set of
strings that could describe a different tree, an earlier turn, or a run nobody
bound to anything.

**Fix.** The parameter is gone from both `evaluate_slice_readiness` and
`run_checkpoint`. A command counts as passed only when a **usable** witness
reports it:

```python
passed_commands = {item.command_name for item in usable if item.passed}
```

`usable` is the post-validation set, so a stale witness cannot make a required
command pass. A test asserts exactly that: same command name, wrong worktree
fingerprint, `REQUIRED_COMMAND_NOT_PASSED`.

## Interfaces and integrations are now dischargeable by observation

Closing defect 1 by deletion alone would have left real interface obligations
unexpressible. So `AcceptanceObligation` gains two fields backed by observation
rather than assertion:

- **`required_symbols`** — discharged from `CoverageObservation.observed_symbols`,
  read out of the same controller-produced, hashed artifact as the line data.
- **`required_routes`** — discharged from a launch or behavioural witness's
  `exchanges`. A route is exercised by being *called*, which no coverage tool
  reports.

`StructuredWitness` exposes `exercised_symbols` (observed symbols ∪ imported
modules) and `exercised_routes` (from exchanges) as properties over measured
data, so neither is a claim the witness makes about itself.

A test compiles a contract with a real interface obligation and drives it to
`ready` with a single witness — the practical point being that a plan with
genuine interface obligations can now complete automatically, without the gate
being weakened to let it.

## What did not change

Coverage is still not independently re-derived. `collection_method` and
`source_artifact_sha256` force the wrapper to say how it measured and from
which file; nothing re-derives it from first principles. Per the review, the
deterministic controller-owned artifact is the trust boundary for this stage,
and the independent verifier is documented later work (ADR 0077 Layer 4).

`observed_symbols` is read from `executed_functions`/`executed_classes` in the
coverage artifact. `coverage.py` does not emit those natively — a wrapper that
can report executed symbol names populates them, and one that cannot leaves the
list empty rather than guessing. Until such a wrapper exists, an interface
obligation is expressible and not yet dischargeable in practice. That is a
limitation, and this time it is one: the gate is honest about what it has.

Non-Python symbol precision and the route heuristic's Flask shape remain as
recorded in Slice 4B.

## Verification

`compileall` clean. Focused set — checkpoint, acceptance, admission, agent
profile, workcell, paired scoring — **217 passing** (1 skipped where symlinks
are not creatable). `git diff --check` clean. No assertion was weakened; the
one test asserting the old `passed_commands` behaviour was rewritten to the
honest scenario (a witness for a *different* command) and joined by five new
ones.
