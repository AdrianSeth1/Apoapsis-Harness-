from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from apoapsis.cli.app import _aggregate_eval_reports, _eval_download_service
from apoapsis.config import (
    AgentLoopConfig,
    AgentRoute,
    CompletionPolicy,
    ContextCompilerConfig,
    ExecutionConfig,
    ExecutionMode,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ApoapsisConfig,
)
from apoapsis.evaluation.fixture import prepare_fixture_repository
from apoapsis.evaluation.harness import run_eval_lane
from apoapsis.evaluation.lanes import apply_lane_overlay, requires_frontier_coder
from apoapsis.evaluation.aggregate import aggregate_evaluations
from apoapsis.evaluation.oracle import HeldOutOracleDefinition
from apoapsis.evaluation.report import (
    render_aggregate_markdown,
    render_markdown,
    write_aggregate,
    write_comparison,
)
from apoapsis.evaluation.schemas import (
    EvalComparisonReport,
    EvalEvidenceKind,
    EvalLane,
    EvalLaneResult,
    HeldOutOracleResult,
    MetricStatus,
    OracleStatus,
)
from apoapsis.models.base import ModelOperation
from apoapsis.models.provider import ModelRole
from apoapsis.models.telemetry import ProviderCallTelemetry
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.reporting.report import FinalTaskReport, TaskOutcome
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from tests.fakes import FakeModelProvider
from tests.test_vertical_slice import (
    COMPLETE_PATCH,
    IMPLEMENTATION_PATCH,
    REPAIR_PATCH,
    REQUEST,
    specification_response,
)

_FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "download-service"
_HOLDOUT = _FIXTURE / "tests" / "test_resumable_acceptance.py"


def action(name: str, **values: object) -> str:
    return json.dumps({"action": name, **values})


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


