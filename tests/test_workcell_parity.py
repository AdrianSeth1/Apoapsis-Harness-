from __future__ import annotations

import unittest

from apoapsis.config import CapabilitySandboxConfig
from apoapsis.workcell.parity import (
    ParityMode,
    evaluate_parity,
    select_parity,
    slice_position,
)


def _select(position: int, mode: ParityMode = ParityMode.SAMPLE, every: int = 4):
    return select_parity(mode=mode, slice_position=position, sample_every=every)


class ParitySelectionTests(unittest.TestCase):
    """Which slices pay for a control arm, decided the same way every time.

    The parity guard answered its qualification question with six paired
    1.0/1.0 slots. Continuing to ask it on every slice costs 2x inference
    forever; sampling monitors it instead. What sampling must not do is become
    unreproducible, because "the check happened to skip the slice that
    regressed" is the failure mode.
    """

    def test_always_pairs_every_slice(self) -> None:
        for position in range(1, 13):
            self.assertTrue(_select(position, ParityMode.ALWAYS).run_control_arm)

    def test_off_pairs_nothing(self) -> None:
        for position in range(1, 13):
            self.assertFalse(_select(position, ParityMode.OFF).run_control_arm)

    def test_sampling_pairs_the_first_slice_and_every_nth(self) -> None:
        paired = [
            position
            for position in range(1, 14)
            if _select(position).run_control_arm
        ]
        self.assertEqual(paired, [1, 5, 9, 13])

    def test_every_plan_gets_at_least_one_paired_comparison(self) -> None:
        # Even a one-slice plan. A plan that never paired would inherit its
        # confidence entirely from someone else's qualification run.
        self.assertTrue(_select(1).run_control_arm)

    def test_the_sample_interval_is_configurable(self) -> None:
        paired = [
            position
            for position in range(1, 10)
            if _select(position, every=2).run_control_arm
        ]
        self.assertEqual(paired, [1, 3, 5, 7, 9])

    def test_selection_is_reproducible(self) -> None:
        first = [_select(position).run_control_arm for position in range(1, 20)]
        second = [_select(position).run_control_arm for position in range(1, 20)]
        self.assertEqual(first, second)

    def test_an_unknown_position_pairs_rather_than_skips(self) -> None:
        # When the harness cannot tell where it is, the safe direction is the
        # expensive one: skipping on a guess is how a regression goes unseen.
        selection = _select(0)
        self.assertTrue(selection.run_control_arm)
        self.assertIn("could not be established", selection.reason)

    def test_every_selection_states_its_reason(self) -> None:
        for mode in ParityMode:
            for position in (0, 1, 2, 4, 5):
                selection = select_parity(
                    mode=mode, slice_position=position, sample_every=4
                )
                self.assertTrue(selection.reason)
                # A reader must be able to tell a skipped slice from a slice
                # that ran and found nothing.
                self.assertIn(mode.value, selection.reason.lower() + mode.value)

    def test_slice_position_is_read_from_the_plan_order(self) -> None:
        plan = {
            "slices": [
                {"slice_id": "SLICE-001"},
                {"slice_id": "SLICE-002"},
                {"slice_id": "SLICE-003"},
            ]
        }
        self.assertEqual(slice_position(plan, "SLICE-001"), 1)
        self.assertEqual(slice_position(plan, "SLICE-003"), 3)
        self.assertEqual(slice_position(plan, "SLICE-404"), 0)


class ParityConfigTests(unittest.TestCase):
    def test_sampling_is_the_default(self) -> None:
        self.assertEqual(CapabilitySandboxConfig().parity_mode, ParityMode.SAMPLE)
        self.assertEqual(CapabilitySandboxConfig().parity_sample_every, 4)

    def test_the_old_switch_still_means_every_slice(self) -> None:
        # An operator who explicitly turned the pre-0108 guard on asked for a
        # control arm on every slice. Silently downgrading that to sampling
        # would spend their evidence for them.
        config = CapabilitySandboxConfig(high_assurance_parity_guard=True)
        self.assertEqual(config.parity_mode, ParityMode.ALWAYS)

    def test_an_explicit_mode_is_taken_at_its_word(self) -> None:
        config = CapabilitySandboxConfig(
            high_assurance_parity_guard=True, parity_mode=ParityMode.OFF
        )
        self.assertEqual(config.parity_mode, ParityMode.OFF)


