# Slice 7P.1b: one Crisis Atlas case, authored and orchestration-validated

Date: 2026-07-31. **No inference, no `llama-server`, no container, no network.**
No manifest was finalized, no lock written, and the eight-case draft is
untouched.

> **Correction, 7P.1c.** This record originally reported "all eight proofs
> passed" and "the package is registerable". Both statements came from a run
> against an **injected fake probe**. That run made no clone, executed no
> command, and emitted no witness, so it validated the *validator* and said
> nothing about the package. The package's status at `918bc82` was **NOT YET
> REGISTERABLE**, and the claim substituted orchestration coverage for
> qualification evidence — structurally the same error as the label hashes
> 7P.1a was written to eliminate.
>
> What `918bc82` did establish stands: the twelve components are authored and
> artifact-backed, the historical candidate is recovered and digest-bound, the
> seed's object identities are verified, and every orchestration branch of the
> validator is covered. The real evidence arrives in 7P.1c —
> `docs/evaluation/slice-7p1c-real-qualification-2026-07-31.md`. Sections
> below are left as written except where marked, so the original claim and its
> correction stay legible next to each other.

## What was built

`docs/qualification/pilot/crisis-atlas/` — 18 files, **17 declared artifacts**
(`package.json` declares the other seventeen and is not self-declaring),
package digest
`993e7a5610f09f0ee5aedf7bd1d35580cb8c169840ab0ecbc6b55e9c102514e8`.
All twelve required components are present and mandatory.

`src/apoapsis/qualification/case_package.py` — resolution, the twelve-component
requirement, repetition and criteria validation, the eight proofs, typed
containment, and `GitCloneObserver` for the real clone half of the probe.

## Verified seed identity

| Role | Object | Type, confirmed by `git cat-file -t` |
| --- | --- | --- |
| Seed commit | `197b3610e5720cf36718c548fa19c05fe784a978` | `commit` |
| Seed tree | `02fb45efeb4e19c619e3f730bd05a1f70bef9f13` | `tree` |
| Parent (provenance only) | `50bffcfe498129b833eaa35eb8c097a825b2ee39` | `commit` |

The tree was read via `git cat-file -p HEAD`, not `HEAD^{tree}`. The braces are
PowerShell metacharacters; an unquoted invocation prints the **parent commit**
and then fails, which is why `50bffcfe…` was previously mistaken for the tree.
`GitCloneObserver` parses the commit object for exactly this reason, and
`ObjectTypeTests` asserts that `50bffcfe…` is refused when declared as the tree.

A real clone of the seed reproduced the commit and tree, reported 8 tracked
files and a clean working tree, and found neither `IncidentService` nor
`ExportService` anywhere in it.

## The incomplete candidate is historical, not reconstructed

Recovered from
`.apoapsis-eval/slice-e-crisis-atlas-64k-codex-slice2-2026-07-29/.apoapsis/tasks/TASK-CB6141309D6E/`:

| Field | Value |
| --- | --- |
| Changed paths | `services/incident_service.py`, one `write`, outcome `created` |
| Declared characters | 4,598 |
| Claimed base digest | `67ad553da3e95d00c1215b22529bbf011dd547906955a0854da26aaa4b5b1670` |
| Observed base digest | identical to the claimed value |
| Resulting digest | `b13d9253f3a8b69c20eb7da43a69d3e304f2b1ea9175fe900e728f6d94926954` |
| Model summary | "Implement IncidentService and ExportService with unit tests" |
| `finish_reason` | `stop` |
| Verification status | `passed` — `unit-tests`, `web-product-integrity`, `behavioral-integration`, `launch-smoke`, all exit 0 |

Two things are worth stating plainly. The summary claims two services and unit
tests; the change set in the same directory records **one** write and nothing
else, so the proposal is refuted by its own artifact. And `finish_reason` is
`stop`, not `length` — the response was not truncated, so the omissions are a
proposal miss rather than an output-cap artifact.

**A trap that was nearly walked into.** The preserved worktree at
`.apoapsis/worktrees/cb6141309d6e/` contains `service/incident_service.py`
(singular), an `export_service.py`, and a seven-file test suite. That is the
**post-Codex-repair** state. Treating it as the candidate would have shipped a
repaired tree labelled as the failure. The proposal wrote `services/`
(plural), once. The distinction is recorded in `provenance.json` and asserted
by `HistoricalProvenanceTests`.

Fields the trial record genuinely does not contain — the candidate's Git tree
object id, repair distance in files, repair distance in lines — are recorded as
unavailable rather than invented.

## The known-good reference is not a model achievement

It is derived from the Slice 4B turn-two fixture in
`tests/test_workcell_checkpoint.py`, re-pathed from that fixture's `incident`
package onto the real `crisis_atlas` package. No model produced it. Both the
evidence index and the module docstring say so.

## Four independently mapped blocks

Each is mapped to its own evidence and each alone refuses the candidate:

