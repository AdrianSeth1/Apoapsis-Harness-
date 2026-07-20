from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apoapsis.architect.audit import write_package_artifact
from apoapsis.architect.errors import (
    ActiveSliceExecutionExistsError,
    SliceApprovalError,
    SlicePackagingError,
)
from apoapsis.architect.package import build_planner_request_package
from apoapsis.architect.schema import PlanValidationResult, ValidationSeverity
from apoapsis.architect.slice_service import (
    approve_slice,
    package_slice,
    project_slice_status,
    start_slice,
)
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.architect.validation import validate_plan
from apoapsis.config import (
    ApoapsisConfig,
    ContextCompilerConfig,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ProviderPricing,
)
from apoapsis.execution.operation_schema import ExecutionOperationStatus
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.states import WorkflowState
from tests.architect_helpers import make_plan, make_slice
from tests.fakes import FakeModelProvider
from tests.test_agent_loop import action
from tests.test_vertical_slice import COMPLETE_PATCH, IMPLEMENTATION_PATCH


class PlanSliceExecutionTestsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "download-service"
        example = (
            Path(__file__).resolve().parents[1] / "examples" / "download-service"
        )
        shutil.copytree(example, self.root)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Apoapsis Tests")
        self._git("add", ".")
        self._git("commit", "-m", "controlled baseline")
        (self.root / ".apoapsis").mkdir()
        self.task_store = SQLiteTaskStore(self.root / ".apoapsis" / "apoapsis.db")
        self.plan_store = SQLitePlanStore(self.root / ".apoapsis" / "architect-plans.db")
        self.slice_store = PlanSliceExecutionStore(
            self.root / ".apoapsis" / "plan-slice-executions.db"
        )
        self.operation_store = ExecutionOperationStore(
            self.root / ".apoapsis" / "execution-operations.db"
        )

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )

    def _config(self) -> ApoapsisConfig:
        return ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1", model="fake-coder-v1"
                )
            ),
            context=ContextCompilerConfig(
                max_files=10, max_excerpt_lines=200, max_total_chars=50_000
            ),
            patch=PatchPolicyConfig(max_changed_lines=100),
            verification=VerificationConfig(
                commands=[
                    VerificationCommand(
                        name="unit-tests",
                        category="tests",
                        argv=[
                            sys.executable,
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "tests",
                            "-v",
                        ],
                        timeout_seconds=30,
                    )
                ]
            ),
        )

    def _approved_plan(self, *, slices=None):
        config = self._config()
        package = build_planner_request_package(self.root, "Add resumable downloads.", config)
        write_package_artifact(self.root, package)
        plan = make_plan(slices=slices)
        record = self.plan_store.create_plan(
            f"PLAN-{len(self.plan_store.list_plans()) + 1:012d}",
            package.package_id,
            plan.idea_text,
            plan,
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=config.architect.ceilings,
        )
        result = PlanValidationResult(
            plan_id=record.plan_id,
            plan_version=record.version,
            valid=not any(f.severity == ValidationSeverity.ERROR for f in findings),
            findings=findings,
        )
        record = self.plan_store.record_validation(
            record.plan_id, result, expected_version=record.version
        )
        record = self.plan_store.approve_plan(record.plan_id, expected_version=record.version)
        return record, config

    def _provider(self, outputs) -> InstrumentedModelProvider:
        return InstrumentedModelProvider(FakeModelProvider(outputs), ProviderPricing())

    def _worktree_branch(self, task_id: str) -> str:
        from apoapsis.execution.worktree import WorktreeManager

        slug = task_id.removeprefix("TASK-").lower()
        return WorktreeManager(self.root).describe(slug).branch

    def _commit_worktree(self, task_id: str) -> None:
        """Simulates the human finalizing a completed slice's work: a real
        commit inside its isolated worktree. Apoapsis itself never does
        this (ADR 0024's "no automatic commit" non-goal, unchanged)."""

        from apoapsis.execution.worktree import WorktreeManager

        slug = task_id.removeprefix("TASK-").lower()
        path = WorktreeManager(self.root).describe(slug).path
        subprocess.run(
            ["git", "add", "-A"], cwd=path, check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "commit", "-m", "slice work"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        )


