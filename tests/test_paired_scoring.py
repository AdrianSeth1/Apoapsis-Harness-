from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from apoapsis.cli.app import _score_paired_corpus_command
from apoapsis.evaluation.crisis_atlas_facts import (
    CRISIS_ATLAS_CASE_ID,
    CRISIS_ATLAS_SLICE_FACTS,
    CrisisAtlasMissKind,
    crisis_atlas_records,
    sliced_local_power_record,
    unrestricted_control_record,
)
from apoapsis.evaluation.paired import (
    BaselineCapability,
    CapabilityObservation,
    CapabilityStatus,
    CaseOutcome,
    GateStatus,
    HarnessDetectionScorecard,
    ModelProposalScorecard,
    PairedArmKind,
    PairedArmRecord,
    PairedFindingCode,
    PairedRunManifest,
    PairedVerdict,
    ReleaseGate,
    compare_case,
    score_paired_corpus,
)
from apoapsis.evaluation.paired_report import (
    render_paired_markdown,
    write_paired_corpus,
)
from apoapsis.evaluation.schemas import MetricStatus, RateMetric
from apoapsis.models.ceilings import (
    CeilingStopReason,
    classify_ceiling_stop_reason,
    is_ceiling_failure,
    partition_failures,
)

_SHA = "a" * 64


def _rate(numerator: int, denominator: int) -> RateMetric:
    return RateMetric(
        status=MetricStatus.MEASURED,
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator,
    )


def _unmeasured(reason: str = "not measured in this fixture") -> RateMetric:
    return RateMetric(status=MetricStatus.UNMEASURED, value=None, reason=reason)


def _capabilities(
    absent: set[BaselineCapability] | None = None,
) -> list[CapabilityObservation]:
    absent = absent or set()
    return [
        CapabilityObservation(
            capability=capability,
            status=(
                CapabilityStatus.ABSENT
                if capability in absent
                else CapabilityStatus.PROVIDED
            ),
            evidence="fixture",
        )
        for capability in BaselineCapability
    ]


def _manifest(
    arm: PairedArmKind,
    *,
    case_id: str = "case-1",
    absent: set[BaselineCapability] | None = None,
    **overrides: object,
) -> PairedRunManifest:
    payload: dict[str, object] = {
        "arm": arm,
        "case_id": case_id,
        "seed_commit": "1" * 40,
        "worktree_fingerprint": "wt-1",
        "task_sha256": _SHA,
        "plan_sha256": _SHA,
        "model_file_sha256": _SHA,
        "model_name": "qwen3.6-27b",
        "quantization": "Q4_K_M",
        "endpoint": "http://127.0.0.1:8080/v1",
        "sampling_seed": 7,
        "temperature": 0.0,
        "server_flags_sha256": _SHA,
        "context_limit_tokens": 65_536,
        "max_output_tokens": 16_384,
        "wall_clock_ceiling_seconds": 1_800.0,
        "cpu_allocation": "4",
        "gpu_allocation": "1",
        "network_policy": "none",
        "mount_policy": "disposable-clone",
        "verifier_version": "v1",
        "capabilities": _capabilities(absent),
    }
    payload.update(overrides)
    return PairedRunManifest.model_validate(payload)


def _proposal(
    *,
    obligations: tuple[int, int] = (4, 4),
    checks: tuple[int, int] = (8, 8),
    input_tokens: int = 100_000,
    **overrides: object,
) -> ModelProposalScorecard:
    payload: dict[str, object] = {
        "obligations_implemented_before_repair": _rate(*obligations),
        "independent_checks_passed_at_first_checkpoint": _rate(*checks),
        "model_authored_test_relevance": _unmeasured(),
        "model_calls": 20,
        "attempted_model_calls": 20,
        "input_tokens": input_tokens,
        "output_tokens": 20_000,
        "provider_latency_seconds": 900.0,
    }
    payload.update(overrides)
    return ModelProposalScorecard.model_validate(payload)


