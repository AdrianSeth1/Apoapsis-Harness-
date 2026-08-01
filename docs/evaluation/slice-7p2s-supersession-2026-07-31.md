# Slice 7P.2S: bind the runner, correct the authority, fix the races

Date: 2026-07-31. **No `llama-server`, no GGUF load, no readiness call, no
inference, no rehearsal.** The v1 manifest and lock are preserved unedited.

Three commits: **R** `b30079a` (executables), **M** `e4b82f5` (manifest v2),
**L** this commit (lock v2).

## What 7P.3 found, and what R does about it

**No runner was bound.** The locked commits held every decision kernel and
nothing that sequenced them, so the lock could not authorise a rehearsal. R
adds the missing executables — six-slot scheduler, scripted provider, arm
drivers, teardown prover, evidence writer, negative-control injector, verdict
model — as reviewable source, bound by digest in M before anything runs.

**The lock did not bind its own validator.** `pilot.py` defines `PilotLock` and
`authorize_rehearsal`, arrived in `a5a30d2`, and the lock named `22cd8af`,
where it does not exist. Every test passed because every test imported the
module from the working tree.

`qualification/authority.py` never imports what it checks. It reads Git objects
(`cat-file -e`, `rev-parse`, `cat-file blob`), so a checkout holding newer or
better files cannot make a missing object present. A test builds a real
repository, commits a module *after* the commit under test, and proves the
working tree cannot satisfy the authority. Another asserts `22cd8af` still
cannot, and says in its message that a pass there means history was rewritten.

## Five intermittents, two root causes, no retries

Two were named in the handoff. Running the suite found three more, all of the
same two families.

### Worker lifecycle — a product defect

`IntakeWorker` and `ReviewWorker` ran `while True: queue.get()` on daemon
threads with **no way to stop**. A caller finishing with a service could only
drop its reference, leaving a thread writing into `.apoapsis` while
`TemporaryDirectory.cleanup` removed it. The old comment — "daemon threads stop
with the process" — is true and beside the point: the process outlives one test
by a whole suite.

Both workers now take a queue sentinel and join. The sentinel goes *through the
queue* rather than setting a flag, so queued work is drained rather than
dropped, and a worker blocked in `get()` wakes rather than waiting for a job
that will never arrive. `ApoapsisUIService.shutdown_workers` reports whether
they actually stopped, because "no surviving background worker" is evidence a
caller must check — Stage 8 of the rehearsal needs exactly that.

### Relay observation — a test defect plus a missing product affordance

An HTTP call returns when the response is written; the request is recorded
afterwards on the handler thread. Any observer reading `stats` immediately
races the relay and usually wins, which is why this surfaced once every few
dozen runs rather than at once.

`ModelRelay.wait_for_records` is a synchronisation primitive, not a retry: it
waits for the event the caller is about to assert and reports honestly if it
never arrived. The rehearsal's containment stage needs it, because a count read
too early understates traffic and would make a bypassed turn look like a quiet
one.

The dropped-stream test was waiting for the wrong event entirely — the relay's
own record, rather than the far end noticing its socket closed on a 5 ms
cadence — and now waits for the one it asserts.

### Measured, before and after

| Test | Before | After |
| --- | --- | --- |
| `relay ... dropped_stream` | 1 fail / 5 | **50/50 consecutive** |
| `relay ... cross_origin_redirect` | 1 fail / 30 at baseline | **50/50** |
| `relay ... unauthorized_path` | 2 fail / 30 at baseline | **50/50** |
| `intake ... background_worker` | 2/20 baseline, 5/20 here | **50/50** |
| `review ... background_worker` | present | **50/50** |

A failure aborts the streak, so "48 of 50" cannot be read as a pass. Two
deterministic regression tests cover the lifecycle directly: one asserts the
thread object is gone rather than sleeping and hoping, the other proves the
sentinel drains queued work.

## Package evidence was regenerated, not reused

The reuse validator compares **blobs** between the original qualification
authority `22cd8af` and Commit R. Comparing commits would be too strict;
comparing behaviour too weak. It found exactly one difference and named it:

```
src/apoapsis/qualification/case_package.py
```

which changed in 7P.2 when `validate_repetitions` learned to refuse
`sampling_seed` without proven propagation. That module decided the eight
proofs, so the earlier evidence no longer describes the code that produced it.
All eight real proofs were **re-run**; all eight pass.

| | Before | After |
| --- | --- | --- |
| Package digest | `d7c4b195…` | `d7c4b195…` (bytes unmoved) |
| Evidence digest | `d6c67ce6…` | **`236e650f5f899abe5585ab0921ff7305f8af354efa9abc25c5bfe931660009cf`** |

Assuming reuse was safe would have bound evidence produced by code that no
longer exists.

## Supersession

The v1 pair is preserved rather than edited, and marked: superseded, **never
rehearsed**, **never authorized for live inference**, invalid for both recorded
reasons. Rewriting it to look correct would destroy the record of what was
actually locked when — which is the only reason anyone can now say it never
authorised anything.

The v1 manifest is therefore *deliberately stale*: it records the
pre-requalification evidence digest. A test asserts it still parses and is
still marked, and another asserts the v1 lock cannot authorise the v2 manifest.

## Two tests that changed state on their own

The evaluator-commit check was `expectedFailure` in 7P.3, written so it would
start passing by itself once the binding was corrected. Under the v2 lock it
does, so the marker is removed — a passing test under `expectedFailure` is an
*unexpected success*, which is a failure with a friendlier name.

The placeholder asserting "no runner is bound" has been rewritten into an
assertion that one is. That rewrite exposed a flaw worth recording: the
placeholder inspected the **lock** for runner-shaped fields, and the authority
lives in the **manifest**. Left alone it would have kept passing vacuously
after the binding landed. Asserting an absence is only safe when it is checked
where the presence would actually be.

## Identities

| Item | Value |
| --- | --- |
| Manifest v2 | `docs/qualification/slice7-crisis-atlas-pilot-manifest-v2.json`, digest `91bc99d68dc0e63233a44d5316cc0982ff1593cf5c4a99f101bf434d1f5a169f` |
| Lock v2 | `docs/qualification/slice7-crisis-atlas-pilot-lock-v2.json`, digest `032afa70b81bb8dbe752588a82e2081239ed2c9c34a9424627e58037bc82b83c` |
| Pilot authority | `b30079a4697095d5528a3966d3314942b3ad8601`, 13 bound modules |
| Fake-provider script | `d90a85cf1decf1d215d275c7ca87c165004b020e8692a1e93ae9dc2739916f75` |
| Subject implementation | `b30079a…` (changed: R altered controller-side code) |
| Controller image | `apoapsis-pilot-controller:b30079a`, rebuilt from R |

## Verification

Focused: pilot 58, authority 10, rehearsal 31, orchestration 61, real
qualification 9, qualification+workcell 118. Canonical Linux/ext4/CPython 3.12,
venv activated: **1,874 tests, 19 skipped, zero failures.** `compileall` clean,
`diff --check` clean.

## Status

Rehearsal — **not executed**; 7P.3 restarts from Stage 0 under lock v2.
Live inference — **not authorized**, and the lock cannot authorize it.
Eight-case draft — untouched, `8c374827…`.
Crisis Atlas remains the only qualified pilot case, is not held-out evidence,
carries no model-quality claim, and default rollout stays prohibited.
