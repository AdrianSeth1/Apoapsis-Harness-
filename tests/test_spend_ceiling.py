from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from apoapsis.cli.app import _eval_download_service
from apoapsis.config import ApoapsisConfig, FrontierProviderConfig, ProviderPricing
from apoapsis.evaluation.harness import run_eval_lane
from apoapsis.evaluation.schemas import EvalLane
from apoapsis.evaluation.spend_ceiling import (
    HostedSpendCeilingExceededError,
    SpendCeilingModelProvider,
    SpendLedger,
    estimate_worst_case_call_cost_usd,
    estimate_worst_case_run_cost_usd,
)
from apoapsis.models.base import ModelOperation
from apoapsis.models.provider import ProviderInvocation
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.reporting.report import TaskOutcome
from apoapsis.workflow.engine import TaskStoreError
from tests.fakes import FakeModelProvider
from tests.test_evaluation import _FIXTURE, _base_config, _inject_task_id, action
from tests.test_vertical_slice import COMPLETE_PATCH, REQUEST, specification_response


def _invocation(*, prompt: str = "x", max_output_tokens: int | None = None) -> ProviderInvocation:
    return ProviderInvocation(
        request_id="MRQ-1",
        operation=ModelOperation.IMPLEMENT_PATCH,
        prompt=prompt,
        max_output_tokens=max_output_tokens,
    )


def _config(**model_overrides: object) -> ApoapsisConfig:
    values: dict[str, object] = dict(
        provider="openai_compatible",
        base_url="https://frontier.invalid/v1",
        model="hosted-coder",
        api_key_env="APOAPSIS_SPEND_TEST_KEY",
        max_output_tokens=1000,
        pricing=ProviderPricing(
            input_per_million_usd=10.0, output_per_million_usd=30.0
        ),
    )
    values.update(model_overrides)
    frontier_coder = FrontierProviderConfig(**values)
    return ApoapsisConfig.model_validate(
        {
            "models": {
                "frontier": {
                    "provider": "ollama",
                    "base_url": "http://127.0.0.1:11434",
                    "model": "local-model",
                },
                "frontier_coder": frontier_coder.model_dump(),
            },
            "verification": {
                "commands": [
                    {
                        "name": "tests",
                        "category": "tests",
                        "argv": ["git", "--version"],
                        "required": True,
                    }
                ]
            },
        }
    )


class EstimateWorstCaseCallCostTests(unittest.TestCase):
    def test_computes_pessimistic_upper_bound(self) -> None:
        pricing = ProviderPricing(
            input_per_million_usd=10.0, output_per_million_usd=30.0
        )
        # 400 chars -> 100 estimated input tokens (4 chars/token).
        cost = estimate_worst_case_call_cost_usd(
            prompt_chars=400, max_output_tokens=1000, pricing=pricing
        )
        expected = (100 * 10.0 + 1000 * 30.0) / 1_000_000
        self.assertAlmostEqual(cost, expected)

    def test_partial_chars_round_up_to_a_whole_token(self) -> None:
        pricing = ProviderPricing(input_per_million_usd=100.0)
        # 1 char still counts as 1 whole estimated token, never 0.
        cost = estimate_worst_case_call_cost_usd(
            prompt_chars=1, max_output_tokens=0, pricing=pricing
        )
        self.assertAlmostEqual(cost, 100.0 / 1_000_000)