def _detection(**overrides: object) -> HarnessDetectionScorecard:
    payload: dict[str, object] = {
        "defects_detected": _unmeasured(),
        "negative_controls_caught": _unmeasured(),
        "criteria_with_current_state_evidence": _unmeasured(),
        "structured_witness_coverage": _unmeasured(),
    }
    payload.update(overrides)
    return HarnessDetectionScorecard.model_validate(payload)


def _record(
    arm: PairedArmKind,
    *,
    case_id: str = "case-1",
    independent: CaseOutcome = CaseOutcome.PASSED,
    delivered: CaseOutcome = CaseOutcome.PASSED,
    absent: set[BaselineCapability] | None = None,
    manifest_overrides: dict[str, object] | None = None,
    proposal: ModelProposalScorecard | None = None,
    detection: HarnessDetectionScorecard | None = None,
    **overrides: object,
) -> PairedArmRecord:
    payload: dict[str, object] = {
        "manifest": _manifest(
            arm, case_id=case_id, absent=absent, **(manifest_overrides or {})
        ),
        "proposal": proposal or _proposal(),
        "detection": detection or _detection(),
        "independent_case_outcome": independent,
        "delivered_case_outcome": delivered,
    }
    payload.update(overrides)
    return PairedArmRecord.model_validate(payload)


class CeilingClassificationTests(unittest.TestCase):
    def test_full_window_is_context_exhaustion_not_an_output_cap_hit(self) -> None:
        # The Crisis Atlas control's rollover: 64,409 + 1,127 == 65,536 exactly,
        # well under the 16,384 output cap. Charging this to the output cap would
        # justify raising a limit that was never reached.
        self.assertEqual(
            classify_ceiling_stop_reason(
                finish_reason="length",
                input_tokens=64_409,
                output_tokens=1_127,
                context_limit=65_536,
                max_output_tokens=16_384,
            ),
            CeilingStopReason.INPUT_CONTEXT_EXHAUSTED,
        )

    def test_completion_at_the_declared_cap_is_output_truncation(self) -> None:
        # Sliced arm calls 1/9 and 1/11: 8,192 output tokens against a 65,536
        # window that was nowhere near full.
        self.assertEqual(
            classify_ceiling_stop_reason(
                finish_reason="length",
                input_tokens=13_562,
                output_tokens=8_192,
                context_limit=65_536,
                max_output_tokens=8_192,
            ),
            CeilingStopReason.OUTPUT_CEILING_TRUNCATION,
        )

    def test_provider_error_after_rollover_is_attributed_to_the_window(self) -> None:
        self.assertEqual(
            classify_ceiling_stop_reason(
                provider_error=True, context_rolled_over=True, context_limit=65_536
            ),
            CeilingStopReason.PROVIDER_ERROR_AFTER_ROLLOVER,
        )

    def test_provider_error_without_rollover_is_not_a_ceiling(self) -> None:
        self.assertIsNone(
            classify_ceiling_stop_reason(provider_error=True, context_limit=65_536)
        )

    def test_tool_output_truncation_outranks_a_pressure_reading(self) -> None:
        self.assertEqual(
            classify_ceiling_stop_reason(
                tool_output_truncated=True,
                input_tokens=60_000,
                context_limit=65_536,
            ),
            CeilingStopReason.TOOL_OUTPUT_TRUNCATION,
        )

    def test_pressure_is_advisory_and_below_threshold_is_silent(self) -> None:
        self.assertEqual(
            classify_ceiling_stop_reason(
                finish_reason="stop", input_tokens=50_000, context_limit=65_536
            ),
            CeilingStopReason.INPUT_CONTEXT_PRESSURE,
        )
        self.assertFalse(is_ceiling_failure(CeilingStopReason.INPUT_CONTEXT_PRESSURE))
        self.assertIsNone(
            classify_ceiling_stop_reason(
                finish_reason="stop", input_tokens=1_000, context_limit=65_536
            )
        )

    def test_ordinary_completion_has_no_ceiling_reason(self) -> None:
        self.assertIsNone(
            classify_ceiling_stop_reason(
                finish_reason="stop",
                input_tokens=1_000,
                output_tokens=500,
                context_limit=65_536,
                max_output_tokens=16_384,
            )
        )

    def test_partition_keeps_ceilings_out_of_model_reasoning(self) -> None:
        partition = partition_failures(
            [
                CeilingStopReason.OUTPUT_CEILING_TRUNCATION,
                CeilingStopReason.INPUT_CONTEXT_EXHAUSTED,
                CeilingStopReason.INPUT_CONTEXT_PRESSURE,
                None,
            ]
        )
        self.assertEqual(partition.ceiling_failures, 2)
        self.assertEqual(partition.model_reasoning_failures, 1)
        self.assertEqual(partition.advisory_pressure_events, 1)
        self.assertEqual(partition.total, 3)


