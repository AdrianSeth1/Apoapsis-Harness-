from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apoapsis.workcell.acceptance import CheckpointOutcome
from apoapsis.workcell.product import CapabilitySandboxError, _approved_plan_payload
from apoapsis.workcell.product_live import ProductSupervisor
from apoapsis.workcell.product_live import _base_tree
from tests.architect_helpers import make_plan


class ProductSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "base"
        self.base.mkdir()
        (self.base / "README.md").write_text("fixture\n", encoding="utf-8")
        self.request = {
            "slice_id": "SLICE-1",
            "plan": make_plan().model_dump(mode="json"),
            "verification_commands": [
                {
                    "name": "unit-tests",
                    "argv": ["python", "-m", "unittest", "discover", "-s", "tests", "-v"],
                    "timeout_seconds": 30,
                }
            ],
            "patch_policy": {
                "max_files": 20,
                "max_changed_lines": 500,
                "allow_test_changes": True,
                "allow_dependency_changes": True,
            },
        }

    def _candidate(self, *, include_test: bool = True) -> Path:
        candidate = self.root / ("candidate-good" if include_test else "candidate-gap")
        candidate.mkdir()
        (candidate / "README.md").write_text("fixture\n", encoding="utf-8")
        source = candidate / "src" / "example.py"
        source.parent.mkdir(parents=True)
        source.write_text("def example_function(x: int) -> int:\n    return x + 1\n", encoding="utf-8")
        if include_test:
            test = candidate / "tests" / "test_example.py"
            test.parent.mkdir(parents=True)
            test.write_text(
                "import unittest\nfrom src.example import example_function\n\n"
                "class ExampleTests(unittest.TestCase):\n"
                "    def test_example(self):\n"
                "        self.assertEqual(example_function(1), 2)\n",
                encoding="utf-8",
            )
        return candidate

    def test_complete_candidate_reaches_authoritative_checkpoint(self) -> None:
        supervisor = ProductSupervisor(self.request, self.base, self.root / "evidence")

        record = supervisor.checkpoint(self._candidate(), 0)

        self.assertEqual(record.decision.outcome, CheckpointOutcome.COMPLETE)
        self.assertIsNotNone(supervisor.final_snapshot)
        self.assertTrue((supervisor.final_snapshot / "src" / "example.py").is_file())

    def test_green_inherited_shape_without_exercising_new_file_continues(self) -> None:
        supervisor = ProductSupervisor(self.request, self.base, self.root / "evidence-gap")

        record = supervisor.checkpoint(self._candidate(include_test=False), 0)

        self.assertEqual(record.decision.outcome, CheckpointOutcome.CONTINUE)
        self.assertFalse(record.readiness.ready)
        self.assertIsNone(supervisor.final_snapshot)

    def test_forbidden_candidate_is_refused_as_one_delta(self) -> None:
        candidate = self._candidate()
        (candidate / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        supervisor = ProductSupervisor(self.request, self.base, self.root / "evidence-refused")

        record = supervisor.checkpoint(candidate, 0)

        self.assertEqual(record.decision.outcome, CheckpointOutcome.CANDIDATE_REFUSED)
        self.assertFalse(record.admission.admitted)


class ApprovedPlanPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_hash_bound_embedded_plan_needs_no_version_named_artifact(self) -> None:
        plan = make_plan()
        package = SimpleNamespace(
            approved_plan=plan,
            plan_id="PLAN-EMBEDDED",
            plan_version=5,
        )

        payload = _approved_plan_payload(self.root, package)

        self.assertEqual(payload, plan.model_dump(mode="json"))

    def test_legacy_package_fails_closed_without_exact_artifact(self) -> None:
        package = SimpleNamespace(
            approved_plan=None,
            plan_id="PLAN-LEGACY",
            plan_version=5,
        )

        with self.assertRaisesRegex(CapabilitySandboxError, "plan v5 artifact"):
            _approved_plan_payload(self.root, package)

    def test_launcher_accepts_git_worktree_metadata_files(self) -> None:
        launcher = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "run_capability_sandbox_task.sh"
        ).read_text(encoding="utf-8")

        self.assertIn('SEED_GIT_POINTER=', launcher)
        self.assertIn('wslpath -u "${SEED_GIT_POINTER}"', launcher)
        self.assertIn('RUNTIME="$(mktemp -d /tmp/apx-product-XXXXXXXX)"', launcher)
        self.assertIn('NORMALIZED_SEED="${RUNTIME}/seed"', launcher)
        self.assertIn('git clone --quiet --no-local', launcher)
        self.assertIn('-v "${RUNTIME}:${RUNTIME}:rw"', launcher)
        self.assertIn('--runtime-root "${RUNTIME}/controller"', launcher)
        self.assertNotIn('test -d "${SEED}/.git"', launcher)
        self.assertIn("Capability Sandbox task path is not a Git worktree", launcher)

    def test_controller_git_cleanup_trusts_only_its_exact_disposable_copy(self) -> None:
        seed = self.root / "seed"
        target = self.root / "controller-runtime" / "approved-base"
        (seed / ".git").mkdir(parents=True)
        (seed / "README.md").write_text("seed\n", encoding="utf-8")

        with patch("apoapsis.workcell.product_live.subprocess.run") as run:
            _base_tree(seed, target)

        expected_override = f"safe.directory={target}"
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][0:3], ["git", "-c", expected_override])
            self.assertEqual(call.kwargs["cwd"], target)
        self.assertFalse((target / ".git").exists())


if __name__ == "__main__":
    unittest.main()
