from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.workcell.acceptance import CheckpointOutcome
from apoapsis.workcell.product import (
    CapabilitySandboxError,
    _approved_plan_payload,
    _model_usage,
    _slice_contributions,
)
from apoapsis.workcell.product_live import ProductSupervisor
from apoapsis.workcell.product_live import (
    CHARS_PER_TOKEN_ESTIMATE,
    MAX_JUDGEMENT_CONTRACT_TOKENS,
    _base_tree,
    _judgement_contract,
    _task_text,
)
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


class JudgementContractTests(unittest.TestCase):
    """The model is told how completion is decided, rather than inferring it.

    CAP-4EE9F101146E4556's stream log shows the agent inventing output-marker
    schemes to satisfy a proof mechanism nobody had described to it. These
    assert the description exists, says the mechanical things, and stays small.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "base"
        self.base.mkdir()
        (self.base / "README.md").write_text("fixture\n", encoding="utf-8")
        plan = make_plan().model_dump(mode="json")
        self.request = {
            "slice_id": "SLICE-1",
            "plan": plan,
            "task": {
                "task_id": "TASK-1",
                "hard_constraints": [],
                "acceptance_criteria": [{"id": "AC-1", "text": "it works"}],
            },
            "independent_verification": {"platform": "Windows", "backend": "local"},
            "verification_commands": [
                {
                    "name": "unit-tests",
                    "argv": ["python", "-m", "unittest", "discover", "-s", "tests"],
                    "timeout_seconds": 30,
                }
            ],
            "patch_policy": {
                "max_files": 20,
                "max_changed_lines": 500,
                "allow_test_changes": True,
                "allow_dependency_changes": False,
            },
        }

    def test_the_contract_states_the_proof_mechanics(self) -> None:
        text = _judgement_contract(self.request)
        # What is actually measured, in the model's terms.
        self.assertIn("which lines of your code execute", text)
        self.assertIn("Inherited tests passing is no evidence", text)
        # What not to waste turns on.
        self.assertIn("Do not print markers", text)
        # The limits it is judged against, from the approved policy.
        self.assertIn("at most 20 changed files", text)
        self.assertIn("at most 500 changed lines", text)
        self.assertIn("dependency manifests must not change", text)
        self.assertIn("python -m unittest discover -s tests", text)

    def test_the_contract_speaks_no_internal_vocabulary(self) -> None:
        lowered = _judgement_contract(self.request).lower()
        for word in ("witness", "obligation", "behaviour unit", "readiness", "capsule"):
            self.assertNotIn(word, lowered, f"{word!r} is our noun, not the model's")

    def test_the_contract_stays_within_its_token_budget(self) -> None:
        estimate = len(_judgement_contract(self.request)) // CHARS_PER_TOKEN_ESTIMATE
        self.assertLessEqual(estimate, MAX_JUDGEMENT_CONTRACT_TOKENS)

    def test_the_initial_task_carries_the_contract(self) -> None:
        self.assertIn(_judgement_contract(self.request), _task_text(self.request))

    def test_the_orientation_brief_reaches_the_slice_brief(self) -> None:
        """The controller passes it through; it does not rebuild it.

        The brief is built on the host, where the earlier slices' reports and
        checkpoint records actually live -- the controller sees only this
        request. Passing it through verbatim is what keeps the one that was
        built and the one that was sent identical.
        """

        request = dict(self.request)
        request["orientation"] = (
            "Inherited state - read before exploring\n\nbackend/app.py - 40 lines\n\n"
        )
        text = _task_text(request)
        self.assertIn("Inherited state - read before exploring", text)
        self.assertIn("backend/app.py - 40 lines", text)
        # Before the judgement contract and the instruction to implement:
        # what exists is what a fresh session would otherwise go looking for.
        self.assertLess(
            text.index("Inherited state"), text.index("How this slice is judged")
        )

    def test_a_request_without_a_brief_still_builds_a_slice_brief(self) -> None:
        # Slice 1 of a new project inherits nothing, and an older controller
        # request carries no `orientation` key at all.
        self.assertNotIn("Inherited state", _task_text(self.request))

    def test_every_repair_packet_carries_the_same_contract(self) -> None:
        supervisor = ProductSupervisor(self.request, self.base, self.root / "evidence")
        candidate = self.root / "candidate"
        candidate.mkdir()
        (candidate / "README.md").write_text("fixture\n", encoding="utf-8")
        source = candidate / "src" / "example.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            "def example_function(x: int) -> int:\n    return x + 1\n", encoding="utf-8"
        )

        record, continuation = supervisor(candidate, 0)

        self.assertEqual(record.decision.outcome, CheckpointOutcome.CONTINUE)
        self.assertIsNotNone(continuation)
        # One constant, so the task and the repair packet cannot drift into
        # describing two different proof mechanisms.
        self.assertIn(_judgement_contract(self.request), continuation)
        self.assertIn(record.decision.repair_packet, continuation)

class SandboxModelUsageTests(unittest.TestCase):
    """Reading the controller's observed usage out of one slice result."""

    def test_reported_usage_is_carried_verbatim(self) -> None:
        usage = _model_usage(
            {
                "relay_requests": 46,
                "model_usage": {
                    "calls": 46,
                    "input_tokens": 1_978_100,
                    "output_tokens": 36_304,
                    "cached_input_tokens": 1_797_345,
                    "peak_input_tokens": 64_409,
                },
            },
            series_artifact="evidence/sandbox/model-usage-series.json",
        )
        self.assertEqual(usage.input_tokens, 1_978_100)
        self.assertEqual(usage.output_tokens, 36_304)
        self.assertEqual(usage.peak_input_tokens, 64_409)
        self.assertEqual(usage.exchanges_observed, 46)
        self.assertTrue(usage.fully_measured)
        self.assertEqual(
            usage.series_artifact, "evidence/sandbox/model-usage-series.json"
        )

    def test_a_result_without_a_usage_block_reports_absence_not_zero(self) -> None:
        # An older controller's result. Reporting zeros here would be
        # indistinguishable from a run that genuinely spent nothing, which is
        # the exact misreading this telemetry exists to end.
        self.assertIsNone(_model_usage({"outcome": "complete"}, series_artifact=None))

    def test_unmeasured_exchanges_are_visible_in_the_summary(self) -> None:
        usage = _model_usage(
            {
                "relay_requests": 46,
                "model_usage": {"calls": 2, "input_tokens": 500, "output_tokens": 3},
            },
            series_artifact=None,
        )
        self.assertFalse(usage.fully_measured)

