# Slice 7P.1c: real qualification, and the claim it replaces

Date: 2026-07-31. **No inference, no `llama-server`, no model, no network.**
The eight-case draft manifest is untouched and no lock was written.

## The correction

`918bc82` reported "all eight proofs passed" and "the package is
registerable". Both came from a run against an **injected fake probe** — a
run that cloned nothing, executed nothing and emitted no witness. It validated
the validator. The package's real status at that commit was **NOT YET
REGISTERABLE**.

The error is worth naming precisely, because it is the same shape as the defect
7P.1a existed to close. There, a well-formed digest stood in for a measurement.
Here, a green orchestration run stood in for qualification evidence. In both
cases the artefact was structurally valid and referred to nothing, and in both
cases nothing in the type system objected.

So the distinction is now a value, not a convention:

```python
class EvidenceKind(StrEnum):
    ORCHESTRATION_ONLY = "orchestration_only"    # injected probe
    REAL_QUALIFICATION = "real_qualification"    # clones, commands, witnesses
```

`PackageProbe` must declare it — required, never defaulted, because whichever
default were chosen would be wrong half the time and silently. `registerable`
consults it, so an orchestration-only run **cannot** return true however green
it is. What used to be called `registerable` is now `all_proofs_passed`, named
for what it actually measures. `status` returns `NOT_YET_REGISTERABLE` or
`REGISTERABLE`, and `why_not_registerable()` says which of the two reasons
applies. A regression test asserts that eight fake passes do not register.

`918bc82` is preserved unchanged. What it established stands: twelve authored
artifact-backed components, the historical candidate recovered and digest-bound,
verified seed object identities, and full orchestration branch coverage.

## The real probe

`src/apoapsis/qualification/real_probe.py` supplies the half that was missing.
It drives the **existing** authoritative machinery — `run_checkpoint`,
`admit_candidate`, `emit_test_witness`, `evaluate_slice_readiness` — rather than
reimplementing any of it, because a second implementation would be a second
thing to be wrong.

Each `run_checkpoint` call takes **two independent fresh clones** of the seed,
one as base and one as candidate. Copying the base into the candidate would be
faster and would also mean a defect in the copy appears as a delta the
checkpoint attributes to the model.

Coverage comes from the standard library `trace` module, not `coverage.py`. The
harness declares only `pydantic` as a runtime dependency, and a proof that
reports `unrun` on any host lacking an optional package is a proof that will
usually report `unrun`. The witness records `collection_method: stdlib trace
module`, which is exactly what that field is for.

Two defects in the probe were found by running it, and both would have been
invisible under a fake:

- **The runner could not import the project under test.** `python -m unittest`
  puts the working directory on `sys.path`; a script invoked by path does not.
  Every test errored, and proof 3 reported the seed's suite as *red* — which
  reads as a finding about the package rather than a bug in the measurement.