class EstimateWorstCaseRunCostTests(unittest.TestCase):
    def test_zero_when_frontier_coder_not_configured(self) -> None:
        config = ApoapsisConfig.model_validate(
            {
                "models": {
                    "frontier": {
                        "provider": "ollama",
                        "base_url": "http://127.0.0.1:11434",
                        "model": "local-model",
                    }
                },
                "verification": {
                    "commands": [
                        {
                            "name": "tests",
                            "category": "tests",
                            "argv": ["git", "--version"],
                            "required": True,
                        }
                    ]
                },
            }
        )
        self.assertEqual(
            estimate_worst_case_run_cost_usd(config, [EvalLane.FRONTIER]), 0.0
        )

    def test_zero_when_no_requested_lane_needs_frontier_coder(self) -> None:
        config = _config()
        self.assertEqual(
            estimate_worst_case_run_cost_usd(config, [EvalLane.LOCAL]), 0.0
        )

    def test_scales_with_hosted_lane_count_and_configured_budget(self) -> None:
        config = _config()
        one_lane = estimate_worst_case_run_cost_usd(config, [EvalLane.FRONTIER])
        two_lanes = estimate_worst_case_run_cost_usd(
            config, [EvalLane.FRONTIER, EvalLane.HYBRID]
        )
        self.assertAlmostEqual(two_lanes, one_lane * 2)
        self.assertGreater(one_lane, 0.0)
        # A non-hosted lane mixed in must not add anything.
        mixed = estimate_worst_case_run_cost_usd(
            config, [EvalLane.FRONTIER, EvalLane.LOCAL]
        )
        self.assertAlmostEqual(mixed, one_lane)

    def test_matches_max_turns_times_per_call_estimate(self) -> None:
        config = _config()
        config = config.model_copy(
            update={
                "execution": config.execution.model_copy(
                    update={
                        "frontier_agent": config.execution.frontier_agent.model_copy(
                            update={"max_turns": 3}
                        )
                    }
                )
            }
        )
        assert config.models.frontier_coder is not None
        per_call = estimate_worst_case_call_cost_usd(
            prompt_chars=config.context.max_total_chars,
            max_output_tokens=config.models.frontier_coder.max_output_tokens,
            pricing=config.models.frontier_coder.pricing,
        )
        total = estimate_worst_case_run_cost_usd(config, [EvalLane.FRONTIER])
        self.assertAlmostEqual(total, per_call * 3)


class SpendLedgerTests(unittest.TestCase):
    def test_negative_ceiling_is_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            SpendLedger(ceiling_usd=-1.0)

    def test_refusal_never_mutates_spent_and_sets_exceeded(self) -> None:
        ledger = SpendLedger(ceiling_usd=1.0)
        with self.assertRaises(HostedSpendCeilingExceededError):
            ledger.refuse_if_worst_case_exceeds(1.5)
        self.assertEqual(ledger.spent_usd, 0.0)
        self.assertEqual(ledger.calls_refused, 1)
        self.assertTrue(ledger.exceeded)

    def test_worst_case_within_remaining_budget_is_allowed(self) -> None:
        ledger = SpendLedger(ceiling_usd=1.0)
        ledger.refuse_if_worst_case_exceeds(0.5)  # must not raise
        self.assertEqual(ledger.spent_usd, 0.0)
        self.assertFalse(ledger.exceeded)

    def test_record_actual_accumulates_across_calls(self) -> None:
        ledger = SpendLedger(ceiling_usd=1.0)
        ledger.record_actual(0.3)
        ledger.record_actual(0.3)
        self.assertAlmostEqual(ledger.spent_usd, 0.6)
        self.assertEqual(ledger.calls_recorded, 2)
        self.assertFalse(ledger.exceeded)

    def test_record_actual_past_ceiling_raises_and_sets_exceeded(self) -> None:
        ledger = SpendLedger(ceiling_usd=1.0)
        ledger.record_actual(0.9)
        with self.assertRaises(HostedSpendCeilingExceededError):
            ledger.record_actual(0.2)
        # the real cost is still recorded even though it breaches the
        # ceiling -- refusal only prevents *future* calls, it never
        # pretends a completed, billed call didn't happen.
        self.assertAlmostEqual(ledger.spent_usd, 1.1)
        self.assertTrue(ledger.exceeded)

    def test_remaining_usd_never_goes_negative(self) -> None:
        ledger = SpendLedger(ceiling_usd=1.0)
        try:
            ledger.record_actual(1.5)
        except HostedSpendCeilingExceededError:
            pass
        self.assertEqual(ledger.remaining_usd, 0.0)


