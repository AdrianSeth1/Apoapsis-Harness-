"""Slice 6: the nine required repair-checkpoint cases, plus the boundary.

The organising claim is that local, frontier and human repair are *the same
transition*. Three near-identical tests assert exactly that, and the duplication
is the point: if one of them ever needs a different setup, the interface has
grown a second path and the guarantee is gone.
"""

from __future__ import annotations

import unittest

from apoapsis.workcell.acceptance import CheckpointDecision, CheckpointOutcome
from apoapsis.workcell.plan_checkpoint import (
    ActorClass,
    PlanCheckpoint,
    PlanCheckpointLedger,
    RepairBinding,
    RepairProposal,
    RepairRejection,
    StaleProjection,
    apply_repair,
    authoritative_delivery_input,
    next_slice_base,
)

_COMMIT = "a" * 40
_CONTRACT = "c" * 64
_FINGERPRINT = "f" * 64
_REPAIRED = "e" * 64
_PACKET = "d" * 64
_LOCAL = "qwen3.6-27b@sha256:5ed60d"


def _decision(outcome=CheckpointOutcome.COMPLETE, detail="ok"):
    return CheckpointDecision(
        outcome=outcome,
        admitted=outcome is not CheckpointOutcome.CANDIDATE_REFUSED,
        ready=outcome is CheckpointOutcome.COMPLETE,
        detail=detail,
    )


def _seed(outcome=CheckpointOutcome.CONTINUE):
    """A ledger whose head is an admitted but unfinished slice."""

    ledger = PlanCheckpointLedger()
    checkpoint = PlanCheckpoint(
        checkpoint_id=PlanCheckpoint.compute_id(
            parent_checkpoint_id=None,
            slice_id="slice-2",
            contract_digest=_CONTRACT,
            base_commit=_COMMIT,
            worktree_fingerprint=_FINGERPRINT,
            actor=ActorClass.LOCAL_MODEL,
            actor_fingerprint=_LOCAL,
            outcome=str(outcome),
            repair_proposal_id=None,
        ),
        slice_id="slice-2",
        contract_digest=_CONTRACT,
        base_commit=_COMMIT,
        worktree_fingerprint=_FINGERPRINT,
        actor=ActorClass.LOCAL_MODEL,
        actor_fingerprint=_LOCAL,
        decision=_decision(outcome, "obligations outstanding"),
        obligations_open=["AC-2"],
        failure_packet_sha256=_PACKET,
    )
    ledger.append(checkpoint)
    return ledger


def _binding(**overrides):
    values = {
        "parent_checkpoint_id": None,
        "base_commit": _COMMIT,
        "worktree_fingerprint": _FINGERPRINT,
        "contract_digest": _CONTRACT,
        "failure_packet_sha256": _PACKET,
    }
    values.update(overrides)
    return RepairBinding(**values)


def _proposal(ledger, actor=ActorClass.LOCAL_MODEL, fingerprint=None, **overrides):
    binding = overrides.pop(
        "binding", _binding(parent_checkpoint_id=ledger.head().checkpoint_id)
    )
    return RepairProposal(
        proposal_id=overrides.pop("proposal_id", "repair-1"),
        actor=actor,
        actor_fingerprint=fingerprint or _LOCAL,
        binding=binding,
        declared_paths=overrides.pop("declared_paths", ["app/export_service.py"]),
        **overrides,
    )


def _apply(paths=("app/export_service.py",)):
    def run(proposal):
        return list(paths)

    return run


def _verify(outcome=CheckpointOutcome.COMPLETE, fingerprint=_REPAIRED):
    def run(proposal):
        return (
            _decision(outcome),
            ["witness-export-1"],
            ["AC-1", "AC-2"],
            [],
            fingerprint,
        )

    return run


