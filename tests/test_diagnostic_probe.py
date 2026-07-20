from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from apoapsis.agent.session import (
    AgentTurnRecord,
    BoundedAgentSession,
)
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.audit.store import TaskAuditStore
from apoapsis.cli.app import build_parser
from apoapsis.config import (
    AgentLoopConfig,
    FrontierProviderConfig,
    ProviderPricing,
)
from apoapsis.context.compiler import ContextPackage
from apoapsis.evaluation.diagnostic_probe import (
    AlternateModelSpec,
    DiagnosticProbeError,
    ModelSelection,
    PromptCondition,
    _PROGRESS_ADVISORY_NOTE,
    alternate_model_provider_config,
    progress_advisory_agent_step_prompt,
    run_single_slice_diagnostic_probe,
    summarize_diagnostic_probe,
    validate_single_independent_variable,
    verify_alternate_model_authorized,
)
from apoapsis.evaluation.diagnostic_probe_report import write_diagnostic_probe_report
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.models.prompts import agent_step_prompt
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.reporting.report import TaskOutcome
from apoapsis.specification.schema import AcceptanceCriterion, SourceKind
from apoapsis.verification.runner import VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.vertical_slice import VerticalSliceRunner
from tests.architect_helpers import make_plan, make_slice
from tests.fakes import FakeModelProvider
from tests.test_agent_loop import action
from tests.test_planning_evaluation import (
    _TARGET_JOBS,
    PlanningEvaluationTestsBase,
)


class ProgressAdvisoryPromptTests(unittest.TestCase):
    """Pure, no-I/O checks that the evaluation-only prompt variant is
    strictly additive and never touches the production prompt (ADR 0029)."""

    def _context(self) -> ContextPackage:
        from tests.helpers import make_specification

        specification = make_specification()
        return ContextPackage.specification_only(specification, "0" * 40)

    def test_progress_advisory_prompt_is_the_production_prompt_plus_one_appended_note(
        self,
    ) -> None:
        context = self._context()
        kwargs = dict(
            turn=1,
            remaining_budgets={"turns": 8, "patch_attempts": 3, "verification_runs": 3},
            verification_commands=["unit-tests"],
            history=[],
        )
        base = agent_step_prompt(context, **kwargs)
        variant = progress_advisory_agent_step_prompt(context, **kwargs)
        self.assertTrue(variant.startswith(base))
        self.assertEqual(variant, f"{base}\n{_PROGRESS_ADVISORY_NOTE}\n")

    def test_advisory_note_never_forces_a_specific_action(self) -> None:
        for forbidden in ("\"action\": \"run_check\"", "\"action\":\"run_check\""):
            self.assertNotIn(forbidden, _PROGRESS_ADVISORY_NOTE)
        self.assertIn("advisory only", _PROGRESS_ADVISORY_NOTE)