| Block | Readiness block | Evidence |
| --- | --- | --- |
| Wrong package path | `MISSING_REQUIRED_ARTIFACT` | change set `operations[0].path` |
| `ExportService` absent | `MISSING_REQUIRED_ARTIFACT` | `crisis_atlas_facts.py` defect `MISSING` |
| Changed behaviour unexercised | `CHANGED_BEHAVIOUR_UNEXERCISED` | `crisis_atlas_facts.py` defect `DEAD` |
| Inherited green insufficient | `MISSING_REQUIRED_ARTIFACT` | four commands at exit 0 |

A refusal for only *some* of these fails proof 6: a refusal for the wrong
reason does not prove the mapped blocks work.

## The eight proofs

**Corrected.** All eight `passed` against the authored package **under the
injected fake probe**. That result is orchestration coverage: it shows each
proof reports the state its inputs imply and that a defect in any one of them
surfaces as a failure. It is not qualification evidence, and the package was
**not** registerable on it. See 7P.1c for the real run.

Each proof is separately `passed`/`failed`/`unrun`/`inconclusive`; `unrun` and
`inconclusive` both block registration, and a duplicated proof cannot
substitute for a missing one — a test asserts that eight all-passing results
containing two copies of proof 1 do not register.

Proofs 1 and 2 were additionally satisfied against the **real seed** via
`GitCloneObserver`, outside the fake probe. Those two were the only real
evidence this commit produced.

## Verification

| Check | Result |
| --- | --- |
| Focused (case package, artifacts, manifest, checkpoint, acceptance), Linux/3.12 | **173 passed, 56 subtests**, 0 failed |
| New module alone, Windows/3.12 | 55 passed, **1 skipped** (Windows symlink) |
| New module alone, Linux/3.12 | 55 passed, 0 skipped — the skip executes here |
| Full suite, Linux/3.12, `unittest discover -s tests` | **1756 tests, 3 failures, 2 errors, 12 skipped**, 349.0s |
| Baseline at `2ee8afd`, Linux/3.12, ext4 | **1701 tests, 2 failures, 12 skipped**, 284.5s |
| `python -m compileall -q src tests` | clean |
| `git diff --check` | clean |

**The five are not five new failures.** Two are pre-existing and appear in the
baseline unchanged: `test_context_measurement_integration.test_one_shot_report_carries_one_measurement_per_call`
and `test_specification_correction.test_successful_correction_completes_the_task`.

The other three —
`test_agent_loop.test_changed_paths_separate_generated_byproducts_from_authored_work`,
`test_agent_loop.test_tracked_generated_looking_files_stay_in_the_review_surface`
and `test_review_ui.test_authorize_frontier_stage_completes_via_background_worker`
— are a **filesystem effect, not a code effect.** A four-way isolation run
establishes this:

| Run | Tree | Filesystem | Result |
| --- | --- | --- | --- |
| A | baseline `2ee8afd` | ext4 | 50 tests **OK** |
| B | baseline **plus every 7P.1b file** | ext4 | 50 tests **OK** |
| C | 7P.1b module inside the baseline tree | ext4 | 55 tests **OK** |
| D | working tree | `/mnt/c` | 50 tests, **1 failure, 1 error** |

B is the controlled comparison: the same additive files, the same tests, a
different filesystem, and it passes. The 7P.1b change is exonerated.

## A pre-existing discrepancy found on the way

Running the suite under **`pytest` from the repository root** reports 34
failures, of which **30 are in `examples/download-service{,-v2}/tests/`** —
fixture repositories that are not this project's suite. Every recorded
full-suite result in `HANDOFF.md` used `unittest discover -s tests`, which never
collects them. This is not caused by 7P.1b and is not fixed by it; it is
recorded because the two commands describe different universes and the
difference is large enough to mislead.

A root `conftest.py` **was** added, for a related but narrower reason: the
Crisis Atlas reference candidate ships `tests/test_services.py`, which imports
`crisis_atlas.services`. Under pytest that import fails and **aborts collection
of the entire suite**. `collect_ignore_glob` excludes
`docs/qualification/pilot/*`. That is a collection boundary only; containment
for the arms is `assert_arm_visible_set_is_contained`, which compares resolved
absolute paths against declared artifact kinds and trusts no path convention.

## What this does not establish

Not a Capability Sandbox win. No model quality was measured. Crisis Atlas is a
regression benchmark — its failure mode was known before these acceptance rules
were written — so it is not held-out evidence and cannot support a
non-inferiority claim or authorize Apoapsis as a default. Seven corpus cases
remain deferred.

`PackageProbe.run_checkpoint` has no production implementation. The clone half
is real; wiring the checkpoint half to a live workcell is 7P.2 work, and until
then proofs 3-7 run only against an injected probe.

## Manifest state, unchanged

`unresolved_hashes()` — **8**.
`ready_for_inference()` — **false**.
Manifest digest — **`8c374827aa4ace9576ed9d2d2f0db04747f3b4fb05d425b10e6fc770454f3762`**.
Lock artifact — **not written.**
