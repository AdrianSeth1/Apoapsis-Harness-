from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apoapsis.workcell.budgets import (
    BudgetKind,
    BudgetUsage,
    ProgressKind,
    ProgressTracker,
    SessionBudget,
    SessionClock,
    TokenLedger,
    TurnObservation,
    evaluate_budget,
)
from apoapsis.workcell.compaction import (
    DEFAULT_COMPACTION_THRESHOLD,
    CompactionPolicy,
    CompactionTier,
    HistorySegment,
    SegmentKind,
    ToolOutputBudget,
    bound_observation,
    compact,
)
from apoapsis.workcell.context import (
    ObservedWitness,
    StateCapsule,
    TaskKernel,
    build_layout,
    check_prefix_stability,
)

_FP = "a" * 64
_OTHER_FP = "b" * 64


def _kernel(**overrides) -> TaskKernel:
    payload = {
        "objective": "Add subtract() and cover it.",
        "slice_id": "SLICE-services",
        "acceptance_obligations": ["AC-INCIDENT proved", "AC-EXPORT proved"],
        "canonical_commands": ["unit-tests"],
        "checkpoint_instructions": "Call ready_for_evaluation when you believe the slice is done.",
    }
    payload.update(overrides)
    return TaskKernel(**payload)


class TaskKernelTests(unittest.TestCase):
    def test_the_kernel_renders_identically_every_time(self) -> None:
        self.assertEqual(_kernel().render(), _kernel().render())
        self.assertEqual(_kernel().digest(), _kernel().digest())

    def test_reordering_a_list_does_not_change_the_prefix(self) -> None:
        # Order carries no meaning the agent needs, and a reordered list is a
        # different prefix, which costs every cache hit.
        first = _kernel(acceptance_obligations=["A", "B"])
        second = _kernel(acceptance_obligations=["B", "A"])
        self.assertEqual(first.digest(), second.digest())

    def test_changing_the_task_does_change_the_digest(self) -> None:
        self.assertNotEqual(_kernel().digest(), _kernel(objective="Something else").digest())

    def test_a_volatile_looking_value_is_reported_and_not_refused(self) -> None:
        # Slice 5 refused these at construction. That was the wrong test: a
        # legitimate objective may quote a fixed upstream UUID or a historical
        # incident timestamp, and neither changes between calls. Volatility is
        # a provenance property, enforced by KernelArtifact reuse; the shape
        # scan is a hint for the owner to confirm.
        for field, value, label in (
            ("objective", "Reproduce the 2026-07-30T12:00 outage", "timestamp"),
            ("checkpoint_instructions", "Close MRQ-ABC123 then checkpoint", "request id"),
            (
                "objective",
                "Fix tenant 3f2b1c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
                "uuid",
            ),
        ):
            kernel = _kernel(**{field: value})
            hints = kernel.volatility_hints()
            self.assertIn(label, {item.label for item in hints}, msg=value)

    def test_a_clean_kernel_reports_no_hints(self) -> None:
        self.assertEqual(_kernel().volatility_hints(), [])

    def test_the_kernel_states_that_a_checkpoint_is_not_completion(self) -> None:
        rendered = _kernel().render()
        self.assertIn("does not mark the", rendered)
        self.assertIn("current-state evidence", rendered)