class OneTransitionForEveryActorTests(unittest.TestCase):
    """Cases 1-3. The same call, three actor classes, one state shape."""

    def _repair_as(self, actor, fingerprint):
        ledger = _seed()
        result = apply_repair(
            ledger,
            _proposal(ledger, actor=actor, fingerprint=fingerprint),
            apply=_apply(),
            verify=_verify(),
            local_model_fingerprint=_LOCAL,
        )
        return ledger, result

    def test_local_repair_succeeds_and_becomes_the_next_slice_base(self) -> None:
        ledger, result = self._repair_as(ActorClass.LOCAL_MODEL, _LOCAL)
        self.assertTrue(result.accepted)
        self.assertIs(next_slice_base(ledger), result.checkpoint)
        self.assertEqual(next_slice_base(ledger).worktree_fingerprint, _REPAIRED)

    def test_frontier_repair_follows_the_identical_transition(self) -> None:
        ledger, result = self._repair_as(ActorClass.FRONTIER_MODEL, "codex-5@openai")
        self.assertTrue(result.accepted)
        self.assertIs(result.checkpoint.actor, ActorClass.FRONTIER_MODEL)
        self.assertIs(next_slice_base(ledger), result.checkpoint)

    def test_human_repair_follows_the_identical_transition(self) -> None:
        ledger, result = self._repair_as(ActorClass.HUMAN, "owner@apoapsis")
        self.assertTrue(result.accepted)
        self.assertIs(result.checkpoint.actor, ActorClass.HUMAN)
        self.assertIs(next_slice_base(ledger), result.checkpoint)

    def test_the_three_differ_only_in_recorded_actor(self) -> None:
        """A human repair is not exempt from verification.

        Being made by a person is a fact about provenance, not evidence about
        the tree. If this ever needs a different code path, the single-transition
        guarantee has been lost.
        """

        shapes = set()
        for actor, fingerprint in (
            (ActorClass.LOCAL_MODEL, _LOCAL),
            (ActorClass.FRONTIER_MODEL, "codex-5@openai"),
            (ActorClass.HUMAN, "owner@apoapsis"),
        ):
            _, result = self._repair_as(actor, fingerprint)
            shapes.add(
                (
                    result.accepted,
                    result.checkpoint.decision.outcome,
                    result.checkpoint.worktree_fingerprint,
                    tuple(result.checkpoint.witness_ids),
                )
            )
        self.assertEqual(len(shapes), 1)