class SummarizeDiagnosticProbeTests(unittest.TestCase):
    """Pure, no-I/O checks of the deterministic behavior summary."""

    def test_detects_the_d4b_read_loop_pattern(self) -> None:
        records = [
            AgentTurnRecord(
                turn=1, action="read_file", accepted=True,
                summary="read jobs.py:1-30", evidence_ids=["EV-1"],
            ),
            AgentTurnRecord(
                turn=2, action="search_repository", accepted=False,
                summary="repository search failed: [WinError 2]", evidence_ids=[],
            ),
            AgentTurnRecord(
                turn=3, action="read_file", accepted=True,
                summary="read jobs.py:1-30", evidence_ids=[],
            ),
            AgentTurnRecord(
                turn=4, action="inspect_diff", accepted=True,
                summary="current worktree has no diff", evidence_ids=[],
            ),
            AgentTurnRecord(
                turn=5, action="replace_text", accepted=True,
                summary="edit passed policy and was applied in the task worktree",
                evidence_ids=["EV-2"], patch_attempt=1,
            ),
            AgentTurnRecord(
                turn=6, action="read_file", accepted=True,
                summary="read jobs.py:1-30", evidence_ids=["EV-3"],
            ),
            AgentTurnRecord(
                turn=7, action="read_file", accepted=True,
                summary="read jobs.py:1-30", evidence_ids=[],
            ),
            AgentTurnRecord(
                turn=8, action="read_file", accepted=True,
                summary="read jobs.py:1-30", evidence_ids=[],
            ),
        ]
        summary = summarize_diagnostic_probe(
            records,
            outcome=TaskOutcome.HUMAN_REVIEW_REQUIRED,
            stop_reason="agent turn budget exhausted after 8 turns",
            verification_runs=0,
            patch_attempts=1,
        )
        self.assertFalse(summary.invoked_run_check)
        self.assertFalse(summary.invoked_submit_for_verification)
        # Turn 3 repeats turn 1's exact (action, summary) pair.
        self.assertEqual(summary.first_no_progress_turn, 3)
        # Turns 6, 7, 8 share an identical (action, summary) pair.
        self.assertEqual(summary.max_identical_action_streak, 3)
        self.assertEqual(summary.total_turns, 8)
        self.assertEqual(summary.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)

    def test_a_normal_verify_and_complete_session_is_never_flagged_as_no_progress(
        self,
    ) -> None:
        records = [
            AgentTurnRecord(
                turn=1, action="inspect_diff", accepted=True,
                summary="current worktree has no diff", evidence_ids=[],
            ),
            AgentTurnRecord(
                turn=2, action="read_file", accepted=True,
                summary="read x.py:1-20", evidence_ids=["EV-1"],
            ),
            AgentTurnRecord(
                turn=3, action="replace_text", accepted=True,
                summary="edit passed policy and was applied in the task worktree",
                evidence_ids=["EV-2"], patch_attempt=1,
            ),
            AgentTurnRecord(
                turn=4, action="run_check", accepted=True,
                summary="deterministic verification passed", evidence_ids=[],
                verification_run=1,
            ),
            AgentTurnRecord(
                turn=5, action="submit_for_verification", accepted=True,
                summary="deterministic verification passed", evidence_ids=[],
                verification_run=2,
            ),
        ]
        summary = summarize_diagnostic_probe(
            records, outcome=TaskOutcome.COMPLETE, verification_runs=2, patch_attempts=1,
        )
        self.assertTrue(summary.invoked_run_check)
        self.assertTrue(summary.invoked_submit_for_verification)
        self.assertIsNone(summary.first_no_progress_turn)
        self.assertEqual(summary.max_identical_action_streak, 1)

    def test_empty_session_is_well_defined(self) -> None:
        summary = summarize_diagnostic_probe([])
        self.assertEqual(summary.total_turns, 0)
        self.assertFalse(summary.invoked_run_check)
        self.assertIsNone(summary.first_no_progress_turn)
        self.assertEqual(summary.max_identical_action_streak, 0)

    def test_a_fresh_post_edit_reread_with_new_evidence_is_never_no_progress(
        self,
    ) -> None:
        """initial read -> edit -> fresh reread with new evidence ->
        identical reread with no evidence. `(action, summary)` alone
        cannot distinguish the fresh reread (turn 3) from the genuinely
        no-progress repeat (turn 4), since `summary` only encodes the
        path/line-range, not the file's content -- exactly the real
        `read_file` shape observed in the D4b artifacts. Only `evidence_
        ids` distinguishes them, so the fresh reread (which added real new
        evidence after the edit) must never be flagged; only the final,
        genuinely uninformative repeat must be."""

        records = [
            AgentTurnRecord(
                turn=1, action="read_file", accepted=True,
                summary="read jobs.py:1-30", evidence_ids=["EV-1"],
            ),
            AgentTurnRecord(
                turn=2, action="replace_text", accepted=True,
                summary="edit passed policy and was applied in the task worktree",
                evidence_ids=["EV-2"], patch_attempt=1,
            ),
            AgentTurnRecord(
                turn=3, action="read_file", accepted=True,
                summary="read jobs.py:1-30", evidence_ids=["EV-3"],
            ),
            AgentTurnRecord(
                turn=4, action="read_file", accepted=True,
                summary="read jobs.py:1-30", evidence_ids=[],
            ),
        ]
        summary = summarize_diagnostic_probe(records)
        self.assertEqual(
            summary.first_no_progress_turn, 4,
            "the fresh post-edit reread (turn 3, non-empty evidence_ids) "
            "must not be classified as no-progress; only the identical "
            "repeat that added nothing (turn 4) may be",
        )


