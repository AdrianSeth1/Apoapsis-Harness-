"""The real probe, exercised against the real seed.

Separate module from `test_qualification_case_package.py` on purpose. That one
runs entirely on a fake probe and reports orchestration coverage; this one
makes clones and runs commands and reports qualification evidence. Keeping
them in one file is how the two got conflated in the first place.

Every test here skips rather than fails when the Crisis Atlas seed or `git` is
absent, because a host without the evaluation fixture has nothing to say about
the package -- which is precisely the `UNRUN` distinction the proof states
encode, applied one level up.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from apoapsis.qualification.case_package import (
    EvidenceKind,
    PackageStatus,
    ProofId,
    ProofState,
    validate_case_package,
)
from apoapsis.qualification.real_probe import (
    VOLATILE_EVIDENCE_FIELDS,
    RealCasePackageProbe,
)

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "docs" / "qualification" / "pilot" / "crisis-atlas"
SEED = REPO / ".apoapsis-eval" / "slice-e-crisis-atlas-seed-2026-07-29"

SEED_COMMIT = "197b3610e5720cf36718c548fa19c05fe784a978"
SEED_TREE = "02fb45efeb4e19c619e3f730bd05a1f70bef9f13"


def _requirements_met() -> str | None:
    if shutil.which("git") is None:
        return "git is not available on this host"
    if not (SEED / ".git").is_dir():
        return f"the Crisis Atlas seed is not present at {SEED}"
    return None


class RealProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reason = _requirements_met()
        if reason:
            raise unittest.SkipTest(reason)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.probe = RealCasePackageProbe(
            seed_repository=SEED,
            package_root=PACKAGE,
            evidence_root=self.workspace / "evidence",
        )

    def test_the_probe_declares_real_qualification_evidence(self) -> None:
        self.assertIs(
            self.probe.evidence_kind, EvidenceKind.REAL_QUALIFICATION
        )

    def test_a_real_clone_reproduces_the_declared_commit_and_tree(self) -> None:
        observed = self.probe.clone_seed(destination=self.workspace / "clone")
        self.assertEqual(observed.commit.object_id, SEED_COMMIT)
        self.assertEqual(observed.commit.object_type, "commit")
        self.assertEqual(observed.tree.object_id, SEED_TREE)
        self.assertEqual(observed.tree.object_type, "tree")
        self.assertTrue(observed.working_tree_clean)

    def test_the_real_inherited_suite_passes_and_reaches_no_service(self) -> None:
        """The discriminating property, measured rather than asserted."""

        destination = self.workspace / "clone"
        self.probe.clone_seed(destination=destination)
        outcome = self.probe.run_inherited_suite(destination=destination)

        self.assertEqual(outcome.exit_code, 0)
        self.assertIn("tests/test_smoke.py", outcome.covered_paths)
        for path in outcome.covered_paths:
            self.assertFalse(
                path.startswith("crisis_atlas/services/"),
                f"the inherited suite reached {path}, so it is not the "
                "false-green shape this case depends on",
            )

    def test_the_real_reference_candidate_completes(self) -> None:
        destination = self.workspace / "clone"
        self.probe.clone_seed(destination=destination)
        observation = self.probe.run_checkpoint(
            destination=destination, candidate="reference"
        )
        self.assertEqual(observation.outcome, "COMPLETE", observation.repair_packet)
        self.assertIn("AC-INCIDENT-SERVICE", observation.satisfied_criteria)
        self.assertIn("AC-EXPORT-SERVICE", observation.satisfied_criteria)
        self.assertFalse(observation.emitter_failed)

    def test_the_real_historical_candidate_is_refused_while_a_command_passes(
        self,
    ) -> None:
        destination = self.workspace / "clone"
        self.probe.clone_seed(destination=destination)
        observation = self.probe.run_checkpoint(
            destination=destination, candidate="incomplete"
        )
        self.assertNotEqual(observation.outcome, "COMPLETE")
        self.assertIn("MISSING_REQUIRED_ARTIFACT", observation.readiness_blocks)
        self.assertIn("CHANGED_BEHAVIOUR_UNEXERCISED", observation.readiness_blocks)
        # The refusal has to coexist with a green command, or it does not
        # reproduce the condition the historical arm completed under.
        self.assertTrue(observation.commands)
        self.assertTrue(all(item.exit_code == 0 for item in observation.commands))

    def test_a_real_witness_is_bound_to_the_admitted_snapshot(self) -> None:
        destination = self.workspace / "clone"
        self.probe.clone_seed(destination=destination)
        observation = self.probe.run_checkpoint(
            destination=destination, candidate="reference"
        )
        fingerprints = {item.worktree_fingerprint for item in observation.commands}
        self.assertEqual(len(fingerprints), 1)
        self.assertRegex(next(iter(fingerprints)), r"^[0-9a-f]{64}$")

    def test_raw_evidence_is_persisted_outside_the_checkpoint(self) -> None:
        destination = self.workspace / "clone"
        self.probe.clone_seed(destination=destination)
        self.probe.run_inherited_suite(destination=destination)
        self.probe.run_checkpoint(destination=destination, candidate="reference")

        root = self.workspace / "evidence"
        self.assertTrue((root / "inherited-suite.json").is_file())
        self.assertTrue((root / "inherited-coverage.json").is_file())
        records = sorted(root.glob("checkpoint-*/checkpoint-record.json"))
        self.assertTrue(records)
        payload = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertIn("readiness", payload)


class RealQualificationTests(unittest.TestCase):
    """The whole eight-proof run, for real. Slow by nature: it clones."""

    @classmethod
    def setUpClass(cls) -> None:
        reason = _requirements_met()
        if reason:
            raise unittest.SkipTest(reason)

    def test_real_qualification_registers_the_package(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            workspace = Path(scratch)
            probe = RealCasePackageProbe(
                seed_repository=SEED,
                package_root=PACKAGE,
                evidence_root=workspace / "evidence",
            )
            validation = validate_case_package(
                PACKAGE,
                probe=probe,
                workspace=workspace / "clones",
                volatile_evidence_fields=VOLATILE_EVIDENCE_FIELDS,
            )

        self.assertIs(validation.evidence_kind, EvidenceKind.REAL_QUALIFICATION)
        self.assertEqual(
            validation.summary(),
            {str(proof): "passed" for proof in ProofId},
            validation.why_not_registerable(),
        )
        self.assertTrue(validation.registerable)
        self.assertIs(validation.status, PackageStatus.REGISTERABLE)

    def test_the_incomplete_candidate_proof_names_both_block_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            workspace = Path(scratch)
            probe = RealCasePackageProbe(
                seed_repository=SEED,
                package_root=PACKAGE,
                evidence_root=workspace / "evidence",
            )
            validation = validate_case_package(
                PACKAGE,
                probe=probe,
                workspace=workspace / "clones",
                volatile_evidence_fields=VOLATILE_EVIDENCE_FIELDS,
            )
        result = validation.result(ProofId.INCOMPLETE_CANDIDATE_CANNOT_COMPLETE)
        self.assertIs(result.state, ProofState.PASSED)
        self.assertEqual(result.evidence["outcome"], "CONTINUE")
        self.assertIn("unit-tests", result.evidence["passing_commands"])


if __name__ == "__main__":
    unittest.main()