class SlicePackagingTests(PlanSliceExecutionTestsBase):
    def test_package_is_deterministic_and_carries_exact_inherited_records(self) -> None:
        record, config = self._approved_plan()
        package = package_slice(
            self.root,
            self.plan_store,
            self.slice_store,
            self.task_store,
            self.operation_store,
            record.plan_id,
            "SLICE-1",
            expected_plan_version=record.version,
            config=config,
        )
        self.assertEqual(len(package.inherited_hard_constraints), 1)
        self.assertEqual(package.inherited_hard_constraints[0].id, "HC-1")
        self.assertEqual(
            package.inherited_hard_constraints[0].verbatim_source,
            "Preserve the current public API.",
        )
        self.assertEqual(len(package.acceptance_criteria), 1)
        self.assertEqual(package.acceptance_criteria[0].id, "AC-1")
        # Repackaging without any change reproduces the same hash.
        again = package_slice(
            self.root,
            self.plan_store,
            self.slice_store,
            self.task_store,
            self.operation_store,
            record.plan_id,
            "SLICE-1",
            expected_plan_version=record.version,
            config=config,
        )
        self.assertEqual(package.package_sha256, again.package_sha256)

    def test_stale_plan_version_is_rejected(self) -> None:
        record, config = self._approved_plan()
        with self.assertRaises(SlicePackagingError):
            package_slice(
                self.root,
                self.plan_store,
                self.slice_store,
                self.task_store,
                self.operation_store,
                record.plan_id,
                "SLICE-1",
                expected_plan_version=record.version + 1,
                config=config,
            )

    def test_unapproved_plan_is_rejected(self) -> None:
        config = self._config()
        package = build_planner_request_package(self.root, "idea", config)
        write_package_artifact(self.root, package)
        plan = make_plan()
        record = self.plan_store.create_plan(
            "PLAN-000000000001", package.package_id, plan.idea_text, plan
        )
        with self.assertRaises(SlicePackagingError):
            package_slice(
                self.root,
                self.plan_store,
                self.slice_store,
                self.task_store,
                self.operation_store,
                record.plan_id,
                "SLICE-1",
                expected_plan_version=record.version,
                config=config,
            )

    def test_changed_repository_is_rejected(self) -> None:
        record, config = self._approved_plan()
        # Rebuild the stored request package with a different repository
        # root, as if this plan had been built against a different
        # repository -- reconstructed (not hand-edited) so the package's
        # own self-consistency hash still validates; only the repository
        # identity check under test should reject it.
        from apoapsis.architect.schema import PlannerRequestPackage

        package_path = (
            self.root
            / ".apoapsis"
            / "plan-packages"
            / record.package_id
            / "request-package.json"
        )
        original = PlannerRequestPackage.model_validate_json(
            package_path.read_text(encoding="utf-8")
        )
        payload = original.model_dump(mode="json")
        payload["repository"]["root"] = str(self.root / "not-the-same-repo")
        payload["package_sha256"] = None
        rebuilt = PlannerRequestPackage.model_validate(payload)
        package_path.write_text(rebuilt.model_dump_json(), encoding="utf-8")
        with self.assertRaises(SlicePackagingError):
            package_slice(
                self.root,
                self.plan_store,
                self.slice_store,
                self.task_store,
                self.operation_store,
                record.plan_id,
                "SLICE-1",
                expected_plan_version=record.version,
                config=config,
            )

    def test_missing_inherited_constraint_is_rejected_by_plan_validation(self) -> None:
        """A slice referencing a nonexistent hard constraint can never
        reach an APPROVED plan in the first place -- ``validate_plan``
        (ADR 0019) already rejects it as ``UNKNOWN_CONSTRAINT_REFERENCE``,
        so ``approve_plan`` is unreachable for this plan."""

        bad_slice = make_slice(inherited_constraint_ids=["HC-DOES-NOT-EXIST"])
        plan = make_plan(slices=[bad_slice])
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=self._config().architect.ceilings,
        )
        self.assertTrue(
            any(item.code == "UNKNOWN_CONSTRAINT_REFERENCE" for item in findings)
        )

    def test_packaging_fails_closed_if_a_referenced_constraint_cannot_be_recovered(
        self,
    ) -> None:
        """Defense in depth, exercised directly: even though the approval
        gate above makes this unreachable through the normal plan
        lifecycle, ``_exact_constraints`` itself must never silently drop
        or invent a missing reference."""

        from apoapsis.architect.errors import SlicePackagingError as _Error
        from apoapsis.architect.slice_package import _exact_constraints

        bad_slice = make_slice(inherited_constraint_ids=["HC-DOES-NOT-EXIST"])
        plan = make_plan(slices=[bad_slice])
        with self.assertRaises(_Error):
            _exact_constraints(plan, bad_slice)

    def test_discovery_originated_plan_can_be_packaged(self) -> None:
        """A plan approved through the discovery-to-frontier-planning
        handoff (ADR 0032) carries an ``FPKG-`` package id, backed by a
        ``FrontierPlanningRequestPackage`` under
        ``.apoapsis/discovery-planning-packages/``, not the ``PKG-``/
        ``PlannerRequestPackage`` shape Architect Mode's own ``plan
        export`` produces. Packaging must recognize and verify this
        origin too, not just Architect Mode's own export path."""

        from apoapsis.discovery.audit import write_frontier_package_artifact
        from apoapsis.discovery.frontier_package import (
            build_frontier_planning_request_package,
        )
        from apoapsis.discovery.schema import IdeaBrief

        config = self._config()
        brief = IdeaBrief(summary="Add resumable downloads.", goals=["resume"])
        package = build_frontier_planning_request_package(
            self.root,
            config,
            session_id="DISC-000000000001",
            idea_text="Add resumable downloads.",
            idea_brief=brief,
            local_questions=[],
            local_answers=[],
            frontier_prior_questions=[],
            frontier_prior_answers=[],
            frontier_round=1,
        )
        write_frontier_package_artifact(self.root, package)

        plan = make_plan()
        record = self.plan_store.create_plan(
            "PLAN-000000000042", package.package_id, plan.idea_text, plan
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=config.architect.ceilings,
        )
        result = PlanValidationResult(
            plan_id=record.plan_id,
            plan_version=record.version,
            valid=not any(f.severity == ValidationSeverity.ERROR for f in findings),
            findings=findings,
        )
        record = self.plan_store.record_validation(
            record.plan_id, result, expected_version=record.version
        )
        record = self.plan_store.approve_plan(
            record.plan_id, expected_version=record.version
        )

        package_result = package_slice(
            self.root,
            self.plan_store,
            self.slice_store,
            self.task_store,
            self.operation_store,
            record.plan_id,
            "SLICE-1",
            expected_plan_version=record.version,
            config=config,
        )
        self.assertEqual(package_result.plan_package_id, package.package_id)
        self.assertEqual(len(package_result.inherited_hard_constraints), 1)

        # Fails closed exactly as before once the originating package is
        # genuinely gone, regardless of which flow produced it.
        shutil.rmtree(
            self.root
            / ".apoapsis"
            / "discovery-planning-packages"
            / package.package_id
        )
        with self.assertRaises(SlicePackagingError):
            package_slice(
                self.root,
                self.plan_store,
                self.slice_store,
                self.task_store,
                self.operation_store,
                record.plan_id,
                "SLICE-1",
                expected_plan_version=record.version,
                config=config,
            )

    def test_advisory_paths_do_not_restrict_the_derived_specification(self) -> None:
        """Suggested paths/symbols are hints, never a filesystem allowlist:
        the derived ``TaskSpecification`` carries no field that could
        restrict which files the bounded agent may touch."""

        slice_with_hints = make_slice(suggested_paths=["src/only_this_file.py"])
        record, config = self._approved_plan(slices=[slice_with_hints])
        package = package_slice(
            self.root,
            self.plan_store,
            self.slice_store,
            self.task_store,
            self.operation_store,
            record.plan_id,
            "SLICE-1",
            expected_plan_version=record.version,
            config=config,
        )
        self.assertEqual(
            package.advisory_suggested_paths, ["src/only_this_file.py"]
        )
        spec_fields = set(type(package.derived_specification).model_fields)
        self.assertNotIn("suggested_paths", spec_fields)
        self.assertNotIn("allowed_paths", spec_fields)