class AlternateModelAuthorizationTests(unittest.TestCase):
    def test_fails_closed_when_model_was_not_explicitly_authorized(self) -> None:
        def _never_called(base_url: str) -> set[str]:
            raise AssertionError(
                "installed_models must not be queried before authorization"
            )

        with self.assertRaises(DiagnosticProbeError):
            verify_alternate_model_authorized(
                AlternateModelSpec(model="qwen3-coder:30b"),
                base_url="http://127.0.0.1:11434",
                authorized_model_names=frozenset({"gpt-oss:20b"}),
                installed_models=_never_called,
            )

    def test_fails_closed_when_authorized_model_is_not_installed(self) -> None:
        with self.assertRaises(DiagnosticProbeError):
            verify_alternate_model_authorized(
                AlternateModelSpec(model="qwen3-coder:30b"),
                base_url="http://127.0.0.1:11434",
                authorized_model_names=frozenset({"qwen3-coder:30b"}),
                installed_models=lambda base_url: {"gpt-oss:20b"},
            )

    def test_passes_when_authorized_and_installed(self) -> None:
        verify_alternate_model_authorized(
            AlternateModelSpec(model="qwen3-coder:30b"),
            base_url="http://127.0.0.1:11434",
            authorized_model_names=frozenset({"qwen3-coder:30b"}),
            installed_models=lambda base_url: {"qwen3-coder:30b", "gpt-oss:20b"},
        )

    def test_bare_tag_matches_the_installed_latest_tag(self) -> None:
        verify_alternate_model_authorized(
            AlternateModelSpec(model="gpt-oss"),
            base_url="http://127.0.0.1:11434",
            authorized_model_names=frozenset({"gpt-oss"}),
            installed_models=lambda base_url: {"gpt-oss:latest"},
        )

    def test_alternate_model_config_only_changes_the_model_field(self) -> None:
        base = FrontierProviderConfig(
            provider="ollama",
            base_url="http://127.0.0.1:11434",
            model="qwen3-coder-next:q4_K_M",
            temperature=0.0,
            context_window_tokens=65536,
            think=False,
        )
        alternate = alternate_model_provider_config(
            base, AlternateModelSpec(model="qwen3-coder:30b")
        )
        self.assertEqual(alternate.model, "qwen3-coder:30b")
        self.assertEqual(
            alternate.model_copy(update={"model": base.model}), base,
            "every field other than `.model` must be inherited unchanged",
        )


class ValidateSingleIndependentVariableTests(unittest.TestCase):
    """Pure, no-I/O checks of the shared one-independent-variable
    invariant `run_single_slice_diagnostic_probe` enforces first, before
    any filesystem access, installed-model lookup, or provider
    construction."""

    def test_progress_advisory_with_an_explicit_alternate_model_is_rejected(self) -> None:
        # The only forbidden combination (both directions of the
        # invariant collapse to this one 2x2 cell): PROGRESS_ADVISORY
        # requires the project's own model, and an explicit alternate
        # model requires PRODUCTION -- both are violated at once here.
        with self.assertRaises(DiagnosticProbeError):
            validate_single_independent_variable(
                PromptCondition.PROGRESS_ADVISORY,
                ModelSelection(model="qwen3-coder:30b", source="explicit_alternate"),
            )

    def test_production_with_project_local_coder_is_allowed(self) -> None:
        validate_single_independent_variable(
            PromptCondition.PRODUCTION,
            ModelSelection(model="qwen3-coder-next:q4_K_M", source="project_local_coder"),
        )

    def test_production_with_explicit_alternate_is_allowed(self) -> None:
        validate_single_independent_variable(
            PromptCondition.PRODUCTION,
            ModelSelection(model="qwen3-coder:30b", source="explicit_alternate"),
        )

    def test_progress_advisory_with_project_local_coder_is_allowed(self) -> None:
        validate_single_independent_variable(
            PromptCondition.PROGRESS_ADVISORY,
            ModelSelection(model="qwen3-coder-next:q4_K_M", source="project_local_coder"),
        )