class StateCapsuleTests(unittest.TestCase):
    def _capsule(self, **overrides) -> StateCapsule:
        payload = {
            "slice_id": "SLICE-services",
            "worktree_fingerprint": _FP,
            "unresolved_obligations": ["export-service not created"],
            "model_notes": ["I think persistence is done"],
        }
        payload.update(overrides)
        return StateCapsule(**payload)

    def test_model_notes_are_marked_advisory(self) -> None:
        # They are the model's beliefs about its own work, and the one part of
        # this document it wrote.
        rendered = self._capsule().render()
        self.assertIn("Advisory", rendered)
        self.assertIn("may be wrong", rendered)

    def test_a_witness_from_an_older_worktree_is_labelled_not_current(self) -> None:
        rendered = self._capsule(
            observed_witnesses=[
                ObservedWitness(
                    command_name="unit-tests", passed=True, worktree_fingerprint=_OTHER_FP
                )
            ]
        ).render()
        self.assertIn("not current evidence", rendered)

    def test_a_current_witness_is_not_labelled_stale(self) -> None:
        rendered = self._capsule(
            observed_witnesses=[
                ObservedWitness(
                    command_name="unit-tests", passed=True, worktree_fingerprint=_FP
                )
            ]
        ).render()
        self.assertNotIn("not current evidence", rendered)

    def test_refused_and_no_progress_actions_are_carried_forward(self) -> None:
        # A fresh context that immediately retries what already failed is how
        # the Slice 2C sandbox arm reached nine identical calls.
        rendered = self._capsule(
            refused_actions=["write to /task/task.md"],
            no_progress_actions=["skill('qc-helper')"],
        ).render()
        self.assertIn("do not retry unchanged", rendered)
        self.assertIn("try something different", rendered)

    def test_the_capsule_carries_no_raw_logs(self) -> None:
        fields = set(StateCapsule.model_fields)
        self.assertFalse({"transcript", "raw_output", "logs"} & fields)


class PromptLayoutTests(unittest.TestCase):
    def _layout(self, **overrides):
        payload = {
            "system_prompt": "You are a coding agent.",
            "tool_schemas": ["write_file", "read_file", "run_shell_command"],
            "kernel": _kernel(),
        }
        payload.update(overrides)
        return build_layout(**payload)

    def test_the_prefix_is_stable_across_differing_observations(self) -> None:
        first = self._layout(observation="turn 1 output")
        second = self._layout(observation="turn 2 output")
        self.assertEqual(first.prefix_digest(), second.prefix_digest())
        self.assertNotEqual(first.render(), second.render())

    def test_tool_schemas_are_sorted_here_not_trusted_to_arrive_sorted(self) -> None:
        first = self._layout(tool_schemas=["a", "b", "c"])
        second = self._layout(tool_schemas=["c", "a", "b"])
        self.assertEqual(first.prefix_digest(), second.prefix_digest())

    def test_history_is_outside_the_cacheable_prefix(self) -> None:
        with_history = self._layout(
            capsule=StateCapsule(slice_id="SLICE-services")
        )
        without = self._layout()
        self.assertEqual(with_history.prefix_digest(), without.prefix_digest())

    def test_a_prefix_that_moved_mid_session_is_detected(self) -> None:
        # The symptom is invisible: the run still works and the cache-hit rate
        # quietly goes to zero.
        drift = check_prefix_stability(["a" * 64, "a" * 64, "b" * 64, "b" * 64])
        self.assertFalse(drift.stable)
        self.assertEqual(drift.first_change_at_call, 3)
        self.assertIn("paid full prompt evaluation", drift.detail)

    def test_a_stable_session_reports_stable(self) -> None:
        self.assertTrue(check_prefix_stability(["a" * 64] * 5).stable)

    def test_no_calls_is_unmeasured_not_stable(self) -> None:
        self.assertFalse(check_prefix_stability([]).stable)