class SpendCeilingModelProviderTests(unittest.TestCase):
    def _provider(self, outputs, ceiling_usd: float, *, max_output_tokens: int = 20):
        # FakeModelProvider always reports output_tokens=20 -- at $500/M
        # output (and $0 input/cached) that makes each successful call cost
        # exactly $0.01, a deliberately round number the tests below reason
        # about directly.
        pricing = ProviderPricing(output_per_million_usd=500.0)
        fake = FakeModelProvider(outputs)
        inner = InstrumentedModelProvider(fake, pricing)
        ledger = SpendLedger(ceiling_usd=ceiling_usd)
        wrapped = SpendCeilingModelProvider(
            inner, ledger, default_max_output_tokens=max_output_tokens
        )
        return wrapped, fake, ledger

    def test_a_call_within_budget_is_recorded_and_forwarded(self) -> None:
        wrapped, fake, ledger = self._provider(["patch"], ceiling_usd=10.0)
        call = wrapped.complete(_invocation())
        self.assertEqual(len(fake.invocations), 1)
        self.assertEqual(call.output.content, "patch")
        self.assertGreater(ledger.spent_usd, 0.0)
        self.assertEqual(ledger.calls_recorded, 1)

    def test_a_call_whose_worst_case_exceeds_the_ceiling_is_never_forwarded(
        self,
    ) -> None:
        # 20 max_output_tokens at $1000/M output alone is already $0.02;
        # a $0.001 ceiling cannot possibly cover it.
        wrapped, fake, ledger = self._provider(
            ["should never be produced"], ceiling_usd=0.001
        )
        with self.assertRaises(HostedSpendCeilingExceededError):
            wrapped.complete(_invocation())
        self.assertEqual(
            len(fake.invocations),
            0,
            "the refused call must never reach the underlying provider",
        )
        self.assertEqual(ledger.spent_usd, 0.0)
        self.assertTrue(ledger.exceeded)

    def test_provider_name_and_model_name_pass_through(self) -> None:
        wrapped, _fake, _ledger = self._provider(["x"], ceiling_usd=10.0)
        self.assertEqual(wrapped.provider_name, "fake_frontier")
        self.assertEqual(wrapped.model_name, "fake-coder-v1")

    def test_second_call_can_be_refused_after_the_first_succeeds(self) -> None:
        # One $0.01 call fits under $0.015; a second would bring the
        # worst-case prospective total to $0.02, over the ceiling.
        wrapped, fake, ledger = self._provider(["p1", "p2"], ceiling_usd=0.015)
        first = wrapped.complete(_invocation())
        self.assertAlmostEqual(first.telemetry.estimated_cost_usd, 0.01)
        with self.assertRaises(HostedSpendCeilingExceededError):
            wrapped.complete(_invocation())
        # the second call was refused before it reached the provider.
        self.assertEqual(len(fake.invocations), 1)

    def test_failed_inner_call_does_not_corrupt_the_ledger(self) -> None:
        wrapped, fake, ledger = self._provider(
            [RuntimeError("provider exploded")], ceiling_usd=10.0
        )
        with self.assertRaises(RuntimeError):
            wrapped.complete(_invocation())
        self.assertEqual(ledger.spent_usd, 0.0)
        self.assertEqual(ledger.calls_recorded, 0)