def _base_config(
    *,
    route: AgentRoute = AgentRoute.LOCAL_ONLY,
    frontier_coder: FrontierProviderConfig | None = None,
    frontier_turns: int = 8,
) -> ApoapsisConfig:
    return ApoapsisConfig(
        models=ModelsConfig(
            frontier=FrontierProviderConfig(
                base_url="https://provider.invalid/v1",
                model="fake-coder-v1",
            ),
            frontier_coder=frontier_coder,
        ),
        execution=ExecutionConfig(
            mode=ExecutionMode.AGENT,
            route=route,
            agent=AgentLoopConfig(
                max_turns=8,
                max_patch_attempts=3,
                max_verification_runs=3,
                max_search_results=10,
                max_read_lines=120,
                max_observation_chars=20_000,
            ),
            frontier_agent=AgentLoopConfig(
                max_turns=frontier_turns,
                max_patch_attempts=3,
                max_verification_runs=3,
                max_search_results=10,
                max_read_lines=120,
                max_observation_chars=20_000,
            ),
        ),
        context=ContextCompilerConfig(
            max_files=10, max_excerpt_lines=200, max_total_chars=50_000
        ),
        patch=PatchPolicyConfig(max_changed_lines=100),
        verification=VerificationConfig(
            commands=[
                VerificationCommand(
                    name="download-tests",
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


class LaneOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _base_config(route=AgentRoute.AUTO)

    def test_requires_frontier_coder_mapping(self) -> None:
        self.assertFalse(requires_frontier_coder(EvalLane.LOCAL))
        self.assertFalse(requires_frontier_coder(EvalLane.ONE_SHOT))
        self.assertTrue(requires_frontier_coder(EvalLane.HYBRID))
        self.assertTrue(requires_frontier_coder(EvalLane.FORCED_ESCALATION))
        self.assertTrue(requires_frontier_coder(EvalLane.FRONTIER))

    def test_local_lane_overlay(self) -> None:
        overlay = apply_lane_overlay(self.config, EvalLane.LOCAL)
        self.assertEqual(overlay.execution.mode, ExecutionMode.AGENT)
        self.assertEqual(overlay.execution.route, AgentRoute.LOCAL_ONLY)
        self.assertEqual(overlay.execution.agent.max_turns, 8)

    def test_frontier_lane_overlay(self) -> None:
        overlay = apply_lane_overlay(self.config, EvalLane.FRONTIER)
        self.assertEqual(overlay.execution.mode, ExecutionMode.AGENT)
        self.assertEqual(overlay.execution.route, AgentRoute.FRONTIER_ONLY)

    def test_hybrid_lane_overlay_keeps_natural_budget(self) -> None:
        overlay = apply_lane_overlay(self.config, EvalLane.HYBRID)
        self.assertEqual(overlay.execution.mode, ExecutionMode.AGENT)
        self.assertEqual(overlay.execution.route, AgentRoute.LOCAL_THEN_FRONTIER)
        self.assertEqual(overlay.execution.agent.max_turns, 8)

    def test_forced_escalation_lane_overlay_constrains_local_budget(self) -> None:
        overlay = apply_lane_overlay(self.config, EvalLane.FORCED_ESCALATION)
        self.assertEqual(overlay.execution.mode, ExecutionMode.AGENT)
        self.assertEqual(overlay.execution.route, AgentRoute.LOCAL_THEN_FRONTIER)
        self.assertEqual(overlay.execution.agent.max_turns, 1)
        self.assertEqual(overlay.execution.agent.max_patch_attempts, 1)
        self.assertEqual(overlay.execution.agent.max_verification_runs, 1)
        # only the local budget is constrained; the frontier budget is untouched.
        self.assertEqual(overlay.execution.frontier_agent.max_turns, 8)

    def test_one_shot_lane_overlay(self) -> None:
        overlay = apply_lane_overlay(self.config, EvalLane.ONE_SHOT)
        self.assertEqual(overlay.execution.mode, ExecutionMode.ONE_SHOT)

    def test_overlay_never_mutates_models(self) -> None:
        for lane in EvalLane:
            overlay = apply_lane_overlay(self.config, lane)
            self.assertEqual(overlay.models, self.config.models)

    def test_every_lane_explicitly_selects_baseline_even_from_a_strict_project(
        self,
    ) -> None:
        # Ordinary product configuration now defaults to STRICT (ADR 0016).
        # A real caller's `.apoapsis/config.toml` may therefore already be
        # STRICT; every evaluation lane must still explicitly force
        # BASELINE so historical false-success measurement stays
        # comparable, not silently inherit whatever the project selected.
        strict_execution = self.config.execution.model_copy(
            update={"completion_policy": CompletionPolicy.STRICT}
        )
        strict_config = self.config.model_copy(
            update={"execution": strict_execution}
        )
        self.assertEqual(
            strict_config.execution.completion_policy, CompletionPolicy.STRICT
        )
        for lane in EvalLane:
            overlay = apply_lane_overlay(strict_config, lane)
            self.assertEqual(
                overlay.execution.completion_policy,
                CompletionPolicy.BASELINE,
                f"lane {lane.value} must explicitly select BASELINE",
            )


class FixtureRepositoryTests(unittest.TestCase):
    def test_prepares_isolated_committed_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "copy"
            prepare_fixture_repository(_FIXTURE, destination)
            self.assertTrue((destination / ".git").is_dir())
            downloader = destination / "src" / "download_service" / "downloader.py"
            original_text = downloader.read_text(encoding="utf-8")
            downloader.write_text(original_text + "\n# mutated\n", encoding="utf-8")
            source_text = (
                _FIXTURE / "src" / "download_service" / "downloader.py"
            ).read_text(encoding="utf-8")
            self.assertNotIn("# mutated", source_text)

    def test_refuses_to_overwrite_an_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "copy"
            destination.mkdir()
            with self.assertRaises(FileExistsError):
                prepare_fixture_repository(_FIXTURE, destination)

    def test_can_exclude_held_out_file_without_mutating_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "copy"
            prepare_fixture_repository(
                _FIXTURE,
                destination,
                excluded_relative_files=["tests/test_resumable_acceptance.py"],
            )
            self.assertFalse(
                (destination / "tests" / "test_resumable_acceptance.py").exists()
            )
            self.assertTrue(_HOLDOUT.is_file())
            with self.assertRaises(ValueError):
                prepare_fixture_repository(
                    _FIXTURE,
                    Path(tmp) / "unsafe",
                    excluded_relative_files=["../outside.py"],
                )
            self.assertFalse((Path(tmp) / "unsafe").exists())


class RunEvalLaneIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.output_root = Path(self.temporary_directory.name)

    def _fixture(self, name: str) -> Path:
        destination = self.output_root / name / "download-service"
        prepare_fixture_repository(_FIXTURE, destination)
        return destination

    def _fixture_without_holdout(self, name: str) -> Path:
        destination = self.output_root / name / "download-service"
        prepare_fixture_repository(
            _FIXTURE,
            destination,
            excluded_relative_files=["tests/test_resumable_acceptance.py"],
        )
        return destination

    @staticmethod
    def _oracle() -> HeldOutOracleDefinition:
        return HeldOutOracleDefinition(
            oracle_id="download-service-resumable-v1",
            version="1.0",
            source_path=_HOLDOUT,
            withheld_relative_path="tests/test_resumable_acceptance.py",
        )

    def test_local_lane_completes_after_one_repair(self) -> None:
        fake = FakeModelProvider(
            [
                specification_response(),
                action("propose_patch", unified_diff=IMPLEMENTATION_PATCH),
                action("run_check", command_name="download-tests"),
                action(
                    "replace_text",
                    path="src/download_service/downloader.py",
                    old_text=(
                        '        mode = "ab" if offset else "wb"\n'
                        "        downloaded = offset"
                    ),
                    new_text=(
                        "        should_append = offset > 0 and "
                        "response.status_code == 206\n"
                        '        mode = "ab" if should_append else "wb"\n'
                        "        downloaded = offset if should_append else 0"
                    ),
                ),
                action("run_check", command_name="download-tests"),
            ]
        )
        _inject_task_id(fake)
        result = run_eval_lane(
            self._fixture("local"),
            EvalLane.LOCAL,
            _base_config(),
            InstrumentedModelProvider(fake),
            task_text=REQUEST,
        )
        self.assertFalse(result.skipped)
        self.assertEqual(result.report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(result.report.agent_route, AgentRoute.LOCAL_ONLY)
        self.assertGreater(result.duration_seconds, 0)
        self.assertTrue(
            (Path(result.fixture_path) / ".apoapsis" / "effective-config.json").is_file()
        )

    def test_local_lane_without_frontier_requires_human_review(self) -> None:
        fake = FakeModelProvider(
            [
                specification_response(),
                action(
                    "request_escalation",
                    reason="The change requires an unapproved dependency.",
                ),
            ]
        )
        _inject_task_id(fake)
        result = run_eval_lane(
            self._fixture("local-escalates"),
            EvalLane.LOCAL,
            _base_config(),
            InstrumentedModelProvider(fake),
            task_text=REQUEST,
        )
        self.assertEqual(result.report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)

    def test_frontier_lane_completes(self) -> None:
        frontier_config = FrontierProviderConfig(
            base_url="https://frontier.invalid/v1", model="big-coder-v1"
        )
        specification_provider = FakeModelProvider([specification_response()])
        frontier = FakeModelProvider(
            [
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
            ],
            provider_name="fake_hosted",
            model_name="big-coder-v1",
        )
        _inject_task_id(specification_provider)
        result = run_eval_lane(
            self._fixture("frontier"),
            EvalLane.FRONTIER,
            _base_config(frontier_coder=frontier_config),
            InstrumentedModelProvider(specification_provider),
            frontier_coder_provider=InstrumentedModelProvider(frontier),
            task_text=REQUEST,
        )
        self.assertEqual(result.report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(result.report.agent_route, AgentRoute.FRONTIER_ONLY)
        self.assertEqual(result.report.local_agent_turns, 0)

    def test_hybrid_lane_escalates_naturally_and_completes_on_frontier(self) -> None:
        frontier_config = FrontierProviderConfig(
            base_url="https://frontier.invalid/v1", model="big-coder-v1"
        )
        local = FakeModelProvider(
            [
                specification_response(),
                action("propose_patch", unified_diff=IMPLEMENTATION_PATCH),
                action("run_check", command_name="download-tests"),
                action(
                    "request_escalation",
                    reason="The remaining server-ignore failure needs review.",
                ),
            ]
        )
        frontier = FakeModelProvider(
            [
                action(
                    "replace_text",
                    path="src/download_service/downloader.py",
                    old_text=(
                        '        mode = "ab" if offset else "wb"\n'
                        "        downloaded = offset"
                    ),
                    new_text=(
                        "        should_append = offset > 0 and "
                        "response.status_code == 206\n"
                        '        mode = "ab" if should_append else "wb"\n'
                        "        downloaded = offset if should_append else 0"
                    ),
                ),
                action("run_check", command_name="download-tests"),
            ],
            provider_name="fake_hosted",
            model_name="big-coder-v1",
        )
        _inject_task_id(local)
        fixture_root = self._fixture("hybrid")
        result = run_eval_lane(
            fixture_root,
            EvalLane.HYBRID,
            _base_config(frontier_coder=frontier_config),
            InstrumentedModelProvider(local),
            frontier_coder_provider=InstrumentedModelProvider(frontier),
            task_text=REQUEST,
        )
        self.assertEqual(result.report.outcome, TaskOutcome.COMPLETE)
        self.assertTrue(result.report.escalation_triggered)
        self.assertEqual(result.report.local_agent_turns, 3)
        self.assertEqual(result.report.frontier_agent_turns, 2)
        package = json.loads(
            (
                fixture_root
                / ".apoapsis"
                / "tasks"
                / result.report.task_id
                / "frontier-escalation-package.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(package["frontier_model"], "big-coder-v1")

    def test_forced_escalation_lane_hands_off_after_one_local_turn(self) -> None:
        frontier_config = FrontierProviderConfig(
            base_url="https://frontier.invalid/v1", model="big-coder-v1"
        )
        local = FakeModelProvider(
            [
                specification_response(),
                action("search_repository", query="get_offset"),
            ]
        )
        frontier = FakeModelProvider(
            [
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
            ],
            provider_name="fake_hosted",
            model_name="big-coder-v1",
        )
        _inject_task_id(local)
        fixture_root = self._fixture("forced-escalation")
        result = run_eval_lane(
            fixture_root,
            EvalLane.FORCED_ESCALATION,
            _base_config(frontier_coder=frontier_config),
            InstrumentedModelProvider(local),
            frontier_coder_provider=InstrumentedModelProvider(frontier),
            task_text=REQUEST,
        )
        # the local stage never touched the task or the patch; it was only
        # ever given a one-turn budget, so it hands off immediately.
        self.assertEqual(result.report.local_agent_turns, 1)
        self.assertEqual(result.report.agent_patch_attempts, 1)
        self.assertEqual(result.report.outcome, TaskOutcome.COMPLETE)
        self.assertTrue(result.report.escalation_triggered)
        self.assertIn("turn budget exhausted", result.report.escalation_reason or "")

    def test_forced_escalation_lane_requires_human_review_when_frontier_also_fails(
        self,
    ) -> None:
        frontier_config = FrontierProviderConfig(
            base_url="https://frontier.invalid/v1", model="big-coder-v1"
        )
        local = FakeModelProvider(
            [specification_response(), action("search_repository", query="x")]
        )
        frontier = FakeModelProvider(
            [json.dumps({}), json.dumps({})],
            provider_name="fake_hosted",
            model_name="big-coder-v1",
        )
        _inject_task_id(local)
        result = run_eval_lane(
            self._fixture("forced-escalation-fails"),
            EvalLane.FORCED_ESCALATION,
            _base_config(frontier_coder=frontier_config, frontier_turns=2),
            InstrumentedModelProvider(local),
            frontier_coder_provider=InstrumentedModelProvider(frontier),
            task_text=REQUEST,
        )
        self.assertEqual(result.report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)

    def test_one_shot_lane_completes_immediately(self) -> None:
        fake = FakeModelProvider([specification_response(), COMPLETE_PATCH])
        _inject_task_id(fake)
        result = run_eval_lane(
            self._fixture("one-shot"),
            EvalLane.ONE_SHOT,
            _base_config(),
            InstrumentedModelProvider(fake),
            task_text=REQUEST,
        )
        self.assertEqual(result.report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(result.report.execution_mode, ExecutionMode.ONE_SHOT)
        self.assertEqual(result.report.number_of_calls, 2)

    def test_one_shot_lane_completes_after_its_one_repair(self) -> None:
        fake = FakeModelProvider(
            [specification_response(), IMPLEMENTATION_PATCH, REPAIR_PATCH]
        )
        _inject_task_id(fake)
        result = run_eval_lane(
            self._fixture("one-shot-repair"),
            EvalLane.ONE_SHOT,
            _base_config(),
            InstrumentedModelProvider(fake),
            task_text=REQUEST,
        )
        self.assertEqual(result.report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(result.report.number_of_calls, 3)

    def test_held_out_oracle_passes_without_entering_model_context(self) -> None:
        fake = FakeModelProvider(
            [
                specification_response(),
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
            ]
        )
        _inject_task_id(fake)
        fixture = self._fixture_without_holdout("oracle-pass")

        result = run_eval_lane(
            fixture,
            EvalLane.LOCAL,
            _base_config(),
            InstrumentedModelProvider(fake),
            task_text=REQUEST,
            held_out_oracle=self._oracle(),
        )

        self.assertEqual(result.report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(result.held_out_oracle.status, OracleStatus.PASSED)
        self.assertTrue(result.held_out_oracle.audit_artifact)
        self.assertFalse(
            any(
                "test_server_ignores_range" in invocation.prompt
                or "test_resumable_acceptance.py" in invocation.prompt
                for invocation in fake.invocations
            )
        )
        self.assertFalse(
            any(Path(result.report.worktree_path).glob(".apoapsis_holdout_*.py"))
        )
        audit = (
            fixture
            / ".apoapsis"
            / "tasks"
            / result.report.task_id
            / "held-out-oracle.json"
        )
        self.assertTrue(audit.is_file())

    def test_held_out_oracle_detects_false_success(self) -> None:
        fake = FakeModelProvider(
            [
                specification_response(),
                action("propose_patch", unified_diff=IMPLEMENTATION_PATCH),
                action("run_check", command_name="download-tests"),
            ]
        )
        _inject_task_id(fake)

        result = run_eval_lane(
            self._fixture_without_holdout("oracle-fail"),
            EvalLane.LOCAL,
            _base_config(),
            InstrumentedModelProvider(fake),
            task_text=REQUEST,
            held_out_oracle=self._oracle(),
        )

        self.assertEqual(result.report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(result.held_out_oracle.status, OracleStatus.FAILED)

    def test_oracle_must_be_absent_before_any_model_call(self) -> None:
        fake = FakeModelProvider([specification_response()])
        fixture = self._fixture("oracle-visible-rejected")

        with self.assertRaisesRegex(ValueError, "agent-visible"):
            run_eval_lane(
                fixture,
                EvalLane.LOCAL,
                _base_config(),
                InstrumentedModelProvider(fake),
                task_text=REQUEST,
                held_out_oracle=self._oracle(),
            )

        self.assertEqual(fake.invocations, [])


class ComparisonReportTests(unittest.TestCase):
    def test_render_and_write_round_trip(self) -> None:
        report = EvalComparisonReport(
            run_id="EVAL-TEST",
            fixture_source="examples/download-service",
            task_text=REQUEST,
            lanes=[
                EvalLaneResult(
                    lane=EvalLane.HYBRID,
                    skipped=True,
                    skip_reason="frontier_coder not configured",
                )
            ],
        )
        markdown = render_markdown(report)
        self.assertIn("EVAL-TEST", markdown)
        self.assertIn("hybrid", markdown)
        self.assertIn("skipped", markdown)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            write_comparison(output_dir, report)
            self.assertTrue((output_dir / "comparison.json").is_file())
            self.assertTrue((output_dir / "comparison.md").is_file())
            round_tripped = EvalComparisonReport.model_validate(
                json.loads((output_dir / "comparison.json").read_text(encoding="utf-8"))
            )
            self.assertEqual(round_tripped.run_id, "EVAL-TEST")


class AggregateReportTests(unittest.TestCase):
    @staticmethod
    def _telemetry(
        *, input_tokens: int = 100, output_tokens: int = 20, cost: float = 1.0
    ) -> ProviderCallTelemetry:
        now = datetime.now(timezone.utc)
        return ProviderCallTelemetry(
            request_id="MRQ-EVAL",
            response_id="MRS-EVAL",
            operation=ModelOperation.AGENT_STEP,
            role=ModelRole.FRONTIER_CODING_AGENT,
            provider="synthetic-hosted",
            model="synthetic-frontier",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=0,
            cache_hit=False,
            estimated_cost_usd=cost,
            started_at=now,
            finished_at=now,
            latency_seconds=1.0,
        )

    @staticmethod
    def _report(
        outcome: TaskOutcome,
        *,
        calls: list[ProviderCallTelemetry] | None = None,
        escalation: bool = False,
        latency: float = 2.0,
    ) -> FinalTaskReport:
        calls = calls or []
        return FinalTaskReport(
            task_id="TASK-AGGREGATE",
            outcome=outcome,
            escalation_triggered=escalation,
            provider_calls=calls,
            number_of_calls=len(calls),
            input_tokens=sum(item.input_tokens for item in calls),
            output_tokens=sum(item.output_tokens for item in calls),
            cached_input_tokens=sum(item.cached_input_tokens for item in calls),
            estimated_cost_usd=sum(item.estimated_cost_usd for item in calls),
            latency_seconds=latency,
            transmitted_files=3,
            transmitted_lines=30,
        )

    @staticmethod
    def _oracle(status: OracleStatus) -> HeldOutOracleResult:
        return HeldOutOracleResult(
            oracle_id="oracle",
            oracle_version="1",
            source_sha256="a" * 64,
            status=status,
        )

    def test_aggregate_formulas_and_hosted_pairing(self) -> None:
        direct_call = self._telemetry(input_tokens=1000, output_tokens=200, cost=2.0)
        hybrid_call = self._telemetry(input_tokens=400, output_tokens=80, cost=0.8)
        comparison = EvalComparisonReport(
            run_id="EVAL-SYNTHETIC",
            fixture_source="fixture",
            task_text=REQUEST,
            context_profile="64k",
            lanes=[
                EvalLaneResult(
                    lane=EvalLane.LOCAL,
                    report=self._report(TaskOutcome.COMPLETE, latency=2),
                    evidence_kind=EvalEvidenceKind.LIVE_LOCAL,
                    patch_attempts=2,
                    unsafe_patch_rejections=1,
                    held_out_oracle=self._oracle(OracleStatus.FAILED),
                ),
                EvalLaneResult(
                    lane=EvalLane.HYBRID,
                    report=self._report(
                        TaskOutcome.COMPLETE,
                        calls=[hybrid_call],
                        escalation=True,
                        latency=4,
                    ),
                    evidence_kind=EvalEvidenceKind.LIVE_HOSTED,
                    patch_attempts=1,
                ),
                EvalLaneResult(
                    lane=EvalLane.FRONTIER,
                    report=self._report(
                        TaskOutcome.COMPLETE, calls=[direct_call], latency=5
                    ),
                    evidence_kind=EvalEvidenceKind.LIVE_HOSTED,
                ),
                EvalLaneResult(
                    lane=EvalLane.ONE_SHOT,
                    report=self._report(TaskOutcome.FAILED, latency=1),
                    evidence_kind=EvalEvidenceKind.LIVE_LOCAL,
                ),
            ],
        )

        aggregate = aggregate_evaluations(
            [comparison], aggregate_id="EVAL-AGG-SYNTHETIC"
        )

        self.assertEqual(aggregate.false_success_rate.value, 1.0)
        self.assertEqual(aggregate.frontier_rescue_rate.value, 1.0)
        self.assertEqual(aggregate.unsafe_patch_rejection_rate.value, 1 / 3)
        self.assertEqual(aggregate.hosted_savings.status, MetricStatus.MEASURED)
        self.assertEqual(aggregate.hosted_savings.hosted_calls_avoided, 1)
        self.assertEqual(aggregate.hosted_savings.hosted_input_tokens_saved, 1000)
        self.assertEqual(aggregate.local_vs_one_shot.completion_rate_delta, 1.0)
        self.assertEqual(aggregate.context_profiles[0].profile, "64k")

        markdown = render_aggregate_markdown(aggregate)
        self.assertIn("False success", markdown)
        self.assertIn("Hosted calls avoided", markdown)
        with tempfile.TemporaryDirectory() as tmp:
            write_aggregate(Path(tmp), aggregate)
            self.assertTrue((Path(tmp) / "aggregate.json").is_file())
            self.assertTrue((Path(tmp) / "aggregate.md").is_file())

    def test_hosted_and_false_success_metrics_are_unmeasured_without_evidence(self) -> None:
        comparison = EvalComparisonReport(
            run_id="EVAL-FAKE",
            fixture_source="fixture",
            task_text=REQUEST,
            lanes=[
                EvalLaneResult(
                    lane=EvalLane.LOCAL,
                    report=self._report(TaskOutcome.COMPLETE),
                    evidence_kind=EvalEvidenceKind.DETERMINISTIC_FAKE,
                )
            ],
        )

        aggregate = aggregate_evaluations(
            [comparison], aggregate_id="EVAL-AGG-FAKE"
        )

        self.assertEqual(aggregate.frontier_rescue_rate.status, MetricStatus.UNMEASURED)
        self.assertIsNone(aggregate.frontier_rescue_rate.value)
        self.assertEqual(aggregate.false_success_rate.status, MetricStatus.UNMEASURED)
        self.assertIsNone(aggregate.false_success_rate.value)
        self.assertEqual(aggregate.hosted_savings.status, MetricStatus.UNMEASURED)

    def test_invalid_evidence_cannot_populate_real_world_metrics(self) -> None:
        comparison = EvalComparisonReport(
            run_id="EVAL-MIXED-EVIDENCE",
            fixture_source="fixture",
            task_text=REQUEST,
            lanes=[
                EvalLaneResult(
                    lane=EvalLane.LOCAL,
                    report=self._report(TaskOutcome.FAILED),
                    evidence_kind=EvalEvidenceKind.DETERMINISTIC_FAKE,
                    held_out_oracle=self._oracle(OracleStatus.FAILED),
                ),
                EvalLaneResult(
                    lane=EvalLane.FRONTIER,
                    report=self._report(
                        TaskOutcome.COMPLETE,
                        calls=[
                            self._telemetry(
                                input_tokens=100, output_tokens=20, cost=0.5
                            )
                        ],
                    ),
                    evidence_kind=EvalEvidenceKind.LIVE_HOSTED,
                ),
            ],
        )

        aggregate = aggregate_evaluations(
            [comparison], aggregate_id="EVAL-AGG-MIXED-EVIDENCE"
        )

        self.assertEqual(aggregate.false_success_rate.status, MetricStatus.UNMEASURED)
        self.assertEqual(aggregate.false_success_rate.denominator, 0)
        self.assertEqual(aggregate.hosted_savings.status, MetricStatus.UNMEASURED)

    def test_aggregate_cli_path_reads_persisted_reports_without_a_provider(self) -> None:
        comparison = EvalComparisonReport(
            run_id="EVAL-PERSISTED",
            fixture_source="fixture",
            task_text=REQUEST,
            lanes=[
                EvalLaneResult(
                    lane=EvalLane.LOCAL,
                    report=self._report(TaskOutcome.COMPLETE),
                    evidence_kind=EvalEvidenceKind.LIVE_LOCAL,
                    held_out_oracle=self._oracle(OracleStatus.PASSED),
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            output = root / "aggregate"
            write_comparison(source, comparison)

            payload = _aggregate_eval_reports(
                root, [source / "comparison.json"], output
            )

            self.assertEqual(payload["source_run_ids"], ["EVAL-PERSISTED"])
            self.assertTrue((output / "aggregate.json").is_file())
            self.assertTrue((output / "aggregate.md").is_file())


class _FakeOllamaChatServer:
    """A tiny loopback-only Ollama-shaped HTTP server for CLI-level eval tests.

    Serves canned `/api/chat` responses in order, patching the specification
    call's placeholder task_id to match whatever task_id Apoapsis generated
    for that request -- the same trick the agent-loop integration tests use
    against `FakeModelProvider`, applied over a real HTTP transport so the
    CLI's own adapter-building code path is exercised for real.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._index = 0

    def next_response(self, prompt: str) -> str:
        content = self._responses[self._index]
        self._index += 1
        if 'task_id to "' in prompt:
            task_id = prompt.split('task_id to "', 1)[1].split('"', 1)[0]
            raw = json.loads(content)
            raw["task_id"] = task_id
            content = json.dumps(raw)
        return content

    def __enter__(self) -> str:
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self._respond({"models": []})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                prompt = body["messages"][0]["content"]
                content = server.next_response(prompt)
                self._respond(
                    {
                        "message": {"content": content},
                        "done_reason": "stop",
                        "model": body.get("model", "fake-coder"),
                        "prompt_eval_count": 100,
                        "eval_count": 20,
                    }
                )

            def _respond(self, payload: dict[str, object]) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        self.http_server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.http_server.serve_forever, daemon=True
        )
        self.thread.start()
        return f"http://127.0.0.1:{self.http_server.server_port}"

    def __exit__(self, *exc_info: object) -> None:
        self.http_server.shutdown()
        self.http_server.server_close()


class EvalCliDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "examples").mkdir()
        import shutil

        shutil.copytree(_FIXTURE, self.root / "examples" / "download-service")
        (self.root / ".apoapsis").mkdir()

    def _write_config(self, base_url: str) -> None:
        config_toml = f"""
[models.frontier]
provider = "ollama"
base_url = "{base_url}"
model = "fake-coder"
context_window_tokens = 8192

[execution]
mode = "agent"
route = "auto"

[context]
max_files = 10
max_total_chars = 20000

[verification]
[[verification.commands]]
name = "download-tests"
category = "tests"
argv = ['{sys.executable}', "-m", "unittest", "discover", "-s", "tests", "-v"]
required = true
timeout_seconds = 30
"""
        (self.root / ".apoapsis" / "config.toml").write_text(
            config_toml, encoding="utf-8"
        )

    def test_lanes_requiring_frontier_coder_are_skipped_without_touching_network(
        self,
    ) -> None:
        with _FakeOllamaChatServer(
            [
                specification_response(),
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
                specification_response(),
                COMPLETE_PATCH,
            ]
        ) as base_url:
            self._write_config(base_url)
            output_dir = self.root / ".apoapsis-eval" / "run"
            comparison = _eval_download_service(self.root, None, None, output_dir)

        by_lane = {item["lane"]: item for item in comparison["lanes"]}
        self.assertTrue(by_lane["hybrid"]["skipped"])
        self.assertTrue(by_lane["forced-escalation"]["skipped"])
        self.assertTrue(by_lane["frontier"]["skipped"])
        for lane in ("hybrid", "forced-escalation", "frontier"):
            self.assertFalse(
                (output_dir / lane / "download-service").exists(),
                f"{lane} must not copy a fixture when skipped",
            )

        self.assertFalse(by_lane["local"]["skipped"])
        self.assertEqual(by_lane["local"]["report"]["outcome"], "complete")
        self.assertFalse(by_lane["one-shot"]["skipped"])
        self.assertEqual(by_lane["one-shot"]["report"]["outcome"], "complete")
        self.assertTrue((output_dir / "comparison.json").is_file())
        self.assertTrue((output_dir / "comparison.md").is_file())


if __name__ == "__main__":
    unittest.main()