class PairedCaseComparisonTests(unittest.TestCase):
    def test_identical_arms_are_parity(self) -> None:
        comparison = compare_case(
            _record(PairedArmKind.DEFAULT_QWEN_CONTROL),
            _record(PairedArmKind.CAPABILITY_SANDBOX),
        )
        self.assertEqual(comparison.proposal_verdict, PairedVerdict.PARITY)
        self.assertEqual(comparison.delivered_verdict, PairedVerdict.PARITY)
        self.assertEqual(comparison.findings, [])

    def test_a_case_the_control_passes_and_the_candidate_fails_regresses(self) -> None:
        comparison = compare_case(
            _record(PairedArmKind.DEFAULT_QWEN_CONTROL),
            _record(
                PairedArmKind.CAPABILITY_SANDBOX, independent=CaseOutcome.FAILED
            ),
        )
        self.assertEqual(comparison.proposal_verdict, PairedVerdict.REGRESSION)
        self.assertIn(
            PairedFindingCode.PROPOSAL_CASE_REGRESSION,
            {item.code for item in comparison.findings},
        )

    def test_lower_obligation_rate_regresses_even_when_the_case_passes(self) -> None:
        comparison = compare_case(
            _record(PairedArmKind.DEFAULT_QWEN_CONTROL),
            _record(
                PairedArmKind.CAPABILITY_SANDBOX,
                proposal=_proposal(obligations=(2, 4)),
            ),
        )
        self.assertEqual(comparison.proposal_verdict, PairedVerdict.REGRESSION)
        self.assertIn(
            PairedFindingCode.PROPOSAL_QUALITY_REGRESSION,
            {item.code for item in comparison.findings},
        )

    def test_candidate_passing_a_case_the_control_failed_is_superior(self) -> None:
        comparison = compare_case(
            _record(
                PairedArmKind.DEFAULT_QWEN_CONTROL,
                independent=CaseOutcome.FAILED,
                delivered=CaseOutcome.FAILED,
            ),
            _record(PairedArmKind.CAPABILITY_SANDBOX),
        )
        self.assertEqual(comparison.proposal_verdict, PairedVerdict.SUPERIOR)
        self.assertEqual(comparison.delivered_verdict, PairedVerdict.SUPERIOR)

    def test_missing_evidence_accepted_as_complete_always_regresses(self) -> None:
        comparison = compare_case(
            _record(PairedArmKind.DEFAULT_QWEN_CONTROL),
            _record(
                PairedArmKind.CAPABILITY_SANDBOX,
                detection=_detection(missing_evidence_accepted_as_complete=1),
            ),
        )
        self.assertEqual(comparison.delivered_verdict, PairedVerdict.REGRESSION)
        self.assertIn(
            PairedFindingCode.MISSING_EVIDENCE_ACCEPTED_AS_COMPLETE,
            {item.code for item in comparison.findings},
        )

    def test_a_defect_escaping_acceptance_regresses(self) -> None:
        comparison = compare_case(
            _record(PairedArmKind.DEFAULT_QWEN_CONTROL),
            _record(
                PairedArmKind.CAPABILITY_SANDBOX,
                detection=_detection(defects_escaping_acceptance=1),
            ),
        )
        self.assertEqual(comparison.delivered_verdict, PairedVerdict.REGRESSION)

    def test_a_new_delivered_regression_counts_even_with_a_passing_case(self) -> None:
        comparison = compare_case(
            _record(PairedArmKind.DEFAULT_QWEN_CONTROL),
            _record(
                PairedArmKind.CAPABILITY_SANDBOX,
                delivered_regressions=["export ordering is nondeterministic"],
            ),
        )
        self.assertEqual(comparison.delivered_verdict, PairedVerdict.REGRESSION)
        self.assertIn(
            PairedFindingCode.ADDITIONAL_DELIVERED_REGRESSION,
            {item.code for item in comparison.findings},
        )

    def test_a_regression_shared_with_the_control_is_not_additional(self) -> None:
        shared = ["the status filter never reaches the service"]
        comparison = compare_case(
            _record(
                PairedArmKind.DEFAULT_QWEN_CONTROL, delivered_regressions=shared
            ),
            _record(PairedArmKind.CAPABILITY_SANDBOX, delivered_regressions=shared),
        )
        self.assertEqual(comparison.delivered_verdict, PairedVerdict.PARITY)

    def test_a_dropped_capability_is_a_finding(self) -> None:
        comparison = compare_case(
            _record(PairedArmKind.DEFAULT_QWEN_CONTROL),
            _record(
                PairedArmKind.CAPABILITY_SANDBOX,
                absent={BaselineCapability.PERSISTENT_SHELL},
            ),
        )
        finding = next(
            item
            for item in comparison.findings
            if item.code == PairedFindingCode.CAPABILITY_REGRESSION
        )
        self.assertIn("persistent_shell", finding.detail)

    def test_an_unproven_capability_counts_as_lost(self) -> None:
        candidate = _record(PairedArmKind.CAPABILITY_SANDBOX)
        candidate.manifest.capabilities = [
            item
            for item in candidate.manifest.capabilities
            if item.capability != BaselineCapability.PERSISTENT_SHELL
        ]
        comparison = compare_case(
            _record(PairedArmKind.DEFAULT_QWEN_CONTROL), candidate
        )
        self.assertIn(
            PairedFindingCode.CAPABILITY_REGRESSION,
            {item.code for item in comparison.findings},
        )

    def test_a_mismatched_controlled_variable_is_incomparable(self) -> None:
        comparison = compare_case(
            _record(PairedArmKind.DEFAULT_QWEN_CONTROL),
            _record(
                PairedArmKind.CAPABILITY_SANDBOX,
                manifest_overrides={"seed_commit": "2" * 40},
            ),
        )
        self.assertEqual(comparison.proposal_verdict, PairedVerdict.INCOMPARABLE)
        self.assertEqual(comparison.delivered_verdict, PairedVerdict.INCOMPARABLE)
        self.assertIn(
            PairedFindingCode.MATCHED_MANIFEST_MISMATCH,
            {item.code for item in comparison.findings},
        )

    def test_an_unrecorded_controlled_variable_is_incomparable(self) -> None:
        comparison = compare_case(
            _record(PairedArmKind.DEFAULT_QWEN_CONTROL),
            _record(
                PairedArmKind.CAPABILITY_SANDBOX,
                manifest_overrides={"seed_commit": None},
            ),
        )
        self.assertEqual(comparison.proposal_verdict, PairedVerdict.INCOMPARABLE)
        self.assertIn(
            PairedFindingCode.MATCHED_MANIFEST_UNRECORDED,
            {item.code for item in comparison.findings},
        )

    def test_differing_prompts_and_cli_versions_stay_comparable(self) -> None:
        # The two arms have different prompts by construction. Requiring those
        # to match would make every legitimate comparison incomparable.
        comparison = compare_case(
            _record(PairedArmKind.DEFAULT_QWEN_CONTROL),
            _record(
                PairedArmKind.CAPABILITY_SANDBOX,
                manifest_overrides={
                    "system_prompt_sha256": "b" * 64,
                    "cli_version": "qwen-code 1.2.3",
                    "container_image_digest": "sha256:beef",
                },
            ),
        )
        self.assertEqual(comparison.proposal_verdict, PairedVerdict.PARITY)

    def test_pairing_different_cases_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            compare_case(
                _record(PairedArmKind.DEFAULT_QWEN_CONTROL, case_id="a"),
                _record(PairedArmKind.CAPABILITY_SANDBOX, case_id="b"),
            )

    def test_unattributed_failed_calls_are_flagged(self) -> None:
        comparison = compare_case(
            _record(PairedArmKind.DEFAULT_QWEN_CONTROL),
            _record(
                PairedArmKind.CAPABILITY_SANDBOX,
                proposal=_proposal(model_calls=18, attempted_model_calls=20),
            ),
        )
        self.assertIn(
            PairedFindingCode.UNCLASSIFIED_CEILING_FAILURE,
            {item.code for item in comparison.findings},
        )


