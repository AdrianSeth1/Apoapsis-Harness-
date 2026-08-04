from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from apoapsis.architect.audit import write_package_artifact
from apoapsis.architect.delivery import load_plan_delivery, prepare_plan_delivery
from apoapsis.architect.errors import (
    ActiveSliceExecutionExistsError,
    SliceApprovalError,
    SlicePackagingError,
)
from apoapsis.architect.final_verification import (
    FINAL_VERIFICATION_ARTIFACT,
    FinalProjectVerification,
    FinalVerificationStatus,
    load_final_project_verification,
    run_final_project_verification,
)
from apoapsis.execution.worktree import WorktreeManager
from apoapsis.architect.package import build_planner_request_package
from apoapsis.architect.schema import (
    AcceptanceProofObligation,
    PlanDeliveryContract,
    PlanValidationResult,
    ValidationSeverity,
    VerificationStrategy,
)
from apoapsis.architect.slice_package import enrich_specification_with_slice_package
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
    CapabilitySandboxConfig,
    ContextCompilerConfig,
    ExecutionConfig,
    ExecutionMode,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ProviderPricing,
)
from apoapsis.execution.operation_schema import ExecutionOperationStatus
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.models.telemetry import (
    InstrumentedModelProvider,
    RelayObservedModelUsage,
)
from apoapsis.reporting.current_state import project_current_task_evidence
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.states import WorkflowState
from apoapsis.workflow.vertical_slice import VerticalSliceRunner
from apoapsis.agent.session import AgentSessionOutcome, AgentSessionResult
from apoapsis.verification.runner import VerificationRunner
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

    def _approved_plan(self, *, slices=None, config=None, plan=None):
        config = config or self._config()
        package = build_planner_request_package(self.root, "Add resumable downloads.", config)
        write_package_artifact(self.root, package)
        plan = plan if plan is not None else make_plan(slices=slices)
        record = self.plan_store.create_plan(
            f"PLAN-{len(self.plan_store.list_plans()) + 1:012d}",
            package.package_id,
            plan.idea_text,
            plan,
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={
                item.name for item in config.verification.commands
            },
            ceilings=config.architect.ceilings,
            configured_commands=config.verification.commands,
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

    def _complete_slice_with_patch(self, record, config, slice_id: str, patch_text: str):
        """Packages, approves, and runs one slice to completion with a
        fake model provider that emits exactly ``patch_text``, and returns
        its execution record. Shared by the delivery-hardening tests below,
        which need several fully COMPLETE slices chained together."""

        package = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store,
            self.operation_store, record.plan_id, slice_id,
            expected_plan_version=record.version, config=config,
        )
        approve_slice(
            self.root, self.task_store, self.slice_store, record.plan_id, slice_id,
            expected_package_sha256=package.package_sha256,
        )
        with patch(
            "apoapsis.execution.operation_service._build_providers",
            return_value=(
                self._provider([patch_text]),
                self._provider([patch_text]),
                None,
            ),
        ):
            start_slice(
                self.root, self.task_store, self.slice_store, self.operation_store,
                record.plan_id, slice_id, config,
            )
        return self.slice_store.get(record.plan_id, slice_id)