- **`criteria_proved` was never populated**, so every obligation came back
  `unproved` and the known-good reference failed proof 4. The fix reads the
  claim from the package's own acceptance criteria (`required_witness_kind ==
  test_coverage`), which is owner configuration, not the emitter grading itself
  — and the claim remains necessary but far from sufficient, as proof 5 shows.

Offline by construction: nothing opens a socket, and the subprocess environment
is scrubbed of proxy variables with `PYTHONDONTWRITEBYTECODE=1` so a run cannot
leave byproducts a later admission would read as authored work (ADR 0063). This
is process-level offline, not a network namespace; the namespace-enforced
boundary is the ADR 0077 Docker workcell, which no proof here needs.

## Real results: eight of eight

Run on Linux/ext4/CPython 3.12. Seed copied to ext4 first, so every clone is
ext4 too. Raw evidence persisted outside the ephemeral clones, under
`7p1c/evidence/`: `validation.json`, `inherited-suite.json`,
`inherited-coverage.json`, and a `checkpoint-NN-*/` directory per checkpoint
holding the full `CheckpointRecord` and the derived observation.

| # | Proof | Real result |
| --- | --- | --- |
| 1 | Fresh clone reproduces commit and tree | **passed** — `197b3610…` (`commit`), `02fb45ef…` (`tree`), clean tree, 8 tracked files |
| 2 | Requested behaviour absent from seed | **passed** — neither declared path nor either symbol present |
| 3 | Inherited suite state recorded | **passed** — exit 0, coverage reaches only `crisis_atlas/__init__.py`, `tests/__init__.py`, `tests/test_smoke.py` |
| 4 | Reference satisfies every criterion | **passed** — `COMPLETE`, both obligations `proved`, no unexercised behaviour |
| 5 | Removal fails its mapped criterion | **passed** — both declared removals |
| 6 | Historical candidate cannot complete | **passed** — `CONTINUE` with the acceptance command green |
| 7 | Witnesses bound to admitted snapshot | **passed** — one fingerprint, `122b7e35…` |
| 8 | Second fresh clone identical | **passed** — identical states, evidence and fingerprints |

`evidence_kind: real_qualification`, `status: REGISTERABLE`,
`registerable: True`, package digest
`993e7a5610f09f0ee5aedf7bd1d35580cb8c169840ab0ecbc6b55e9c102514e8`.

> **Digest amended, 7P.2.** The package was re-issued when the sampling-seed
> audit found that no declared seed reaches any provider request, so
> `repetitions.json` renames `sampling_seed` to `repetition_identity` and adds
> the audit. That changes the package bytes, and therefore its digest, to
> **`d7c4b195ef505975c90f21892a17f633dce6d943dc4224ef3fd01010aef25d22`**. The
> eight proofs were **re-run in full against the re-issued package** and all
> eight pass again; the committed evidence under `slice-7p1c-evidence/` is
> regenerated from that run and digests to
> `d6c67ce643977c938c9486069100f2b3d02f12c8e49b1c983ae65176f6da52fa`. Every
> finding below is unchanged — the candidate fingerprints are identical,
> because `repetitions.json` is not part of any candidate tree.

### Proof 3, measured rather than asserted

The seed's own suite exits 0 and its coverage names **no** service path. The
false-green shape the whole case depends on is now a measurement.

### Proof 6: the four blocks, in the loop's own words

Outcome `CONTINUE`, candidate fingerprint
`f1a0451d03e85fa9664842f5486f5461504f23add10576ef1c78a1c46851dbaa`, with the
acceptance-designated `unit-tests` command **passing**:

1. `missing_required_artifact` — *incident-service requires
   `crisis_atlas/services/incident_service.py`, which the candidate does not
   contain at that path* (it wrote `services/incident_service.py`).
2. `missing_required_artifact` — *export-service requires
   `crisis_atlas/services/export_service.py`, which the candidate does not
   contain at that path.*
3. `changed_behaviour_unexercised` — *`services/incident_service.py` is new in
   this candidate and no current-state witness proves it is reached. Inherited
   tests staying green is not evidence: they stay green because they never
   reach it.*
4. `obligation_unproved` ×2 — both obligations unproved **while the configured
   command passed**, which is the regression itself.

### Proof 5: removal is discriminating

| Removed | Outcome | Effect |
| --- | --- | --- |
| `crisis_atlas/services/export_service.py` | `CONTINUE` | export-service `unproved`, artifact missing; incident-service also drops to `unproved` because its test import now fails |
| `tests/test_services.py` | `CONTINUE` | both artifacts present, both `never exercised` — the behaviour half of the rule, isolated |

The second row is the one that matters: nothing is missing, and the criteria
still fail, because presence was never what they asserted.

### Proof 8: determinism at the byte level

The two independent clone runs produced not merely matching outcomes but
identical candidate fingerprints:

| Candidate | Run 1 | Run 2 | Fingerprint |
| --- | --- | --- | --- |
| reference | `complete` | `complete` | `122b7e35035d8573b997395cf5aebbc21be927d6cddaf0030737ba0da5a297a9` |
| omit `export_service.py` | `continue` | `continue` | `30ba22d0261a71cdfd3aaafec8380210b295508791c1cce7dc6d2f0ada83ba47` |
| omit `test_services.py` | `continue` | `continue` | `b14e0392a7a7b770dd0aae16a3f23e2ad5e692fd9727a6ad60a515205a307fe3` |
| incomplete (historical) | `continue` | `continue` | `f1a0451d03e85fa9664842f5486f5461504f23add10576ef1c78a1c46851dbaa` |

Volatile fields excluded from the comparison: `workspace`,
`duration_seconds` — **declared**, and reported on the validation record. A
comparison that dropped whatever happened to differ would agree with itself by
construction, so undeclared divergence still fails, and a test proves it.

## The canonical ruler, and a diagnosis

The 7P.1b report gave a full-suite result of 3 failures and 2 errors on
`/mnt/c`, and attributed three of them to the filesystem. On canonical ext4 that
attribution held exactly: **1756 tests, 2 failures** at `918bc82` — the same two
as at `2ee8afd`, and the three extras gone.

Those two remaining failures were then diagnosed rather than waived. Both are
`TaskOutcome.FAILED != COMPLETE` in fake-provider task runs, and the cause is
environmental, not a product defect and not a regression:

> The fixtures configure a verification command as `argv=["python", ...]`.
> Ubuntu ships no `python` — only `python3`. Invoking the suite through
> `.venv/bin/python` by absolute path leaves the venv's `bin/` off `PATH`, the
> command cannot be found, and the task fails.

Demonstrated directly: with the venv **not** activated, those two fail; with it
**activated**, `which python` resolves inside the venv and the same five tests
pass. 25 fixture sites across 16 test modules shell out to a bare `python`.

The canonical invocation therefore activates the virtual environment, which is
how the suite is meant to be run. The fixtures' dependence on a `python`
executable being on `PATH` is a real portability weakness — `sys.executable`
would be correct on every platform — but repairing 16 test modules is not this
slice's work, and doing it here would mean editing tests to suit a runner.
Recorded in `NEXT_STEPS.md` instead.

## Scope audit of the 918bc82 infrastructure changes

Both were checked rather than asserted.

**`conftest.py` hides no canonical test.** `unittest discover -s tests` finds
**1756** cases and never loads `conftest.py` at all. Under pytest, `tests/`
collects **1756 with the conftest present and 1756 with it removed** — an
identical set. It excludes only `docs/qualification/pilot/*`, and it cannot
affect product execution: pytest alone loads it, and the shipped entry point is
`apoapsis.cli.app:main`.

**`.gitattributes` alters exactly the package.** `git check-attr` across every
tracked file reports a changed `text` attribute for **18 files**, all under
`docs/qualification/pilot/crisis-atlas/`. **3597** other tracked files are
unaffected. The rule exists because the declared digests were taken from those
exact bytes; a clone with `core.autocrlf=true` would rewrite them and the
failure would read as tampering.

## Verification

| Check | Result |
| --- | --- |
| Fake/orchestration focused module, Linux/3.12 | reported separately — see the totals in the final report |
| Real-probe focused module, Linux/3.12 | real clones and commands; skips where `git` or the seed is absent |
| Canonical full suite, Linux/ext4/3.12, venv activated | see final report |
| `/mnt/c` full suite | portability finding only, never the qualification result |

## What this still does not establish

Not a Capability Sandbox win. **No model ran, no server started, and no model
quality was measured.** Crisis Atlas is a regression benchmark — its failure
mode was known before these acceptance rules were written — so it is not
held-out evidence and supports no non-inferiority claim. One case with three
declared repetitions cannot stand in for the seven deferred corpus cases.

The package is now genuinely registerable. Nothing about the *pilot* is
authorized: 7P.2 must still capture model/server/workcell identities, author
the separate pilot manifest, bind the three paired executions, and write the
lock.

## Manifest state, unchanged

`unresolved_hashes()` — **8**.
`ready_for_inference()` — **false**.
Manifest digest — **`8c374827aa4ace9576ed9d2d2f0db04747f3b4fb05d425b10e6fc770454f3762`**.
Lock artifact — **not written.**