class ReleaseGateTests(unittest.TestCase):
    def _corpus(self, **candidate_kwargs: object):
        detection = candidate_kwargs.pop(
            "detection",
            _detection(integrated_defects_caught_before_delivery=1),
        )
        return score_paired_corpus(
            [
                _record(
                    PairedArmKind.DEFAULT_QWEN_CONTROL,
                    proposal=_proposal(input_tokens=2_080_801),
                ),
                _record(
                    PairedArmKind.CAPABILITY_SANDBOX,
                    detection=detection,
                    **candidate_kwargs,
                ),
            ],
            corpus_id="EVAL-PAIRED-TEST",
        )

    def test_a_clean_corpus_passes_all_four_gates(self) -> None:
        report = self._corpus()
        for gate in ReleaseGate:
            self.assertEqual(
                report.gate(gate).status,
                GateStatus.PASSED,
                msg=f"{gate.value}: {report.gate(gate).detail}",
            )
        self.assertTrue(report.recommended_for_default)

    def test_parity_without_a_detection_advantage_is_not_superiority(self) -> None:
        report = self._corpus(detection=_detection())
        self.assertEqual(
            report.gate(ReleaseGate.DELIVERED_SUPERIORITY).status, GateStatus.FAILED
        )
        # The other three gates must still be reported on their own terms.
        self.assertEqual(
            report.gate(ReleaseGate.PROPOSAL_NON_INFERIORITY).status,
            GateStatus.PASSED,
        )
        self.assertFalse(report.recommended_for_default)

    def test_a_cheap_arm_cannot_buy_off_a_quality_regression(self) -> None:
        # Far fewer input tokens, but the case regressed. The efficiency gate
        # passes and the proposal gate fails; neither cancels the other and the
        # mode is not recommended.
        report = self._corpus(
            independent=CaseOutcome.FAILED,
            proposal=_proposal(input_tokens=1_000),
        )
        self.assertEqual(
            report.gate(ReleaseGate.EFFICIENCY).status, GateStatus.PASSED
        )
        self.assertEqual(
            report.gate(ReleaseGate.PROPOSAL_NON_INFERIORITY).status,
            GateStatus.FAILED,
        )
        self.assertFalse(report.recommended_for_default)

    def test_higher_input_tokens_fail_only_the_efficiency_gate(self) -> None:
        report = self._corpus(proposal=_proposal(input_tokens=3_000_000))
        self.assertEqual(report.gate(ReleaseGate.EFFICIENCY).status, GateStatus.FAILED)
        self.assertEqual(
            report.gate(ReleaseGate.PROPOSAL_NON_INFERIORITY).status,
            GateStatus.PASSED,
        )
        self.assertIn(
            PairedFindingCode.INPUT_TOKEN_REGRESSION,
            {item.code for item in report.gate(ReleaseGate.EFFICIENCY).findings},
        )

    def test_a_lost_capability_fails_only_the_capability_gate(self) -> None:
        report = self._corpus(absent={BaselineCapability.ARBITRARY_SANDBOX_COMMANDS})
        self.assertEqual(
            report.gate(ReleaseGate.CAPABILITY_PRESERVATION).status, GateStatus.FAILED
        )
        self.assertEqual(
            report.gate(ReleaseGate.PROPOSAL_NON_INFERIORITY).status,
            GateStatus.PASSED,
        )
        self.assertFalse(report.recommended_for_default)

    def test_an_empty_corpus_is_unmeasured_not_passing(self) -> None:
        report = score_paired_corpus([], corpus_id="EVAL-PAIRED-EMPTY")
        for gate in ReleaseGate:
            self.assertEqual(report.gate(gate).status, GateStatus.UNMEASURED)
        self.assertFalse(report.recommended_for_default)

    def test_an_unpaired_case_is_dropped_rather_than_scored(self) -> None:
        report = score_paired_corpus(
            [_record(PairedArmKind.CAPABILITY_SANDBOX)],
            corpus_id="EVAL-PAIRED-UNPAIRED",
        )
        self.assertEqual(report.cases, [])
        self.assertFalse(report.recommended_for_default)

    def test_no_combined_score_field_exists(self) -> None:
        # The handoff forbids collapsing the four gates into one number. The
        # absence of such a field is the enforcement, so it is asserted.
        fields = set(score_paired_corpus([], corpus_id="X").model_dump())
        self.assertFalse(
            {"overall_score", "score", "aggregate_score", "composite"} & fields
        )