class SlicePackagingTests(PlanSliceExecutionTestsBase):
    def test_approved_plan_slice_uses_capability_sandbox_product_adapter(self) -> None:
        record, base_config = self._approved_plan()
        config = base_config.model_copy(
            update={
                "execution": ExecutionConfig(
                    mode=ExecutionMode.AGENT,
                    capability_sandbox=CapabilitySandboxConfig(enabled=True),
                )
            }
        )
        package = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store,
            self.operation_store, record.plan_id, "SLICE-1",
            expected_plan_version=record.version, config=config,
        )
        self.assertEqual(package.approved_plan, record.plan)
        approved = approve_slice(
            self.root, self.task_store, self.slice_store, record.plan_id, "SLICE-1",
            expected_package_sha256=package.package_sha256,
        )

        class FakeNativeExecutor:
            calls = 0

            def run(inner_self, **kwargs):
                inner_self.calls += 1
                worktree = kwargs["worktree"]
                source = worktree / "src" / "download_service" / "capability.py"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("MODE = 'native-qwen'\n", encoding="utf-8")
                verification = VerificationRunner(kwargs["config"].verification).run(
                    kwargs["specification"].task_id, worktree
                )
                return AgentSessionResult(
                    outcome=AgentSessionOutcome.COMPLETE,
                    stop_reason="fake native checkpoint complete",
                    turns=1,
                    patch_attempts=1,
                    verification_runs=1,
                    changed_files=["src/download_service/capability.py"],
                    verification_results=[verification],
                )

        executor = FakeNativeExecutor()
        runner = VerticalSliceRunner(
            self.root,
            self.task_store,
            self._provider([]),
            config,
            capability_sandbox_executor=executor,
        )
        report = runner.execute_approved_task(approved.task_id)

        self.assertEqual(report.outcome.value, "complete")
        self.assertEqual(executor.calls, 1)
        events = [item.event_type for item in self.task_store.events(approved.task_id)]
        self.assertIn("capability_sandbox_patch_ready", events)
        self.assertIn("capability_sandbox_verification_passed", events)

    def test_capability_sandbox_usage_reaches_the_task_report(self) -> None:
        """The live path's tokens are reported, not published as zero.

        The sandbox's model traffic never passes through a harness provider
        call, so before the relay reported usage a completed slice published
        `input_tokens: 0` -- indistinguishable from a task that spent nothing.
        """

        record, base_config = self._approved_plan()
        config = base_config.model_copy(
            update={
                "execution": ExecutionConfig(
                    mode=ExecutionMode.AGENT,
                    capability_sandbox=CapabilitySandboxConfig(enabled=True),
                )
            }
        )
        package = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store,
            self.operation_store, record.plan_id, "SLICE-1",
            expected_plan_version=record.version, config=config,
        )
        approved = approve_slice(
            self.root, self.task_store, self.slice_store, record.plan_id, "SLICE-1",
            expected_package_sha256=package.package_sha256,
        )

        usage = RelayObservedModelUsage(
            calls=46,
            exchanges_observed=46,
            input_tokens=1_978_100,
            output_tokens=36_304,
            cached_input_tokens=1_797_345,
            peak_input_tokens=64_409,
            series_artifact=".apoapsis/tasks/x/model-usage-series.json",
        )

        class FakeNativeExecutor:
            def run(inner_self, **kwargs):
                worktree = kwargs["worktree"]
                source = worktree / "src" / "download_service" / "capability.py"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("MODE = 'native-qwen'\n", encoding="utf-8")
                verification = VerificationRunner(kwargs["config"].verification).run(
                    kwargs["specification"].task_id, worktree
                )
                return AgentSessionResult(
                    outcome=AgentSessionOutcome.COMPLETE,
                    stop_reason="fake native checkpoint complete",
                    turns=1,
                    patch_attempts=1,
                    verification_runs=1,
                    changed_files=["src/download_service/capability.py"],
                    verification_results=[verification],
                    model_usage=usage,
                )

        runner = VerticalSliceRunner(
            self.root,
            self.task_store,
            self._provider([]),
            config,
            capability_sandbox_executor=FakeNativeExecutor(),
        )
        report = runner.execute_approved_task(approved.task_id)

        self.assertEqual(report.outcome.value, "complete")
        self.assertEqual(report.input_tokens, 1_978_100)
        self.assertEqual(report.output_tokens, 36_304)
        self.assertEqual(report.cached_input_tokens, 1_797_345)
        self.assertIsNotNone(report.local_model_usage)
        self.assertEqual(report.local_model_usage.calls, 46)
        self.assertEqual(report.local_model_usage.peak_input_tokens, 64_409)
        self.assertTrue(report.local_model_usage.fully_measured)
        # The summary is reported beside the harness's own calls, never as one
        # of them: `number_of_calls` counts calls the harness actually made.
        self.assertEqual(report.number_of_calls, len(report.provider_calls))

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

        # The test-discovery path is here only to keep the plan valid
        # (UNASSIGNED_TEST_DISCOVERY_ROOT); the point under test is that
        # whatever paths a slice names stay advisory, which two paths make
        # exactly as well as one.
        slice_with_hints = make_slice(
            suggested_paths=["src/only_this_file.py", "tests/test_only_this_file.py"]
        )
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
            package.advisory_suggested_paths,
            ["src/only_this_file.py", "tests/test_only_this_file.py"],
        )
        spec_fields = set(type(package.derived_specification).model_fields)
        self.assertNotIn("suggested_paths", spec_fields)
        self.assertNotIn("allowed_paths", spec_fields)

    def test_derived_specification_preserves_full_approved_slice_contract(self) -> None:
        slice_with_contract = make_slice(
            # `tests/` path keeps the plan valid under
            # UNASSIGNED_TEST_DISCOVERY_ROOT; this test is about the
            # contract fields the derived specification preserves, not paths.
            suggested_paths=["src/generation.py", "tests/test_generation.py"]
        ).model_copy(
            update={
                "interface_contracts": ["generate_reply(text: str) -> str"],
                "exclusions": ["Do not send the message."],
                "integration_assumptions": [
                    "API key is supplied via the environment."
                ],
                "stop_conditions": ["A coherent reply is returned."],
                "suggested_symbols": ["generate_reply"],
                "work_brief": "Implement the approved contextual reply generator.",
            }
        )
        record, config = self._approved_plan(slices=[slice_with_contract])
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

        facts = "\n".join(
            item.text for item in package.derived_specification.known_facts
        )
        self.assertIn("Implement the approved contextual reply generator", facts)
        self.assertIn("generate_reply(text: str) -> str", facts)
        self.assertIn("Do not send the message", facts)
        self.assertIn("API key is supplied", facts)
        self.assertIn("A coherent reply is returned", facts)
        self.assertIn("Advisory suggested paths (not an allowlist)", facts)
        self.assertEqual(
            package.derived_specification.verification_requirements,
            slice_with_contract.verification_commands,
        )
        legacy_specification = package.derived_specification.model_copy(
            update={"known_facts": [], "verification_requirements": []}
        )
        restored = enrich_specification_with_slice_package(
            legacy_specification, package
        )
        self.assertEqual(restored.known_facts, package.derived_specification.known_facts)
        self.assertEqual(
            restored.verification_requirements,
            package.derived_specification.verification_requirements,
        )

    def test_enrichment_restores_the_two_requirement_facts_by_name(self) -> None:
        """The equality assertion above proves the mirror is complete, but
        reports a drift as "lists differ" over several kilobytes. These two
        facts are the ones that were actually missing, and they are the two
        that exist because live slices got them wrong -- so name them, and
        make a future regression say which one went."""

        first = make_slice(slice_id="SLICE-1")
        second = make_slice(slice_id="SLICE-2", dependencies=["SLICE-1"])
        record, config = self._approved_plan(slices=[first, second])
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        record = self.plan_store.get_plan(record.plan_id)
        package = package_slice(
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

        # Packaging resolved both from live state.
        self.assertEqual(package.test_discovery_roots, ["tests"])
        self.assertEqual(package.inherited_slice_ids, ["SLICE-1"])

        stripped = package.derived_specification.model_copy(
            update={"known_facts": [], "verification_requirements": []}
        )
        restored_facts = "\n".join(
            item.text
            for item in enrich_specification_with_slice_package(
                stripped, package
            ).known_facts
        )
        self.assertIn("REQUIRED test location", restored_facts)
        self.assertIn("tests", restored_facts)
        self.assertIn("Scope boundary", restored_facts)
        self.assertIn("inherited work is out of scope", restored_facts)


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

    def test_completed_dependency_is_checkpointed_and_inherited_without_main_merge(self) -> None:
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

        main_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        package2 = package_slice(
            self.root, self.plan_store, self.slice_store, self.task_store,
            self.operation_store, record.plan_id, "SLICE-2",
            expected_plan_version=record.version, config=config,
        )
        self.assertEqual(package2.inherited_slice_ids, ["SLICE-1"])
        self.assertNotEqual(package2.execution_base_commit, main_before)
        main_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(main_after, main_before)
        approve_slice(
            self.root, self.task_store, self.slice_store, record.plan_id,
            "SLICE-2", expected_package_sha256=package2.package_sha256,
        )
        slice2_patch = (
            "diff --git a/slice2.txt b/slice2.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/slice2.txt\n"
            "@@ -0,0 +1 @@\n"
            "+slice two\n"
        )
        with patch(
            "apoapsis.execution.operation_service._build_providers",
            return_value=(
                self._provider([slice2_patch]),
                self._provider([slice2_patch]),
                None,
            ),
        ):
            start_slice(
                self.root, self.task_store, self.slice_store,
                self.operation_store, record.plan_id, "SLICE-2", config,
            )
        slice2_record = self.slice_store.get(record.plan_id, "SLICE-2")
        from apoapsis.execution.worktree import WorktreeManager
        slice2_path = WorktreeManager(self.root).describe(
            slice2_record.task_id.removeprefix("TASK-").lower()
        ).path
        inherited_source = Path(slice2_path) / "src" / "download_service" / "downloader.py"
        self.assertIn("response.status_code == 206", inherited_source.read_text(encoding="utf-8"))

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
        # A manual merge remains supported; the inherited base resolves to
        # the same completed branch tip.
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

        delivery = prepare_plan_delivery(
            self.root,
            self.plan_store,
            self.slice_store,
            self.task_store,
            record.plan_id,
        verification_config=config.verification,
        )
        self.assertTrue((self.root / delivery.archive_path).is_file())
        self.assertTrue((self.root / delivery.frontier_review_handoff_path).is_file())
        self.assertEqual(delivery.completed_slice_ids, ["SLICE-1"])
        self.assertEqual(
            self.plan_store.get_plan(record.plan_id).status.value, "executed"
        )

    def test_delivery_verification_summary_serializes_real_report_data(self) -> None:
        # Regression test: `prepare_plan_delivery` previously read
        # `item.command_name` off `report.verification_results` directly,
        # but that field is `list[VerificationResult]` (one aggregate
        # verification *run*), not `list[VerificationCommandResult]` -- the
        # per-command `name`/`status`/`exit_code` live on each run's nested
        # `.commands`. Both the wrong attribute name and the wrong
        # collection type raised `AttributeError` for any real report data,
        # so this exercises `prepare_plan_delivery` against a genuine
        # `FinalTaskReport` produced by real slice execution (not a hand-
        # built fixture) and asserts the serialized summary is correct, not
        # just that no exception was raised.
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
            start_slice(
                self.root, self.task_store, self.slice_store, self.operation_store,
                record.plan_id, "SLICE-1", config,
            )

        delivery = prepare_plan_delivery(
            self.root,
            self.plan_store,
            self.slice_store,
            self.task_store,
            record.plan_id,
        verification_config=config.verification,
        )

        self.assertEqual(len(delivery.verification_summary), 1)
        entry = delivery.verification_summary[0]
        self.assertEqual(entry["slice_id"], "SLICE-1")
        self.assertEqual(entry["outcome"], "complete")
        self.assertEqual(
            entry["verification"],
            [{"name": "unit-tests", "status": "passed", "exit_code": 0}],
        )

        # The same data must round-trip through delivery.json unchanged.
        reloaded = load_plan_delivery(self.root, record.plan_id)
        assert reloaded is not None
        self.assertEqual(reloaded.verification_summary, delivery.verification_summary)

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


_SLICE2_PATCH = (
    "diff --git a/slice2.txt b/slice2.txt\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/slice2.txt\n"
    "@@ -0,0 +1 @@\n"
    "+slice two\n"
)


class DeliveryHardeningTests(PlanSliceExecutionTestsBase):
    """Covers the six delivery-hardening review items against
    ``prepare_plan_delivery`` and its collaborators: multi-slice
    inheritance, divergent-branch failure clarity, ZIP exclusion of
    Apoapsis/credential state, frontier-review handoff completeness,
    fail-closed EXECUTED-transition ordering, and idempotent redelivery.
    """

    def test_multi_slice_delivery_covers_the_whole_chain(self) -> None:
        base = make_slice(slice_id="SLICE-1")
        dependent = make_slice(slice_id="SLICE-2", dependencies=["SLICE-1"])
        record, config = self._approved_plan(slices=[base, dependent])

        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        self._complete_slice_with_patch(record, config, "SLICE-2", _SLICE2_PATCH)

        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store, record.plan_id,
        verification_config=config.verification,
        )
        # One integrated commit across the whole chain, both slices listed
        # in plan order, and the plan reached EXECUTED.
        self.assertEqual(delivery.completed_slice_ids, ["SLICE-1", "SLICE-2"])
        self.assertEqual(
            [item["slice_id"] for item in delivery.verification_summary],
            ["SLICE-1", "SLICE-2"],
        )
        self.assertEqual(
            self.plan_store.get_plan(record.plan_id).status.value, "executed"
        )
        # The archive contains cumulative content from every slice, not
        # just the last one.
        with zipfile.ZipFile(self.root / delivery.archive_path) as archive:
            names = archive.namelist()
            self.assertIn("slice2.txt", names)
            downloader = archive.read(
                "src/download_service/downloader.py"
            ).decode("utf-8")
        self.assertIn("response.status_code == 206", downloader)

    def test_divergent_completed_slice_branches_fail_delivery_clearly(self) -> None:
        base = make_slice(slice_id="SLICE-1")
        dependent = make_slice(slice_id="SLICE-2", dependencies=["SLICE-1"])
        record, config = self._approved_plan(slices=[base, dependent])

        slice1 = self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        self._complete_slice_with_patch(record, config, "SLICE-2", _SLICE2_PATCH)

        # Simulate independent post-completion drift directly on SLICE-1's
        # own branch -- work SLICE-2 never inherited, since it already
        # branched off SLICE-1's earlier checkpoint. No single commit now
        # integrates both completed slices.
        from apoapsis.execution.worktree import WorktreeManager

        slice1_path = Path(
            WorktreeManager(self.root).describe(
                slice1.task_id.removeprefix("TASK-").lower()
            ).path
        )
        (slice1_path / "drift.txt").write_text("independent change\n", encoding="utf-8")

        with self.assertRaises(SlicePackagingError) as ctx:
            prepare_plan_delivery(
                self.root, self.plan_store, self.slice_store, self.task_store,
                record.plan_id,
            verification_config=config.verification,
            )
        message = str(ctx.exception)
        # A clear, specific, actionable error -- not a KeyError/AttributeError
        # and not a silently-wrong worktree -- naming which slices diverged.
        self.assertIn("diverge", message)
        self.assertIn("SLICE-1", message)
        self.assertIn("SLICE-2", message)
        self.assertEqual(
            self.plan_store.get_plan(record.plan_id).status.value, "approved"
        )
        self.assertIsNone(load_plan_delivery(self.root, record.plan_id))

    def test_zip_excludes_apoapsis_git_and_credential_state(self) -> None:
        record, config = self._approved_plan()
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)

        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store, record.plan_id,
        verification_config=config.verification,
        )
        with zipfile.ZipFile(self.root / delivery.archive_path) as archive:
            names = archive.namelist()
        self.assertFalse(any(name == ".git" or name.startswith(".git/") for name in names))
        self.assertFalse(
            any(name == ".apoapsis" or name.startswith(".apoapsis/") for name in names)
        )
        self.assertFalse(any(name.endswith(".env") for name in names))

    def test_forbidden_tracked_paths_block_delivery(self) -> None:
        """Defense in depth beyond ``git archive``'s natural exclusion: if
        Apoapsis runtime state or a credential-shaped file ends up
        genuinely tracked at the final commit (e.g. force-added, or a
        repository that predates ``apoapsis init`` ensuring ``.gitignore``
        coverage), delivery must refuse to ship it rather than silently
        include it in a "finished project" archive."""

        record, config = self._approved_plan()
        slice1 = self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)

        from apoapsis.execution.worktree import WorktreeManager

        slice1_path = Path(
            WorktreeManager(self.root).describe(
                slice1.task_id.removeprefix("TASK-").lower()
            ).path
        )
        (slice1_path / ".env").write_text("SECRET=super-secret\n", encoding="utf-8")
        runtime_dir = slice1_path / ".apoapsis" / "tasks" / "TASK-FAKE00000000000001"
        runtime_dir.mkdir(parents=True)
        (runtime_dir / "report.json").write_text("{}", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-f", ".env", ".apoapsis"],
            cwd=slice1_path, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "oops: committed secrets and runtime state"],
            cwd=slice1_path, check=True, capture_output=True, text=True,
        )

        with self.assertRaises(SlicePackagingError) as ctx:
            prepare_plan_delivery(
                self.root, self.plan_store, self.slice_store, self.task_store,
                record.plan_id,
            verification_config=config.verification,
            )
        message = str(ctx.exception)
        self.assertIn(".env", message)
        self.assertIn(".apoapsis", message)
        self.assertEqual(
            self.plan_store.get_plan(record.plan_id).status.value, "approved"
        )
        self.assertIsNone(load_plan_delivery(self.root, record.plan_id))

    def test_frontier_review_handoff_contains_whole_project_context(self) -> None:
        slice_a = make_slice(slice_id="SLICE-1").model_copy(
            update={
                "objective": "Add resumable download offset tracking.",
                "work_brief": "Persist download offsets to a side file per URL.",
            }
        )
        slice_b = make_slice(slice_id="SLICE-2", dependencies=["SLICE-1"]).model_copy(
            update={
                "objective": "Expose a resumable download CLI flag.",
                "work_brief": "Add a --resume flag that reuses the tracked offset.",
            }
        )
        record, config = self._approved_plan(slices=[slice_a, slice_b])

        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        self._complete_slice_with_patch(record, config, "SLICE-2", _SLICE2_PATCH)

        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store, record.plan_id,
        verification_config=config.verification,
        )
        handoff = (self.root / delivery.frontier_review_handoff_path).read_text(
            encoding="utf-8"
        )
        # Original idea/plan text, not just a file inventory.
        self.assertIn(record.plan.idea_text, handoff)
        self.assertIn(record.plan.architecture_summary, handoff)
        # Each slice's own objective/work brief text, not just its ID.
        self.assertIn("Add resumable download offset tracking.", handoff)
        self.assertIn("Persist download offsets to a side file per URL.", handoff)
        self.assertIn("Expose a resumable download CLI flag.", handoff)
        self.assertIn("Add a --resume flag that reuses the tracked offset.", handoff)
        # Resolved acceptance criteria text (both slices reference AC-1).
        self.assertIn(
            "Resumed downloads continue from the correct offset.", handoff
        )
        # Verification summary and space for cross-slice integration risk.
        self.assertIn("unit-tests", handoff)
        self.assertIn("cross_slice_integration_risks", handoff)

    def test_mark_executed_failure_leaves_plan_unexecuted_with_no_delivery_record(
        self,
    ) -> None:
        record, config = self._approved_plan()
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)

        with patch.object(
            self.plan_store, "mark_executed", side_effect=RuntimeError("simulated failure"),
        ):
            with self.assertRaises(RuntimeError):
                prepare_plan_delivery(
                    self.root, self.plan_store, self.slice_store, self.task_store,
                    record.plan_id,
                verification_config=config.verification,
                )
        self.assertEqual(
            self.plan_store.get_plan(record.plan_id).status.value, "approved"
        )
        self.assertIsNone(load_plan_delivery(self.root, record.plan_id))

        # A later retry (without the injected failure) heals cleanly to a
        # fully consistent EXECUTED plan with a real delivery record --
        # proving the missing-delivery.json-after-EXECUTED gap is closed
        # by prepare_plan_delivery's own retry path, not left dangling.
        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store, record.plan_id,
        verification_config=config.verification,
        )
        self.assertEqual(
            self.plan_store.get_plan(record.plan_id).status.value, "executed"
        )
        reloaded = load_plan_delivery(self.root, record.plan_id)
        assert reloaded is not None
        self.assertEqual(delivery, reloaded)
        self.assertEqual(
            delivery.plan_version, self.plan_store.get_plan(record.plan_id).version
        )

    def test_archive_and_handoff_exist_before_plan_is_marked_executed(self) -> None:
        record, config = self._approved_plan()
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)

        plan_dir = self.root / ".apoapsis" / "plans" / record.plan_id
        archive_name = f"{record.plan_id}-finished-project.zip"
        handoff_name = f"FRONTIER-WHOLE-PROJECT-REVIEW-{record.plan_id}.md"
        observed: dict[str, bool] = {}
        original = self.plan_store.mark_executed

        def _spy(plan_id, **kwargs):
            observed["archive_exists"] = (plan_dir / archive_name).is_file()
            observed["handoff_exists"] = (plan_dir / handoff_name).is_file()
            observed["delivery_json_exists"] = (plan_dir / "delivery.json").is_file()
            return original(plan_id, **kwargs)

        with patch.object(self.plan_store, "mark_executed", side_effect=_spy):
            delivery = prepare_plan_delivery(
                self.root, self.plan_store, self.slice_store, self.task_store,
                record.plan_id,
            verification_config=config.verification,
            )
        self.assertTrue(observed["archive_exists"])
        self.assertTrue(observed["handoff_exists"])
        self.assertFalse(observed["delivery_json_exists"])
        self.assertTrue((self.root / delivery.archive_path).is_file())
        self.assertTrue((self.root / delivery.frontier_review_handoff_path).is_file())

    def test_second_delivery_call_is_idempotent(self) -> None:
        record, config = self._approved_plan()
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)

        first = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store, record.plan_id,
        verification_config=config.verification,
        )
        archive_path = self.root / first.archive_path
        first_mtime = archive_path.stat().st_mtime_ns
        first_bytes = archive_path.read_bytes()

        with patch.object(
            self.plan_store, "mark_executed",
            side_effect=AssertionError("a second delivery must not re-execute the plan"),
        ):
            second = prepare_plan_delivery(
                self.root, self.plan_store, self.slice_store, self.task_store,
                record.plan_id,
            verification_config=config.verification,
            )

        self.assertEqual(first, second)
        self.assertEqual(archive_path.stat().st_mtime_ns, first_mtime)
        self.assertEqual(archive_path.read_bytes(), first_bytes)
        events = self.plan_store.events(record.plan_id)
        self.assertEqual(
            sum(1 for event in events if event.event_type == "plan_delivery_prepared"),
            1,
        )