class CompactionTests(unittest.TestCase):
    def _policy(self, **overrides) -> CompactionPolicy:
        payload = {"context_limit_tokens": 65_536}
        payload.update(overrides)
        return CompactionPolicy(**payload)

    def _history(self, turn_count: int, tokens_each: int) -> list[HistorySegment]:
        segments = [
            HistorySegment(
                segment_id="capsule",
                kind=SegmentKind.CAPSULE,
                turn=0,
                estimated_tokens=500,
                text="state",
            )
        ]
        for turn in range(1, turn_count + 1):
            segments.append(
                HistorySegment(
                    segment_id=f"reason-{turn}",
                    kind=SegmentKind.REASONING,
                    turn=turn,
                    estimated_tokens=tokens_each,
                    text="thinking" * 20,
                )
            )
            segments.append(
                HistorySegment(
                    segment_id=f"output-{turn}",
                    kind=SegmentKind.TOOL_OUTPUT,
                    turn=turn,
                    estimated_tokens=tokens_each,
                    text=f"output for turn {turn}\n" * 50,
                )
            )
        return segments

    def test_below_the_threshold_nothing_happens(self) -> None:
        segments = self._history(2, 500)
        surviving, decision = compact(segments, self._policy())
        self.assertEqual(decision.tier, CompactionTier.NONE)
        self.assertEqual(surviving, segments)

    def test_the_crisis_atlas_near_boundary_run_would_have_compacted(self) -> None:
        # Slice 2D's control reached 58,038 tokens -- 88.6% of the 65,536
        # window -- and fired no compaction event. At the default threshold it
        # would have. This is a statement about the policy, not a claim about
        # what the model would then have done.
        policy = self._policy()
        self.assertTrue(policy.should_compact(58_038))
        self.assertAlmostEqual(policy.utilisation(58_038), 0.8856, places=3)
        # And the unrestricted control's fatal prompt, well past it.
        self.assertTrue(policy.should_compact(64_409))

    def test_the_threshold_is_configurable_not_a_constant(self) -> None:
        # The handoff wants 60/70/80 compared on the corpus; the default is a
        # first experiment point.
        self.assertEqual(DEFAULT_COMPACTION_THRESHOLD, 0.70)
        loose = self._policy(threshold=0.80, target=0.5)
        self.assertFalse(loose.should_compact(50_000))
        tight = self._policy(threshold=0.60, target=0.4)
        self.assertTrue(tight.should_compact(50_000))

    def test_a_target_at_or_above_the_threshold_is_refused(self) -> None:
        # Otherwise a session compacts, lands just under the line, and
        # compacts again on the very next turn.
        with self.assertRaises(ValueError):
            self._policy(threshold=0.70, target=0.70)

    def test_mechanical_compaction_drops_reasoning_and_spills_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            segments = self._history(20, 2_000)
            surviving, decision = compact(
                segments,
                self._policy(),
                spill_directory=Path(tmp),
                current_turn=20,
            )
            self.assertIn(
                decision.tier, {CompactionTier.MECHANICAL, CompactionTier.SEMANTIC}
            )
            self.assertTrue(decision.dropped_segment_ids)
            self.assertTrue(decision.spilled_segment_ids)
            self.assertGreater(decision.tokens_saved, 0)
            # The capsule is what compaction exists to preserve.
            self.assertIn("capsule", [item.segment_id for item in surviving])
            # Every spilled output left a pointer the model can follow.
            spilled = [
                item
                for item in surviving
                if item.kind == SegmentKind.TOOL_OUTPUT and item.artifact_pointer
            ]
            self.assertTrue(spilled)
            for item in spilled:
                self.assertTrue(Path(item.artifact_pointer).is_file())

    def test_recent_turns_survive_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            segments = self._history(20, 2_000)
            surviving, _ = compact(
                segments,
                self._policy(keep_recent_turns=2),
                spill_directory=Path(tmp),
                current_turn=20,
            )
            recent = {
                item.segment_id
                for item in surviving
                if item.turn > 18 and item.kind == SegmentKind.REASONING
            }
            self.assertEqual(recent, {"reason-19", "reason-20"})

    def test_output_with_nowhere_to_spill_is_kept_not_dropped(self) -> None:
        # Dropping it would make the only record of what a command printed
        # vanish, which is the failure this design keeps finding.
        segments = self._history(20, 2_000)
        surviving, _ = compact(segments, self._policy(), current_turn=20)
        kept = [
            item
            for item in surviving
            if item.kind == SegmentKind.TOOL_OUTPUT and item.turn < 18
        ]
        self.assertTrue(kept)
        self.assertTrue(all(item.text for item in kept))

    def test_semantic_is_requested_only_when_mechanical_was_not_enough(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Almost all of the history is recent tool calls, which mechanical
            # compaction may not touch.
            segments = [
                HistorySegment(
                    segment_id=f"call-{index}",
                    kind=SegmentKind.TOOL_CALL,
                    turn=20,
                    estimated_tokens=3_000,
                    text="call",
                )
                for index in range(18)
            ]
            _surviving, decision = compact(
                segments, self._policy(), spill_directory=Path(tmp), current_turn=20
            )
            self.assertEqual(decision.tier, CompactionTier.SEMANTIC)
            self.assertIn("semantic compaction is required", decision.detail)


class BoundedObservationTests(unittest.TestCase):
    def test_short_output_is_untouched(self) -> None:
        result = bound_observation("hello", max_chars=100)
        self.assertFalse(result.truncated)
        self.assertEqual(result.text, "hello")

    def test_truncation_keeps_the_head_and_the_tail(self) -> None:
        # The first lines say what a command did; the last say how it ended.
        with tempfile.TemporaryDirectory() as tmp:
            text = "START\n" + ("filler\n" * 5_000) + "TRACEBACK AT THE END"
            result = bound_observation(
                text, max_chars=2_000, spill_directory=Path(tmp)
            )
            self.assertTrue(result.truncated)
            self.assertIn("START", result.text)
            self.assertIn("TRACEBACK AT THE END", result.text)
            self.assertIn("characters omitted", result.text)
            self.assertTrue(Path(result.artifact_pointer).is_file())

    def test_truncation_without_anywhere_to_spill_does_not_truncate(self) -> None:
        # Refusing to truncate is safer than truncating irreversibly.
        text = "x" * 10_000
        result = bound_observation(text, max_chars=100)
        self.assertFalse(result.truncated)
        self.assertEqual(len(result.text), 10_000)

    def test_per_tool_budgets_differ_by_tool_and_by_outcome(self) -> None:
        budget = ToolOutputBudget()
        self.assertLess(budget.for_tool("glob"), budget.for_tool("read_file"))
        # A failing test needs far more room than a passing one.
        self.assertGreater(
            budget.for_tool("unit-tests", passed=False),
            budget.for_tool("unit-tests", passed=True),
        )
        self.assertLess(budget.background_chars, budget.shell_chars)


class BudgetTests(unittest.TestCase):
    def test_a_fresh_session_is_within_budget(self) -> None:
        verdict = evaluate_budget(SessionBudget(), BudgetUsage())
        self.assertTrue(verdict.within_budget)

    def test_every_exhausted_ceiling_is_reported_not_just_the_first(self) -> None:
        verdict = evaluate_budget(
            SessionBudget(wall_clock_seconds=10, max_input_tokens=1_000),
            BudgetUsage(
                wall_clock_seconds=20,
                tokens=TokenLedger(reported=True, input_tokens=5_000),
            ),
        )
        kinds = {item.kind for item in verdict.breaches}
        self.assertIn(BudgetKind.WALL_CLOCK, kinds)
        self.assertIn(BudgetKind.INPUT_TOKENS, kinds)

    def test_no_progress_carries_guidance_and_wall_clock_does_not(self) -> None:
        # A wall-clock expiry is not something the agent can repair.
        stalled = evaluate_budget(
            SessionBudget(), BudgetUsage(consecutive_no_progress_turns=3)
        )
        breach = next(
            item for item in stalled.breaches if item.kind == BudgetKind.NO_PROGRESS
        )
        self.assertTrue(breach.guidance)
        expired = evaluate_budget(
            SessionBudget(wall_clock_seconds=1), BudgetUsage(wall_clock_seconds=5)
        )
        clock = next(
            item for item in expired.breaches if item.kind == BudgetKind.WALL_CLOCK
        )
        self.assertEqual(clock.guidance, "")

    def test_repeated_identical_actions_are_a_no_progress_breach(self) -> None:
        # The Slice 2C sandbox arm made nine identical calls before its own
        # loop detection halted it.
        verdict = evaluate_budget(
            SessionBudget(), BudgetUsage(max_identical_action_run=9)
        )
        self.assertFalse(verdict.within_budget)
        self.assertIn(
            BudgetKind.NO_PROGRESS, {item.kind for item in verdict.breaches}
        )

    def test_a_low_emergency_ceiling_is_refused(self) -> None:
        # Setting it low would recreate the turn-count budget by the back door.
        with self.assertRaises(ValueError):
            SessionBudget(emergency_call_ceiling=12)

    def test_pressure_is_reported_before_it_becomes_a_stop(self) -> None:
        verdict = evaluate_budget(
            SessionBudget(wall_clock_seconds=100), BudgetUsage(wall_clock_seconds=80)
        )
        self.assertTrue(verdict.within_budget)
        self.assertGreaterEqual(verdict.pressure, 0.8)


class ProgressTrackerTests(unittest.TestCase):
    def test_a_changed_worktree_is_progress(self) -> None:
        tracker = ProgressTracker()
        self.assertTrue(
            tracker.record_turn(
                TurnObservation(action_signature="edit", worktree_fingerprint=_FP)
            ).made_progress
        )
        self.assertFalse(
            tracker.record_turn(
                TurnObservation(action_signature="read", worktree_fingerprint=_FP)
            ).made_progress
        )
        self.assertEqual(tracker.consecutive_no_progress, 1)
        self.assertTrue(
            tracker.record_turn(
                TurnObservation(action_signature="edit", worktree_fingerprint=_OTHER_FP)
            ).made_progress
        )
        self.assertEqual(tracker.consecutive_no_progress, 0)

    def test_a_diagnosis_that_edits_nothing_is_still_progress(self) -> None:
        # A turn that runs a failing test and yields a new coverage artifact
        # has advanced the session's knowledge. Counting it as no-progress
        # would punish the debugging behaviour the unrestricted control did
        # well -- it repaired its own tests across several such turns.
        tracker = ProgressTracker()
        tracker.record_turn(
            TurnObservation(action_signature="edit", worktree_fingerprint=_FP)
        )
        result = tracker.record_turn(
            TurnObservation(
                action_signature="pytest",
                worktree_fingerprint=_FP,
                evidence_artifacts=["witness-coverage-1"],
            )
        )
        self.assertTrue(result.made_progress)
        self.assertIn(ProgressKind.EVIDENCE, result.kinds)

    def test_a_newly_discharged_obligation_is_progress(self) -> None:
        tracker = ProgressTracker()
        result = tracker.record_turn(
            TurnObservation(
                action_signature="checkpoint",
                worktree_fingerprint=None,
                discharged_obligations=["SLICE-services-criterion-AC-1"],
            )
        )
        self.assertTrue(result.made_progress)
        self.assertIn(ProgressKind.OBLIGATION, result.kinds)

    def test_the_same_obligation_twice_is_progress_only_once(self) -> None:
        # Otherwise a stalled session could report progress forever by
        # re-reporting a discharge it made three turns ago.
        tracker = ProgressTracker()
        observation = TurnObservation(
            action_signature="checkpoint", discharged_obligations=["ob-1"]
        )
        self.assertTrue(tracker.record_turn(observation).made_progress)
        self.assertFalse(tracker.record_turn(observation).made_progress)

    def test_narration_alone_is_not_progress(self) -> None:
        # There is no field for the model's account of its own turn, which is
        # the point: every input here is controller-observed.
        tracker = ProgressTracker()
        result = tracker.record_turn(
            TurnObservation(action_signature="I have now finished the service")
        )
        self.assertFalse(result.made_progress)
        self.assertEqual(result.kinds, [ProgressKind.NONE])

    def test_identical_actions_are_counted(self) -> None:
        tracker = ProgressTracker()
        for _ in range(9):
            tracker.record_turn(
                TurnObservation(
                    action_signature="skill('qc-helper')", worktree_fingerprint=_FP
                )
            )
        self.assertEqual(tracker.max_identical_run, 9)
        self.assertIn("skill('qc-helper')", tracker.no_progress_actions)

    def test_the_clock_separates_wall_time_from_process_time(self) -> None:
        # "The session took 30 minutes" and "25 of those were the test suite"
        # call for different fixes.
        clock = SessionClock()
        clock.add_process_time(12.5)
        usage = clock.usage(tokens=TokenLedger(reported=True, input_tokens=100))
        self.assertEqual(usage.process_seconds, 12.5)
        self.assertGreaterEqual(usage.wall_clock_seconds, 0.0)
        self.assertEqual(usage.tokens.input_tokens, 100)


if __name__ == "__main__":
    unittest.main()