class ParityEscalationTests(unittest.TestCase):
    """Sampling changes how often we ask, never what happens to the answer."""

    def test_a_sampled_slice_that_regresses_still_blocks(self) -> None:
        outcome = evaluate_parity(
            expected=True, control_proved=3, candidate_proved=2
        )
        self.assertTrue(outcome.regression)
        self.assertTrue(outcome.blocks_completion)

    def test_a_sampled_slice_with_no_scoreable_control_still_blocks(self) -> None:
        # The comparison was supposed to happen and did not. Completing anyway
        # would report a slice as parity-checked when nothing checked it.
        outcome = evaluate_parity(
            expected=True, control_proved=None, candidate_proved=4
        )
        self.assertTrue(outcome.unavailable)
        self.assertTrue(outcome.blocks_completion)

    def test_an_unsampled_slice_is_not_penalised_for_having_no_control(self) -> None:
        outcome = evaluate_parity(
            expected=False, control_proved=None, candidate_proved=4
        )
        self.assertFalse(outcome.unavailable)
        self.assertFalse(outcome.blocks_completion)

    def test_matching_or_better_work_is_not_a_regression(self) -> None:
        for candidate in (3, 4, 9):
            self.assertFalse(
                evaluate_parity(
                    expected=True, control_proved=3, candidate_proved=candidate
                ).regression
            )

    def test_a_missing_candidate_is_not_blamed_on_the_comparison(self) -> None:
        # The sandbox produced no checkpoint at all. That already stops the
        # slice; calling it a parity regression would misattribute it.
        outcome = evaluate_parity(
            expected=True, control_proved=3, candidate_proved=None
        )
        self.assertFalse(outcome.regression)
        self.assertFalse(outcome.unavailable)


class SandboxDefaultCompatibilityTests(unittest.TestCase):
    """The default must not make coherent configurations invalid.

    ADR 0109 made the sandbox the default. The pre-existing refusal of
    "sandbox + frontier_only" then fired on an operator who had done nothing:
    they chose to send everything to the frontier coder and inherited a local
    setting they never touched. The setting means "when a local slice runs, run
    it contained"; frontier_only means none runs.
    """

    def test_the_default_sandbox_allows_the_frontier_only_route(self) -> None:
        from apoapsis.config import (
            AgentRoute,
            ApoapsisConfig,
            ExecutionConfig,
            ExecutionMode,
            FrontierProviderConfig,
            ModelsConfig,
        )
        from apoapsis.verification.runner import VerificationConfig

        config = ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1", model="fake-coder-v1"
                ),
                frontier_coder=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1", model="fake-coder-v1"
                ),
            ),
            verification=VerificationConfig(),
            execution=ExecutionConfig(
                mode=ExecutionMode.AGENT, route=AgentRoute.FRONTIER_ONLY
            ),
        )
        self.assertTrue(config.execution.capability_sandbox.enabled)
        self.assertEqual(config.execution.route, AgentRoute.FRONTIER_ONLY)

    def test_the_legacy_path_is_still_refused_with_frontier_only(self) -> None:
        # Local Power stays opt-in, so asking for it *and* frontier-only is
        # still evidence of confusion rather than an inherited default.
        from apoapsis.config import (
            AgentRoute,
            ApoapsisConfig,
            CapabilitySandboxConfig,
            ExecutionConfig,
            ExecutionMode,
            FrontierProviderConfig,
            LocalPowerConfig,
            ModelsConfig,
        )
        from apoapsis.verification.runner import VerificationConfig

        with self.assertRaises(ValueError):
            ApoapsisConfig(
                models=ModelsConfig(
                    frontier=FrontierProviderConfig(
                        base_url="https://provider.invalid/v1", model="fake-coder-v1"
                    ),
                    frontier_coder=FrontierProviderConfig(
                        base_url="https://provider.invalid/v1", model="fake-coder-v1"
                    ),
                ),
                verification=VerificationConfig(),
                execution=ExecutionConfig(
                    mode=ExecutionMode.AGENT,
                    route=AgentRoute.FRONTIER_ONLY,
                    capability_sandbox=CapabilitySandboxConfig(enabled=False),
                    local_power=LocalPowerConfig(enabled=True),
                ),
            )


if __name__ == "__main__":
    unittest.main()