class DeliveryCurrentEvidenceTests(PlanSliceExecutionTestsBase):
    """Delivery must report each slice's *current* outcome and refuse to
    ship one it can no longer evidence (ADR 0072).

    Crisis Atlas (`PLAN-E1B90639E58D`) is the case these cover: a plan
    delivered a `delivery.json` and a whole-project frontier handoff that
    both said `human_review_required` with a failed verification, for a
    slice whose persisted state was `COMPLETE` and whose manual-frontier
    repair had passed. Delivery was reading `report.json`, which is only
    ever written once, at the slice's first stop.
    """

    def test_delivery_summary_agrees_with_the_projection(self) -> None:
        """The delivered outcome and every delivered command result must
        come from one evidence generation, not two: the task label from
        persisted state and the commands from a superseded snapshot is
        exactly the contradiction being fixed."""

        record, config = self._approved_plan()
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        execution = self.slice_store.get(record.plan_id, "SLICE-1")
        assert execution.task_id is not None

        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id,
        verification_config=config.verification,
        )
        evidence = project_current_task_evidence(
            self.root, self.task_store, execution.task_id
        )

        entry = delivery.verification_summary[0]
        self.assertEqual(entry["outcome"], "complete")
        self.assertEqual(entry["outcome"], evidence.outcome.value)
        self.assertEqual(entry["verification"], evidence.command_results())
        self.assertEqual(
            entry["evidence_generation"], evidence.evidence_generation.value
        )
        self.assertEqual(entry["evidence_integrity"], "intact")
        self.assertEqual(entry["evidence_sources"], evidence.evidence_sources)
        self.assertFalse(entry["supersedes_original_report"])
        # The provenance travels into the whole-project frontier handoff
        # too, under a heading that does not present per-slice history as
        # proof of the integrated project.
        handoff = (self.root / delivery.frontier_review_handoff_path).read_text(
            encoding="utf-8"
        )
        self.assertIn("## Per-slice verification history", handoff)
        self.assertIn("evidence_generation", handoff)
        self.assertNotIn("## Harness verification summary", handoff)

    def test_delivery_refuses_a_slice_whose_evidence_is_missing(self) -> None:
        """A COMPLETE workflow state is necessary but not sufficient. If
        the artifact that proved completion is gone, delivery fails closed
        rather than falling back to whatever `report.json` last said."""

        record, config = self._approved_plan()
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        execution = self.slice_store.get(record.plan_id, "SLICE-1")
        assert execution.task_id is not None
        report_path = (
            self.root / ".apoapsis" / "tasks" / execution.task_id / "report.json"
        )
        self.assertTrue(report_path.is_file())
        report_path.unlink()

        with self.assertRaises(SlicePackagingError) as caught:
            prepare_plan_delivery(
                self.root, self.plan_store, self.slice_store, self.task_store,
                record.plan_id,
            verification_config=config.verification,
            )

        message = str(caught.exception)
        self.assertIn("persisted COMPLETE", message)
        self.assertIn("integrity=missing", message)
        self.assertEqual(
            self.plan_store.get_plan(record.plan_id).status.value, "approved"
        )
        self.assertIsNone(load_plan_delivery(self.root, record.plan_id))

    def test_delivery_refuses_a_slice_whose_evidence_is_malformed(self) -> None:
        record, config = self._approved_plan()
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        execution = self.slice_store.get(record.plan_id, "SLICE-1")
        assert execution.task_id is not None
        (
            self.root / ".apoapsis" / "tasks" / execution.task_id / "report.json"
        ).write_text('{"outcome": "complete"}', encoding="utf-8")

        with self.assertRaises(SlicePackagingError) as caught:
            prepare_plan_delivery(
                self.root, self.plan_store, self.slice_store, self.task_store,
                record.plan_id,
            verification_config=config.verification,
            )

        self.assertIn("integrity=malformed", str(caught.exception))

    def test_slice_status_and_delivery_share_one_projection(self) -> None:
        """The plan surface and the delivery record must not maintain
        separate notions of a slice's outcome."""

        record, config = self._approved_plan()
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)

        status = project_slice_status(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, "SLICE-1",
        )
        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id,
        verification_config=config.verification,
        )

        self.assertEqual(status["task_state"], "COMPLETE")
        self.assertEqual(status["current_evidence"]["outcome"], "complete")
        self.assertEqual(
            status["current_evidence"]["evidence_generation"],
            delivery.verification_summary[0]["evidence_generation"],
        )
        self.assertEqual(status["current_evidence"]["evidence_integrity"], "intact")


