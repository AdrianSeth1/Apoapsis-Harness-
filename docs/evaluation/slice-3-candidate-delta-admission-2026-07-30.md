# Slice 3: candidate delta admission

Date: 2026-07-30  
Evidence class: **deterministic (28 tests) plus a live demonstration** against
the real Slice 2D workcell output — the trees two Qwen agents actually edited.
No new model calls were made; admission is a controller-side operation.

## What Slice 3 is for

ADR 0071 made the model express a slice as an atomic JSON change set so the
harness could judge it whole. The Crisis Atlas control showed the envelope cost
more capability than the atomicity was worth. Slice 3 keeps the atomicity and
drops the envelope: **the agent edits files normally, and the controller
assembles those edits into one candidate that is accepted or refused together.**

## The trust boundary is the design

The delta is **not** `git diff`. ADR 0077 gives the agent a real shell and real
Git inside a sacrificial clone, so the clone's history is the agent's to
rewrite — it may commit, amend, rebase, reset, or delete `.git` outright.

`compute_delta` therefore walks and hashes two trees the controller
materialises itself, and `.git` is never read. A test writes a fabricated
`HEAD` and a 5 KB `COMMIT_EDITMSG` into the candidate and asserts the delta is
still empty.

Reconstruction inherits the same discipline: the verifier tree is built from
**the approved base plus the admitted entries**, never copied from the
workcell. A stray artifact, a leftover process file, or anything the policy
refused cannot reach the verifier, because the workcell is not consulted.

## Live demonstration, against real agent output

Base is the Slice 2D seed; candidates are the two clones the arms actually
edited.

| | control arm | sandbox arm |
| --- | --- | --- |
| Delta paths | `calc.py`, `run_tests.py` | `calc.py`, `run_tests.py` |
| Classified | 2 production, 2 modified | 2 production, 2 modified |
| Changed lines | 9 | 9 |
| Admitted | **yes** | **yes** |

`.git` and `__pycache__` were present in both clones and appear in neither
delta. The reconstructed verifier tree runs: `python3 run_tests.py` → exit 0,
`OK`.

### One forbidden path refuses the whole candidate

A copy of the real sandbox clone plus `.env` and `.apoapsis/state.json`:

```
FORBIDDEN CASE admitted: False
  delta paths: ['.apoapsis/state.json', '.env', 'calc.py', 'run_tests.py']
  snapshot written: False
The candidate was refused with 2 violation(s). Fix all of them before requesting evaluation again.
- forbidden_path [.apoapsis/state.json]: ... may never reach the owner's branch
- forbidden_path [.env]: ... may never reach the owner's branch
```

Both violations reported together, and the legitimate `calc.py` and
`run_tests.py` are refused *along with them*. That is what atomic means here: a
candidate is one thing.

**Base fingerprint before and after every case: identical.** There is no code
path in `admission.py` that writes to the base.

## Two bugs the tests caught, both silent-failure shaped

**`lstrip("./")` strips characters, not a prefix.** It turned `.env` into
`env` and `.apoapsis/state.json` into `apoapsis/state.json`, so both
classified as ordinary **production source**. A credential would have been
admitted as application code. Now `removeprefix("./")`.

**Excluding `.apoapsis/` from the walk made a boundary violation invisible.**
My first exclusion list treated it as noise alongside `.git`. An agent writing
controller state would have produced a delta that reported *clean* — the exact
change admission exists to catch. `.git` and the caches stay excluded because
their contents are genuinely not work; `.apoapsis` and `.sol` are now walked so
they are seen, classified `FORBIDDEN`, and refused.

Both were found by tests written from the handoff's exit criteria rather than
from the implementation, which is the only reason they surfaced.

## Design choices worth arguing with

**Classification is by path shape alone**, and is used to report and to apply
per-class ceilings — never to infer that a change is safe. The point of the
`TEST` class is that an owner can say "tests may not change" and have it mean
something, not that test files are trustworthy.

**Forbidden is not configurable.** `AdmissionPolicy` can loosen file counts,
line counts, test changes, dependency changes, and deletions. It cannot permit
`.git/`, `.env`, or the task artifact, because a configuration that could would
make the boundary advisory.

**Generated artifacts are refused by default.** They are not work, they inflate
the delta, and admitting one makes the verifier depend on state the manifest
does not describe.

**Symlinks are never followed, and are reported.** A symlink is not content,
and following one is how a delta acquires a file from outside the workspace.
They appear in `skipped_non_regular` and each produces a violation.

**An empty delta is a finding.** A session that changed nothing cannot be
promoted as though it had.

**Line counts are a cheap multiset difference, not a minimal edit script.**
They feed size ceilings, where over-counting a reordered file errs safe.
Binary content reports no line counts at all, so a large blob cannot slip past
a changed-line ceiling by claiming zero.

## The Slice 2 gate bites here

`admit_candidate(..., slice2_spike=...)` calls `require_slice3_unblocked`,
which raises unless the spike says `CAPABILITY_PRESERVED` with containment,
provider-protocol conformance, agent execution-profile identity, and capability
readiness all holding. Admission is the first thing Slice 3 does, so this is
where the gate has to be. A test asserts that a `NOT_MEASURABLE` spike raises
`Slice3Blocked` and leaves no snapshot behind.

Passing `slice2_spike=None` is permitted for the deterministic tests, which
exercise admission's own logic. The live demonstration above passed the real
spike report.

## Honest limitations

- **Verification does not run in a separate verifier *workcell* yet.** The
  reconstruction is a clean controller-owned tree and the tests were executed
  in it, but ADR 0077's Layer 4 — a fresh container built from the approved
  base plus the admitted delta, with its own environment digest — is not built.
  Running `run_tests.py` in the controller proves the tree is coherent, not
  that it verifies under a pinned environment.
- **No `PlanCheckpoint` yet.** Admission produces a snapshot and a decision
  record; binding those into the plan graph as an authoritative checkpoint is
  Slice 6.
- **Classification is heuristic.** A project that puts production code under
  `spec/`, or names a module `test_utils.py`, will be misclassified. The
  consequence is a misapplied per-class ceiling, not a boundary failure —
  `FORBIDDEN` does not depend on these heuristics.
- **The live demonstration is one tiny two-file delta.** It exercises the
  paths; it is not evidence about large or adversarial deltas.
- **`relay.py` still cannot be imported on Windows** (Slice 2A defect,
  unfixed), so relay tests do not run on the host.

## Verification

`compileall` clean. `tests/test_workcell_admission.py` 28 tests (1 skipped
where symlinks are not creatable). Focused set —
`test_workcell_admission`, `test_workcell_agent_profile`, `test_workcell`,
`test_paired_scoring` — **158 passing**. `git diff --check` clean.
