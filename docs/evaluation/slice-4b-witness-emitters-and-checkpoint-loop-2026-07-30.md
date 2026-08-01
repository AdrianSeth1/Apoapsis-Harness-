# Slice 4B: witness emitters, contract compilation, and the checkpoint loop

Date: 2026-07-30  
Evidence class: **deterministic only.** 20 new tests, 207 in the focused set.
No model calls, no container. Real temp trees, real coverage artifacts on disk,
real `run_checkpoint`.

## What this closes

Review of commit `123c2b5` found Slice 4 operationally incomplete, and was
right:

> The production tree has no caller for `evaluate_checkpoint` and no emitter
> for `StructuredWitness`; they are currently used only by the new module and
> tests.

The rule was enforceable in principle and unenforced in practice. The commit
also skipped `HANDOFF.md`, `README.md`, and an ADR, which the repository's
maintenance contract requires for an architecture change — my lapse, since I
had committed to full docs compliance early in this work and then did not hold
to it.

| Gap | Closed by |
| --- | --- |
| No contract compiler | `workcell/contract_compiler.py` |
| No witness emitters | `workcell/emitters.py` |
| Coverage from model claims | artifact the controller deletes, requests, reads, hashes |
| File-level rule only | `workcell/behaviour.py`, symbol and route granularity |
| No caller | `workcell/checkpoint.py::run_checkpoint` |
| No two-turn test | `CrisisAtlasTwoTurnTests::test_partial_then_finished` |
| No ADR or docs | ADR 0079, `HANDOFF.md`, `README.md` |

## The integration test that matters

`test_partial_then_finished` runs the real loop twice.

**Turn one** is the actual Crisis Atlas Slice 2 proposal: one partial file at
`services/incident_service.py`, no export service, no new tests, and an
inherited suite that passes with coverage naming only `incident/domain.py`.

Result: the candidate is **admitted** (it breaks no policy) and the checkpoint
returns **`CONTINUE`**, with a repair packet naming both
`services/incident_service.py` (unreached) and
`incident/services/export_service.py` (missing). Blocks:
`MISSING_REQUIRED_ARTIFACT` and `CHANGED_BEHAVIOUR_UNEXERCISED`.

**Turn two** moves the service to its declared package, adds the export
service, and runs a suite whose coverage reaches both.

Result: **`COMPLETE`**, `unexercised_behaviour` empty, repair packet empty.

Only the second turn completes, and it does so through `run_checkpoint` rather
than a hand-built readiness report.

## Coverage provenance is the load-bearing change

`emit_test_witness` deletes the coverage artifact before the run, tells the
command where to write it, then reads and hashes that file itself.
`CoverageObservation.source_artifact_sha256` records which file the numbers
came from.

Four tests hold the line:

- coverage carries a hash and real executed lines;
- a run that produced **no** artifact raises rather than emitting a witness
  that asserts coverage anyway;
- a **stale** artifact left by a previous attempt is refused, because deletion
  happens first — the same staleness problem the fingerprint solves one layer
  up;
- a **failing** run claims no criteria, which is the producer side of
  `validate_witness`'s refusal of a failing witness that lists proofs.

The reasoning is not that a model would lie. It is that a confident report and
a true one are indistinguishable — which is exactly what the Crisis Atlas
record documents.

## Changed behaviour, not changed files

Slice 4's rule looked at added files. Crisis Atlas Slice 3's unreachable export
routes lived in a **modified** file: already covered, addition not.

`changed_behaviour` now yields a `BehaviourUnit` for a whole added production
file, for each new top-level symbol in a modified file, and for each new route
literal — each with a line range, checked against line-level coverage. A route
is additionally satisfied by a launch or behavioural witness that *called* it,
which no coverage tool reports.

An added file is **one** unit, not one per symbol. Requiring every helper in a
brand-new module to be individually covered would be stricter than the handoff
asks and would let an untested helper block a slice whose behaviour is proven.

Python symbols come from `ast` and are exact. Routes come from a narrow regex
over routing-looking lines and carry `heuristic=True`, and the finding says so:
a route it invents is a false obligation the owner can see and delete, which is
the safe direction for a heuristic to err.

## Two ordering decisions in the loop

**Witnesses are emitted against the admitted snapshot, not the workcell.** A
command must never be observed running over a file the policy refused. A test
asserts the emitter received `admission.snapshot_path` and not the candidate
tree.

**An emitter failure cannot coexist with `COMPLETE`.** If emission failed, the
evidence readiness accepted cannot be current, so the loop downgrades rather
than trusting the coincidence.

## Honest limitations

- **Still no live run.** Every test uses temp trees and a fake command runner
  that writes a real coverage artifact. Wiring `run_checkpoint` into a live
  workcell session — and running a real `coverage.py` inside the verifier — is
  not done.
- **Coverage is not independently re-derived.** `collection_method` and
  `source_artifact_sha256` force the wrapper to say how it measured and from
  which file. A wrapper that writes a false artifact produces a witness that
  validates. Closing this needs the verifier workcell of ADR 0077 Layer 4,
  where the controller runs the tool itself.
- **The route heuristic is Python/Flask-shaped.** It looks for routing-ish
  identifiers near a quoted absolute path. It will miss decorator-based routing
  in other frameworks and any route built by string concatenation.
- **Symbol extraction is Python-only.** Other languages fall back to file
  granularity, which is Slice 4's weaker rule.
- **The interface and integration obligations compile as intentionally
  unmeasured** by default, because no witness kind currently maps to a symbol
  name or an `INT-` contract id. That is honest — they block completion and
  route to human review — but it means a slice with declared symbols cannot
  reach automatic `COMPLETE` until a witness kind exists for them.
- **The legacy Local Power loop still terminates under ADR 0069.** The two
  coexist until the Capability Sandbox is the default.
- `relay.py` still cannot be imported on Windows (Slice 2A defect, unfixed).

## Verification

`compileall` clean. `tests/test_workcell_checkpoint.py` 20 tests. Focused set —
checkpoint, acceptance, admission, agent profile, workcell, paired scoring —
**207 passing** (1 skipped where symlinks are not creatable).
`git diff --check` clean.

Slice 4's own tests were updated for the renamed vocabulary
(`CHANGED_BEHAVIOUR_UNEXERCISED`, `unexercised_behaviour`); no assertion was
weakened in the process.