class RunEvalLaneSpendCeilingIntegrationTests(unittest.TestCase):
    """Exercises `SpendCeilingModelProvider` through the real
    `run_eval_lane`/`VerticalSliceRunner` path a live FRONTIER lane would
    use, not just in isolation -- mirrors
    `tests.test_evaluation.RunEvalLaneIntegrationTests
    .test_frontier_lane_completes` exactly, with the frontier coder
    provider wrapped in a spend ceiling."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.output_root = Path(self.temporary_directory.name)

    def _fixture(self, name: str) -> Path:
        from apoapsis.evaluation.fixture import prepare_fixture_repository

        destination = self.output_root / name / "download-service"
        prepare_fixture_repository(_FIXTURE, destination)
        return destination

    def test_a_completing_run_records_real_spend_against_the_ledger(self) -> None:
        frontier_config = FrontierProviderConfig(
            base_url="https://frontier.invalid/v1",
            model="big-coder-v1",
            # matches FakeModelProvider's fixed output_tokens=20, so the
            # pre-call worst-case estimate equals the real recorded cost.
            max_output_tokens=256,
            pricing=ProviderPricing(output_per_million_usd=500.0),
        )
        specification_provider = FakeModelProvider([specification_response()])
        _inject_task_id(specification_provider)
        frontier = FakeModelProvider(
            [
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
            ],
            provider_name="fake_hosted",
            model_name="big-coder-v1",
        )
        ledger = SpendLedger(ceiling_usd=1.0)
        wrapped = SpendCeilingModelProvider(
            InstrumentedModelProvider(frontier, frontier_config.pricing),
            ledger,
            default_max_output_tokens=frontier_config.max_output_tokens,
        )
        result = run_eval_lane(
            self._fixture("frontier-spend-ok"),
            EvalLane.FRONTIER,
            _base_config(frontier_coder=frontier_config),
            InstrumentedModelProvider(specification_provider),
            frontier_coder_provider=wrapped,
            task_text=REQUEST,
        )
        self.assertEqual(result.report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(ledger.calls_recorded, 2)
        self.assertAlmostEqual(ledger.spent_usd, 0.02)  # 2 calls x $0.01
        self.assertFalse(ledger.exceeded)

    def test_a_ceiling_too_small_for_the_second_call_stops_the_lane(self) -> None:
        frontier_config = FrontierProviderConfig(
            base_url="https://frontier.invalid/v1",
            model="big-coder-v1",
            # matches FakeModelProvider's fixed output_tokens=20, so the
            # pre-call worst-case estimate equals the real recorded cost.
            max_output_tokens=256,
            pricing=ProviderPricing(output_per_million_usd=500.0),
        )
        specification_provider = FakeModelProvider([specification_response()])
        _inject_task_id(specification_provider)
        frontier = FakeModelProvider(
            [
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
            ],
            provider_name="fake_hosted",
            model_name="big-coder-v1",
        )
        # Pre-call worst case is 256 tokens x $500/M = $0.128; $0.13 covers
        # exactly one call's worst-case estimate (actual cost $0.01) but not
        # a second call's prospective total ($0.01 spent + $0.128 estimate).
        ledger = SpendLedger(ceiling_usd=0.13)
        wrapped = SpendCeilingModelProvider(
            InstrumentedModelProvider(frontier, frontier_config.pricing),
            ledger,
            default_max_output_tokens=frontier_config.max_output_tokens,
        )
        result = run_eval_lane(
            self._fixture("frontier-spend-exceeded"),
            EvalLane.FRONTIER,
            _base_config(frontier_coder=frontier_config),
            InstrumentedModelProvider(specification_provider),
            frontier_coder_provider=wrapped,
            task_text=REQUEST,
        )
        self.assertNotEqual(result.report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(ledger.calls_recorded, 1)
        self.assertTrue(ledger.exceeded)
        self.assertAlmostEqual(ledger.spent_usd, 0.01)


class EvalCliSpendCeilingRefusalTests(unittest.TestCase):
    """CLI-level pre-flight refusals -- both must reject before any fixture
    is copied or any provider call is even attempted."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "examples").mkdir()
        shutil.copytree(_FIXTURE, self.root / "examples" / "download-service")
        (self.root / ".apoapsis").mkdir()
        (self.root / ".apoapsis" / "config.toml").write_text(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:1"
model = "fake-coder"

[models.frontier_coder]
provider = "ollama"
base_url = "http://127.0.0.1:1"
model = "fake-hosted-coder"
max_output_tokens = 1000

[models.frontier_coder.pricing]
output_per_million_usd = 500.0

[execution]
mode = "agent"
route = "auto"

[verification]
[[verification.commands]]
name = "download-tests"
category = "tests"
argv = ["git", "--version"]
required = true
""",
            encoding="utf-8",
        )

    def test_hosted_lane_without_a_ceiling_is_refused_before_any_fixture_copy(
        self,
    ) -> None:
        output_dir = self.root / ".apoapsis-eval" / "run"
        with self.assertRaisesRegex(TaskStoreError, "--max-hosted-spend-usd"):
            _eval_download_service(self.root, ["frontier"], None, output_dir)
        self.assertFalse((output_dir / "frontier" / "download-service").exists())

    def test_worst_case_exceeding_the_ceiling_is_refused_before_any_fixture_copy(
        self,
    ) -> None:
        output_dir = self.root / ".apoapsis-eval" / "run"
        # frontier_agent.max_turns defaults to 8; 8 calls at 1000 output
        # tokens and $500/M is already $4.00 worst case -- $0.01 cannot
        # possibly cover it.
        with self.assertRaisesRegex(TaskStoreError, "worst-case hosted spend"):
            _eval_download_service(
                self.root, ["frontier"], None, output_dir, max_hosted_spend_usd=0.01
            )
        self.assertFalse((output_dir / "frontier" / "download-service").exists())

    def test_a_local_only_run_never_requires_a_ceiling(self) -> None:
        # sanity: requesting a non-hosted lane must never trip the new
        # requirement, even though frontier_coder is configured above.
        output_dir = self.root / ".apoapsis-eval" / "run"
        try:
            _eval_download_service(self.root, ["local"], None, output_dir)
        except TaskStoreError as exc:
            if "max-hosted-spend" in str(exc) or "worst-case hosted spend" in str(exc):
                self.fail(f"a local-only run must never require a ceiling: {exc}")
            raise


if __name__ == "__main__":
    unittest.main()
