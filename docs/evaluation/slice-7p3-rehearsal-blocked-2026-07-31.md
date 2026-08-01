# Slice 7P.3: rehearsal blocked at the executable-provenance gate

Date: 2026-07-31. **The rehearsal did not execute.** No `llama-server`, no GGUF
load, no readiness request, no fake-provider run, no arm slot, no container
started. The locked manifest and lock are unmodified.

Verdict: **`NOT_MEASURABLE`** — the rehearsal could not be run under the
current lock, so no rehearsal evidence exists and live preflight is **not**
authorized.

## Stage 0 passed: the locked inputs verify

Recomputed from bytes before the gate was evaluated.

| Item | Value | Match |
| --- | --- | --- |
| Manifest digest | `0f4b0fd5930846841dae90dc4c517141bf98366886f58de55a10528d042019bc` | ✅ |
| Lock digest | `974c1dfe4a413870b6faa0119619d66970e4673b4797d4f9ef9a5bdee1be99ed` | ✅ |
| Package digest | `d7c4b195ef505975c90f21892a17f633dce6d943dc4224ef3fd01010aef25d22` | ✅ |
| Qualification evidence | `d6c67ce643977c938c9486069100f2b3d02f12c8e49b1c983ae65176f6da52fa` | ✅ |
| Mount/network policy | `98f06b56f3658e6e10301d57e4043d5e4e1092aa79f3f27e7e501e0e2a156091` | ✅ |
| Server argv | `f5967deb61bac1c32140610ca825a4223d2fb75da59a1a9f5466585eb7fa59b9` | ✅ |
| Repetitions | `crisis-atlas-rep-1/2/3`, frozen schedule | ✅ |

Every artifact digest recomputes to its locked value. The blockage is not in
the data.

## Gate 1 failed, on two counts

### Finding A — the rehearsal runner does not exist in any locked source

The manifest and lock bind **no executable runner**. The only
runner-adjacent field in either artifact is
`authorises_zero_token_rehearsal`, which is a permission, not an identity.

Searching both locked source identities for the components that would decide
an experimental verdict:

| Component | Evaluator `22cd8af` | Subject `ad13cf0` |
| --- | --- | --- |
| admission | present | present |
| checkpoint transitions | present | present |
| acceptance / scoring rules | present | present |
| paired comparability | present | present |
| relay | present | present |
| clone / arm-setup primitives | present | present |
| **six-slot pilot scheduler** | **absent** | **absent** |
| **scripted pilot fake provider** | **absent** | **absent** |
| **arm-slot driver / teardown prover** | **absent** | **absent** |
| **rehearsal verdict model** | **absent** | **absent** |

The decision *kernels* are locked. The thing that would sequence six arm slots
in the frozen order, drive a scripted provider through the relay, collect
evidence, prove teardown and emit a verdict is not.

`workcell/echo_provider.py` exists and is deterministic, but it is not a
substitute: it returns its input verbatim for the Unicode-integrity check (ADR
0078). It cannot produce the two required outcome shapes — the historical
incomplete proposal and the known-good candidate — because producing them is
scripting, and it does not script.

Writing that runner now and rehearsing under the existing lock is precisely
what the gate forbids. Unbound code would decide the verdict.

### Finding B — the lock's own validator is outside the lock

This one is a defect in `6eb267d`, which I authored.

`src/apoapsis/qualification/pilot.py` defines `PilotManifest`, `PilotLock`,
`authorize_rehearsal`, `accept_execution_record` and the eighteen stop
conditions. It was introduced in **`a5a30d2`**, the manifest commit.

The lock names `evaluator_framework_commit = 22cd8afe36d360ca841521ed7c1700801c651ca0`,
and `pilot.py` **does not exist at that commit**:

```
$ git cat-file -e 22cd8af:src/apoapsis/qualification/pilot.py
fatal: path 'src/apoapsis/qualification/pilot.py' exists on disk,
       but not in '22cd8af'
```

