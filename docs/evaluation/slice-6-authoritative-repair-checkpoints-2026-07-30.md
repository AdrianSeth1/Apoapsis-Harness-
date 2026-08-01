# Slice 6: authoritative repair checkpoints

Date: 2026-07-30. **No model call.** Deterministic implementation and coverage.

## Verdict

| Requirement | Result |
|---|---|
| One `PlanCheckpoint` state model for every repair | **IMPLEMENTED** |
| Local / frontier / human use the same interface | **IMPLEMENTED — asserted identical, not just similar** |
| Repairs bound to snapshot, commit/fingerprint, contract, evidence, failure packet | **IMPLEMENTED — five bindings** |
| Stale / mismatched / out-of-band / partial repairs rejected | **IMPLEMENTED — nine distinct refusals** |
| Repairs applied only in controller-owned candidate state | **IMPLEMENTED** |
| Admission, witnesses, readiness, verification rerun after repair | **IMPLEMENTED** |
| Append-only provenance and resulting checkpoint | **IMPLEMENTED** |
| Later slices inherit the repaired checkpoint | **IMPLEMENTED** |
| Delivery reads current state, never an older report or out-of-band commit | **IMPLEMENTED** |
| Models hold no transition/verification/completion/Git/host/delivery authority | **PRESERVED** |
| Required deterministic coverage | **9/9, plus 16 boundary cases — 25 passing** |
| Full suite on Python 3.11+ | **DEFERRED to qualification, per frozen scope** |

## The nine required cases

| # | Case | Test |
|---|---|---|
| 1 | Local repair succeeds and becomes the next slice's base | `test_local_repair_succeeds_and_becomes_the_next_slice_base` |
| 2 | Frontier repair, identical transition | `test_frontier_repair_follows_the_identical_transition` |
| 3 | Human repair, identical transition | `test_human_repair_follows_the_identical_transition` |
| 4 | Stale repair rejected | `test_a_stale_repair_is_rejected` |
| 5 | Repair failing verification cannot advance | `test_a_repair_that_fails_verification_cannot_advance` |
| 6 | Repeated application idempotent or refused | `test_repeated_application_is_explicitly_refused` |
| 7 | Later-slice planning sees repaired files and evidence | `test_later_slice_planning_sees_repaired_files_and_evidence` |
| 8 | Delivery serialises the repaired checkpoint | `test_delivery_serialises_the_repaired_checkpoint` |
| 9 | Crisis Atlas stale-projection shape cannot recur | `test_delivery_refuses_the_original_failed_fingerprint` |

## What the design turns on

**A repair is a transition, not an edit.** The Crisis Atlas trial's best result
came from Codex repairing Qwen's work, and it was still not a deliverable —
because the repair was a commit somebody made and the plan graph never learned
about it. One shape now, for everyone: bind, apply in controller state, admit,
witness, readiness, required verification, append.

**A human repair is not exempt.** Being made by a person is a fact about
provenance, not evidence about the tree. An exempt path would be the out-of-band
commit this slice exists to refuse. `test_the_three_differ_only_in_recorded_actor`
asserts the three actors produce an identical result shape; if one ever needs
different setup, the single-transition guarantee is gone.

**Binding happens before applying.** A stale proposal never touches candidate
state, and a test asserts the applier is never called.

**Failing verification does not move the head.** No checkpoint is appended, so a
later slice inherits the last authoritative state rather than a repaired tree
that did not pass.

**Delivery and the next slice return the same object.** Two accessors returning
different states is precisely how Crisis Atlas inherited repaired files without
the repaired checkpoint. A test asserts object identity, not equality.

### A gap found while writing the ADR

The first implementation declared a `failure_packet_sha256` binding and a
`FAILURE_PACKET_MISMATCH` rejection, and then never checked either — dead enum
value, unenforced binding, and a validation story that read as stronger than it
was. Closed before the ADR was written.

It matters more than a tidy-up. The other four bindings cannot see this case: a
repair written for failure A, applied after a different repair already fixed A
and left failure B current. Parent, commit, tree and contract all still match
while the proposal answers a question nobody is asking. Two tests cover it,
including a repair proposed against an already-completed checkpoint, which has
no failure to answer at all.

## Deliberate omissions

**No automatic rebasing.** A proposal written against a superseded tree could
often be replayed successfully, and doing so would silently change what the
repair was reviewed against. Callers rebase explicitly or not at all.

**Re-application is refused, not silently idempotent.** The instruction allowed
either. Refusal was chosen because returning the existing checkpoint hides what
is almost always a caller bug.

## What this cost

Repair is more expensive — every repair, including a human's, reruns admission,
witness emission, readiness and verification, and a slightly stale proposal is
refused rather than fixed up. That is the intended trade: the cheap version
produced a best-in-trial result that could not be shipped.

## Against the governing question

Does this help Apoapsis Qwen match or beat unharnessed Qwen per case, reduce
false completion, or preserve the authority boundary?

- **Per-case superiority:** directly. Delivered-result superiority depends on
  repair being real, and a repair outside the plan graph is not a deliverable.
- **False completion:** a repair can no longer produce a `COMPLETE` that
  verification did not reach, and a stale projection now raises.
- **Authority boundary:** preserved and narrowed. `RepairProposal` carries no
  authority from any actor class.

## Verification

25 tests in `tests/test_workcell_plan_checkpoint.py`, all passing under the
documented Python 3.10 compatibility shim; adjacent workcell and paired-scoring
suites unaffected. Per the frozen scope, the full deterministic suite runs once
on Python 3.11+ **before qualification**, not before every implementation step.

**Next: paired corpus, Crisis Atlas must-pass regression, and negative controls.
Not started — and not to be started without an explicit instruction, since
rollout gates on it.**
