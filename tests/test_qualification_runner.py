"""The two R3 runner defects that no test reached, and the guards against them.

R3 was bound as the runner authority on the strength of tests that drove its
*helpers*. Neither `stage_7_accounting` nor `manifest_filename` was ever
called by a test, so both shipped wrong while the suite stayed green:

* `stage_7_accounting` looked up the control arm as `"qwen_default_control"`.
  The manifest, `ArmKind` and `scheduled_slots` all emit
  `"default_qwen_control"`, so every pair returned `comparable=False` and the
  rehearsal could only ever reach `INCOMPARABLE_CONFIGURATION`. The failure
  message even listed both arms it said were missing.
* `manifest_filename` mapped schema `"2.0"` to the literal v2 filename. Every
  manifest from v2 onward is schema 2.0, so under manifest v3 the
  changed-server-argument control would have refused the superseded v2
  document -- the right detector firing against the wrong bytes.

Nothing here starts a container, opens a socket or calls a model.
"""

from __future__ import annotations

import inspect
import shutil
import tempfile
import unittest
from pathlib import Path

from apoapsis.qualification import runner as runner_module
from apoapsis.qualification.fake_pilot_provider import ScriptId
from apoapsis.qualification.pilot import PilotLock, PilotManifest
from apoapsis.qualification.rehearsal import (
    REQUIRED_DETECTORS,
    ArmSlotResult,
    EvidenceWriter,
    NegativeControl,
    RehearsalVerdict,
    StageOutcome,
    StageResult,
    TeardownProof,
    decide_verdict,
    scheduled_slots,
)
from apoapsis.qualification.runner import (
    SHAPE_BY_REPETITION,
    manifest_document,
    stage_6_negative_controls,
    stage_7_accounting,
)

QUALIFICATION_DIR_MARKER = "docs/qualification"

REPO = Path(__file__).resolve().parents[1]
QUALIFICATION = REPO / "docs" / "qualification"


def _manifest() -> PilotManifest:
    """The newest committed manifest, so the arm names come from the artifact.

    Deliberately not a literal list typed into this test. The defect was a
    disagreement between the runner and the frozen document; a test that
    invented its own arm names could reproduce the disagreement.
    """

    for name in (
        "slice7-crisis-atlas-pilot-manifest-v3.json",
        "slice7-crisis-atlas-pilot-manifest-v2.json",
        "slice7-crisis-atlas-pilot-manifest.json",
    ):
        path = QUALIFICATION / name
        if path.exists():
            return PilotManifest.model_validate_json(path.read_text(encoding="utf-8"))
    raise AssertionError(  # pragma: no cover - the pilot cannot exist without one
        "no pilot manifest is committed"
    )


def _teardown() -> TeardownProof:
    return TeardownProof(
        worktree_removed=True,
        qwen_home_removed=True,
        evidence_retained=True,
        no_surviving_worker=True,
        no_surviving_relay_stream=True,
        next_slot_cannot_reach_previous=True,
    )


def _slots_from_frozen_schedule(manifest: PilotManifest) -> tuple[ArmSlotResult, ...]:
    """Six slots whose arm names are read out of the manifest itself."""

    slots: list[ArmSlotResult] = []
    for repetition, arm, order in scheduled_slots(manifest):
        script = SHAPE_BY_REPETITION[repetition]
        incomplete = script is ScriptId.INCOMPLETE_PROPOSAL
        slots.append(
            ArmSlotResult(
                repetition_id=repetition,
                arm=arm,
                order_within_repetition=order,
                script=script,
                seed_commit_verified=True,
                task_bytes_verified=True,
                arm_visible_mounts_verified=True,
                evaluator_only_absent=True,
                provider_requests=1,
                relay_observed_requests=1,
                checkpoint_outcome="CONTINUE" if incomplete else "COMPLETE",
                readiness_blocks=("missing-artifact",) if incomplete else (),
                satisfied_criteria=("criterion-a", "criterion-b"),
                teardown=_teardown(),
                evidence_path=f"/evidence/{repetition}/{arm}",
            )
        )
    return tuple(slots)