class CrisisAtlasFactsTests(unittest.TestCase):
    def test_slice_two_is_both_a_proposal_and_a_detection_miss(self) -> None:
        slice_two = next(
            item for item in CRISIS_ATLAS_SLICE_FACTS if item.slice_number == 2
        )
        self.assertTrue(slice_two.proposal_miss)
        self.assertTrue(slice_two.detection_miss)
        self.assertEqual(
            set(slice_two.misses),
            {CrisisAtlasMissKind.PROPOSAL, CrisisAtlasMissKind.DETECTION},
        )

    def test_slice_one_is_a_proposal_miss_only(self) -> None:
        # The harness correctly refused to complete Slice 1, so charging it a
        # detection miss would be dishonest.
        slice_one = next(
            item for item in CRISIS_ATLAS_SLICE_FACTS if item.slice_number == 1
        )
        self.assertTrue(slice_one.proposal_miss)
        self.assertFalse(slice_one.detection_miss)

    def test_the_arms_rescore_without_any_provider(self) -> None:
        report = score_paired_corpus(
            crisis_atlas_records(),
            corpus_id="EVAL-PAIRED-CRISIS-ATLAS",
            candidate_arm=PairedArmKind.LEGACY_LOCAL_POWER,
        )
        self.assertEqual(len(report.cases), 1)
        self.assertEqual(report.cases[0].case_id, CRISIS_ATLAS_CASE_ID)

    def test_the_historical_arms_are_not_a_matched_pair(self) -> None:
        # The sliced arm's seed commit and per-slice output cap were never
        # written down, so no win or loss can be read from these two runs.
        report = score_paired_corpus(
            crisis_atlas_records(),
            corpus_id="EVAL-PAIRED-CRISIS-ATLAS",
            candidate_arm=PairedArmKind.LEGACY_LOCAL_POWER,
        )
        case = report.cases[0]
        self.assertEqual(case.proposal_verdict, PairedVerdict.INCOMPARABLE)
        self.assertEqual(case.delivered_verdict, PairedVerdict.INCOMPARABLE)
        self.assertEqual(
            report.gate(ReleaseGate.PROPOSAL_NON_INFERIORITY).status,
            GateStatus.UNMEASURED,
        )
        # A token median across arms that never shared their controlled
        # variables measures nothing, so efficiency must not report a pass here.
        self.assertEqual(
            report.gate(ReleaseGate.EFFICIENCY).status, GateStatus.UNMEASURED
        )
        # The capability gate is still decidable: both records observe all
        # eight capabilities, and the sliced arm dropped four of them.
        self.assertEqual(
            report.gate(ReleaseGate.CAPABILITY_PRESERVATION).status,
            GateStatus.FAILED,
        )
        self.assertFalse(report.recommended_for_default)
        unrecorded = [
            item.detail
            for item in case.findings
            if item.code == PairedFindingCode.MATCHED_MANIFEST_UNRECORDED
        ]
        self.assertTrue(any("seed_commit" in detail for detail in unrecorded))

    def test_the_sliced_arm_records_three_false_completions(self) -> None:
        record = sliced_local_power_record()
        self.assertEqual(record.detection.false_complete_count, 3)
        self.assertEqual(record.detection.missing_evidence_accepted_as_complete, 1)
        self.assertEqual(record.independent_case_outcome, CaseOutcome.FAILED)
        # Codex repair is recorded on the delivered outcome only; it never
        # improves the model's proposal score.
        self.assertEqual(record.delivered_case_outcome, CaseOutcome.PASSED)
        self.assertEqual(
            record.proposal.obligations_implemented_before_repair.numerator, 0
        )

    def test_the_sliced_arm_lost_baseline_capabilities(self) -> None:
        manifest = sliced_local_power_record().manifest
        for capability in (
            BaselineCapability.PERSISTENT_SHELL,
            BaselineCapability.ARBITRARY_SANDBOX_COMMANDS,
            BaselineCapability.SELF_DIRECTED_TEST_DEBUG_LOOP,
            BaselineCapability.MULTI_FILE_CHANGE_WITHOUT_JSON_SERIALIZATION,
        ):
            self.assertEqual(
                manifest.capability_status(capability),
                CapabilityStatus.ABSENT,
                msg=capability.value,
            )

    def test_the_control_had_no_compaction(self) -> None:
        manifest = unrestricted_control_record().manifest
        self.assertEqual(
            manifest.capability_status(
                BaselineCapability.CONTEXT_CONTINUATION_OR_COMPACTION
            ),
            CapabilityStatus.ABSENT,
        )

    def test_control_ceiling_events_are_classified_as_recorded(self) -> None:
        events = unrestricted_control_record().proposal.ceiling_events
        self.assertEqual(
            [item.reason for item in events],
            [
                CeilingStopReason.INPUT_CONTEXT_EXHAUSTED,
                CeilingStopReason.PROVIDER_ERROR_AFTER_ROLLOVER,
            ],
        )
        exhausted = events[0]
        self.assertEqual(
            classify_ceiling_stop_reason(
                finish_reason=exhausted.finish_reason,
                input_tokens=exhausted.input_tokens,
                output_tokens=exhausted.output_tokens,
                context_limit=exhausted.context_limit,
                max_output_tokens=exhausted.max_output_tokens,
            ),
            CeilingStopReason.INPUT_CONTEXT_EXHAUSTED,
        )

    def test_published_telemetry_is_preserved_exactly(self) -> None:
        control = unrestricted_control_record().proposal
        sliced = sliced_local_power_record().proposal
        self.assertEqual(
            (control.model_calls, control.input_tokens, control.output_tokens),
            (62, 2_080_801, 35_787),
        )
        self.assertEqual(
            (sliced.model_calls, sliced.input_tokens, sliced.output_tokens),
            (19, 258_632, 55_364),
        )

    def test_unrecorded_repair_distance_is_none_not_zero(self) -> None:
        # Zero would read as "no repair was needed", which is the opposite of
        # what an unrecorded repair means.
        proposal = sliced_local_power_record().proposal
        self.assertIsNone(proposal.repair_distance_files)
        self.assertIsNone(proposal.repair_distance_lines)

    def test_unpriced_frontier_repair_is_flagged(self) -> None:
        comparison = compare_case(
            unrestricted_control_record(), sliced_local_power_record()
        )
        # The pair is incomparable, so the itemization warning is not reachable
        # through the delivered path; assert it directly on a matched pair.
        self.assertEqual(comparison.delivered_verdict, PairedVerdict.INCOMPARABLE)
        matched = compare_case(
            _record(PairedArmKind.DEFAULT_QWEN_CONTROL),
            _record(
                PairedArmKind.CAPABILITY_SANDBOX,
                repair_actors=["frontier_model"],
                detection=_detection(integrated_defects_caught_before_delivery=1),
            ),
        )
        self.assertIn(
            PairedFindingCode.FRONTIER_REPAIR_NOT_ITEMIZED,
            {item.code for item in matched.findings},
        )


