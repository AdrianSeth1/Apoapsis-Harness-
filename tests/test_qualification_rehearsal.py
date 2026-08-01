"""The rehearsal runner: ordering, teardown proof, controls and the verdict.

Nothing here runs a model, opens a socket or starts a container. The provider
is scripted and holds no transport, which one test asserts structurally rather
than trusting the docstring.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from apoapsis.qualification.fake_pilot_provider import (
    SCRIPTS,
    FakePilotProvider,
    ScriptExhausted,
    ScriptId,
    script_digest,
)
from apoapsis.qualification.pilot import PilotManifest
from apoapsis.qualification.rehearsal import (
    REQUIRED_DETECTORS,
    ArmSlotResult,
    EvidenceWriter,
    NegativeControl,
    NegativeControlResult,
    PairScore,
    RehearsalVerdict,
    StageOutcome,
    StageResult,
    TeardownProof,
    TokenAccounting,
    decide_verdict,
    prove_teardown,
    scheduled_slots,
)

REPO = Path(__file__).resolve().parents[1]
MANIFEST = (
    REPO / "docs" / "qualification" / "slice7-crisis-atlas-pilot-manifest.json"
)


def _teardown(clean: bool = True) -> TeardownProof:
    return TeardownProof(
        worktree_removed=clean,
        qwen_home_removed=clean,
        evidence_retained=True,
        no_surviving_worker=clean,
        no_surviving_relay_stream=clean,
        next_slot_cannot_reach_previous=clean,
    )


def _slot(
    repetition: str = "crisis-atlas-rep-1",
    arm: str = "default_qwen_control",
    order: int = 1,
    *,
    provider_requests: int = 1,
    relay_requests: int = 1,
    clean: bool = True,
    evaluator_only_absent: bool = True,
) -> ArmSlotResult:
    return ArmSlotResult(
        repetition_id=repetition,
        arm=arm,
        order_within_repetition=order,
        script=ScriptId.INCOMPLETE_PROPOSAL,
        seed_commit_verified=True,
        task_bytes_verified=True,
        arm_visible_mounts_verified=True,
        evaluator_only_absent=evaluator_only_absent,
        provider_requests=provider_requests,
        relay_observed_requests=relay_requests,
        checkpoint_outcome="CONTINUE",
        teardown=_teardown(clean),
        evidence_path=f"/evidence/{repetition}/{arm}",
    )


def _six_slots() -> tuple[ArmSlotResult, ...]:
    return tuple(
        _slot(repetition, arm, order)
        for repetition, arm, order in (
            ("crisis-atlas-rep-1", "default_qwen_control", 1),
            ("crisis-atlas-rep-1", "apoapsis_sandbox", 2),
            ("crisis-atlas-rep-2", "apoapsis_sandbox", 1),
            ("crisis-atlas-rep-2", "default_qwen_control", 2),
            ("crisis-atlas-rep-3", "default_qwen_control", 1),
            ("crisis-atlas-rep-3", "apoapsis_sandbox", 2),
        )
    )


def _controls() -> tuple[NegativeControlResult, ...]:
    return tuple(
        NegativeControlResult(
            control=control,
            required_detector=detector,
            detector_fired=detector,
            refused=True,
        )
        for control, detector in REQUIRED_DETECTORS.items()
    )


def _accounting() -> TokenAccounting:
    return TokenAccounting(
        session_aggregate_tokens=16_312,
        exposed_message_tokens=14_002,
        residual_tokens=2_310,
    )


def _pairs() -> tuple[PairScore, ...]:
    """Three pairs that carry actual scores.

    These were `PairScore(repetition_id=...)` with every quality left `None`,
    which is precisely the shape `decide_verdict` now refuses: `regressed` is
    False when a score is missing, so three empty pairs read exactly like three
    clean ones. A fixture that cannot be told apart from an unmeasured run is
    not a fixture for a passing run.
    """

    return tuple(
        PairScore(
            repetition_id=f"crisis-atlas-rep-{index}",
            control_proposal_quality=0.5,
            sandbox_proposal_quality=0.5,
            sandbox_detection_quality=1.0,
        )
        for index in (1, 2, 3)
    )


def _stages(outcome: StageOutcome = StageOutcome.PASSED) -> tuple[StageResult, ...]:
    return (
        StageResult(stage="stage-0-lock", outcome=outcome, detail="d"),
        StageResult(stage="stage-2-containment", outcome=outcome, detail="d"),
    )


class ScriptedProviderTests(unittest.TestCase):
    def test_the_provider_holds_no_transport(self) -> None:
        provider = FakePilotProvider(ScriptId.COMPLETE_PROPOSAL)
        self.assertFalse(provider.reaches_network)
        for attribute in ("session", "client", "connection", "socket", "urlopen"):
            self.assertFalse(
                hasattr(provider, attribute),
                f"the scripted provider exposes {attribute!r}",
            )

    def test_the_incomplete_script_reproduces_the_historical_shape(self) -> None:
        provider = FakePilotProvider(ScriptId.INCOMPLETE_PROPOSAL)
        turn = provider.complete("prompt")
        payload = json.loads(turn.content)

        self.assertEqual(len(payload["changes"]), 1)
        self.assertEqual(payload["changes"][0]["path"], "services/incident_service.py")
        # The summary claims three things the change set does not contain.
        self.assertIn("ExportService", payload["summary"])
        self.assertIn("tests", payload["summary"])
        # Not truncation: a proposal miss, exactly as recorded.
        self.assertEqual(turn.finish_reason, "stop")

    def test_the_complete_script_writes_both_services_at_declared_paths(self) -> None:
        provider = FakePilotProvider(ScriptId.COMPLETE_PROPOSAL)
        payload = json.loads(provider.complete("prompt").content)
        paths = {item["path"] for item in payload["changes"]}
        self.assertIn("crisis_atlas/services/incident_service.py", paths)
        self.assertIn("crisis_atlas/services/export_service.py", paths)
        self.assertIn("tests/test_services.py", paths)

    def test_an_exhausted_script_raises_rather_than_improvising(self) -> None:
        provider = FakePilotProvider(ScriptId.COMPLETE_PROPOSAL)
        provider.complete("one")
        with self.assertRaises(ScriptExhausted):
            provider.complete("two")

    def test_absent_telemetry_is_none_and_never_zero(self) -> None:
        turn = SCRIPTS[ScriptId.UNCLASSIFIED_STOP_REASON][0]
        self.assertIsNone(turn.input_tokens)
        self.assertIsNone(turn.output_tokens)
        self.assertIsNone(turn.session_total_tokens)

    def test_the_script_digest_changes_when_a_candidate_byte_changes(self) -> None:
        before = script_digest()
        original = SCRIPTS[ScriptId.COMPLETE_PROPOSAL]
        SCRIPTS[ScriptId.COMPLETE_PROPOSAL] = (
            original[0].model_copy(update={"content": original[0].content + " "}),
        )
        try:
            self.assertNotEqual(script_digest(), before)
        finally:
            SCRIPTS[ScriptId.COMPLETE_PROPOSAL] = original
        self.assertEqual(script_digest(), before)


class ScheduleTests(unittest.TestCase):
    def test_the_six_slots_come_from_the_manifest_in_the_frozen_order(self) -> None:
        manifest = PilotManifest.model_validate_json(
            MANIFEST.read_text(encoding="utf-8")
        )
        slots = scheduled_slots(manifest)
        self.assertEqual(len(slots), 6)
        self.assertEqual(
            [(repetition, arm) for repetition, arm, _ in slots],
            [
                ("crisis-atlas-rep-1", "default_qwen_control"),
                ("crisis-atlas-rep-1", "apoapsis_sandbox"),
                ("crisis-atlas-rep-2", "apoapsis_sandbox"),
                ("crisis-atlas-rep-2", "default_qwen_control"),
                ("crisis-atlas-rep-3", "default_qwen_control"),
                ("crisis-atlas-rep-3", "apoapsis_sandbox"),
            ],
        )


class TeardownProofTests(unittest.TestCase):
    def test_proof_observes_absence_rather_than_trusting_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            worktree = root / "worktree"
            qwen_home = root / "qwen-home"
            evidence = root / "evidence"
            evidence.mkdir()
            (evidence / "record.json").write_text("{}", encoding="utf-8")

            worktree.mkdir()
            dirty = prove_teardown(
                worktree=worktree,
                qwen_home=qwen_home,
                evidence=evidence,
                surviving_workers=0,
                surviving_relay_streams=0,
            )
            self.assertFalse(dirty.clean, "a surviving worktree is not clean")

            worktree.rmdir()
            clean = prove_teardown(
                worktree=worktree,
                qwen_home=qwen_home,
                evidence=evidence,
                surviving_workers=0,
                surviving_relay_streams=0,
            )
            self.assertTrue(clean.clean)

    def test_a_surviving_worker_is_not_clean(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            evidence = root / "evidence"
            evidence.mkdir()
            (evidence / "r.json").write_text("{}", encoding="utf-8")
            proof = prove_teardown(
                worktree=root / "gone",
                qwen_home=root / "gone-too",
                evidence=evidence,
                surviving_workers=1,
                surviving_relay_streams=0,
            )
            self.assertFalse(proof.clean)
            self.assertFalse(proof.no_surviving_worker)

    def test_deleted_evidence_is_not_clean(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            proof = prove_teardown(
                worktree=root / "gone",
                qwen_home=root / "gone-too",
                evidence=root / "never-written",
                surviving_workers=0,
                surviving_relay_streams=0,
            )
            self.assertFalse(proof.evidence_retained)
            self.assertFalse(proof.clean)


class VerdictTests(unittest.TestCase):
    def _decide(self, **overrides):
        arguments = {
            "stages": _stages(),
            "arm_slots": _six_slots(),
            "negative_controls": _controls(),
            "relay_stress_passed": True,
            "token_accounting": _accounting(),
            "pair_scores": _pairs(),
        }
        arguments.update(overrides)
        return decide_verdict(**arguments)

    def test_a_complete_rehearsal_authorises_the_live_preflight(self) -> None:
        verdict, reason = self._decide()
        self.assertIs(verdict, RehearsalVerdict.PASS_LIVE_PREFLIGHT_AUTHORIZED)
        self.assertIn("live inference remains unauthorised", reason)

    def test_an_unrun_stage_is_not_measurable_rather_than_a_failure(self) -> None:
        verdict, _ = self._decide(stages=_stages(StageOutcome.UNRUN))
        self.assertIs(verdict, RehearsalVerdict.NOT_MEASURABLE)

    def test_an_inconclusive_stage_is_not_measurable(self) -> None:
        verdict, _ = self._decide(stages=_stages(StageOutcome.INCONCLUSIVE))
        self.assertIs(verdict, RehearsalVerdict.NOT_MEASURABLE)

    def test_a_failed_stage_fails_the_rehearsal(self) -> None:
        verdict, _ = self._decide(stages=_stages(StageOutcome.FAILED))
        self.assertIs(verdict, RehearsalVerdict.FAIL_REHEARSAL)

    def test_a_missing_arm_slot_is_an_absent_result_not_a_pass(self) -> None:
        verdict, reason = self._decide(arm_slots=_six_slots()[:5])
        self.assertIs(verdict, RehearsalVerdict.NOT_MEASURABLE)
        self.assertIn("six", reason)

    def test_a_turn_that_bypassed_the_relay_fails(self) -> None:
        slots = list(_six_slots())
        slots[2] = _slot(
            "crisis-atlas-rep-2", "apoapsis_sandbox", 1,
            provider_requests=2, relay_requests=1,
        )
        verdict, reason = self._decide(arm_slots=tuple(slots))
        self.assertIs(verdict, RehearsalVerdict.FAIL_REHEARSAL)
        self.assertIn("bypassed containment", reason)

    def test_dirty_teardown_fails(self) -> None:
        slots = list(_six_slots())
        slots[0] = _slot(clean=False)
        verdict, reason = self._decide(arm_slots=tuple(slots))
        self.assertIs(verdict, RehearsalVerdict.FAIL_REHEARSAL)
        self.assertIn("teardown", reason)

    def test_evaluator_only_exposure_fails(self) -> None:
        slots = list(_six_slots())
        slots[1] = _slot(
            "crisis-atlas-rep-1", "apoapsis_sandbox", 2, evaluator_only_absent=False
        )
        verdict, reason = self._decide(arm_slots=tuple(slots))
        self.assertIs(verdict, RehearsalVerdict.FAIL_REHEARSAL)
        self.assertIn("evaluator-only", reason)

    def test_a_control_caught_by_the_wrong_detector_is_not_caught(self) -> None:
        controls = list(_controls())
        controls[0] = controls[0].model_copy(
            update={"detector_fired": "some other check that happened to fail"}
        )
        verdict, reason = self._decide(negative_controls=tuple(controls))
        self.assertIs(verdict, RehearsalVerdict.FAIL_REHEARSAL)
        self.assertIn("mapped detector", reason)

    def test_an_unrefused_control_fails(self) -> None:
        controls = list(_controls())
        controls[3] = controls[3].model_copy(update={"refused": False})
        verdict, _ = self._decide(negative_controls=tuple(controls))
        self.assertIs(verdict, RehearsalVerdict.FAIL_REHEARSAL)

    def test_failed_relay_stress_fails_the_rehearsal(self) -> None:
        verdict, reason = self._decide(relay_stress_passed=False)
        self.assertIs(verdict, RehearsalVerdict.FAIL_REHEARSAL)
        self.assertIn("intermittent", reason)

    def test_missing_accounting_is_not_measurable(self) -> None:
        verdict, _ = self._decide(token_accounting=None)
        self.assertIs(verdict, RehearsalVerdict.NOT_MEASURABLE)

    def test_an_incomparable_pair_is_incomparable_not_a_failure(self) -> None:
        pairs = list(_pairs())
        pairs[1] = pairs[1].model_copy(
            update={"comparable": False, "incomparable_reason": "cold state differed"}
        )
        verdict, _ = self._decide(pair_scores=tuple(pairs))
        self.assertIs(verdict, RehearsalVerdict.INCOMPARABLE_CONFIGURATION)


class AccountingTests(unittest.TestCase):
    def test_residual_is_aggregate_minus_exposed(self) -> None:
        self.assertTrue(_accounting().consistent)

    def test_an_aggregate_counted_as_a_call_is_inconsistent(self) -> None:
        self.assertFalse(
            _accounting().model_copy(update={"aggregate_counted_as_call": True}).consistent
        )

    def test_a_wrong_residual_is_inconsistent(self) -> None:
        self.assertFalse(
            _accounting().model_copy(update={"residual_tokens": 0}).consistent
        )

    def test_absent_telemetry_is_unmeasured_not_zero(self) -> None:
        unmeasured = TokenAccounting(unmeasured_reason="the provider reported none")
        self.assertTrue(unmeasured.consistent)
        self.assertIsNone(unmeasured.session_aggregate_tokens)
        # Silence without a reason is not a measurement either.
        self.assertFalse(TokenAccounting().consistent)


class PairScoringTests(unittest.TestCase):
    def test_an_aggregate_can_never_offset_a_pair_regression(self) -> None:
        for pair in _pairs():
            self.assertFalse(pair.aggregate_may_offset_pair_regression)

    def test_a_regressed_pair_is_visible_on_its_own(self) -> None:
        pair = PairScore(
            repetition_id="crisis-atlas-rep-2",
            control_proposal_quality=0.9,
            sandbox_proposal_quality=0.4,
        )
        self.assertTrue(pair.regressed)

    def test_repaired_quality_is_excluded_from_proposal_quality(self) -> None:
        for pair in _pairs():
            self.assertTrue(pair.repaired_quality_excluded_from_proposal)


class EvidenceWriterTests(unittest.TestCase):
    def test_the_digest_covers_every_written_file(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            writer = EvidenceWriter(Path(scratch) / "evidence")
            writer.write_json("slot-1/record.json", {"a": 1})
            before = writer.digest()
            writer.write_json("slot-2/record.json", {"b": 2})
            self.assertNotEqual(writer.digest(), before)


if __name__ == "__main__":
    unittest.main()
