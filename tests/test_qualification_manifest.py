"""Slice 7 Phase 1: the manifest is frozen, complete, and cannot be gamed.

Every test here asserts something about the *rules*, before any output exists to
be scored. The ones that matter most assert what the manifest makes impossible:
no combined score, no repair-inflated proposal score, no aggregate rescuing a
regressed case, no abstention counted as a pass.
"""

from __future__ import annotations

import json
import unittest

import pydantic

from apoapsis.qualification.manifest import (
    ArmKind,
    CaseVerdict,
    ControlledVariables,
    DetectorLayer,
    KnownPartialProposal,
    NonInferiorityRule,
    PairComparability,
    Phase0Provenance,
    ProposalScore,
    REQUIRED_CASE_KINDS,
    ScoreKind,
    StopCondition,
    TokenAccounting,
    check_pair,
    evaluate_gate,
)
from apoapsis.qualification.slice7 import (
    SLICE7_MANIFEST,
    SOURCE_UNDER_TEST_COMMIT,
    ready_for_inference,
    unresolved_hashes,
)


class ImmutabilityAndHashingTests(unittest.TestCase):
    def test_the_manifest_cannot_be_mutated(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            SLICE7_MANIFEST.repetitions_per_case = 1  # type: ignore[misc]
        with self.assertRaises(pydantic.ValidationError):
            SLICE7_MANIFEST.controlled_variables.cold_start = False  # type: ignore[misc]

    def test_the_digest_is_stable_and_content_addressed(self) -> None:
        self.assertEqual(SLICE7_MANIFEST.digest(), SLICE7_MANIFEST.digest())
        self.assertRegex(SLICE7_MANIFEST.digest(), r"^[0-9a-f]{64}$")
        changed = SLICE7_MANIFEST.model_copy(update={"repetitions_per_case": 4})
        self.assertNotEqual(changed.digest(), SLICE7_MANIFEST.digest())

    def test_the_manifest_commit_is_excluded_from_the_digest(self) -> None:
        """Otherwise committing the manifest would change the manifest."""

        stamped = SLICE7_MANIFEST.model_copy(update={"manifest_commit": "beef123"})
        self.assertEqual(stamped.digest(), SLICE7_MANIFEST.digest())

    def test_the_manifest_commit_must_differ_from_the_source(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            SLICE7_MANIFEST.model_copy(
                update={"manifest_commit": SOURCE_UNDER_TEST_COMMIT}
            ).model_validate(
                SLICE7_MANIFEST.model_dump()
                | {"manifest_commit": SOURCE_UNDER_TEST_COMMIT}
            )

    def test_unresolved_capture_placeholders_block_inference(self) -> None:
        """Fail closed: a placeholder must never pass for a captured hash."""

        outstanding = unresolved_hashes()
        self.assertIn("model_file_sha256", outstanding)
        self.assertIn("llama_server_argv_sha256", outstanding)
        self.assertFalse(ready_for_inference())

    def test_the_manifest_serialises_losslessly(self) -> None:
        payload = SLICE7_MANIFEST.model_dump(mode="json")
        self.assertEqual(
            type(SLICE7_MANIFEST).model_validate(payload).digest(),
            SLICE7_MANIFEST.digest(),
        )
        json.dumps(payload)  # must be plain JSON, no exotic types


class ArmTests(unittest.TestCase):
    def test_exactly_two_arms_and_no_legacy_harness(self) -> None:
        self.assertEqual(
            {arm.kind for arm in SLICE7_MANIFEST.arms},
            {ArmKind.CONTROL, ArmKind.CAPABILITY_SANDBOX},
        )

    def test_neither_arm_holds_host_authority(self) -> None:
        for arm in SLICE7_MANIFEST.arms:
            with self.subTest(arm=arm.kind):
                self.assertFalse(arm.host_authority)

    def test_the_control_uses_the_verified_coding_profile(self) -> None:
        control = next(a for a in SLICE7_MANIFEST.arms if a.kind is ArmKind.CONTROL)
        self.assertEqual(control.permission_mode, "yolo")
        self.assertFalse(control.computer_use_enabled)
        self.assertFalse(control.tool_search_enabled)
        self.assertTrue(control.tool_surface_verified_before_inference)
        self.assertTrue(control.realised_tool_names)

    def test_the_arms_share_cli_and_differ_only_in_supervision(self) -> None:
        control, sandbox = SLICE7_MANIFEST.arms
        self.assertEqual(control.cli_name, sandbox.cli_name)
        self.assertEqual(control.cli_version, sandbox.cli_version)
        self.assertEqual(control.realised_tool_names, sandbox.realised_tool_names)
        self.assertNotEqual(control.supervision, sandbox.supervision)


class ComparabilityTests(unittest.TestCase):
    def _variables(self, **overrides) -> ControlledVariables:
        return SLICE7_MANIFEST.controlled_variables.model_copy(update=overrides)

    def test_identical_variables_are_comparable(self) -> None:
        base = self._variables()
        self.assertIs(check_pair(base, base).comparability, PairComparability.COMPARABLE)

    def test_any_mismatch_makes_the_pair_incomparable(self) -> None:
        base = self._variables()
        for field, value in (
            ("context_limit_tokens", 32_768),
            ("auto_compact_trigger_tokens", 55_706),
            ("cold_start", False),
            ("gpu_allocation", "shared"),
            ("workcell_image_digest", "other:latest"),
        ):
            with self.subTest(field=field):
                check = check_pair(base, self._variables(**{field: value}))
                self.assertIs(check.comparability, PairComparability.INCOMPARABLE)
                self.assertIn(field, check.mismatched_fields)
                self.assertFalse(check.usable_for_gate)

    def test_the_refusal_names_its_own_cause(self) -> None:
        check = check_pair(self._variables(), self._variables(cpu_limit=8.0))
        self.assertIn("cpu_limit", check.detail)
        self.assertIn("no value may be substituted", check.detail)


class ScoringBoundaryTests(unittest.TestCase):
    def test_there_is_no_combined_score_anywhere(self) -> None:
        """A combined score is how a cheap win cancels a quality regression.

        Asserted structurally: no symbol in the manifest module offers one.
        """

        import apoapsis.qualification.manifest as module

        banned = ("combined", "overall_score", "total_score", "composite")
        offenders = [
            name
            for name in dir(module)
            if any(token in name.lower() for token in banned)
        ]
        self.assertEqual(offenders, [])

    def test_a_proposal_score_cannot_include_a_repair(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            ProposalScore(
                case_id="crisis-atlas",
                obligations_implemented=5,
                obligations_required=5,
                independent_checks_passed=3,
                repair_applied=True,
            )

    def test_a_proposal_score_cannot_masquerade_as_another_scorecard(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            ProposalScore(
                case_id="crisis-atlas",
                kind=ScoreKind.FINAL_DELIVERED_QUALITY,
                obligations_implemented=1,
                obligations_required=1,
                independent_checks_passed=1,
            )

    def test_the_residual_is_neither_folded_in_nor_counted_as_a_call(self) -> None:
        ok = TokenAccounting(
            session_aggregate_input_tokens=53_397,
            session_aggregate_output_tokens=475,
            exposed_message_input_tokens=22_433,
            exposed_message_cached_input_tokens=19_742,
            exposed_message_count=1,
            unattributed_residual_input_tokens=30_964,
        )
        self.assertEqual(ok.exposed_message_count, 1)

        with self.assertRaises(pydantic.ValidationError):
            TokenAccounting(
                session_aggregate_input_tokens=53_397,
                session_aggregate_output_tokens=475,
                exposed_message_input_tokens=22_433,
                exposed_message_cached_input_tokens=19_742,
                exposed_message_count=1,
                unattributed_residual_input_tokens=0,
            )

    def test_an_aggregate_smaller_than_its_components_is_refused(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            TokenAccounting(
                session_aggregate_input_tokens=10,
                session_aggregate_output_tokens=1,
                exposed_message_input_tokens=100,
                exposed_message_cached_input_tokens=0,
                exposed_message_count=1,
                unattributed_residual_input_tokens=0,
            )


class GateTests(unittest.TestCase):
    def _verdicts(self, **overrides) -> dict[str, CaseVerdict]:
        verdicts = {case_id: CaseVerdict.TIE for case_id in SLICE7_MANIFEST.required_case_ids}
        verdicts.update(overrides)
        return verdicts

    def _evaluate(self, verdicts, **kwargs):
        return evaluate_gate(
            verdicts,
            required_case_ids=SLICE7_MANIFEST.required_case_ids,
            aggregate_proposal_quality_fell=kwargs.get("fell", False),
            material_improvement_observed=kwargs.get("improved", True),
            rule=SLICE7_MANIFEST.non_inferiority,
        )

    def test_ties_pass_and_improvements_pass(self) -> None:
        self.assertTrue(self._evaluate(self._verdicts()).promoted)
        self.assertTrue(
            self._evaluate(
                self._verdicts(**{"crisis-atlas": CaseVerdict.SANDBOX_BETTER})
            ).promoted
        )

    def test_one_regressed_case_blocks_however_good_the_rest(self) -> None:
        verdicts = {
            case_id: CaseVerdict.SANDBOX_BETTER
            for case_id in SLICE7_MANIFEST.required_case_ids
        }
        verdicts["cross-file-refactor"] = CaseVerdict.SANDBOX_WORSE
        result = self._evaluate(verdicts)
        self.assertFalse(result.promoted)
        self.assertIn("cross-file-refactor", result.blocking_case_ids)
        self.assertIn("aggregate", result.detail)

    def test_every_abstention_blocks_and_none_is_a_pass(self) -> None:
        for verdict in (
            CaseVerdict.NOT_MEASURABLE,
            CaseVerdict.MISSING_EVIDENCE,
            CaseVerdict.UNCLASSIFIED_TRUNCATION,
            CaseVerdict.INFRASTRUCTURE_FAILURE,
            CaseVerdict.INCOMPARABLE,
        ):
            with self.subTest(verdict=verdict):
                self.assertFalse(verdict.passed_for_gate)
                self.assertTrue(verdict.is_abstention)
                result = self._evaluate(self._verdicts(**{"focus-orbit": verdict}))
                self.assertFalse(result.promoted)
                self.assertIn("focus-orbit", result.abstaining_case_ids)

    def test_a_missing_case_cannot_pass_by_omission(self) -> None:
        verdicts = self._verdicts()
        verdicts.pop("test-repair")
        result = self._evaluate(verdicts)
        self.assertFalse(result.promoted)
        self.assertIn("test-repair", result.blocking_case_ids)

    def test_falling_aggregate_quality_blocks(self) -> None:
        self.assertFalse(self._evaluate(self._verdicts(), fell=True).promoted)

    def test_parity_without_material_improvement_blocks(self) -> None:
        result = self._evaluate(self._verdicts(), improved=False)
        self.assertFalse(result.promoted)
        self.assertIn("material improvement", result.detail)

    def test_the_frozen_rule_keeps_quality_above_efficiency(self) -> None:
        rule = SLICE7_MANIFEST.non_inferiority
        self.assertTrue(rule.efficiency_subordinate_to_quality)
        self.assertTrue(rule.per_case_regression_blocks_rollout)
        self.assertEqual(rule.false_completions_permitted, 0)
        self.assertEqual(rule.permitted_failure_or_ceiling_rate, 0.0)


class CorpusAndNegativeControlTests(unittest.TestCase):
    def test_every_required_case_kind_is_present(self) -> None:
        self.assertEqual(
            {case.kind for case in SLICE7_MANIFEST.corpus}, set(REQUIRED_CASE_KINDS)
        )

    def test_at_least_three_repetitions(self) -> None:
        self.assertGreaterEqual(SLICE7_MANIFEST.repetitions_per_case, 3)
        self.assertEqual(SLICE7_MANIFEST.paired_executions, 24)
        self.assertEqual(SLICE7_MANIFEST.arm_runs, 48)

    def test_all_ten_negative_controls_are_frozen_with_detectors(self) -> None:
        self.assertGreaterEqual(len(SLICE7_MANIFEST.negative_controls), 10)
        for control in SLICE7_MANIFEST.negative_controls:
            with self.subTest(control=control.control_id):
                self.assertTrue(control.required_evidence)
                self.assertTrue(control.failure_classification)
                self.assertIsInstance(control.expected_detector, DetectorLayer)

    def test_an_unrelated_detector_does_not_satisfy_a_control(self) -> None:
        control = SLICE7_MANIFEST.negative_controls[0]
        self.assertTrue(control.satisfied_by(control.expected_detector))
        self.assertFalse(control.satisfied_by(DetectorLayer.DELIVERY))
        self.assertEqual(control.allowed_secondary_detectors, ())

    def test_every_referenced_control_is_mapped(self) -> None:
        mapped = {item.control_id for item in SLICE7_MANIFEST.negative_controls}
        for case in SLICE7_MANIFEST.corpus:
            for control_id in case.negative_control_ids:
                with self.subTest(case=case.case_id, control=control_id):
                    self.assertIn(control_id, mapped)

    def test_the_output_ceiling_control_is_present(self) -> None:
        ids = {item.control_id for item in SLICE7_MANIFEST.negative_controls}
        self.assertIn("NC-10-output-ceiling-truncation", ids)


class CrisisAtlasTests(unittest.TestCase):
    def test_all_fifteen_must_pass_requirements_are_frozen(self) -> None:
        self.assertEqual(len(SLICE7_MANIFEST.crisis_atlas_must_pass), 15)

    def test_the_known_partial_slice2_proposal_cannot_complete(self) -> None:
        """The candidate this entire programme exists to refuse."""

        proposal = KnownPartialProposal()
        self.assertFalse(proposal.may_complete)
        self.assertTrue(proposal.inherited_checks_green)
        self.assertEqual(proposal.tests_added, 0)
        for declared in proposal.declared_artifacts:
            with self.subTest(artifact=declared):
                self.assertNotIn(declared, proposal.changed_paths)


class Phase0ProvenanceTests(unittest.TestCase):
    def test_the_suite_history_is_recorded_exactly(self) -> None:
        history = {item.commit: item for item in SLICE7_MANIFEST.phase0.suite_history}
        self.assertEqual((history["d50ddf2"].failed, history["d50ddf2"].passed), (6, 1546))
        self.assertEqual((history["f68827e"].failed, history["f68827e"].passed), (6, 1625))
        self.assertEqual((history["bd5aea0"].failed, history["bd5aea0"].passed), (2, 1629))
        self.assertEqual((history["ad13cf0"].failed, history["ad13cf0"].passed), (0, 1631))
        self.assertEqual(history["ad13cf0"].subtests_passed, 57)
        for item in history.values():
            with self.subTest(commit=item.commit):
                self.assertEqual(item.skipped, 11)

    def test_phase0_repairs_are_never_a_capability_sandbox_win(self) -> None:
        self.assertFalse(SLICE7_MANIFEST.phase0.counts_as_capability_sandbox_win)
        with self.assertRaises(pydantic.ValidationError):
            Phase0Provenance(
                suite_history=SLICE7_MANIFEST.phase0.suite_history,
                baseline_ruler_repairs=(),
                obsolete_test_mechanisms=(),
                windows_status="x",
                counts_as_capability_sandbox_win=True,
            )

    def test_the_platform_and_windows_status_are_recorded(self) -> None:
        self.assertEqual(
            SLICE7_MANIFEST.phase0.qualification_platform, "Linux + Python 3.12"
        )
        self.assertIn("stalls", SLICE7_MANIFEST.phase0.windows_status)
        self.assertIn("NOT a", SLICE7_MANIFEST.phase0.windows_status)
        self.assertEqual(len(SLICE7_MANIFEST.phase0.baseline_ruler_repairs), 5)
        self.assertEqual(len(SLICE7_MANIFEST.phase0.obsolete_test_mechanisms), 2)


class StopConditionTests(unittest.TestCase):
    def test_all_nine_stop_conditions_are_frozen(self) -> None:
        self.assertEqual(
            set(SLICE7_MANIFEST.stop_conditions), set(StopCondition)
        )
        self.assertGreaterEqual(len(SLICE7_MANIFEST.stop_conditions), 8)

    def test_source_or_seed_change_is_a_stop_condition(self) -> None:
        self.assertIn(
            StopCondition.SOURCE_OR_SEED_CHANGED, SLICE7_MANIFEST.stop_conditions
        )


if __name__ == "__main__":
    unittest.main()
