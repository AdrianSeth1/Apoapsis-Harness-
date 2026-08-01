# ADR 0084: every repair is one authoritative state transition

Date: 2026-07-30

Status: accepted. Implements Slice 6. Builds on ADR 0079 (readiness-based
completion) and ADR 0077 (authority boundary); supersedes nothing.

## Context

The Crisis Atlas trial produced its best result by having Codex repair what Qwen
proposed — and that result was still not a deliverable. The reason is not that
the repair was bad. It is that the repair happened *beside* the state machine.
It was a commit somebody made. The plan graph never learned about it, later
slices inherited the files without inheriting the checkpoint, and the final
report went on projecting the outcome of the run that had failed.

The handoff states the requirement directly:

> A Codex or human repair must become an authoritative `PlanCheckpoint`. Direct
> repair commits outside the plan graph are evaluation evidence, not a
> deliverable plan.

Two failure shapes have to be closed together. A repair that never enters the
graph is one. A repair that enters it while being stale — written against a tree,
contract, or failure that is no longer current — is the other, and it is worse,
because it looks like it worked.

## Decision

**A repair is a state transition, not an edit, and it has exactly one shape:**

```
bind -> apply in controller state -> admit -> witness -> readiness
     -> required verification -> append authoritative checkpoint
```

### One interface for three actors

Local Qwen, a genuinely stronger frontier model, and a human all enter through
`apply_repair` with the same `RepairProposal`. There is no second path.

A human repair is not exempt from verification. Being made by a person is a fact
about provenance, not evidence about the tree, and an exempt path would be
precisely the out-of-band commit this ADR exists to refuse. A test asserts the
three actors produce an identical result shape, and the duplication in that test
is deliberate: if one ever needs different setup, the guarantee is gone.

`ActorClass.FRONTIER_MODEL` from the local model's own fingerprint is refused as
`NOT_A_STRONGER_TIER`. More turns from the same endpoint is a continuation.

### Five bindings, because they drift independently

A proposal names the parent checkpoint, base commit, worktree fingerprint,
contract digest, and **failure packet**. The first four catch the obvious
staleness. The fifth catches a case none of them can see: a repair written for
failure A, applied after a different repair already fixed A and left failure B
current. Commit, tree, contract and parent all still match while the proposal
answers a question nobody is asking.

Binding is checked **before** anything is applied, so a stale proposal never
touches candidate state. A test asserts the applier is never called.

### Refusals are distinct, because their repairs differ

`STALE_PARENT`, `FINGERPRINT_MISMATCH`, `CONTRACT_DRIFT`,
`FAILURE_PACKET_MISMATCH`, `OUT_OF_BAND`, `PARTIALLY_APPLIED`,
`ALREADY_APPLIED`, `VERIFICATION_FAILED`, `NOT_A_STRONGER_TIER`. A stale
proposal should be rebased; a verification failure should not. Collapsing these
into one failure would tell the caller nothing about what to do next.

Re-application is **refused**, not silently idempotent. Returning the existing
checkpoint would hide what is almost always a caller bug.

A partially applied repair is refused rather than verified as though whole: the
applier reports what it actually changed, and a mismatch against the declared
paths stops it there.

### Failing verification does not move the head

If the repaired candidate does not reach `COMPLETE`, no checkpoint is appended.
The head stays where it was, so a later slice inherits the last *authoritative*
state rather than a repaired tree that did not pass.

### The ledger is append-only

Nothing is edited, nothing removed. A repair appends a child; the failed parent
stays as history. `append` refuses anything whose parent is not the current
head, which is an out-of-band commit in ledger form.

Checkpoint identity is content **plus ancestry**, so two checkpoints with
identical trees and different histories are different objects. That is what
makes the ledger a chain rather than a set.

### Delivery and later slices read the same object

`authoritative_delivery_input` and `next_slice_base` both return the head, and a
test asserts they return the *same object*. Two accessors returning different
states is exactly how Crisis Atlas inherited repaired files without inheriting
the repaired checkpoint.

`authoritative_delivery_input` takes an optional `claimed_fingerprint` to catch
the stale projection head-on: a caller holding a pre-repair report passes the
fingerprint that report describes, and delivery raises `StaleProjection` rather
than presenting a superseded run as the delivered result.

An unfinished head can be neither delivered nor inherited.

### Models remain untrusted proposers

Nothing here grants a model authority to transition state, run its own
verification, declare completion, touch Git, reach the host, or produce a
delivery. `RepairProposal` carries no authority; it is a request the controller
adjudicates.

## Consequences

Repair becomes more expensive. Every repair — including a human's — reruns
admission, witness emission, readiness and required verification, and a repair
bound to slightly stale state is refused rather than rebased automatically. That
is the intended cost: the cheap version is what produced a best-in-trial result
that could not be shipped.

Automatic rebasing is deliberately absent. A proposal written against a
superseded tree could often be replayed successfully, and doing so would silently
change what the repair was reviewed against. The caller rebases explicitly or
not at all.

Slice 6 exits when a repaired checkpoint is the sole authoritative input to
later slices and delivery, which the nine required cases cover. Corpus
qualification and rollout do not begin here.