_INTEGRATION_MARKER_PATCH = (
    "diff --git a/integration.txt b/integration.txt\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/integration.txt\n"
    "@@ -0,0 +1 @@\n"
    "+SLICE-2 wired itself to SLICE-1\n"
)


class FinalIntegratedVerificationTests(PlanSliceExecutionTestsBase):
    """The whole-project gate before delivery (ADR 0074).

    The case that matters here is the one no per-slice check can reach: two
    slices that each pass their own verification in their own worktree, and
    an integrated project that does not work. Crisis Atlas delivered exactly
    that -- a functioning backend and a UI prototype that never called it,
    four green slices, a green delivery.
    """

    def _integration_config(self, *, marker: str = "SLICE-2 wired itself"):
        """Config whose whole-project command inspects the *combined* tree.

        `unit-tests` is the per-slice command and passes for either slice on
        its own. `integration-check` asserts that both slices' contributions
        are present together, which is only ever true at the integrated
        commit -- and which neither slice's isolated worktree could have
        established.

        It is configured `required=False` because `VerificationRunner` runs
        the whole configured set for every task, and an integration check
        cannot succeed inside the worktree of a slice whose counterpart does
        not exist yet. Delivery forces it required for the final run
        (ADR 0074), which is what makes it a gate rather than a suggestion.
        """

        config = self._config()
        return config.model_copy(
            update={
                "verification": config.verification.model_copy(
                    update={
                        "commands": [
                            *config.verification.commands,
                            VerificationCommand(
                                name="integration-check",
                                category="acceptance",
                                argv=[
                                    sys.executable,
                                    "-c",
                                    "import pathlib, sys;"
                                    "downloader = pathlib.Path("
                                    "'src/download_service/downloader.py'"
                                    ").read_text(encoding='utf-8');"
                                    "marker = pathlib.Path('integration.txt');"
                                    "ok = 'get_offset' in downloader and "
                                    "marker.is_file() and "
                                    f"{marker!r} in marker.read_text(encoding='utf-8');"
                                    "sys.exit(0 if ok else 1)",
                                ],
                                timeout_seconds=30,
                                required=False,
                                acceptance=True,
                            ),
                        ]
                    }
                )
            }
        )

    def _integration_plan(self, *, whole_project=("integration-check",)):
        base = make_slice(slice_id="SLICE-1")
        dependent = make_slice(slice_id="SLICE-2", dependencies=["SLICE-1"])
        return make_plan(
            slices=[base, dependent],
            verification_strategy=VerificationStrategy(
                whole_project_verification_commands=list(whole_project),
                acceptance_proof_obligations=[
                    AcceptanceProofObligation(
                        criterion_id="AC-1",
                        proof="The integrated project wires both slices together.",
                        verification_commands=["integration-check"],
                    )
                ],
            ),
        )

    def test_integrated_failure_blocks_delivery_when_every_slice_passed(self) -> None:
        config = self._integration_config()
        record, config = self._approved_plan(
            config=config, plan=self._integration_plan()
        )

        # Both slices complete. Neither contributes `integration.txt`, so
        # each one's own `unit-tests` passes and the combined result does
        # not satisfy the plan's whole-project contract.
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        self._complete_slice_with_patch(record, config, "SLICE-2", _SLICE2_PATCH)
        for slice_id in ("SLICE-1", "SLICE-2"):
            execution = self.slice_store.get(record.plan_id, slice_id)
            assert execution.task_id is not None
            self.assertEqual(
                self.task_store.get_task(execution.task_id).state,
                WorkflowState.COMPLETE,
            )

        with self.assertRaises(SlicePackagingError) as caught:
            prepare_plan_delivery(
                self.root, self.plan_store, self.slice_store, self.task_store,
                record.plan_id, verification_config=config.verification,
            )

        message = str(caught.exception)
        self.assertIn("integrated project", message)
        self.assertIn("integration-check", message)
        self.assertIn("Per-slice verification history is not evidence", message)
        # Fail closed and leave nothing half-done: no ZIP, no delivery
        # record, and the plan is still APPROVED rather than EXECUTED.
        self.assertEqual(
            self.plan_store.get_plan(record.plan_id).status.value, "approved"
        )
        self.assertIsNone(load_plan_delivery(self.root, record.plan_id))
        # The refusal still leaves evidence of why.
        persisted = load_final_project_verification(self.root, record.plan_id)
        assert persisted is not None
        self.assertEqual(persisted.status, FinalVerificationStatus.FAILED)
        self.assertEqual(persisted.executed_command_names, ["integration-check"])
        self.assertEqual(persisted.unproven_criterion_ids(), ["AC-1"])
        self.assertRegex(persisted.final_commit, r"^[0-9a-f]{40}$")
        # The point of the whole gate, stated as an assertion: per-slice
        # state is unanimously COMPLETE and the integrated project is not
        # deliverable. One is not evidence for the other.
        self.assertFalse(persisted.is_sufficient_for_delivery)

    def test_repairing_the_integration_lets_delivery_proceed(self) -> None:
        """The same plan, with the second slice actually wiring itself to
        the first. This is the control for the test above: it establishes
        that the gate blocks on the integration defect rather than on
        something incidental to the fixture."""

        config = self._integration_config()
        record, config = self._approved_plan(
            config=config, plan=self._integration_plan()
        )
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        self._complete_slice_with_patch(
            record, config, "SLICE-2", _INTEGRATION_MARKER_PATCH
        )

        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, verification_config=config.verification,
        )

        final = delivery.final_project_verification
        self.assertEqual(final.status, FinalVerificationStatus.PASSED)
        self.assertEqual(final.final_commit, delivery.final_commit)
        self.assertEqual(final.executed_command_names, ["integration-check"])
        self.assertEqual(final.unproven_criterion_ids(), [])
        self.assertEqual(
            self.plan_store.get_plan(record.plan_id).status.value, "executed"
        )

    def test_final_verification_is_bound_to_the_integrated_commit(self) -> None:
        config = self._integration_config()
        record, config = self._approved_plan(
            config=config, plan=self._integration_plan()
        )
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        self._complete_slice_with_patch(
            record, config, "SLICE-2", _INTEGRATION_MARKER_PATCH
        )
        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, verification_config=config.verification,
        )

        persisted = load_final_project_verification(self.root, record.plan_id)
        assert persisted is not None
        self.assertEqual(persisted.final_commit, delivery.final_commit)
        self.assertEqual(persisted.final_branch, delivery.final_branch)
        self.assertEqual(persisted.worktree_path, delivery.final_worktree_path)
        self.assertRegex(persisted.worktree_fingerprint, r"^[0-9a-f]{64}$")
        # The binding is what makes the record non-transferable: a record
        # for any other commit or fingerprint does not match this state.
        self.assertTrue(
            persisted.matches(
                final_commit=delivery.final_commit,
                worktree_fingerprint=persisted.worktree_fingerprint,
            )
        )
        self.assertFalse(
            persisted.matches(
                final_commit="0" * 40,
                worktree_fingerprint=persisted.worktree_fingerprint,
            )
        )
        self.assertFalse(
            persisted.matches(
                final_commit=delivery.final_commit, worktree_fingerprint="0" * 64
            )
        )

    def test_a_stale_record_is_rerun_rather_than_reused(self) -> None:
        """A record bound to a different commit must not be accepted as
        evidence for this one, and must not deadlock delivery either."""

        config = self._integration_config()
        record, config = self._approved_plan(
            config=config, plan=self._integration_plan()
        )
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        self._complete_slice_with_patch(
            record, config, "SLICE-2", _INTEGRATION_MARKER_PATCH
        )
        # A passing record for some other commit, planted before delivery.
        audit_path = (
            self.root / ".apoapsis" / "plans" / record.plan_id
            / FINAL_VERIFICATION_ARTIFACT
        )
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        planted = FinalProjectVerification(
            plan_id=record.plan_id,
            plan_version=record.version,
            status=FinalVerificationStatus.PASSED,
            final_commit="a" * 40,
            final_branch="apoapsis/elsewhere",
            worktree_path=str(self.root),
            worktree_fingerprint="b" * 64,
            measured_in_task_id="TASK-PLANTED",
            requested_command_names=["integration-check"],
            executed_command_names=["integration-check"],
            reason="planted record for a different commit",
        )
        audit_path.write_text(planted.model_dump_json(indent=2), encoding="utf-8")

        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, verification_config=config.verification,
        )

        final = delivery.final_project_verification
        self.assertNotEqual(final.final_commit, "a" * 40)
        self.assertEqual(final.final_commit, delivery.final_commit)
        self.assertNotEqual(final.measured_in_task_id, "TASK-PLANTED")
        self.assertIsNotNone(final.result)

    def test_a_malformed_record_causes_a_fresh_run(self) -> None:
        config = self._integration_config()
        record, config = self._approved_plan(
            config=config, plan=self._integration_plan()
        )
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        self._complete_slice_with_patch(
            record, config, "SLICE-2", _INTEGRATION_MARKER_PATCH
        )
        audit_path = (
            self.root / ".apoapsis" / "plans" / record.plan_id
            / FINAL_VERIFICATION_ARTIFACT
        )
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text('{"status": "passed"}', encoding="utf-8")
        self.assertIsNone(load_final_project_verification(self.root, record.plan_id))

        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, verification_config=config.verification,
        )
        self.assertEqual(
            delivery.final_project_verification.status,
            FinalVerificationStatus.PASSED,
        )

    def test_a_whole_project_command_missing_from_configuration_blocks(self) -> None:
        """The plan's approved final contract cannot be executed, which is
        not the same thing as it having failed -- and neither is a reason to
        deliver."""

        config = self._integration_config()
        record, config = self._approved_plan(
            config=config, plan=self._integration_plan()
        )
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        self._complete_slice_with_patch(
            record, config, "SLICE-2", _INTEGRATION_MARKER_PATCH
        )
        stripped = config.verification.model_copy(
            update={
                "commands": [
                    item
                    for item in config.verification.commands
                    if item.name != "integration-check"
                ]
            }
        )

        with self.assertRaises(SlicePackagingError) as caught:
            prepare_plan_delivery(
                self.root, self.plan_store, self.slice_store, self.task_store,
                record.plan_id, verification_config=stripped,
            )

        self.assertIn("commands_unavailable", str(caught.exception))
        self.assertEqual(
            self.plan_store.get_plan(record.plan_id).status.value, "approved"
        )
        persisted = load_final_project_verification(self.root, record.plan_id)
        assert persisted is not None
        self.assertEqual(persisted.missing_command_names, ["integration-check"])

    def test_a_plan_with_no_whole_project_command_is_never_sufficient(self) -> None:
        """Such a plan is now invalid and cannot be approved at all
        (`MISSING_WHOLE_PROJECT_VERIFICATION`, covered in
        `test_architect_validation`). This asserts the second line of
        defence: even if one reached delivery -- an approval predating this
        change, say -- the gate refuses it, because per-slice history is not
        evidence about the combined result."""

        config = self._integration_config()
        record, config = self._approved_plan(
            config=config, plan=self._integration_plan()
        )
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        self._complete_slice_with_patch(
            record, config, "SLICE-2", _INTEGRATION_MARKER_PATCH
        )
        execution = self.slice_store.get(record.plan_id, "SLICE-2")
        assert execution.task_id is not None
        worktree = WorktreeManager(self.root).describe(
            execution.task_id.removeprefix("TASK-").lower()
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree.path,
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        verification = run_final_project_verification(
            self.root,
            self._integration_plan(whole_project=()),
            config.verification,
            plan_id=record.plan_id,
            plan_version=record.version,
            final_commit=head,
            final_branch=worktree.branch,
            final_worktree_path=worktree.path,
            measured_in_task_id=execution.task_id,
        )

        self.assertEqual(verification.status, FinalVerificationStatus.NOT_CONFIGURED)
        self.assertFalse(verification.is_sufficient_for_delivery)
        self.assertIsNone(verification.result)
        self.assertIn("nothing has ever been executed", verification.reason)
        # Every acceptance criterion is unproven, because nothing ran.
        self.assertEqual(verification.unproven_criterion_ids(), ["AC-1"])

    def test_delivery_record_keeps_the_two_evidence_sections_apart(self) -> None:
        config = self._integration_config()
        record, config = self._approved_plan(
            config=config, plan=self._integration_plan()
        )
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        self._complete_slice_with_patch(
            record, config, "SLICE-2", _INTEGRATION_MARKER_PATCH
        )
        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, verification_config=config.verification,
        )

        # Per-slice history is per-task and unbound to any integrated
        # state: each entry names its own task, and `VerificationRunner`
        # ran the whole configured set in that slice's isolated worktree.
        # The final section is exactly the plan's whole-project contract,
        # executed once, bound to the integrated commit. The two are
        # separate fields carrying separate claims.
        self.assertEqual(
            [item["slice_id"] for item in delivery.verification_summary],
            ["SLICE-1", "SLICE-2"],
        )
        self.assertEqual(
            {item["task_id"] for item in delivery.verification_summary},
            set(delivery.task_ids),
        )
        self.assertEqual(
            [item["name"] for item in
             delivery.final_project_verification.command_results()],
            ["integration-check"],
        )
        self.assertEqual(
            delivery.final_project_verification.final_commit, delivery.final_commit
        )
        # Nothing in the per-slice section carries a commit or fingerprint
        # binding, which is precisely why it cannot stand in for the other.
        for item in delivery.verification_summary:
            self.assertNotIn("final_commit", item)
            self.assertNotIn("worktree_fingerprint", item)

        handoff = (self.root / delivery.frontier_review_handoff_path).read_text(
            encoding="utf-8"
        )
        self.assertIn("## Per-slice verification history", handoff)
        self.assertIn("## Final integrated-project verification", handoff)
        self.assertIn("is not evidence that the integrated project", handoff)
        self.assertIn(delivery.final_project_verification.worktree_fingerprint, handoff)

        with zipfile.ZipFile(self.root / delivery.archive_path) as archive:
            guide = archive.read(
                "APOAPSIS-USING-THE-FINISHED-PROJECT.md"
            ).decode("utf-8")
        self.assertIn("What was actually verified", guide)
        self.assertIn("integration-check", guide)
        self.assertIn("whole-project verification passed", guide)

    def test_the_record_round_trips_through_delivery_json(self) -> None:
        config = self._integration_config()
        record, config = self._approved_plan(
            config=config, plan=self._integration_plan()
        )
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        self._complete_slice_with_patch(
            record, config, "SLICE-2", _INTEGRATION_MARKER_PATCH
        )
        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, verification_config=config.verification,
        )
        reloaded = load_plan_delivery(self.root, record.plan_id)
        assert reloaded is not None
        self.assertEqual(
            reloaded.final_project_verification, delivery.final_project_verification
        )


