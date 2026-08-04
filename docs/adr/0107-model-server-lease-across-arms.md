# ADR 0107: Lease one model server per run, verified before each arm

## Status

Accepted and implemented on 2026-08-03.

## Context

`ModelServer` cold-starts `llama-server` per arm per attempt: the 16.8 GB
Qwen3.6-27B GGUF is loaded from disk every time. With
`high_assurance_parity_guard` on that is two loads per slice, and aborted
attempts multiply it — `TASK-198B36B72AEB637B0FAACE7B` accumulated 17 CAP
directories for roughly five slice executions, several containing only bridge
logs from attempts that died before preflight finished. This, not inference, is
the dominant wall-clock cost of a plan run and the main reason runs *feel*
slow.

The reason it was built that way is sound and must survive: every evidence
record is bound to a server whose identity was established for this run, and a
resident process nobody checked is precisely what a frozen manifest exists to
rule out.

But reloading is not what establishes provenance. *Checking* is. The weights a
running server has open, the alias it serves, and the argv it is running under
are all observable from outside the process — and observing them is strictly
more evidence than re-running a load that was trusted only because it was
recent.

## Decision

A **run-scoped lease** starts `llama-server` once and re-verifies it before
each arm.

**Verification is per arm, never per lease.** A server verified at the start of
a plan run and used for twelve slices afterwards has an identity twelve slices
old. Each arm re-observes: `/health`, the model path from `/props`, the alias
from `/v1/models`, and the argv read back from `/proc/<pid>/cmdline` — read
back, not remembered, because the argv we intended to pass is not evidence
about the process that is running.

**Unavailable is not a pass.** A check that could not run is recorded in
`checks_unavailable`, separately from mismatches, and does not contribute to
`verified`. A build serving neither `/props` nor `/v1/models` offers nothing to
check the weights against, so it is not leased. `verified` additionally
requires `health` plus at least one weight-identifying check, and
`checks_passed` names what actually ran — otherwise a verification that checked
nothing and one that checked four things would both report success.

**A failed check falls back to a cold start.** `verified` goes false, the
mismatch is recorded with expected and observed values, and the arm runs
against a fresh `ModelServer` exactly as before this ADR. That makes the change
never worse than the previous behaviour, only sometimes much faster. What is
never permitted is the third option — serving an arm from a process whose
identity nobody could establish — because a run that quietly continued against
the wrong weights would produce evidence indistinguishable from real evidence.

**The KV cache is erased between arms, and the erase is recorded.** Two arms
sharing a process share its cache. A prefix left behind by the control arm
would make the sandbox arm's first prefill cheaper for a reason that has
nothing to do with the sandbox, which is exactly the kind of contamination the
paired comparison exists to exclude. A refused erase is recorded as such rather
than assumed.

**Qualification pilots are untouched.** `live_pilot.run_live_pilot` keeps its
per-slot cold start. Its frozen semantics are the authority the product path
inherits, and quietly changing the conditions under which that authority was
established would invalidate it.

`LeaseRecord` — server starts, arms served, every verification and reset, and
any fallback — is written to the run's evidence and into `result.json`, so the
saving is a number rather than an impression.

## Consequences

A multi-slice plan run loads the model once in the common case instead of twice
per slice plus once per aborted attempt. On the observed task that is one load
where there were at least ten.

The lease is a new place a mistake could serve the wrong model. The mitigation
is that it checks more before each arm than the cold-start path ever did: a
cold start proves the *file* it was pointed at, while the lease proves what the
running process actually has open, every time.

`/slots?action=erase` is a llama.cpp endpoint. A build that does not serve it
records a refused erase rather than failing the run — arms then share a warm
cache, which is a measurement caveat and is recorded as one, not a correctness
failure.
