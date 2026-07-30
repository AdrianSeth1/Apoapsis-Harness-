from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apoapsis.workcell.admission import (
    AdmissionFinding,
    AdmissionPolicy,
    AdmissionRefused,
    admit_candidate,
    evaluate_admission,
    reconstruct_candidate,
    repair_packet,
    require_admitted,
)
from apoapsis.workcell.delta import (
    ChangeKind,
    PathClass,
    classify_path,
    compute_delta,
    tree_fingerprint,
)

_BASE = {
    "calc.py": "def add(a, b):\n    return a + b\n",
    "run_tests.py": "from calc import add\n\nassert add(2, 3) == 5\nprint('OK')\n",
    "README.md": "# Calc\n",
}


class _Trees:
    """A base tree and a candidate tree, on disk, in a temp directory."""

    def __init__(self, stack: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        stack.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.base = self.root / "base"
        self.candidate = self.root / "candidate"
        for target in (self.base, self.candidate):
            target.mkdir()
            for name, body in _BASE.items():
                (target / name).write_text(body, encoding="utf-8")

    def write(self, relative: str, body: str) -> None:
        path = self.candidate / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def delete(self, relative: str) -> None:
        (self.candidate / relative).unlink()

    def delta(self):
        return compute_delta(self.base, self.candidate, base_commit="a" * 40)


class PathClassificationTests(unittest.TestCase):
    def test_ordinary_source_is_production(self) -> None:
        self.assertEqual(classify_path("calc.py"), PathClass.PRODUCTION)
        self.assertEqual(classify_path("api/server.py"), PathClass.PRODUCTION)

    def test_tests_are_recognised_by_directory_and_by_name(self) -> None:
        for path in ("tests/test_calc.py", "test_calc.py", "calc_test.py", "a.spec.js"):
            self.assertEqual(classify_path(path), PathClass.TEST, path)

    def test_dependency_manifests_are_recognised(self) -> None:
        for path in ("requirements.txt", "pyproject.toml", "package-lock.json"):
            self.assertEqual(classify_path(path), PathClass.DEPENDENCY, path)

    def test_generated_artifacts_are_recognised(self) -> None:
        for path in ("dist/app.js", "calc.pyc", "build/x.o", "app.log"):
            self.assertEqual(classify_path(path), PathClass.GENERATED, path)

    def test_forbidden_beats_every_other_class(self) -> None:
        # A credential inside a directory called tests/ is still a credential.
        for path in (
            ".git/config", ".apoapsis/state.json", "task/task.md", ".env",
            "tests/.env", "tests/id_rsa", "config/server.pem", ".ssh/known_hosts",
        ):
            self.assertEqual(classify_path(path), PathClass.FORBIDDEN, path)


class DeltaTests(unittest.TestCase):
    def test_an_untouched_tree_has_an_empty_delta(self) -> None:
        trees = _Trees(self)
        self.assertTrue(trees.delta().is_empty)

    def test_multi_file_work_is_captured_without_any_envelope(self) -> None:
        # The Slice 3 exit criterion: the agent edited files normally, and the
        # controller assembles them into one candidate.
        trees = _Trees(self)
        trees.write("calc.py", "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n")
        trees.write("run_tests.py", "from calc import add, subtract\n\nassert add(2, 3) == 5\nassert subtract(5, 3) == 2\nprint('OK')\n")
        delta = trees.delta()
        self.assertEqual(sorted(delta.paths), ["calc.py", "run_tests.py"])
        self.assertTrue(all(item.kind == ChangeKind.MODIFIED for item in delta.entries))
        self.assertGreater(delta.changed_lines, 0)

    def test_additions_and_deletions_are_distinguished(self) -> None:
        trees = _Trees(self)
        trees.write("extra.py", "x = 1\n")
        trees.delete("README.md")
        delta = trees.delta()
        kinds = {item.path: item.kind for item in delta.entries}
        self.assertEqual(kinds["extra.py"], ChangeKind.ADDED)
        self.assertEqual(kinds["README.md"], ChangeKind.DELETED)

    def test_the_workcells_git_is_never_read(self) -> None:
        # The agent may commit, amend, or delete .git in a sacrificial clone.
        # None of it may influence the admitted delta.
        trees = _Trees(self)
        (trees.candidate / ".git").mkdir()
        (trees.candidate / ".git" / "HEAD").write_text("ref: refs/heads/lies\n", "utf-8")
        (trees.candidate / ".git" / "COMMIT_EDITMSG").write_text("x" * 5000, "utf-8")
        self.assertTrue(trees.delta().is_empty)

    def test_symlinks_are_skipped_and_reported_not_followed(self) -> None:
        trees = _Trees(self)
        link = trees.candidate / "escape.py"
        try:
            link.symlink_to(trees.root / "base" / "calc.py")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not creatable in this environment")
        delta = trees.delta()
        self.assertIn("escape.py", delta.skipped_non_regular)
        self.assertNotIn("escape.py", delta.paths)

    def test_binary_content_reports_no_line_counts(self) -> None:
        trees = _Trees(self)
        (trees.candidate / "blob.bin").write_bytes(b"\0\1\2" * 1000)
        entry = next(item for item in trees.delta().entries if item.path == "blob.bin")
        self.assertTrue(entry.binary)
        self.assertEqual(entry.changed_lines, 0)
        self.assertGreater(entry.size_bytes, 0)

    def test_the_fingerprint_changes_with_content_and_not_with_walk_order(self) -> None:
        trees = _Trees(self)
        first = tree_fingerprint(trees.candidate)
        self.assertEqual(first, tree_fingerprint(trees.candidate))
        trees.write("calc.py", "def add(a, b):\n    return b + a\n")
        self.assertNotEqual(first, tree_fingerprint(trees.candidate))


class AdmissionTests(unittest.TestCase):
    def _good(self) -> _Trees:
        trees = _Trees(self)
        trees.write("calc.py", "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n")
        trees.write("tests/test_calc.py", "from calc import subtract\n\nassert subtract(5, 3) == 2\n")
        return trees

    def test_valid_multi_file_work_is_admitted(self) -> None:
        trees = self._good()
        decision = evaluate_admission(trees.delta())
        self.assertTrue(decision.admitted, decision.detail)
        self.assertEqual(decision.file_count, 2)
        self.assertEqual(decision.counts_by_class["production"], 1)
        self.assertEqual(decision.counts_by_class["test"], 1)

    def test_one_forbidden_path_refuses_the_whole_candidate(self) -> None:
        # The Slice 3 exit criterion. The other, legitimate files are refused
        # along with it: admission is atomic.
        trees = self._good()
        trees.write(".apoapsis/state.json", "{}\n")
        decision = evaluate_admission(trees.delta())
        self.assertFalse(decision.admitted)
        self.assertIn(
            AdmissionFinding.FORBIDDEN_PATH,
            {item.finding for item in decision.violations},
        )

    def test_every_violation_is_reported_at_once(self) -> None:
        # A repair context that is told about one problem per call burns a
        # model call per finding.
        trees = self._good()
        trees.write(".env", "SECRET=1\n")
        trees.write("id_rsa", "key\n")
        trees.write("dist/bundle.js", "x\n")
        decision = evaluate_admission(trees.delta())
        findings = [item.finding for item in decision.violations]
        self.assertGreaterEqual(findings.count(AdmissionFinding.FORBIDDEN_PATH), 2)
        self.assertIn(AdmissionFinding.GENERATED_ARTIFACT_PRESENT, findings)
        packet = repair_packet(decision)
        self.assertIn(".env", packet)
        self.assertIn("id_rsa", packet)
        self.assertIn("dist/bundle.js", packet)

    def test_size_ceilings_apply_to_the_whole_delta(self) -> None:
        trees = self._good()
        decision = evaluate_admission(trees.delta(), AdmissionPolicy(max_files=1))
        self.assertIn(
            AdmissionFinding.TOO_MANY_FILES,
            {item.finding for item in decision.violations},
        )
        decision = evaluate_admission(
            trees.delta(), AdmissionPolicy(max_changed_lines=1)
        )
        self.assertIn(
            AdmissionFinding.TOO_MANY_CHANGED_LINES,
            {item.finding for item in decision.violations},
        )

    def test_protected_classes_are_honoured(self) -> None:
        trees = self._good()
        decision = evaluate_admission(
            trees.delta(), AdmissionPolicy(allow_test_changes=False)
        )
        self.assertIn(
            AdmissionFinding.TEST_CHANGE_NOT_PERMITTED,
            {item.finding for item in decision.violations},
        )
        trees.write("requirements.txt", "requests\n")
        decision = evaluate_admission(
            trees.delta(), AdmissionPolicy(allow_dependency_changes=False)
        )
        self.assertIn(
            AdmissionFinding.DEPENDENCY_CHANGE_NOT_PERMITTED,
            {item.finding for item in decision.violations},
        )

    def test_deletions_can_be_refused(self) -> None:
        trees = self._good()
        trees.delete("README.md")
        decision = evaluate_admission(
            trees.delta(), AdmissionPolicy(allow_deletions=False)
        )
        self.assertIn(
            AdmissionFinding.DELETION_NOT_PERMITTED,
            {item.finding for item in decision.violations},
        )

    def test_an_empty_delta_cannot_be_promoted(self) -> None:
        trees = _Trees(self)
        decision = evaluate_admission(trees.delta())
        self.assertFalse(decision.admitted)
        self.assertIn(
            AdmissionFinding.EMPTY_DELTA,
            {item.finding for item in decision.violations},
        )

    def test_a_tree_that_moved_after_freezing_is_refused(self) -> None:
        trees = self._good()
        delta = trees.delta()
        decision = evaluate_admission(delta, expected_fingerprint="f" * 64)
        self.assertIn(
            AdmissionFinding.FINGERPRINT_MISMATCH,
            {item.finding for item in decision.violations},
        )


class ReconstructionTests(unittest.TestCase):
    def _good(self) -> _Trees:
        trees = _Trees(self)
        trees.write("calc.py", "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n")
        trees.write("tests/test_calc.py", "from calc import subtract\n")
        return trees

    def test_reconstruction_reproduces_the_candidate_exactly(self) -> None:
        trees = self._good()
        delta = trees.delta()
        target = trees.root / "verifier"
        fingerprint = reconstruct_candidate(trees.base, trees.candidate, delta, target)
        self.assertEqual(fingerprint, delta.candidate_fingerprint)
        self.assertIn("def subtract", (target / "calc.py").read_text(encoding="utf-8"))
        # Untouched base files come through unchanged.
        self.assertEqual(
            (target / "README.md").read_text(encoding="utf-8"), _BASE["README.md"]
        )

    def test_the_verifier_never_inherits_what_the_delta_excluded(self) -> None:
        # The verifier tree is built from base + delta, never copied from the
        # workcell, so a stray artifact cannot travel with the candidate.
        trees = self._good()
        delta = trees.delta()
        (trees.candidate / "leftover.log").write_text("noise\n", encoding="utf-8")
        (trees.candidate / ".git").mkdir(exist_ok=True)
        (trees.candidate / ".git" / "HEAD").write_text("x\n", encoding="utf-8")
        target = trees.root / "verifier"
        reconstruct_candidate(trees.base, trees.candidate, delta, target)
        self.assertFalse((target / "leftover.log").exists())
        self.assertFalse((target / ".git").exists())

    def test_a_deletion_is_reproduced(self) -> None:
        trees = _Trees(self)
        trees.delete("README.md")
        delta = trees.delta()
        target = trees.root / "verifier"
        reconstruct_candidate(trees.base, trees.candidate, delta, target)
        self.assertFalse((target / "README.md").exists())

    def test_a_refused_candidate_is_never_materialised(self) -> None:
        trees = self._good()
        trees.write(".apoapsis/state.json", "{}\n")
        snapshot = trees.root / "snapshot"
        decision = admit_candidate(
            trees.base, trees.candidate, trees.delta(), snapshot_root=snapshot
        )
        self.assertFalse(decision.admitted)
        self.assertIsNone(decision.snapshot_path)
        self.assertFalse(snapshot.exists())

    def test_an_admitted_candidate_is_materialised_and_the_base_is_untouched(
        self,
    ) -> None:
        trees = self._good()
        base_before = tree_fingerprint(trees.base)
        snapshot = trees.root / "snapshot"
        decision = admit_candidate(
            trees.base, trees.candidate, trees.delta(), snapshot_root=snapshot
        )
        self.assertTrue(decision.admitted, decision.detail)
        self.assertEqual(decision.snapshot_path, str(snapshot))
        self.assertTrue((snapshot / "calc.py").exists())
        # The Slice 3 exit criterion: the original source tree is unchanged.
        self.assertEqual(tree_fingerprint(trees.base), base_before)

    def test_the_raising_form_stops_a_refused_candidate(self) -> None:
        trees = self._good()
        trees.write(".env", "SECRET=1\n")
        decision = evaluate_admission(trees.delta())
        with self.assertRaises(AdmissionRefused):
            require_admitted(decision)


if __name__ == "__main__":
    unittest.main()


class Slice3GateEnforcementTests(unittest.TestCase):
    """Admission is the first thing Slice 3 does, so the gate bites here."""

    def _trees(self) -> _Trees:
        trees = _Trees(self)
        trees.write("calc.py", "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n")
        return trees

    def _spike(self, *, preserved: bool):
        from apoapsis.workcell.agent_profile import ProfileGateResult
        from apoapsis.workcell.capability_readiness import (
            CapabilityReadinessReport,
            ReadinessOperation,
            ReadinessOperationResult,
            ReadinessStatus,
        )
        from apoapsis.workcell.conformance import ConformanceCheck, CheckResult, ConformanceStatus, evaluate_conformance
        from apoapsis.workcell.containment import (
            DEFAULT_CONTAINMENT_PROBES,
            ProbeResult,
            ProbeStatus,
            evaluate_containment,
        )
        from apoapsis.workcell.spike import CapabilitySpikeReport, SpikeVerdict
        from tests.test_workcell import _pin, _run_record

        sha = "a" * 64
        return CapabilitySpikeReport(
            workcell_manifest_digest=sha,
            pin=_pin(),
            run=_run_record(),
            containment=evaluate_containment(
                [
                    ProbeResult(probe_id=probe.probe_id, status=ProbeStatus.CONTAINED)
                    for probe in DEFAULT_CONTAINMENT_PROBES
                ],
                workcell_manifest_digest=sha,
            ),
            conformance=evaluate_conformance(
                [
                    CheckResult(check=check, status=ConformanceStatus.PASSED)
                    for check in ConformanceCheck
                ],
                workcell_manifest_digest=sha,
            ),
            agent_profile=ProfileGateResult(ok=True, detail="coding profile"),
            capability_readiness=CapabilityReadinessReport(
                results=[
                    ReadinessOperationResult(
                        operation=operation, status=ReadinessStatus.PASSED
                    )
                    for operation in ReadinessOperation
                ],
                ready=True,
                residue_free=True,
                detail="exercised",
            ),
            verdict=(
                SpikeVerdict.CAPABILITY_PRESERVED
                if preserved
                else SpikeVerdict.NOT_MEASURABLE
            ),
            detail="fixture",
        )

    def test_admission_runs_when_slice2_is_preserved(self) -> None:
        trees = self._trees()
        decision = admit_candidate(
            trees.base,
            trees.candidate,
            trees.delta(),
            snapshot_root=trees.root / "snapshot",
            slice2_spike=self._spike(preserved=True),
        )
        self.assertTrue(decision.admitted, decision.detail)

    def test_admission_refuses_to_start_without_slice2(self) -> None:
        from apoapsis.workcell.gate import Slice3Blocked

        trees = self._trees()
        with self.assertRaises(Slice3Blocked):
            admit_candidate(
                trees.base,
                trees.candidate,
                trees.delta(),
                snapshot_root=trees.root / "snapshot",
                slice2_spike=self._spike(preserved=False),
            )
        self.assertFalse((trees.root / "snapshot").exists())
