"""The Crisis Atlas arms, frozen as replayable evaluation facts.

Slice 0 of `docs/handoff-2026-07-30-qwen-baseline-preserving-superiority.md`
requires that the existing arms can be rescored **without model calls**, so that
changing the scoring rules never requires re-running inference, and so that
Slice 2's failure is recorded once and for all as *two* distinct misses:

* a **proposal miss** — Qwen proposed one partial file at the wrong package
  path, created no export service, and wrote no tests; and
* a **detection miss** — Apoapsis applied that edit, saw inherited tests stay
  green, and terminated the session as `COMPLETE`.

Every number below traces to one of two dated records and nothing is inferred:

* `docs/evaluation/crisis-atlas-qwen-cli-control-2026-07-30.md`
* `docs/evaluation/crisis-atlas-64k-codex-frontier-trial-2026-07-30.md`

Where a record does not contain a figure, the field is left unrecorded and the
corresponding metric is `UNMEASURED` with a written reason. That is why the two
arms rescore as `INCOMPARABLE` rather than as a result: they were not run as a
matched pair, the sliced arm's seed commit was never written down, and Codex sat
inside one arm's loop. Producing "incomparable" from real historical evidence is
the correct answer, and having the scorer say so is the point of Slice 0.

`ProductionArtifactDefect.declared_path` carries a component identifier rather
than a path where the trial record named the component but not its declared
package path. The `detail` field says so in each case.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from apoapsis.evaluation.paired import (
    ArtifactDefectKind,
    BaselineCapability,
    CapabilityObservation,
    CapabilityStatus,
    CaseOutcome,
    HarnessDetectionScorecard,
    ModelProposalScorecard,
    PairedArmKind,
    PairedArmRecord,
    PairedRunManifest,
    ProductionArtifactDefect,
    RepairActorClass,
)
from apoapsis.evaluation.schemas import MetricStatus, RateMetric
from apoapsis.models.ceilings import CeilingEvent, CeilingStopReason
from apoapsis.specification.schema import StrictModel

CRISIS_ATLAS_CASE_ID = "crisis-atlas"

CONTROL_RECORD_PATH = "docs/evaluation/crisis-atlas-qwen-cli-control-2026-07-30.md"
SLICED_RECORD_PATH = (
    "docs/evaluation/crisis-atlas-64k-codex-frontier-trial-2026-07-30.md"
)


def _unmeasured(reason: str) -> RateMetric:
    return RateMetric(status=MetricStatus.UNMEASURED, value=None, reason=reason)


def _measured(numerator: int, denominator: int) -> RateMetric:
    return RateMetric(
        status=MetricStatus.MEASURED,
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator,
    )


class CrisisAtlasMissKind(StrEnum):
    #: The model proposed the wrong or incomplete thing.
    PROPOSAL = "proposal"
    #: The harness accepted, or failed to notice, what the model proposed.
    DETECTION = "detection"


class CrisisAtlasSliceFact(StrictModel):
    """One slice of the sliced arm, with its misses attributed by owner.

    Attribution matters more than the count. Slice 2 is the handoff's central
    example precisely because it belongs to *both* owners, and a record that
    charged it only to the model would justify the wrong repair.
    """

    slice_number: int = Field(ge=1)
    title: str = Field(min_length=1)
    reported_outcome: str = Field(min_length=1)
    misses: list[CrisisAtlasMissKind] = Field(default_factory=list)
    detail: str = Field(min_length=1)
    verified_checkpoint: str | None = None

    @property
    def proposal_miss(self) -> bool:
        return CrisisAtlasMissKind.PROPOSAL in self.misses

    @property
    def detection_miss(self) -> bool:
        return CrisisAtlasMissKind.DETECTION in self.misses


CRISIS_ATLAS_SLICE_FACTS: tuple[CrisisAtlasSliceFact, ...] = (
    CrisisAtlasSliceFact(
        slice_number=1,
        title="domain/persistence",
        reported_outcome="HUMAN_REVIEW_REQUIRED after 12 calls",
        misses=[CrisisAtlasMissKind.PROPOSAL],
        detail=(
            "unit and behavioral checks passed; the configured launch check failed "
            "because `tests.test_launch` was absent. The harness correctly refused "
            "to complete, so this is a proposal miss only."
        ),
        verified_checkpoint="f7785bfad1ec99f0c9c9fcb3d2ababa3d85b4c6b",
    ),
    CrisisAtlasSliceFact(
        slice_number=2,
        title="services/exports",
        reported_outcome="COMPLETE in one call",
        misses=[CrisisAtlasMissKind.PROPOSAL, CrisisAtlasMissKind.DETECTION],
        detail=(
            "Proposal miss: Qwen said it would implement both `IncidentService` and "
            "`ExportService` with tests, then proposed a single partial file at the "
            "wrong package path `services/incident_service.py`, with no export "
            "service and no tests. The response was short, so this was not output "
            "truncation, and the prompt carried repository evidence, so it was not a "
            "missing-context failure. Detection miss: Apoapsis applied that one "
            "change, ran inherited checks that never imported the new file, observed "
            "green, and terminated the session as COMPLETE. Qwen never received a "
            "turn in which it could notice its own omissions."
        ),
        verified_checkpoint="4693b745cce97b223caf08749fe367e28fa28146",
    ),
    CrisisAtlasSliceFact(
        slice_number=3,
        title="HTTP API",
        reported_outcome="COMPLETE in one call",
        misses=[CrisisAtlasMissKind.PROPOSAL, CrisisAtlasMissKind.DETECTION],
        detail=(
            "Codex found a nonexistent static directory, unreachable export routes, "
            "non-serializable timeline/action responses, and crashing traversal "
            "handling. The harness had already reported COMPLETE."
        ),
        verified_checkpoint="91bd5cdace7d56f108f4e37543f9b2aa842e800a",
    ),
    CrisisAtlasSliceFact(
        slice_number=4,
        title="dashboard/integration",
        reported_outcome="COMPLETE after five calls and three verification runs",
        misses=[CrisisAtlasMissKind.PROPOSAL, CrisisAtlasMissKind.DETECTION],
        detail=(
            "The browser flow worked, but README flags and check names were wrong, "
            "the configured behavioral and launch checks did not prove their labels, "
            "mutation failures used alerts, and detail controls lacked labels. A "
            "command named `behavioral-integration` was treated as evidence that "
            "integration occurred."
        ),
        verified_checkpoint="0d591d7bbf9eebd276df0bc6677f24d19f505f5e",
    ),
)


def unrestricted_control_record() -> PairedArmRecord:
    """The unrestricted Qwen CLI control, per `CONTROL_RECORD_PATH`."""

    manifest = PairedRunManifest(
        arm=PairedArmKind.DEFAULT_QWEN_CONTROL,
        case_id=CRISIS_ATLAS_CASE_ID,
        seed_commit="197b3610e5720cf36718c548fa19c05fe784a978",
        model_name="qwen3.6-27b",
        quantization="Q4_K_M",
        temperature=0.0,
        context_limit_tokens=65_536,
        max_output_tokens=16_384,
        wall_clock_ceiling_seconds=1_800.0,
        cpu_allocation="4 CPUs",
        network_policy="none",
        mount_policy="single disposable clone of the seed commit at /workspace",
        capabilities=[
            CapabilityObservation(
                capability=BaselineCapability.PERSISTENT_SHELL,
                status=CapabilityStatus.PROVIDED,
                evidence="62 successful shell actions inside the disposable container",
            ),
            CapabilityObservation(
                capability=BaselineCapability.REPOSITORY_WIDE_INSPECTION,
                status=CapabilityStatus.PROVIDED,
                evidence="the agent inspected the seed before implementing",
            ),
            CapabilityObservation(
                capability=BaselineCapability.ORDINARY_FILE_EDITING,
                status=CapabilityStatus.PROVIDED,
                evidence="create, overwrite, and delete anywhere under /workspace",
            ),
            CapabilityObservation(
                capability=BaselineCapability.ARBITRARY_SANDBOX_COMMANDS,
                status=CapabilityStatus.PROVIDED,
                evidence="any Bash command, capped at 300 seconds per action",
            ),
            CapabilityObservation(
                capability=BaselineCapability.SELF_DIRECTED_TEST_DEBUG_LOOP,
                status=CapabilityStatus.PROVIDED,
                evidence=(
                    "diagnosed nine fixture errors and two README failures, repaired "
                    "them, and reran the suites without external input"
                ),
            ),
            CapabilityObservation(
                capability=(
                    BaselineCapability.MULTI_FILE_CHANGE_WITHOUT_JSON_SERIALIZATION
                ),
                status=CapabilityStatus.PROVIDED,
                evidence="edits were ordinary file writes, not a typed action envelope",
            ),
            CapabilityObservation(
                capability=BaselineCapability.PERSISTENT_WORKING_DIRECTORY,
                status=CapabilityStatus.PROVIDED,
                evidence="one container worktree survived across all actions",
            ),
            CapabilityObservation(
                capability=BaselineCapability.CONTEXT_CONTINUATION_OR_COMPACTION,
                status=CapabilityStatus.ABSENT,
                evidence=(
                    "the arm had no compaction: at 65,536 tokens the tool-call JSON "
                    "was truncated and the next request returned HTTP 500. The "
                    "evaluator, not the agent, started a fresh continuation."
                ),
            ),
        ],
    )
    proposal = ModelProposalScorecard(
        obligations_implemented_before_repair=_measured(4, 4),
        # From the record's independent verification table: unittest, behavioral,
        # launch, compileall, git diff --check, and the forbidden-storage search
        # passed; strict verify-web-product and the browser lifecycle failed.
        independent_checks_passed_at_first_checkpoint=_measured(6, 8),
        production_artifact_defects=[],
        runtime_defects_found=1,
        repair_distance_files=None,
        repair_distance_lines=None,
        model_authored_test_relevance=_unmeasured(
            "all 88 model-authored tests passed while an acceptance-relevant status "
            "filter defect survived; per-test relevance to acceptance criteria was "
            "never recorded"
        ),
        ceiling_events=[
            CeilingEvent(
                reason=CeilingStopReason.INPUT_CONTEXT_EXHAUSTED,
                input_tokens=64_409,
                output_tokens=1_127,
                context_limit=65_536,
                max_output_tokens=16_384,
                finish_reason="length",
                detail=(
                    "prompt plus completion consumed the whole 65,536-token window "
                    "during a README write; the tool-call JSON was truncated and "
                    "could not execute. This is a context-ceiling failure, not an "
                    "output-cap failure and not a model completion."
                ),
            ),
            CeilingEvent(
                reason=CeilingStopReason.PROVIDER_ERROR_AFTER_ROLLOVER,
                context_limit=65_536,
                detail=(
                    "the following request received HTTP 500 because the "
                    "conversation no longer fit the 64K window"
                ),
            ),
        ],
        model_reasoning_failures=0,
        model_calls=62,
        attempted_model_calls=63,
        input_tokens=2_080_801,
        output_tokens=35_787,
        provider_latency_seconds=1_052.3,
        first_checkpoint_coherent=True,
        seconds_to_first_coherent_checkpoint=None,
    )
    detection = HarnessDetectionScorecard(
        defects_detected=_unmeasured(
            "the control has no detection layer; its only acceptance step was its "
            "own completion claim"
        ),
        negative_controls_caught=_unmeasured(
            "no negative controls were injected into this arm"
        ),
        criteria_with_current_state_evidence=_unmeasured(
            "the arm mapped no acceptance criteria to current-state evidence"
        ),
        structured_witness_coverage=_unmeasured(
            "the arm emitted no structured witnesses"
        ),
        # Qwen claimed all acceptance criteria, including browser dashboard
        # filtering and web integrity, were satisfied. Independent verification
        # disproved both.
        false_complete_count=1,
        weak_command_name_only_claims_refused=0,
        stale_or_inherited_evidence_rejected=0,
        integrated_defects_caught_before_delivery=0,
        # The broken status filter and the strict verify-web-product failure were
        # both found only by verification the arm itself never ran.
        defects_escaping_acceptance=2,
        missing_evidence_accepted_as_complete=0,
    )
    return PairedArmRecord(
        manifest=manifest,
        proposal=proposal,
        detection=detection,
        independent_case_outcome=CaseOutcome.FAILED,
        delivered_case_outcome=CaseOutcome.FAILED,
        independent_checks_failed=[
            "verify-web-product --forbid-external-resources --treat-warnings-as-errors",
            "independent browser lifecycle: status filter",
        ],
        delivered_regressions=[
            "GET /api/incidents?status=... is parsed but never reaches the service "
            "filter, so every status returns the same incidents"
        ],
        repair_actors=[RepairActorClass.LOCAL_MODEL],
        notes=(
            f"Source: {CONTROL_RECORD_PATH}. The arm is preserved at "
            "`.apoapsis-eval/crisis-atlas-qwen-cli-unrestricted-64k-2026-07-30`."
        ),
    )


def sliced_local_power_record() -> PairedArmRecord:
    """The sliced Local Power arm plus Codex repair, per `SLICED_RECORD_PATH`."""

    manifest = PairedRunManifest(
        arm=PairedArmKind.LEGACY_LOCAL_POWER,
        case_id=CRISIS_ATLAS_CASE_ID,
        # Deliberately unrecorded: the trial record names four verified
        # checkpoints but never the seed commit the arm started from, so this
        # arm cannot honestly be paired with the control.
        seed_commit=None,
        model_name="qwen3.6-27b",
        quantization="Q4_K_M",
        endpoint="llama.cpp OpenAI-compatible endpoint",
        context_limit_tokens=65_536,
        # Slice 1 ran at 8,192 and Slices 2-4 at 16,384. A single value would
        # misreport half the run, so the controlled variable is unrecorded.
        max_output_tokens=None,
        capabilities=[
            CapabilityObservation(
                capability=BaselineCapability.PERSISTENT_SHELL,
                status=CapabilityStatus.ABSENT,
                evidence="Local Power accepts one typed JSON action per model call",
            ),
            CapabilityObservation(
                capability=BaselineCapability.REPOSITORY_WIDE_INSPECTION,
                status=CapabilityStatus.PROVIDED,
                evidence="`RepositoryInspector` bounded search and read actions",
            ),
            CapabilityObservation(
                capability=BaselineCapability.ORDINARY_FILE_EDITING,
                status=CapabilityStatus.PROVIDED,
                evidence="`create_file` and `propose_patch` actions (ADR 0057)",
            ),
            CapabilityObservation(
                capability=BaselineCapability.ARBITRARY_SANDBOX_COMMANDS,
                status=CapabilityStatus.ABSENT,
                evidence=(
                    "only owner-configured verification commands run; the model "
                    "cannot request an arbitrary command"
                ),
            ),
            CapabilityObservation(
                capability=BaselineCapability.SELF_DIRECTED_TEST_DEBUG_LOOP,
                status=CapabilityStatus.ABSENT,
                evidence=(
                    "ADR 0069 terminated the session once configured checks were "
                    "green, so Slice 2 never got a turn to inspect its own work"
                ),
            ),
            CapabilityObservation(
                capability=(
                    BaselineCapability.MULTI_FILE_CHANGE_WITHOUT_JSON_SERIALIZATION
                ),
                status=CapabilityStatus.ABSENT,
                evidence=(
                    "ADR 0071 atomic change sets improved multi-file granularity but "
                    "still serialize whole files into one JSON response"
                ),
            ),
            CapabilityObservation(
                capability=BaselineCapability.PERSISTENT_WORKING_DIRECTORY,
                status=CapabilityStatus.PROVIDED,
                evidence="one worktree persists across turns within a slice",
            ),
            CapabilityObservation(
                capability=BaselineCapability.CONTEXT_CONTINUATION_OR_COMPACTION,
                status=CapabilityStatus.PROVIDED,
                evidence=(
                    "`compact_observations` selects a bounded current view of the "
                    "append-only observation ledger"
                ),
            ),
        ],
    )
    proposal = ModelProposalScorecard(
        # Every one of the four slices required Codex repair before it became a
        # verified checkpoint.
        obligations_implemented_before_repair=_measured(0, 4),
        independent_checks_passed_at_first_checkpoint=_unmeasured(
            "the trial ran Codex inspection after Qwen stopped and did not record "
            "per-slice independent check counts before repair"
        ),
        production_artifact_defects=[
            ProductionArtifactDefect(
                kind=ArtifactDefectKind.WRONG_PATH,
                declared_path="IncidentService",
                observed_path="services/incident_service.py",
                detail=(
                    "Slice 2 proposed a partial incident service at the wrong "
                    "package path. The declared package path is named in the slice "
                    "task but was not copied into the trial record."
                ),
            ),
            ProductionArtifactDefect(
                kind=ArtifactDefectKind.MISSING,
                declared_path="ExportService",
                detail=(
                    "Slice 2 stated it would implement the export service and did "
                    "not create it; the declared path is not in the trial record."
                ),
            ),
            ProductionArtifactDefect(
                kind=ArtifactDefectKind.DEAD,
                declared_path="services/incident_service.py",
                observed_path="services/incident_service.py",
                detail=(
                    "the accepted Slice 2 skeleton was never imported by any test, "
                    "so the inherited suite stayed green without reaching it"
                ),
            ),
        ],
        # Slice 3: nonexistent static directory, unreachable export routes,
        # non-serializable timeline/action responses, crashing traversal handling.
        # Slice 4: wrong README flags/check names, checks that did not prove their
        # labels, alert-based mutation feedback, unlabeled detail controls.
        runtime_defects_found=8,
        repair_distance_files=None,
        repair_distance_lines=None,
        model_authored_test_relevance=_unmeasured(
            "the recorded slices authored no new tests of their own; Slice 2 wrote "
            "none at all, so relevance has no denominator"
        ),
        ceiling_events=[
            CeilingEvent(
                reason=CeilingStopReason.OUTPUT_CEILING_TRUNCATION,
                input_tokens=13_562,
                output_tokens=8_192,
                context_limit=65_536,
                max_output_tokens=8_192,
                finish_reason="length",
                detail="Slice 1 call 9 filled the old 8,192-token output cap",
            ),
            CeilingEvent(
                reason=CeilingStopReason.OUTPUT_CEILING_TRUNCATION,
                input_tokens=15_551,
                output_tokens=8_192,
                context_limit=65_536,
                max_output_tokens=8_192,
                finish_reason="length",
                detail="Slice 1 call 11 filled the old 8,192-token output cap",
            ),
        ],
        model_reasoning_failures=0,
        # The record's telemetry counts 19 provider-successful calls, two of which
        # returned an artifact the agent protocol could not use. Those two are the
        # ceiling events above, not reasoning failures.
        model_calls=19,
        attempted_model_calls=19,
        input_tokens=258_632,
        output_tokens=55_364,
        provider_latency_seconds=1_467.5,
        first_checkpoint_coherent=False,
        seconds_to_first_coherent_checkpoint=None,
    )
    detection = HarnessDetectionScorecard(
        defects_detected=_unmeasured(
            "Codex, not the harness, found the slice defects; the harness recorded "
            "no independent detection count"
        ),
        negative_controls_caught=_measured(1, 1),
        criteria_with_current_state_evidence=_unmeasured(
            "the trial predates current-state criterion mapping for this corpus"
        ),
        structured_witness_coverage=_unmeasured(
            "no versioned structured witnesses existed; a command named "
            "`behavioral-integration` was taken as evidence that integration occurred"
        ),
        # Slices 2, 3, and 4 each reported COMPLETE and each required repair.
        false_complete_count=3,
        weak_command_name_only_claims_refused=0,
        stale_or_inherited_evidence_rejected=0,
        integrated_defects_caught_before_delivery=0,
        defects_escaping_acceptance=0,
        # Slice 2 reached COMPLETE with no evidence that either declared service
        # class existed or was reached.
        missing_evidence_accepted_as_complete=1,
    )
    return PairedArmRecord(
        manifest=manifest,
        proposal=proposal,
        detection=detection,
        independent_case_outcome=CaseOutcome.FAILED,
        # Only after Codex repaired every slice.
        delivered_case_outcome=CaseOutcome.PASSED,
        independent_checks_failed=[
            "slice 1 configured launch check (tests.test_launch absent)",
        ],
        delivered_regressions=[],
        repair_actors=[RepairActorClass.FRONTIER_MODEL],
        # Not recorded: "Codex token use during the checkpoint repairs was not
        # recorded in the sliced provider telemetry, so no honest total-Qwen-
        # plus-Codex token comparison is available."
        frontier_repair_calls=0,
        frontier_repair_cost_usd=0.0,
        notes=(
            f"Source: {SLICED_RECORD_PATH}. Codex inspected and repaired every "
            "slice; those repairs were direct commits, not authoritative plan "
            "checkpoints, so they did not remain inside the state machine."
        ),
    )


def crisis_atlas_records() -> list[PairedArmRecord]:
    """Both frozen arms, ready to rescore with no provider involved."""

    return [unrestricted_control_record(), sliced_local_power_record()]
