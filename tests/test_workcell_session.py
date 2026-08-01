"""Slice 5B: the coordinator, and the three authority corrections with it.

The Slice 5 modules were policy with no caller. These tests exercise the loop
that owns them, and in particular the three places where authority moved:

1. kernel stability is byte reuse, not a regex over the text;
2. compaction and token ceilings read provider-reported usage only;
3. progress is authoritative state advancement, of which a worktree change is
   one of three kinds.

The live exit criteria -- a real Qwen session continuing after compaction, and
cache telemetry showing whether the stable prefix bought anything -- are not
here and cannot be. They need the workcell.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from apoapsis.workcell.acceptance import (
    AcceptanceObligation,
    ObligationKind,
    SliceAcceptanceContract,
)
from apoapsis.workcell.budgets import SessionBudget, TokenLedger
from apoapsis.workcell.compaction import (
    CompactionPolicy,
    ContextReading,
    HistorySegment,
    SegmentKind,
)
from apoapsis.workcell.context import (
    KernelArtifact,
    KernelDriftError,
    TaskKernel,
    build_layout,
    persist_kernel,
)
from apoapsis.workcell.session import (
    SessionCoordinator,
    SessionOutcome,
    SessionState,
    TurnResult,
)

_FP = "b" * 64


def _kernel(**overrides) -> TaskKernel:
    payload = {
        "objective": "Build the incident export service",
        "slice_id": "SLICE-services",
        "checkpoint_instructions": "Request a checkpoint when you believe the "
        "slice is done.",
    }
    payload.update(overrides)
    return TaskKernel(**payload)


def _contract() -> SliceAcceptanceContract:
    return SliceAcceptanceContract(
        slice_id="SLICE-services",
        criteria=["AC-1"],
        obligations=[
            AcceptanceObligation(
                obligation_id="SLICE-services-criterion-AC-1",
                kind=ObligationKind.TEST_OR_WITNESS,
                description="AC-1 is proved by current-state evidence",
                criteria=["AC-1"],
            )
        ],
    )


class _Driver:
    """A scripted agent. Returns each queued result in order."""

    def __init__(self, results: list[TurnResult]) -> None:
        self.results = list(results)
        self.layouts: list[str] = []

    def advance(self, layout, turn):  # noqa: ANN001 -- test double
        self.layouts.append(layout.task_kernel)
        if self.results:
            return self.results.pop(0)
        return TurnResult(action_signature="idle", finished=True)


class KernelArtifactTests(unittest.TestCase):
    def test_the_kernel_is_rendered_once_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = persist_kernel(_kernel(), directory)
            self.assertEqual(artifact.load_text(), _kernel().render())
            self.assertEqual(artifact.byte_length, len(_kernel().render().encode()))

    def test_a_fixed_uuid_in_the_objective_is_allowed(self) -> None:
        # The correction: this is a legitimate objective. Slice 5 refused it.
        with tempfile.TemporaryDirectory() as directory:
            kernel = _kernel(
                objective="Fix tenant 3f2b1c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d's export"
            )
            artifact = persist_kernel(kernel, directory)
            self.assertEqual(artifact.load_text(), kernel.render())
            # Recorded as a hint so an owner can confirm it is fixed, and
            # blocking nothing.
            self.assertEqual(
                {item.label for item in artifact.volatility_hints}, {"uuid"}
            )

    def test_editing_the_artifact_mid_session_is_named_not_absorbed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = persist_kernel(_kernel(), directory)
            Path(artifact.path).write_text("something else", encoding="utf-8")
            with self.assertRaises(KernelDriftError):
                artifact.load_text()

    def test_the_layout_reuses_the_artifact_rather_than_re_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = persist_kernel(_kernel(), directory)
            first = build_layout(
                system_prompt="s",
                tool_schemas=["b", "a"],
                kernel=artifact,
                observation="turn 1",
            )
            second = build_layout(
                system_prompt="s",
                tool_schemas=["a", "b"],
                kernel=artifact,
                observation="turn 2",
            )
            self.assertEqual(first.prefix_digest(), second.prefix_digest())


class TokenAuthorityTests(unittest.TestCase):
    def test_an_estimate_never_triggers_compaction(self) -> None:
        # An estimate reading high compacts a session that did not need it; an
        # estimate reading low is how a run reaches 64,409 tokens against a
        # 65,536 window with no compaction event. Neither failure should be
        # attributable to the controller's own arithmetic.
        policy = CompactionPolicy(context_limit_tokens=65_536)
        estimated = ContextReading(
            input_tokens=58_038, provider_reported=False, estimated_tokens=58_038
        )
        self.assertFalse(policy.should_compact_reading(estimated))
        reported = ContextReading(input_tokens=58_038, provider_reported=True)
        self.assertTrue(policy.should_compact_reading(reported))

    def test_missing_telemetry_leaves_token_ceilings_unenforced_not_passing(
        self,
    ) -> None:
        from apoapsis.workcell.budgets import BudgetKind, BudgetUsage, evaluate_budget

        verdict = evaluate_budget(
            SessionBudget(max_input_tokens=1_000),
            BudgetUsage(
                tokens=TokenLedger(reported=False, estimated_input_tokens=9_000_000)
            ),
        )
        # Not a breach -- the estimate does not get a vote -- and not silence.
        self.assertIn(BudgetKind.INPUT_TOKENS, verdict.unenforced)
        self.assertIn("UNENFORCED", verdict.detail)

    def test_the_estimate_error_is_kept_for_diagnosis(self) -> None:
        ledger = TokenLedger(
            reported=True, input_tokens=58_038, estimated_input_tokens=41_000
        )
        self.assertEqual(ledger.estimate_error, -17_038)


class CoordinatorTests(unittest.TestCase):
    def _coordinator(self, driver, **overrides) -> SessionCoordinator:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        payload = {
            "contract": _contract(),
            "kernel_artifact": persist_kernel(_kernel(), directory),
            "driver": driver,
            "system_prompt": "You are a coding agent.",
            "tool_schemas": ["read_file", "write_file"],
            "compaction_policy": CompactionPolicy(context_limit_tokens=65_536),
            "base_root": directory / "base",
            "candidate_root": directory / "candidate",
            "snapshot_root": directory / "snapshot",
            "emit_witnesses": lambda root, fingerprint: [],
        }
        payload.update(overrides)
        return SessionCoordinator(**payload)

    def test_a_budget_stop_is_a_recorded_transition_not_a_bare_return(self) -> None:
        driver = _Driver([])
        coordinator = self._coordinator(
            driver, budget=SessionBudget(wall_clock_seconds=0.01)
        )
        time.sleep(0.05)
        record = coordinator.run()
        self.assertIs(record.outcome, SessionOutcome.BUDGET_EXHAUSTED)
        self.assertIs(record.transitions[-1].to_state, SessionState.STOPPED)
        self.assertTrue(record.transitions[-1].reason)
        # The budget was checked before the call, not after: spending a call
        # and then discovering the allowance was gone makes a ceiling advisory.
        self.assertEqual(driver.layouts, [])

    def test_an_agent_that_stops_without_a_checkpoint_does_not_read_as_complete(
        self,
    ) -> None:
        driver = _Driver([TurnResult(action_signature="think", finished=True)])
        record = self._coordinator(driver).run()
        self.assertIs(record.outcome, SessionOutcome.AGENT_STOPPED)
        self.assertIn("no completion decision", record.detail)

    def test_semantic_compaction_failure_stops_explicitly(self) -> None:
        # Continuing over a context known to be too full is how the control
        # reached a 64,409-token prompt against a 65,536-token window.
        driver = _Driver(
            [
                TurnResult(
                    action_signature="read",
                    tokens=TokenLedger(reported=True, input_tokens=60_000),
                    segments=[
                        HistorySegment(
                            segment_id="tc-1",
                            kind=SegmentKind.TOOL_CALL,
                            turn=1,
                            estimated_tokens=60_000,
                            text="x" * 400,
                        )
                    ],
                ),
                TurnResult(action_signature="read", finished=True),
            ]
        )
        coordinator = self._coordinator(
            driver, semantic_compactor=lambda segments: None
        )
        record = coordinator.run()
        self.assertIs(record.outcome, SessionOutcome.COMPACTION_FAILED)
        self.assertIn("semantic compaction returned no summary", record.detail)

    def test_no_semantic_compactor_configured_is_also_an_explicit_stop(self) -> None:
        driver = _Driver(
            [
                TurnResult(
                    action_signature="read",
                    tokens=TokenLedger(reported=True, input_tokens=60_000),
                    segments=[
                        HistorySegment(
                            segment_id="tc-1",
                            kind=SegmentKind.TOOL_CALL,
                            turn=1,
                            estimated_tokens=60_000,
                            text="x" * 400,
                        )
                    ],
                ),
                TurnResult(action_signature="read", finished=True),
            ]
        )
        record = self._coordinator(driver).run()
        self.assertIs(record.outcome, SessionOutcome.COMPACTION_FAILED)
        self.assertIn("no semantic compactor is configured", record.detail)

    def test_the_58038_token_reading_compacts_before_another_request_is_sent(
        self,
    ) -> None:
        # Slice 2D's own near-boundary reading -- 88.6% of the window, which
        # fired nothing at the time. Here the second call cannot happen until a
        # COMPACTING transition has been recorded.
        driver = _Driver(
            [
                TurnResult(
                    action_signature="read",
                    tokens=TokenLedger(reported=True, input_tokens=58_038),
                    segments=[
                        HistorySegment(
                            segment_id=f"seg-{index}",
                            kind=SegmentKind.REASONING,
                            turn=1,
                            estimated_tokens=100,
                            text="reasoning " * 20,
                        )
                        for index in range(5)
                    ],
                ),
                TurnResult(action_signature="edit", finished=True),
            ]
        )
        coordinator = self._coordinator(
            driver, semantic_compactor=lambda segments: "summary of prior work"
        )
        record = coordinator.run()
        states = [item.to_state for item in record.transitions]
        self.assertIn(SessionState.COMPACTING, states)
        first_model = states.index(SessionState.AWAITING_MODEL)
        compacting = states.index(SessionState.COMPACTING)
        second_model = states.index(SessionState.AWAITING_MODEL, first_model + 1)
        self.assertLess(first_model, compacting)
        self.assertLess(compacting, second_model)
        self.assertTrue(record.compaction_events)

    def test_the_capsule_survives_compaction(self) -> None:
        driver = _Driver(
            [
                TurnResult(
                    action_signature="read",
                    tokens=TokenLedger(reported=True, input_tokens=58_038),
                    model_notes=["persistence is done, I think"],
                    segments=[
                        HistorySegment(
                            segment_id="r-1",
                            kind=SegmentKind.REASONING,
                            turn=1,
                            estimated_tokens=50_000,
                            text="reasoning",
                        )
                    ],
                ),
                TurnResult(action_signature="edit", finished=True),
            ]
        )
        coordinator = self._coordinator(
            driver, semantic_compactor=lambda segments: "summary"
        )
        coordinator.run()
        rendered = coordinator.capsule.render()
        self.assertIn("SLICE-services-criterion-AC-1", rendered)
        self.assertIn("Advisory", rendered)
        self.assertIn("persistence is done", rendered)

    def test_the_prefix_is_identical_across_every_call(self) -> None:
        driver = _Driver(
            [
                TurnResult(action_signature="a", worktree_fingerprint=_FP),
                TurnResult(action_signature="b"),
                TurnResult(action_signature="c", finished=True),
            ]
        )
        record = self._coordinator(driver).run()
        self.assertIsNotNone(record.prefix_drift)
        self.assertTrue(record.prefix_drift.stable)
        self.assertEqual(record.prefix_drift.distinct_prefixes, 1)

    def test_kernel_drift_mid_session_is_its_own_outcome(self) -> None:
        driver = _Driver([TurnResult(action_signature="a", finished=True)])
        coordinator = self._coordinator(driver)
        Path(coordinator.kernel_artifact.path).write_text("edited", encoding="utf-8")
        record = coordinator.run()
        self.assertIs(record.outcome, SessionOutcome.KERNEL_DRIFT)

    def test_a_no_progress_turn_is_carried_into_the_capsule(self) -> None:
        driver = _Driver(
            [
                TurnResult(action_signature="skill('qc-helper')"),
                TurnResult(action_signature="skill('qc-helper')", finished=True),
            ]
        )
        coordinator = self._coordinator(driver)
        coordinator.run()
        # A fresh context should not immediately retry what already changed
        # nothing: the Slice 2C sandbox arm made nine identical calls.
        self.assertIn("skill('qc-helper')", coordinator.capsule.no_progress_actions)
        self.assertIn(
            "Produced no change", coordinator.capsule.render()
        )


class CachedInputSpellingTests(unittest.TestCase):
    """The CLI field name that cost Slice 5C a false NOT_MEASURABLE.

    Qwen Code reports cached input as `cache_read_input_tokens`. The trace
    calls the same quantity `cached_input_tokens`. Stage 7 of the live
    qualification read the trace spelling straight off the raw provider
    message, found nothing, and concluded the server reported no cache
    telemetry -- when in fact the stable arm had climbed from 19,742 to 21,915
    cached tokens while the perturbed arm stayed flat at 19,742.

    That is the failure mode this codebase treats as the worst kind: absence
    of a *reading* reported as absence of the *thing*. `_flatten_usage` is the
    single place that knows both spellings, so it is the thing to pin.
    """

    def test_the_cli_spelling_is_normalised(self) -> None:
        from apoapsis.workcell.events import _flatten_usage

        flat = _flatten_usage(
            {
                "input_tokens": 21_915,
                "output_tokens": 12,
                "cache_read_input_tokens": 21_915,
            }
        )
        self.assertEqual(flat["cached_input_tokens"], 21_915)

    def test_the_trace_spelling_still_wins_when_both_are_present(self) -> None:
        from apoapsis.workcell.events import _flatten_usage

        flat = _flatten_usage(
            {"cached_input_tokens": 21_915, "cache_read_input_tokens": 1}
        )
        self.assertEqual(flat["cached_input_tokens"], 21_915)

    def test_a_missing_cached_field_stays_none_rather_than_zero(self) -> None:
        # Zero would read as "measured, and the cache did nothing", which is a
        # different claim from "not reported".
        from apoapsis.workcell.events import _flatten_usage

        flat = _flatten_usage({"input_tokens": 100, "output_tokens": 5})
        self.assertIsNone(flat["cached_input_tokens"])


if __name__ == "__main__":
    unittest.main()