class PairScoringUsesTheScheduledArmNames(unittest.TestCase):
    """The regression that made `PASS_LIVE_PREFLIGHT_AUTHORIZED` unreachable."""

    def setUp(self) -> None:
        self.manifest = _manifest()
        self.slots = _slots_from_frozen_schedule(self.manifest)
        self.writer = EvidenceWriter(Path(tempfile.mkdtemp(prefix="runner-test-")))

    def test_every_pair_is_comparable_and_populated(self) -> None:
        _, _, pairs = stage_7_accounting(self.slots, writer=self.writer)

        self.assertEqual(len(pairs), 3)
        for pair in pairs:
            with self.subTest(repetition=pair.repetition_id):
                self.assertTrue(pair.comparable, pair.incomparable_reason)
                self.assertIsNone(pair.incomparable_reason)
                # `regressed` is False when a score is None, so an unpopulated
                # pair reads exactly like a pair that did not regress. That is
                # why absence is asserted against here rather than trusted.
                self.assertIsNotNone(pair.control_proposal_quality)
                self.assertIsNotNone(pair.sandbox_proposal_quality)
                self.assertIsNotNone(pair.sandbox_detection_quality)

    def test_the_verdict_these_pairs_support_is_pass(self) -> None:
        _, accounting, pairs = stage_7_accounting(self.slots, writer=self.writer)

        verdict, reason = decide_verdict(
            stages=(StageResult(stage="s", outcome=StageOutcome.PASSED, detail="d"),),
            arm_slots=self.slots,
            negative_controls=(),
            relay_stress_passed=True,
            token_accounting=accounting,
            pair_scores=pairs,
        )

        self.assertIs(verdict, RehearsalVerdict.PASS_LIVE_PREFLIGHT_AUTHORIZED, reason)

    def test_the_control_arm_key_is_not_a_literal(self) -> None:
        """The two names must be unable to drift apart again."""

        # Comments are excluded on purpose: the comment above the lookup names
        # the wrong key deliberately, to record what the defect was. What must
        # not contain it is the executed code.
        source = inspect.getsource(stage_7_accounting)
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("qwen_default_control", code)
        self.assertIn("ArmKind.DEFAULT_QWEN_CONTROL", code)


class ControlsReadTheManifestUnderRehearsal(unittest.TestCase):
    """Control 3 must mutate the bytes actually being rehearsed."""

    def test_the_document_is_the_path_the_caller_named(self) -> None:
        path = Path("/somewhere/slice7-crisis-atlas-pilot-manifest-v3.json")
        self.assertEqual(manifest_document(path), path)

    def test_no_manifest_filename_is_inferred_from_a_schema_version(self) -> None:
        source = Path(runner_module.__file__).read_text(encoding="utf-8")
        offenders = [
            line
            for line in source.splitlines()
            if "slice7-crisis-atlas-pilot-manifest" in line
        ]
        self.assertEqual(offenders, [], "the runner names a manifest file by hand")

    def test_stage_6_requires_the_manifest_path(self) -> None:
        signature = inspect.signature(stage_6_negative_controls)
        parameter = signature.parameters.get("manifest_path")
        self.assertIsNotNone(parameter, "stage 6 does not take a manifest path")
        self.assertIs(parameter.default, inspect.Parameter.empty)


class NegativeControlsAreExecutedNotDescribedTests(unittest.TestCase):
    """Stage 6 must actually run. Reading it is not enough.

    R3 and R4 shipped a control 14 -- orchestration-only evidence offered as
    qualification evidence, the control this pilot is named for -- that raised
    `TypeError` the first time it was ever executed: it passed a resolved
    package where a package root was wanted, passed the probe class instead of
    an instance, and omitted `workspace` entirely. No test called
    `stage_6_negative_controls`, so three signature errors in the same call sat
    behind a stage that read as implemented.

    This test needs the Crisis Atlas seed to clone, which is an evaluation
    fixture rather than repository content, so it skips rather than fails when
    the seed is not present -- and says so, so a skip is not read as a pass.
    """

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is required to clone the seed")
        self.seed = self._seed()
        if self.seed is None:
            self.skipTest(
                "the Crisis Atlas seed is not present; stage 6 clones it and "
                "cannot substitute anything for it"
            )
        self.manifest = _manifest()
        lock_path = QUALIFICATION / "slice7-crisis-atlas-pilot-lock-v3.json"
        if not lock_path.exists():
            self.skipTest("no lock yet; this is the manifest commit")
        self.lock = PilotLock.model_validate_json(
            lock_path.read_text(encoding="utf-8")
        )
        self.manifest_path = (
            QUALIFICATION / "slice7-crisis-atlas-pilot-manifest-v3.json"
        )

    @staticmethod
    def _seed():
        for candidate in (
            REPO / ".apoapsis-eval" / "slice-e-crisis-atlas-seed-2026-07-29",
            Path("/root/crisis-atlas-seed"),
        ):
            if (candidate / ".git").is_dir():
                return candidate
        return None

    def test_control_fourteen_is_injected_and_caught(self) -> None:
        writer = EvidenceWriter(Path(tempfile.mkdtemp(prefix="stage-6-")))
        stage, controls = stage_6_negative_controls(
            self.manifest,
            self.lock,
            repo=REPO,
            writer=writer,
            manifest_path=self.manifest_path,
            seed_repository=self.seed,
        )

        self.assertIs(stage.outcome, StageOutcome.PASSED, stage.detail)
        self.assertEqual(len(controls), len(REQUIRED_DETECTORS))
        by_control = {item.control: item for item in controls}
        fake = by_control[NegativeControl.FAKE_EVIDENCE_AS_REAL_QUALIFICATION]
        self.assertTrue(fake.refused)
        self.assertEqual(fake.detector_fired, "CasePackageValidation.registerable")
        for item in controls:
            with self.subTest(control=str(item.control)):
                self.assertTrue(item.correctly_detected, item.model_dump(mode="json"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