class RefusalTests(unittest.TestCase):
    """Cases 4-6, plus the bindings that make staleness visible."""

    def test_a_stale_repair_is_rejected(self) -> None:
        ledger = _seed()
        stale = _proposal(ledger, binding=_binding(parent_checkpoint_id="9" * 64))
        result = apply_repair(ledger, stale, apply=_apply(), verify=_verify())
        self.assertFalse(result.accepted)
        self.assertIs(result.rejection, RepairRejection.STALE_PARENT)
        self.assertEqual(len(ledger.checkpoints), 1)

    def test_a_stale_repair_never_touches_candidate_state(self) -> None:
        """Binding is checked before applying, not after."""

        touched = []

        def apply(proposal):
            touched.append(proposal.proposal_id)
            return ["app/export_service.py"]

        ledger = _seed()
        apply_repair(
            ledger,
            _proposal(ledger, binding=_binding(parent_checkpoint_id="9" * 64)),
            apply=apply,
            verify=_verify(),
        )
        self.assertEqual(touched, [])

    def test_a_repair_bound_to_a_different_tree_is_rejected(self) -> None:
        ledger = _seed()
        result = apply_repair(
            ledger,
            _proposal(
                ledger,
                binding=_binding(
                    parent_checkpoint_id=ledger.head().checkpoint_id,
                    worktree_fingerprint="b" * 64,
                ),
            ),
            apply=_apply(),
            verify=_verify(),
        )
        self.assertIs(result.rejection, RepairRejection.FINGERPRINT_MISMATCH)

    def test_a_contract_that_drifted_is_its_own_rejection(self) -> None:
        ledger = _seed()
        result = apply_repair(
            ledger,
            _proposal(
                ledger,
                binding=_binding(
                    parent_checkpoint_id=ledger.head().checkpoint_id,
                    contract_digest="7" * 64,
                ),
            ),
            apply=_apply(),
            verify=_verify(),
        )
        self.assertIs(result.rejection, RepairRejection.CONTRACT_DRIFT)

    def test_a_repair_that_fails_verification_cannot_advance(self) -> None:
        ledger = _seed()
        before = ledger.head().checkpoint_id
        result = apply_repair(
            ledger,
            _proposal(ledger),
            apply=_apply(),
            verify=_verify(CheckpointOutcome.CONTINUE),
            local_model_fingerprint=_LOCAL,
        )
        self.assertFalse(result.accepted)
        self.assertIs(result.rejection, RepairRejection.VERIFICATION_FAILED)
        self.assertEqual(ledger.head().checkpoint_id, before)
        self.assertEqual(len(ledger.checkpoints), 1)

    def test_repeated_application_is_explicitly_refused(self) -> None:
        ledger = _seed()
        proposal = _proposal(ledger)
        first = apply_repair(ledger, proposal, apply=_apply(), verify=_verify())
        self.assertTrue(first.accepted)
        again = apply_repair(ledger, proposal, apply=_apply(), verify=_verify())
        self.assertFalse(again.accepted)
        self.assertIs(again.rejection, RepairRejection.ALREADY_APPLIED)
        self.assertEqual(len(ledger.checkpoints), 2)

    def test_a_partial_apply_is_refused_rather_than_verified(self) -> None:
        ledger = _seed()
        result = apply_repair(
            ledger,
            _proposal(ledger, declared_paths=["a.py", "b.py"]),
            apply=_apply(("a.py",)),
            verify=_verify(),
        )
        self.assertIs(result.rejection, RepairRejection.PARTIALLY_APPLIED)

    def test_an_applier_that_raises_is_a_refusal_not_a_crash(self) -> None:
        def boom(proposal):
            raise OSError("disk full mid-write")

        ledger = _seed()
        result = apply_repair(ledger, _proposal(ledger), apply=boom, verify=_verify())
        self.assertIs(result.rejection, RepairRejection.PARTIALLY_APPLIED)
        self.assertIn("disk full", result.detail)

    def test_a_repair_answering_a_superseded_failure_is_refused(self) -> None:
        """The mismatch the other four bindings cannot see.

        Parent, commit, tree and contract can all match while the failure the
        repair was written for has already been fixed by an earlier one.
        """

        ledger = _seed()
        result = apply_repair(
            ledger,
            _proposal(
                ledger,
                binding=_binding(
                    parent_checkpoint_id=ledger.head().checkpoint_id,
                    failure_packet_sha256="2" * 64,
                ),
            ),
            apply=_apply(),
            verify=_verify(),
        )
        self.assertIs(result.rejection, RepairRejection.FAILURE_PACKET_MISMATCH)

    def test_a_completed_checkpoint_has_no_failure_to_repair(self) -> None:
        ledger, _ = InheritanceAndDeliveryTests()._repaired()
        result = apply_repair(
            ledger,
            _proposal(
                ledger,
                proposal_id="repair-2",
                binding=_binding(
                    parent_checkpoint_id=ledger.head().checkpoint_id,
                    worktree_fingerprint=_REPAIRED,
                ),
            ),
            apply=_apply(),
            verify=_verify(),
        )
        self.assertIs(result.rejection, RepairRejection.FAILURE_PACKET_MISMATCH)

    def test_frontier_from_the_local_endpoint_is_refused(self) -> None:
        """More turns from the same model is a continuation, not review."""

        ledger = _seed()
        result = apply_repair(
            ledger,
            _proposal(ledger, actor=ActorClass.FRONTIER_MODEL, fingerprint=_LOCAL),
            apply=_apply(),
            verify=_verify(),
            local_model_fingerprint=_LOCAL,
        )
        self.assertIs(result.rejection, RepairRejection.NOT_A_STRONGER_TIER)