class PromptBuilderIsolationTests(unittest.TestCase):
    """Regression coverage (AGENTS.md): the evaluation-only prompt-builder
    injection point must be provably inert unless a caller explicitly
    passes an override. No product call site does."""

    def test_bounded_agent_session_default_matches_production_prompt_byte_for_byte(
        self,
    ) -> None:
        from types import SimpleNamespace

        from tests.helpers import make_specification

        specification = make_specification()
        context = ContextPackage.specification_only(specification, "0" * 40)
        captured: dict[str, str] = {}

        def model_call(operation, prompt, ctx, **kwargs):
            captured["prompt"] = prompt
            return SimpleNamespace(
                content=action("request_escalation", reason="stop immediately")
            )

        import subprocess

        temp_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(temp_root, ignore_errors=True))
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=temp_root, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "tests@example.invalid"],
            cwd=temp_root, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Apoapsis Tests"],
            cwd=temp_root, check=True, capture_output=True,
        )
        (temp_root / ".gitkeep").write_text("", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=temp_root, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "controlled baseline"],
            cwd=temp_root, check=True, capture_output=True,
        )
        session = BoundedAgentSession(
            specification=specification,
            worktree=temp_root,
            initial_context=context,
            context_compiler=_NullContextCompiler(),
            config=AgentLoopConfig(
                max_turns=1, max_patch_attempts=1, max_verification_runs=1,
                max_search_results=5, max_read_lines=50, max_observation_chars=5_000,
            ),
            verification_config=VerificationConfig(commands=[]),
            audit=TaskAuditStore(temp_root, "TASK-ISOLATION"),
            model_call=model_call,
            apply_patch=lambda patch, attempt: None,
        )
        session.run()
        expected = agent_step_prompt(
            context,
            turn=1,
            remaining_budgets=session._remaining_budgets(1),
            verification_commands=[],
            history=[],
        )
        self.assertEqual(captured["prompt"], expected)
        self.assertNotIn("PROGRESS_ADVISORY_NOTE", captured["prompt"])


class VerticalSliceRunnerPromptIsolationTests(PlanningEvaluationTestsBase):
    """Same regression guarantee, one layer up: an ordinary
    `VerticalSliceRunner(...)` construction (no `agent_step_prompt_fn`
    argument at all -- exactly how every product call site and
    `run_monolithic_condition` already construct it) must never emit the
    evaluation-only advisory note."""

    def test_vertical_slice_runner_without_override_never_emits_the_advisory_note(
        self,
    ) -> None:
        fixture = self._fixture_copy("isolation-check")
        config = self._v2_config()
        fake = FakeModelProvider(
            [
                _monolithic_spec_response(),
                action("request_escalation", reason="stop immediately"),
            ]
        )
        _inject_task_id(fake)
        store = SQLiteTaskStore(fixture / ".apoapsis" / "apoapsis.db")
        provider = InstrumentedModelProvider(fake, ProviderPricing())
        # Exactly the ordinary product construction pattern -- no
        # `agent_step_prompt_fn` argument at all.
        runner = VerticalSliceRunner(fixture, store, provider, config)
        runner.run(
            "Add resilient, checksum-verified resumable downloads.\nPreserve the current public API.",
            approve=lambda specification: True,
        )
        agent_step_prompts = [
            call.prompt for call in fake.invocations if "ALLOWED_ACTIONS" in call.prompt
        ]
        self.assertTrue(agent_step_prompts)
        for prompt in agent_step_prompts:
            self.assertNotIn("PROGRESS_ADVISORY_NOTE", prompt)