class SliceContributionTests(unittest.TestCase):
    """What earlier slices built, read from artifacts the harness wrote."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan = {
            "slices": [
                {"slice_id": "SLICE-001", "title": "Skeleton"},
                {"slice_id": "SLICE-002", "title": "Storage"},
                {"slice_id": "SLICE-003", "title": "Sync"},
            ]
        }
        (self.root / ".apoapsis").mkdir(parents=True)
        store = PlanSliceExecutionStore(
            self.root / ".apoapsis" / "plan-slice-executions.db"
        )
        self.store = store

    def _task(self, task_id: str, *, outcome: str, files: list[str]) -> None:
        directory = self.root / ".apoapsis" / "tasks" / task_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "report.json").write_text(
            json.dumps({"outcome": outcome, "files_changed": files}),
            encoding="utf-8",
        )

    def _record(self, slice_id: str, task_id: str) -> None:
        # Written directly: the store's own transitions are covered by the
        # architect tests, and this is about reading, not writing.
        import sqlite3

        connection = sqlite3.connect(
            self.root / ".apoapsis" / "plan-slice-executions.db"
        )
        connection.execute(
            "INSERT INTO plan_slice_executions (plan_id, slice_id, plan_version, "
            "status, package_sha256, task_id, task_expected_version, "
            "execution_operation_id, error, version, created_at, updated_at) "
            "VALUES (?, ?, 1, 'approved', NULL, ?, NULL, NULL, NULL, 1, "
            "'2026-08-03T00:00:00Z', '2026-08-03T00:00:00Z')",
            ("PLAN-1", slice_id, task_id),
        )
        connection.commit()
        connection.close()

    def test_a_completed_earlier_slice_contributes(self) -> None:
        self._task("TASK-1", outcome="complete", files=["backend/app.py"])
        self._record("SLICE-001", "TASK-1")
        contributions = _slice_contributions(
            self.root, self.plan, "PLAN-1", "SLICE-002"
        )
        self.assertEqual([item.slice_id for item in contributions], ["SLICE-001"])
        self.assertEqual(contributions[0].paths, ["backend/app.py"])
        self.assertEqual(contributions[0].title, "Skeleton")

    def test_the_gate_is_the_reported_outcome_not_the_record_status(self) -> None:
        """Observed live: finished slices sit at `approved`, not `complete`.

        All four completed slices in `test project 6` have a
        `plan-slice-executions.db` status of `approved` while their reports
        read `complete`. Gating on the record's status would have produced an
        empty brief on every real project, silently.
        """

        self._task("TASK-1", outcome="complete", files=["backend/app.py"])
        self._record("SLICE-001", "TASK-1")
        self.assertTrue(
            _slice_contributions(self.root, self.plan, "PLAN-1", "SLICE-002")
        )

        self._task("TASK-2", outcome="human_review_required", files=["half.py"])
        self._record("SLICE-002", "TASK-2")
        contributions = _slice_contributions(
            self.root, self.plan, "PLAN-1", "SLICE-003"
        )
        # The unfinished slice's files are not inherited state.
        self.assertEqual([item.slice_id for item in contributions], ["SLICE-001"])

    def test_legacy_reports_do_not_leak_git_into_the_brief(self) -> None:
        # Reports written before ADR 0102 list `.git`; they are left as
        # written, so the filtering happens where they are reused.
        self._task("TASK-1", outcome="complete", files=[".git", "backend/app.py"])
        self._record("SLICE-001", "TASK-1")
        contributions = _slice_contributions(
            self.root, self.plan, "PLAN-1", "SLICE-002"
        )
        self.assertEqual(contributions[0].paths, ["backend/app.py"])

    def test_a_later_slice_is_never_described_to_an_earlier_one(self) -> None:
        self._task("TASK-2", outcome="complete", files=["later.py"])
        self._record("SLICE-002", "TASK-2")
        self.assertEqual(
            _slice_contributions(self.root, self.plan, "PLAN-1", "SLICE-001"), []
        )

    def test_missing_artifacts_cost_a_row_never_the_run(self) -> None:
        self.assertEqual(
            _slice_contributions(Path("/nonexistent"), self.plan, "PLAN-1", "SLICE-002"),
            [],
        )

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
        self.assertIn('RUNTIME="$(mktemp -d /tmp/apx.XXXXXX)"', launcher)
        self.assertIn('NORMALIZED_SEED="${RUNTIME}/seed"', launcher)
        self.assertIn('git clone --quiet --no-local', launcher)
        self.assertIn('-v "${RUNTIME}:${RUNTIME}:rw"', launcher)
        self.assertIn('--runtime-root "${RUNTIME}/r"', launcher)
        self.assertIn('--containment-preflight-only', launcher)
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