class DependencyEvidenceTests(PlanSliceExecutionTestsBase):
    def test_dependency_never_satisfied_by_status_alone(self) -> None:
        base = make_slice(slice_id="SLICE-1")
        dependent = make_slice(slice_id="SLICE-2", dependencies=["SLICE-1"])
        record, config = self._approved_plan(slices=[base, dependent])

        # SLICE-1 has never even been packaged: SLICE-2 must be blocked.
        with self.assertRaises(SlicePackagingError):
            package_slice(
                self.root,
                self.plan_store,
                self.slice_store,
                self.task_store,
                self.operation_store,
                record.plan_id,
                "SLICE-2",
                expected_plan_version=record.version,
                config=config,
            )

    def test_dependency_complete_but_not_merged_is_still_blocked(self) -> None:
        base = make_slice(slice_id="SLICE-1")
        dependent = make_slice(slice_id="SLICE-2", dependencies=["SLICE-1"])
        record, config = self._approved_plan(slices=[base, dependent])

        package1 = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store, self.operation_store, record.plan_id, "SLICE-1",
            expected_plan_version=record.version, config=config,
        )
        approve_slice(
            self.root, self.task_store, self.slice_store, record.plan_id, "SLICE-1",
            expected_package_sha256=package1.package_sha256,
        )
        with patch(
            "apoapsis.execution.operation_service._build_providers",
            return_value=(
                self._provider([COMPLETE_PATCH]),
                self._provider([COMPLETE_PATCH]),
                None,
            ),
        ):
            start_slice(
                self.root, self.task_store, self.slice_store, self.operation_store,
                record.plan_id, "SLICE-1", config,
            )
        status = project_slice_status(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, "SLICE-1",
        )
        self.assertEqual(status["status"], "complete")

        # SLICE-1's task reached COMPLETE, but nothing merged its worktree
        # branch back into main -- SLICE-2 must still be blocked.
        with self.assertRaises(SlicePackagingError):
            package_slice(
                self.root, self.plan_store, self.slice_store, self.task_store, self.operation_store, record.plan_id, "SLICE-2",
                expected_plan_version=record.version, config=config,
            )

    def test_dependency_satisfied_once_merged_into_current_head(self) -> None:
        base = make_slice(slice_id="SLICE-1")
        dependent = make_slice(slice_id="SLICE-2", dependencies=["SLICE-1"])
        record, config = self._approved_plan(slices=[base, dependent])

        package1 = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store, self.operation_store, record.plan_id, "SLICE-1",
            expected_plan_version=record.version, config=config,
        )
        approve_slice(
            self.root, self.task_store, self.slice_store, record.plan_id, "SLICE-1",
            expected_package_sha256=package1.package_sha256,
        )
        with patch(
            "apoapsis.execution.operation_service._build_providers",
            return_value=(
                self._provider([COMPLETE_PATCH]),
                self._provider([COMPLETE_PATCH]),
                None,
            ),
        ):
            start_slice(
                self.root, self.task_store, self.slice_store, self.operation_store,
                record.plan_id, "SLICE-1", config,
            )
        slice1_record = self.slice_store.get(record.plan_id, "SLICE-1")
        branch = self._worktree_branch(slice1_record.task_id)
        # The human commits the completed worktree's changes and merges
        # that branch into main themselves, through ordinary git commands
        # -- Apoapsis never does either automatically.
        self._commit_worktree(slice1_record.task_id)
        self._git("merge", "--no-ff", "-m", "merge slice 1", branch)

        package2 = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store, self.operation_store, record.plan_id, "SLICE-2",
            expected_plan_version=record.version, config=config,
        )
        self.assertTrue(all(item.satisfied for item in package2.dependency_evidence))