class PairedReportTests(unittest.TestCase):
    def test_markdown_reports_every_gate_and_flags_unmeasured(self) -> None:
        report = score_paired_corpus(
            crisis_atlas_records(),
            corpus_id="EVAL-PAIRED-DOC",
            candidate_arm=PairedArmKind.LEGACY_LOCAL_POWER,
        )
        text = render_paired_markdown(report)
        for gate in ReleaseGate:
            self.assertIn(gate.value, text)
        self.assertIn("Model proposal quality", text)
        self.assertIn("Harness defect-detection quality", text)
        self.assertIn("Not a matched pair", text)
        self.assertIn("is an absence of evidence, not a pass", text)
        self.assertIn("Recommended as the default local mode: **no**", text)

    def test_write_paired_corpus_round_trips(self) -> None:
        report = score_paired_corpus(
            crisis_atlas_records(),
            corpus_id="EVAL-PAIRED-RT",
            candidate_arm=PairedArmKind.LEGACY_LOCAL_POWER,
        )
        with tempfile.TemporaryDirectory() as tmp:
            write_paired_corpus(Path(tmp), report)
            payload = json.loads((Path(tmp) / "paired.json").read_text("utf-8"))
            self.assertEqual(payload["corpus_id"], "EVAL-PAIRED-RT")
            self.assertTrue((Path(tmp) / "paired.md").read_text("utf-8"))