class _SingleSlicePlanMixin:
    """Shared helpers for building and approving a one-slice plan (the
    smallest possible ADR 0028 plan) -- reused by both the orchestration
    tests and the artifact-reporting tests without one subclassing the
    other's `test_*` methods."""

    def _single_slice_plan(self):
        slices = [
            make_slice(
                slice_id="SLICE-A",
                acceptance_criterion_ids=["AC-JOBS"],
                verification_commands=["v2-jobs-tests"],
                suggested_paths=["src/download_service_v2/jobs.py"],
            )
        ]
        return make_plan(
            slices=slices,
            acceptance_criteria=[
                AcceptanceCriterion(
                    id="AC-JOBS",
                    text="Job records track attempts, progress, checksum, and state.",
                    source=SourceKind.USER,
                    source_reference="idea",
                    verification_method="v2-jobs-tests",
                )
            ],
        )

    def _approve_plan(self, root: Path):
        plan_store = SQLitePlanStore(root / ".apoapsis" / "architect-plans.db")
        task_store = SQLiteTaskStore(root / ".apoapsis" / "apoapsis.db")
        slice_store = PlanSliceExecutionStore(root / ".apoapsis" / "plan-slice-executions.db")
        operation_store = ExecutionOperationStore(root / ".apoapsis" / "execution-operations.db")
        from apoapsis.architect.package import build_planner_request_package
        from apoapsis.architect.validation import validate_plan
        from apoapsis.architect.audit import write_package_artifact
        from apoapsis.architect.schema import PlanValidationResult

        config = self._v2_config()
        package = build_planner_request_package(root, "Add resilient downloads.", config)
        write_package_artifact(root, package)
        plan = self._single_slice_plan()
        record = plan_store.create_plan(
            "PLAN-DIAGPROBE", package.package_id, package.idea_text, plan
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={c.name for c in config.verification.commands},
            ceilings=config.architect.ceilings,
        )
        self.assertFalse(any(f.severity.value == "error" for f in findings))
        result = PlanValidationResult(
            plan_id=record.plan_id, plan_version=record.version, valid=True, findings=findings
        )
        record = plan_store.record_validation(
            record.plan_id, result, expected_version=record.version
        )
        record = plan_store.approve_plan(record.plan_id, expected_version=record.version)
        return record, plan_store, task_store, slice_store, operation_store, config


