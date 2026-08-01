# Crisis Atlas pilot package: where every component came from

Nothing here was reconstructed from a conversational summary. Each row names
the file the content was taken from.

| # | Component | Package artifact | Source of record |
|---|---|---|---|
| 1 | Seed locator, commit, tree | `seed.json` | `git cat-file -p HEAD` in `.apoapsis-eval/slice-e-crisis-atlas-seed-2026-07-29` |
| 2 | Immutable task text | `task.md` | Authored for this pilot from the seed's real package layout and the Slice 2 obligations named in `crisis_atlas_facts.py` |
| 3 | Approved plan / contract | `plan-contract.json` | `tests/test_workcell_checkpoint.py::_contract`, re-pathed onto `crisis_atlas` |
| 4 | Mapped acceptance criteria | `acceptance-criteria.json` | Derived from the recovered failure and success evidence, not from names |
| 5 | Verification commands | `verification-commands.json` | `local-power-verification-001.json` (the four commands the arm actually ran) |
| 6 | Evaluator-only oracle | `evaluator-only/oracle.json` | `crisis_atlas_facts.py` defect records + the arm's verification record |
| 7 | Expected witnesses | `evaluator-only/expected-witnesses.json` | `docs/evaluation/slice-4b-witness-emitters-and-checkpoint-loop-2026-07-30.md` |
| 8 | Three repetition identities | `repetitions.json` | Authored; invariants pinned above the list |
| 9 | Budget / output-cap class | `budgets.json` | Control arm envelope in `crisis_atlas_facts.py` |
| 10 | Capability rationale | `capability-discrimination.md` | Seed's `tests/test_smoke.py` + slice-2 miss attribution |
| 11 | Known-good reference | `evaluator-only/reference/` | **Fixture-derived.** Slice 4B turn two, re-pathed. Not a model achievement. |
| 12 | Incomplete candidate | `evaluator-only/incomplete/` | **Actual historical bytes.** `call-001-response.json` + `local-power-change-set-001.json` |

## Two distinctions this package refuses to blur

**Component 11 is not a historical achievement.** No model produced it. It is
evaluator reference material derived from the Slice 4B turn-two fixture. The
fixture used an `incident` package; this package re-paths it onto the real
`crisis_atlas` package, which is a change, and the change is recorded here
rather than hidden.

**Component 12 is not a reconstruction.** Its bytes are the ones the model
emitted, recovered from the arm's own response record, and its change-set
digests (`claimed_base` / `observed_base` / `resulting`) come from the applied
change set. The preserved worktree in the same evaluation directory is the
*post-Codex-repair* state and is deliberately not used.

## Fields that are genuinely unavailable

Recorded as unavailable in `evaluator-only/incomplete/provenance.json` rather
than invented: the candidate's Git tree object id, repair distance in files,
and repair distance in lines. The trial record does not contain them.