class SliceApprovalAndExecutionTests(PlanSliceExecutionTestsBase):
    def test_approval_creates_and_approves_the_derived_task(self) -> None:
        record, config = self._approved_plan()
        package = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store, self.operation_store, record.plan_id, "SLICE-1",
            expected_plan_version=record.version, config=config,
        )
        slice_record = approve_slice(
            self.root, self.task_store, self.slice_store, record.plan_id, "SLICE-1",
            expected_package_sha256=package.package_sha256,
        )
        self.assertIsNotNone(slice_record.task_id)
        task = self.task_store.get_task(slice_record.task_id)
        self.assertEqual(task.state, WorkflowState.SPEC_APPROVED)
        self.assertEqual(
            task.specification.hard_constraints[0].verbatim_source,
            "Preserve the current public API.",
        )

    def test_package_hash_mismatch_is_rejected(self) -> None:
        record, config = self._approved_plan()
        package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store, self.operation_store, record.plan_id, "SLICE-1",
            expected_plan_version=record.version, config=config,
        )
        with self.assertRaises(SliceApprovalError):
            approve_slice(
                self.root, self.task_store, self.slice_store, record.plan_id, "SLICE-1",
                expected_package_sha256="0" * 64,
            )

    def test_duplicate_approval_of_a_second_slice_is_rejected(self) -> None:
        first = make_slice(slice_id="SLICE-1")
        second = make_slice(slice_id="SLICE-2")
        record, config = self._approved_plan(slices=[first, second])

        package1 = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store, self.operation_store, record.plan_id, "SLICE-1",
            expected_plan_version=record.version, config=config,
        )
        approve_slice(
            self.root, self.task_store, self.slice_store, record.plan_id, "SLICE-1",
            expected_package_sha256=package1.package_sha256,
        )
        package2 = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store, self.operation_store, record.plan_id, "SLICE-2",
            expected_plan_version=record.version, config=config,
        )
        with self.assertRaises(ActiveSliceExecutionExistsError):
            approve_slice(
                self.root, self.task_store, self.slice_store, record.plan_id, "SLICE-2",
                expected_package_sha256=package2.package_sha256,
            )

    def test_successful_slice_execution_reflected_in_status(self) -> None:
        record, config = self._approved_plan()
        package = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store, self.operation_store, record.plan_id, "SLICE-1",
            expected_plan_version=record.version, config=config,
        )
        approve_slice(
            self.root, self.task_store, self.slice_store, record.plan_id, "SLICE-1",
            expected_package_sha256=package.package_sha256,
        )
        with patch(
            "apoapsis.execution.operation_service._build_providers",
            return_value=(
                self._provider([COMPLETE_PATCH]),
                self._provider([COMPLETE_PATCH]),
                None,
            ),
        ):
            op_record = start_slice(
                self.root, self.task_store, self.slice_store, self.operation_store,
                record.plan_id, "SLICE-1", config,
            )
        self.assertEqual(op_record.status, ExecutionOperationStatus.SUCCEEDED)
        status = project_slice_status(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, "SLICE-1",
        )
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["task_state"], "COMPLETE")

    def test_human_review_stop_reflected_in_status(self) -> None:
        record, config = self._approved_plan()
        package = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store, self.operation_store, record.plan_id, "SLICE-1",
            expected_plan_version=record.version, config=config,
        )
        approve_slice(
            self.root, self.task_store, self.slice_store, record.plan_id, "SLICE-1",
            expected_package_sha256=package.package_sha256,
        )
        # A one-shot repair attempt that never reaches PASSED exhausts the
        # single repair budget and stops for human review.
        with patch(
            "apoapsis.execution.operation_service._build_providers",
            return_value=(
                self._provider(
                    [
                        IMPLEMENTATION_PATCH,
                        IMPLEMENTATION_PATCH,
                    ]
                ),
                self._provider(
                    [
                        IMPLEMENTATION_PATCH,
                        IMPLEMENTATION_PATCH,
                    ]
                ),
                None,
            ),
        ):
            start_slice(
                self.root, self.task_store, self.slice_store, self.operation_store,
                record.plan_id, "SLICE-1", config,
            )
        status = project_slice_status(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, "SLICE-1",
        )
        self.assertIn(status["status"], {"human_review", "failed"})

    def test_duplicate_start_of_the_same_slice_is_rejected(self) -> None:
        record, config = self._approved_plan()
        package = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store, self.operation_store, record.plan_id, "SLICE-1",
            expected_plan_version=record.version, config=config,
        )
        approve_slice(
            self.root, self.task_store, self.slice_store, record.plan_id, "SLICE-1",
            expected_package_sha256=package.package_sha256,
        )
        with patch(
            "apoapsis.execution.operation_service._build_providers",
            return_value=(
                self._provider([action("search_repository", query="x")] * 4),
                self._provider([action("search_repository", query="x")] * 4),
                None,
            ),
        ):
            start_slice(
                self.root, self.task_store, self.slice_store, self.operation_store,
                record.plan_id, "SLICE-1", config, operation_id="EXOP-FIRST00000000000001",
            )
            with self.assertRaises(Exception):
                start_slice(
                    self.root, self.task_store, self.slice_store, self.operation_store,
                    record.plan_id, "SLICE-1", config,
                    operation_id="EXOP-SECOND0000000000001",
                )

    def test_approving_one_slice_never_starts_or_approves_a_dependent_slice(self) -> None:
        base = make_slice(slice_id="SLICE-1")
        dependent = make_slice(slice_id="SLICE-2", dependencies=["SLICE-1"])
        record, config = self._approved_plan(slices=[base, dependent])

        package1 = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store, self.operation_store, record.plan_id, "SLICE-1",
            expected_plan_version=record.version, config=config,
        )
        approve_slice(
            self.root, self.task_store, self.slice_store, record.plan_id, "SLICE-1",
            expected_package_sha256=package1.package_sha256,
        )
        with patch(
            "apoapsis.execution.operation_service._build_providers",
            return_value=(
                self._provider([COMPLETE_PATCH]),
                self._provider([COMPLETE_PATCH]),
                None,
            ),
        ):
            start_slice(
                self.root, self.task_store, self.slice_store, self.operation_store,
                record.plan_id, "SLICE-1", config,
            )
        # SLICE-2 exists, is dependency-satisfiable, but nothing here ever
        # packaged, approved, or started it automatically.
        with self.assertRaises(Exception):
            self.slice_store.get(record.plan_id, "SLICE-2")
        status = project_slice_status(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, "SLICE-2",
        )
        self.assertEqual(status["status"], "ready_or_blocked")
        self.assertIsNone(status["record"])


if __name__ == "__main__":
    unittest.main()