class RunSingleSliceDiagnosticProbeTests(_SingleSlicePlanMixin, PlanningEvaluationTestsBase):
    def test_production_condition_prompt_never_contains_the_advisory_note(self) -> None:
        root = self._fixture_copy("probe-production")
        record, plan_store, task_store, slice_store, operation_store, config = (
            self._approve_plan(root)
        )
        fake = self._provider(
            [
                action(
                    "replace_text",
                    path="src/download_service_v2/jobs.py",
                    old_text=self._read(root, "src/download_service_v2/jobs.py"),
                    new_text=_TARGET_JOBS,
                ),
                action("run_check", command_name="v2-jobs-tests"),
            ]
        )
        result = run_single_slice_diagnostic_probe(
            root, plan_store, slice_store, task_store, operation_store,
            record.plan_id, "SLICE-A",
            expected_plan_version=record.version,
            config=config,
            provider=fake,
            local_coder_provider=fake,
            prompt_condition=PromptCondition.PRODUCTION,
            model_selection=ModelSelection(model="fake-coder-v1", source="project_local_coder"),
            scenario_id="download-service-v2",
            scenario_version="1.0",
        )
        self.assertEqual(result.prompt_condition, PromptCondition.PRODUCTION)
        self.assertTrue(result.behavior.invoked_run_check)
        self.assertEqual(result.report.outcome, TaskOutcome.COMPLETE)

    def test_progress_advisory_condition_prompts_contain_the_advisory_note(self) -> None:
        root = self._fixture_copy("probe-advisory")
        record, plan_store, task_store, slice_store, operation_store, config = (
            self._approve_plan(root)
        )
        underlying = FakeModelProvider(
            [
                action(
                    "replace_text",
                    path="src/download_service_v2/jobs.py",
                    old_text=self._read(root, "src/download_service_v2/jobs.py"),
                    new_text=_TARGET_JOBS,
                ),
                action("run_check", command_name="v2-jobs-tests"),
            ]
        )
        fake = InstrumentedModelProvider(underlying, ProviderPricing())
        result = run_single_slice_diagnostic_probe(
            root, plan_store, slice_store, task_store, operation_store,
            record.plan_id, "SLICE-A",
            expected_plan_version=record.version,
            config=config,
            provider=fake,
            local_coder_provider=fake,
            prompt_condition=PromptCondition.PROGRESS_ADVISORY,
            model_selection=ModelSelection(model="fake-coder-v1", source="project_local_coder"),
            scenario_id="download-service-v2",
            scenario_version="1.0",
        )
        self.assertEqual(result.prompt_condition, PromptCondition.PROGRESS_ADVISORY)
        self.assertTrue(result.behavior.invoked_run_check)
        agent_prompts = [
            call.prompt for call in underlying.invocations if "ALLOWED_ACTIONS" in call.prompt
        ]
        self.assertTrue(agent_prompts)
        for prompt in agent_prompts:
            self.assertIn("PROGRESS_ADVISORY_NOTE", prompt)

    def test_summary_reports_the_read_loop_when_the_model_never_verifies(self) -> None:
        root = self._fixture_copy("probe-loop")
        record, plan_store, task_store, slice_store, operation_store, config = (
            self._approve_plan(root)
        )
        read_action = action(
            "read_file", path="src/download_service_v2/jobs.py", start_line=1, end_line=30
        )
        fake = self._provider(
            [
                read_action,
                action(
                    "replace_text",
                    path="src/download_service_v2/jobs.py",
                    old_text=self._read(root, "src/download_service_v2/jobs.py"),
                    new_text=_TARGET_JOBS,
                ),
                read_action,
                read_action,
                read_action,
                read_action,
                read_action,
                read_action,
            ]
        )
        small_config = config.model_copy(
            update={
                "execution": config.execution.model_copy(
                    update={
                        "agent": config.execution.agent.model_copy(
                            update={"max_turns": 8}
                        )
                    }
                )
            }
        )
        result = run_single_slice_diagnostic_probe(
            root, plan_store, slice_store, task_store, operation_store,
            record.plan_id, "SLICE-A",
            expected_plan_version=record.version,
            config=small_config,
            provider=fake,
            local_coder_provider=fake,
            prompt_condition=PromptCondition.PRODUCTION,
            model_selection=ModelSelection(model="fake-coder-v1", source="project_local_coder"),
            scenario_id="download-service-v2",
            scenario_version="1.0",
        )
        self.assertFalse(result.behavior.invoked_run_check)
        self.assertFalse(result.behavior.invoked_submit_for_verification)
        self.assertIsNotNone(result.behavior.first_no_progress_turn)
        self.assertGreaterEqual(result.behavior.max_identical_action_streak, 4)
        self.assertEqual(result.report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)