class DeliveredOperabilityTests(PlanSliceExecutionTestsBase):
    """ADR 0076. Plan validation says some slice is *responsible* for each
    delivery artifact; delivery says whether it is actually in the shipped
    tree, and the usage guide says whether the launch path was exercised.

    Crisis Atlas shipped a seed README under a delivery contract that named
    one, and the generated guide confidently recommended reading it.
    """

    def test_a_missing_required_artifact_blocks_delivery(self) -> None:
        record, config = self._approved_plan(
            plan=make_plan(
                delivery_contract=PlanDeliveryContract(
                    primary_documentation_path="README.md",
                    launch_not_runnable_reason="Library change; nothing to launch.",
                    required_artifacts=["src/example.py", "OPERATIONS.md"],
                ),
                slices=[
                    make_slice(
                        "SLICE-1",
                        suggested_paths=[
                            "src/example.py",
                            "README.md",
                            "OPERATIONS.md",
                            # Keeps the plan valid under
                            # UNASSIGNED_TEST_DISCOVERY_ROOT; the missing
                            # artifact under test is OPERATIONS.md.
                            "tests/test_example.py",
                        ],
                    )
                ],
            )
        )
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)

        with self.assertRaises(SlicePackagingError) as caught:
            prepare_plan_delivery(
                self.root, self.plan_store, self.slice_store, self.task_store,
                record.plan_id, verification_config=config.verification,
            )

        message = str(caught.exception)
        self.assertIn("OPERATIONS.md", message)
        self.assertIn("does not contain delivery artifact", message)
        self.assertEqual(
            self.plan_store.get_plan(record.plan_id).status.value, "approved"
        )
        self.assertIsNone(load_plan_delivery(self.root, record.plan_id))

    def test_an_unexercised_launch_is_recorded_as_unexercised(self) -> None:
        record, config = self._approved_plan()
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, verification_config=config.verification,
        )

        operability = delivery.operability
        self.assertFalse(operability.launch_measured)
        self.assertIn(
            "no launchable entry point", operability.launch_unmeasured_reason
        )
        self.assertEqual(operability.primary_documentation_path, "README.md")
        self.assertTrue(operability.primary_documentation_present)
        self.assertEqual(operability.missing_artifacts, [])

        with zipfile.ZipFile(self.root / delivery.archive_path) as archive:
            guide = archive.read(
                "APOAPSIS-USING-THE-FINISHED-PROJECT.md"
            ).decode("utf-8")
        self.assertIn("## Install, launch, and test", guide)
        self.assertIn("**Launch was NOT exercised.**", guide)
        self.assertIn("no launchable entry point", guide)
        # The old filename heuristics survive, but demoted and labelled as
        # inference rather than presented as the project's documented path.
        self.assertIn("## If you need more than the above", guide)
        self.assertIn("inferred from filenames", guide)

    def test_an_exercised_launch_says_which_command_ran(self) -> None:
        config = self._config()
        config = config.model_copy(
            update={
                "verification": config.verification.model_copy(
                    update={
                        "commands": [
                            *config.verification.commands,
                            VerificationCommand(
                                name="launch-smoke",
                                category="acceptance",
                                argv=[
                                    sys.executable,
                                    "-c",
                                    "import pathlib, sys;"
                                    "sys.exit(0 if pathlib.Path("
                                    "'src/download_service/downloader.py'"
                                    ").is_file() else 1)",
                                ],
                                timeout_seconds=30,
                                required=False,
                                acceptance=True,
                            ),
                        ]
                    }
                )
            }
        )
        record, config = self._approved_plan(
            config=config,
            plan=make_plan(
                delivery_contract=PlanDeliveryContract(
                    primary_documentation_path="README.md",
                    launch_verification_command="launch-smoke",
                    install_instructions="pip install -e .",
                    launch_or_usage_instructions="python -m download_service",
                    test_instructions="python -m unittest discover -s tests",
                    readiness_checks=["The service answers on its port."],
                ),
                verification_strategy=VerificationStrategy(
                    whole_project_verification_commands=["launch-smoke"],
                ),
            ),
        )
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)

        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, verification_config=config.verification,
        )

        operability = delivery.operability
        self.assertTrue(operability.launch_measured)
        self.assertEqual(operability.launch_command, "launch-smoke")
        self.assertEqual(operability.launch_unmeasured_reason, "")

        with zipfile.ZipFile(self.root / delivery.archive_path) as archive:
            guide = archive.read(
                "APOAPSIS-USING-THE-FINISHED-PROJECT.md"
            ).decode("utf-8")
        self.assertIn("**Launch was exercised.**", guide)
        self.assertIn("`launch-smoke` ran against this exact commit", guide)
        # The plan's own structured instructions are rendered, not inferred.
        self.assertIn("pip install -e .", guide)
        self.assertIn("python -m download_service", guide)
        self.assertIn("The service answers on its port.", guide)
        self.assertIn("it does not execute prose", guide)

    def test_the_operability_record_round_trips(self) -> None:
        record, config = self._approved_plan()
        self._complete_slice_with_patch(record, config, "SLICE-1", COMPLETE_PATCH)
        delivery = prepare_plan_delivery(
            self.root, self.plan_store, self.slice_store, self.task_store,
            record.plan_id, verification_config=config.verification,
        )
        reloaded = load_plan_delivery(self.root, record.plan_id)
        assert reloaded is not None
        self.assertEqual(reloaded.operability, delivery.operability)


if __name__ == "__main__":
    unittest.main()