So the code that decides whether the lock is valid, whether the rehearsal may
proceed, and whether an execution record belongs to this pilot is covered by
neither locked source identity. The lock does not bind its own validator.

This is the same shape as the two defects this project has already corrected
once each: a digest that referred to nothing (7P.1a) and a green run that
proved something other than what it was reported to prove (7P.1c). Here, an
artifact that names its authority does not actually name the code exercising
it. It went unnoticed in 7P.2 because every test of the lock imports `pilot.py`
from the working tree, where it is present, so nothing ever asked which commit
it came from.

## What was deliberately not done

No runner was written and no rehearsal was attempted. No manifest or lock field
was edited — in particular `workcell_image.provenance_proven` remains `false`
and was not quietly flipped. Stages 1 through 8 did not run, so this record
contains no containment result, no relay stress count, no arm-slot outcome, no
negative-control detection and no token accounting. Reporting any of those as
absent-but-fine would be the substitution this slice exists to prevent.

## A second pre-existing intermittent, found by running the suite

The canonical suite at this tree ran **1,821 tests, 14 skipped, 1 error**:
`test_intake_ui.IntakeUIServiceTests.test_submit_intake_operation_completes_via_background_worker`,
failing in `TemporaryDirectory` cleanup with
`OSError: [Errno 39] Directory not empty: '.apoapsis'`. A background worker is
still writing into the temp tree while teardown removes it.

Measured rather than assumed, 20 repeats each on canonical Linux/ext4:

| Tree | Failures in 20 |
| --- | --- |
| `918bc82` baseline | 2 |
| 7P.3 tree | 5 |

Both fail, so it is pre-existing. The rate difference is not meaningful at
n=20, and this slice's diff is documentation plus `test_qualification_pilot.py`,
which `test_intake_ui` does not import and cannot be affected by.

That makes **two** known intermittents now, both timing-sensitive and both on
infrastructure the pilot depends on: this one and `test_workcell_relay`'s
dropped-stream assertion (4 pass / 1 fail over five repeats at both trees).
Stage 3 of the rehearsal exists precisely to require that the relay
intermittent is not currently reproducible before live inference. Neither is
waived here; both are recorded as pre-live defects.

The 14 skips are the 12 long-standing ones plus the two
`ExecutableProvenanceTests` cases, which skip in the staged canonical tree
because `canonical3.sh` stages without `.git` and both tests need it. On a real
checkout they execute — one passing, one `expectedFailure` — which is where the
Finding B evidence above comes from.

## Remediation, in the handoff's own terms

A missing runner requires a superseding manifest and lock before rehearsal.
The mutation rule gives the order, and both findings are fixed in the same
pass because both change what must be bound:

1. **Author the rehearsal runner** as real, reviewable source: six-slot
   scheduler, scripted pilot provider, arm-slot driver with teardown proof, and
   the rehearsal verdict model.
2. **Bind it.** Add runner identity to the manifest schema — module digests, or
   a runner commit plus a fake-provider script digest — so the verdict-deciding
   executables are inside the artifact that authorises them.
3. **Correct `evaluator_framework_commit`** to a commit that actually contains
   `pilot.py`, and add a validator that refuses a lock naming an evaluator
   commit which does not contain its own schema module. The check is cheap
   (`git cat-file -e <commit>:<path>`) and would have caught this in 7P.2.
4. **Re-run the affected deterministic qualification** — the eight real package
   proofs and the pilot manifest/lock suite.
5. **Issue a superseding manifest and lock**, in two commits as before.
6. **Rehearse from the beginning** under the new lock.

The existing manifest and lock are left intact as the superseded pair rather
than edited, so the supersession is legible.

## Status

Live preflight — **not authorized**.
Rehearsal — **not executed**.
Manifest and lock — **unmodified**, digests as recorded above.
Eight-case draft — untouched, `8c374827…`, 8 unresolved, not ready, no lock.
Model service, model load, inference — **none occurred**.