class DiagnosticProbeReportTests(_SingleSlicePlanMixin, PlanningEvaluationTestsBase):
    def _run_probe(self, name: str, *, prompt_condition, model_selection):
        root = self._fixture_copy(name)
        record, plan_store, task_store, slice_store, operation_store, config = (
            self._approve_plan(root)
        )
        fake = self._provider(
            [
                action(
                    "replace_text",
                    path="src/download_service_v2/jobs.py",
                    old_text=self._read(root, "src/download_service_v2/jobs.py"),
                    new_text=_TARGET_JOBS,
                ),
                action("run_check", command_name="v2-jobs-tests"),
            ]
        )
        return run_single_slice_diagnostic_probe(
            root, plan_store, slice_store, task_store, operation_store,
            record.plan_id, "SLICE-A",
            expected_plan_version=record.version,
            config=config,
            provider=fake,
            local_coder_provider=fake,
            prompt_condition=prompt_condition,
            model_selection=model_selection,
            scenario_id="download-service-v2",
            scenario_version="1.0",
        )

    def test_artifact_records_progress_advisory_with_the_project_model_explicitly(
        self,
    ) -> None:
        # The only valid combination varying the prompt: PROGRESS_ADVISORY
        # must run against the project's own configured model.
        result = self._run_probe(
            "probe-report-advisory",
            prompt_condition=PromptCondition.PROGRESS_ADVISORY,
            model_selection=ModelSelection(
                model="fake-coder-v1", source="project_local_coder"
            ),
        )
        output_dir = self.output_root / "probe-report-advisory-output"
        write_diagnostic_probe_report(output_dir, result)

        persisted = json.loads((output_dir / "diagnostic-probe.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["prompt_condition"], "progress_advisory")
        self.assertEqual(persisted["model"]["model"], "fake-coder-v1")
        self.assertEqual(persisted["model"]["source"], "project_local_coder")

        markdown = (output_dir / "diagnostic-probe.md").read_text(encoding="utf-8")
        self.assertIn("progress_advisory", markdown)
        self.assertIn("fake-coder-v1", markdown)
        self.assertIn("project_local_coder", markdown)

    def test_artifact_records_production_with_an_explicit_alternate_model_explicitly(
        self,
    ) -> None:
        # The only valid combination varying the model: an explicit
        # alternate model must run under the unmodified production prompt.
        result = self._run_probe(
            "probe-report-alternate",
            prompt_condition=PromptCondition.PRODUCTION,
            model_selection=ModelSelection(
                model="qwen3-coder:30b", source="explicit_alternate"
            ),
        )
        output_dir = self.output_root / "probe-report-alternate-output"
        write_diagnostic_probe_report(output_dir, result)

        persisted = json.loads((output_dir / "diagnostic-probe.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["prompt_condition"], "production")
        self.assertEqual(persisted["model"]["model"], "qwen3-coder:30b")
        self.assertEqual(persisted["model"]["source"], "explicit_alternate")

        markdown = (output_dir / "diagnostic-probe.md").read_text(encoding="utf-8")
        self.assertIn("production", markdown)
        self.assertIn("qwen3-coder:30b", markdown)
        self.assertIn("explicit_alternate", markdown)

    def test_progress_advisory_with_an_explicit_alternate_model_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(DiagnosticProbeError):
            self._run_probe(
                "probe-report-invalid-combo",
                prompt_condition=PromptCondition.PROGRESS_ADVISORY,
                model_selection=ModelSelection(
                    model="qwen3-coder:30b", source="explicit_alternate"
                ),
            )


class EvalPlanningProbeCliTests(unittest.TestCase):
    def test_parser_accepts_the_expected_arguments(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "eval-planning-probe",
                "download-service-v2",
                "--plan-id", "PLAN-X",
                "--expected-plan-version", "3",
                "--planned-project-root", ".",
                "--slice-id", "SLICE-JOBS-001",
                "--prompt-condition", "progress_advisory",
            ]
        )
        self.assertEqual(args.command, "eval-planning-probe")
        self.assertEqual(args.prompt_condition, "progress_advisory")
        self.assertIsNone(args.alternate_model)
        self.assertFalse(hasattr(args, "context_profile"))

    def test_parser_rejects_context_profile(self) -> None:
        # Removed deliberately (experiment-integrity correction, ADR 0029):
        # this narrowly scoped command must always inherit the project's
        # baseline configuration unchanged, never a second, unrecorded
        # independent variable.
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "eval-planning-probe",
                    "download-service-v2",
                    "--plan-id", "PLAN-X",
                    "--expected-plan-version", "3",
                    "--planned-project-root", ".",
                    "--slice-id", "SLICE-JOBS-001",
                    "--prompt-condition", "production",
                    "--context-profile", "64k",
                ]
            )

    def test_mismatched_authorization_fails_closed_before_any_filesystem_access(
        self,
    ) -> None:
        from apoapsis.cli.app import _eval_planning_probe

        with self.assertRaises(DiagnosticProbeError):
            _eval_planning_probe(
                Path("/definitely/does/not/exist"),
                "PLAN-X",
                1,
                Path("/definitely/does/not/exist/either"),
                "SLICE-A",
                "production",
                "qwen3-coder:30b",
                "a-different-name",
                None,
            )

    def test_alternate_model_with_progress_advisory_fails_closed_before_any_filesystem_access(
        self,
    ) -> None:
        from apoapsis.cli.app import _eval_planning_probe

        with self.assertRaises(DiagnosticProbeError):
            _eval_planning_probe(
                Path("/definitely/does/not/exist"),
                "PLAN-X",
                1,
                Path("/definitely/does/not/exist/either"),
                "SLICE-A",
                "progress_advisory",
                "qwen3-coder:30b",
                "qwen3-coder:30b",
                None,
            )


class EvalPlanningProbeSameModelRejectionTests(unittest.TestCase):
    """Requires a real initialized project (to know the actual configured
    coding model), so kept separate from the pure, zero-filesystem CLI
    checks above."""

    def setUp(self) -> None:
        import contextlib
        import io
        import subprocess

        from apoapsis.cli.app import main as cli_main

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.root, check=True, capture_output=True, text=True,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli_main(["--project-root", str(self.root), "init"])

    def test_alternate_model_identical_to_the_configured_model_is_rejected(
        self,
    ) -> None:
        from apoapsis.cli.app import _eval_planning_probe

        disposable_project = self.root / "disposable-project"
        disposable_project.mkdir()
        with self.assertRaises(DiagnosticProbeError):
            _eval_planning_probe(
                self.root,
                "PLAN-X",
                1,
                disposable_project,
                "SLICE-A",
                "production",
                "qwen3-coder-next:q4_K_M",
                "qwen3-coder-next:q4_K_M",
                None,
            )


class _NullContextCompiler:
    def compile(self, *args, **kwargs):
        raise AssertionError("no failure-driven recompilation expected in this test")


def _monolithic_spec_response() -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "task_id": "TASK-PLACEHOLDER",
            "objective": {
                "text": "Add resilient, checksum-verified resumable downloads.",
                "source": "user",
                "source_reference": "cli-request",
            },
            "acceptance_criteria": [
                {
                    "id": "AC-JOBS",
                    "text": "Job records track attempts, progress, checksum, and state.",
                    "source": "derived",
                    "source_reference": "cli-request",
                    "status": "active",
                    "verification_method": "v2-jobs-tests",
                }
            ],
            "hard_constraints": [
                {
                    "id": "HC-1",
                    "text": "Keep the public API unchanged.",
                    "verbatim_source": "Preserve the current public API.",
                    "interpreted_meaning": "Do not change public signatures.",
                    "source": "user",
                    "source_reference": "cli-request",
                    "scope": "task",
                    "status": "active",
                    "verification_method": "v2-service-tests",
                }
            ],
            "risk_level": "unclassified",
        }
    )


def _inject_task_id(fake: FakeModelProvider) -> None:
    original_complete = fake.complete

    def complete(invocation):
        output = original_complete(invocation)
        if 'task_id to "' in invocation.prompt:
            task_id = invocation.prompt.split('task_id to "', 1)[1].split('"', 1)[0]
            raw = json.loads(output.content)
            raw["task_id"] = task_id
            return output.model_copy(update={"content": json.dumps(raw)})
        return output

    fake.complete = complete  # type: ignore[method-assign]


if __name__ == "__main__":
    unittest.main()