class PairedCliTests(unittest.TestCase):
    def test_default_invocation_rescores_the_frozen_arms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = _score_paired_corpus_command(
                root, [], root / "out", PairedArmKind.LEGACY_LOCAL_POWER.value
            )
            self.assertEqual(len(payload["cases"]), 1)
            self.assertFalse(payload["recommended_for_default"])
            self.assertTrue((root / "out" / "paired.json").is_file())

    def test_records_are_loaded_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "records.json"
            path.write_text(
                json.dumps(
                    [
                        record.model_dump(mode="json")
                        for record in (
                            _record(
                                PairedArmKind.DEFAULT_QWEN_CONTROL,
                                # The efficiency gate is real: equal input
                                # tokens is not an improvement over the control.
                                proposal=_proposal(input_tokens=2_080_801),
                            ),
                            _record(
                                PairedArmKind.CAPABILITY_SANDBOX,
                                detection=_detection(
                                    integrated_defects_caught_before_delivery=1
                                ),
                            ),
                        )
                    ]
                ),
                encoding="utf-8",
            )
            payload = _score_paired_corpus_command(
                root, [path], root / "out", PairedArmKind.CAPABILITY_SANDBOX.value
            )
            self.assertTrue(payload["recommended_for_default"])

    def test_a_missing_record_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(Exception):
                _score_paired_corpus_command(
                    root,
                    [root / "absent.json"],
                    None,
                    PairedArmKind.CAPABILITY_SANDBOX.value,
                )


if __name__ == "__main__":
    unittest.main()