class InheritanceAndDeliveryTests(unittest.TestCase):
    """Cases 7-9."""

    def _repaired(self):
        ledger = _seed()
        result = apply_repair(
            ledger, _proposal(ledger), apply=_apply(), verify=_verify()
        )
        return ledger, result.checkpoint

    def test_later_slice_planning_sees_repaired_files_and_evidence(self) -> None:
        ledger, repaired = self._repaired()
        base = next_slice_base(ledger)
        self.assertEqual(base.worktree_fingerprint, _REPAIRED)
        self.assertEqual(base.witness_ids, ["witness-export-1"])
        self.assertEqual(base.obligations_open, [])
        self.assertEqual(base.repair_proposal_id, "repair-1")

    def test_delivery_serialises_the_repaired_checkpoint(self) -> None:
        ledger, repaired = self._repaired()
        self.assertIs(authoritative_delivery_input(ledger), repaired)

    def test_delivery_refuses_the_original_failed_fingerprint(self) -> None:
        """Case 9: the Crisis Atlas stale projection, exactly.

        A caller holding the pre-repair report passes the fingerprint that
        report describes. Delivery must refuse rather than project a superseded
        run forward as the delivered result.
        """

        ledger, _ = self._repaired()
        with self.assertRaises(StaleProjection) as caught:
            authoritative_delivery_input(ledger, claimed_fingerprint=_FINGERPRINT)
        self.assertIn("stale projection", str(caught.exception))

    def test_delivery_accepts_the_current_fingerprint(self) -> None:
        ledger, _ = self._repaired()
        self.assertEqual(
            authoritative_delivery_input(
                ledger, claimed_fingerprint=_REPAIRED
            ).worktree_fingerprint,
            _REPAIRED,
        )

    def test_an_unfinished_head_cannot_be_delivered_or_inherited(self) -> None:
        ledger = _seed()
        with self.assertRaises(StaleProjection):
            authoritative_delivery_input(ledger)
        with self.assertRaises(StaleProjection):
            next_slice_base(ledger)

    def test_delivery_and_the_next_slice_read_the_same_object(self) -> None:
        """Two accessors returning different states is the Crisis Atlas bug."""

        ledger, _ = self._repaired()
        self.assertIs(authoritative_delivery_input(ledger), next_slice_base(ledger))


class LedgerTests(unittest.TestCase):
    def test_the_ledger_is_append_only_and_keeps_the_failed_parent(self) -> None:
        ledger, repaired = InheritanceAndDeliveryTests()._repaired()
        self.assertEqual(len(ledger.checkpoints), 2)
        self.assertIs(
            ledger.checkpoints[0].decision.outcome, CheckpointOutcome.CONTINUE
        )
        self.assertEqual(
            ledger.checkpoints[1].parent_checkpoint_id,
            ledger.checkpoints[0].checkpoint_id,
        )

    def test_appending_off_the_head_is_refused(self) -> None:
        """An out-of-band commit, in ledger form."""

        ledger = _seed()
        orphan = ledger.head().model_copy(
            update={"parent_checkpoint_id": "0" * 64, "checkpoint_id": "1" * 64}
        )
        with self.assertRaises(ValueError):
            ledger.append(orphan)

    def test_identity_includes_ancestry(self) -> None:
        common = dict(
            slice_id="slice-2",
            contract_digest=_CONTRACT,
            base_commit=_COMMIT,
            worktree_fingerprint=_FINGERPRINT,
            actor=ActorClass.LOCAL_MODEL,
            actor_fingerprint=_LOCAL,
            outcome="complete",
            repair_proposal_id=None,
        )
        self.assertNotEqual(
            PlanCheckpoint.compute_id(parent_checkpoint_id=None, **common),
            PlanCheckpoint.compute_id(parent_checkpoint_id="3" * 64, **common),
        )

    def test_an_accepted_result_must_carry_a_checkpoint(self) -> None:
        from apoapsis.workcell.plan_checkpoint import RepairResult

        with self.assertRaises(Exception):
            RepairResult(accepted=True, detail="ok")


if __name__ == "__main__":
    unittest.main()
