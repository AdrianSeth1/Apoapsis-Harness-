"""Two scorecards, a paired-run manifest, and four separately reported gates.

`docs/handoff-2026-07-30-qwen-baseline-preserving-superiority.md` requires that
Apoapsis stop reporting one number for a harnessed arm. A harness can look good
on an average while being strictly worse than the model it wraps: the Crisis
Atlas sliced arm used eight times fewer input tokens than the unrestricted
control and still shipped a wrong-path service as `COMPLETE`.

So this module keeps three things apart that a single score would merge:

* **Model proposal quality** — what the inner model produced before any
  external repair. A frontier repair must never be able to rewrite this.
* **Harness defect-detection quality** — what the outer system caught, refused,
  or let escape. Independent of how good the proposal was.
* **Release gates** — four verdicts, computed and reported separately, because
  the handoff forbids letting a cheap failure cancel out a quality regression.

Nothing here calls a provider. Every function is a pure rescore over frozen
records, so historical arms can be re-evaluated as the scoring rules change.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from apoapsis.evaluation.schemas import MetricStatus, RateMetric
from apoapsis.models.ceilings import CeilingEvent
from apoapsis.specification.schema import StrictModel, utc_now


class PairedArmKind(StrEnum):
    """Which execution path produced an arm's result."""

    #: The normal Qwen coding CLI in the same hardened workcell, with no
    #: Apoapsis action protocol. This is the control the harness must not lose
    #: to, not a claim about host access.
    DEFAULT_QWEN_CONTROL = "default_qwen_control"
    #: The same inner CLI supervised by Apoapsis admission and acceptance.
    CAPABILITY_SANDBOX = "capability_sandbox"
    #: ADR 0071 typed atomic change sets. A diagnostic arm; never the baseline.
    LEGACY_LOCAL_POWER = "legacy_local_power"


class CaseOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class BaselineCapability(StrEnum):
    """The interface a harnessed model must not lose relative to the control.

    Enumerated verbatim from the handoff's capability-preservation gate so an
    arm that quietly drops one is visible rather than inferred from a score.
    """

    PERSISTENT_SHELL = "persistent_shell"
    REPOSITORY_WIDE_INSPECTION = "repository_wide_inspection"
    ORDINARY_FILE_EDITING = "ordinary_file_editing"
    ARBITRARY_SANDBOX_COMMANDS = "arbitrary_sandbox_commands"
    SELF_DIRECTED_TEST_DEBUG_LOOP = "self_directed_test_debug_loop"
    MULTI_FILE_CHANGE_WITHOUT_JSON_SERIALIZATION = (
        "multi_file_change_without_json_serialization"
    )
    PERSISTENT_WORKING_DIRECTORY = "persistent_working_directory"
    CONTEXT_CONTINUATION_OR_COMPACTION = "context_continuation_or_compaction"


class CapabilityStatus(StrEnum):
    PROVIDED = "provided"
    ABSENT = "absent"
    #: Neither demonstrated nor ruled out by this arm's evidence. Treated as
    #: absent by the gate, because an unproven capability is not a preserved
    #: one, but reported distinctly so it can be measured rather than argued.
    UNPROVEN = "unproven"


class CapabilityObservation(StrictModel):
    capability: BaselineCapability
    status: CapabilityStatus
    evidence: str = Field(min_length=1)


class ArtifactDefectKind(StrEnum):
    MISSING = "missing"
    WRONG_PATH = "wrong_path"
    PLACEHOLDER = "placeholder"
    #: Present and syntactically fine, but nothing reaches it.
    DEAD = "dead"


class ProductionArtifactDefect(StrictModel):
    kind: ArtifactDefectKind
    declared_path: str = Field(min_length=1)
    observed_path: str | None = None
    detail: str = Field(min_length=1)


class RepairActorClass(StrEnum):
    LOCAL_MODEL = "local_model"
    FRONTIER_MODEL = "frontier_model"
    HUMAN = "human"


class PairedRunManifest(StrictModel):
    """Everything that must be bound and recorded for a comparison to be honest.

    A field left `None` is an *unrecorded* variable, not a matched one. The
    comparison refuses to call two arms paired when a matched variable is
    unrecorded, which is why the historical Crisis Atlas arms rescore as
    incomparable rather than as a win.
    """

    schema_version: str = "1.0"
    arm: PairedArmKind
    case_id: str = Field(min_length=1)
    seed_commit: str | None = None
    worktree_fingerprint: str | None = None
    task_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model_file_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model_name: str | None = None
    quantization: str | None = None
    endpoint: str | None = None
    sampling_seed: int | None = None
    temperature: float | None = Field(default=None, ge=0)
    server_flags_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_limit_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    wall_clock_ceiling_seconds: float | None = Field(default=None, gt=0)
    cpu_allocation: str | None = None
    gpu_allocation: str | None = None
    network_policy: str | None = None
    mount_policy: str | None = None
    verifier_version: str | None = None
    # Recorded but deliberately *not* required to match: the two arms have
    # different prompts and tooling by construction. Requiring equality here
    # would make every valid comparison incomparable.
    system_prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    user_prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    cli_version: str | None = None
    container_image_digest: str | None = None
    capabilities: list[CapabilityObservation] = Field(default_factory=list)

    def capability_status(self, capability: BaselineCapability) -> CapabilityStatus:
        for observation in self.capabilities:
            if observation.capability == capability:
                return observation.status
        return CapabilityStatus.UNPROVEN


#: Variables that must be identical for two arms to be a matched pair. The
#: prompt hashes, CLI version, and image digest are excluded on purpose (see
#: `PairedRunManifest`); everything physically shared by the two runs is here.
MATCHED_MANIFEST_FIELDS: tuple[str, ...] = (
    "seed_commit",
    "worktree_fingerprint",
    "task_sha256",
    "plan_sha256",
    "model_file_sha256",
    "model_name",
    "quantization",
    "endpoint",
    "sampling_seed",
    "temperature",
    "server_flags_sha256",
    "context_limit_tokens",
    "max_output_tokens",
    "wall_clock_ceiling_seconds",
    "cpu_allocation",
    "gpu_allocation",
    "network_policy",
    "mount_policy",
    "verifier_version",
)


class ModelProposalScorecard(StrictModel):
    """What the inner model produced, before any external repair touched it.

    A stronger reviewer's repair is recorded on the delivered result, never
    here. That separation is the point: the handoff forbids using a Codex fix
    to improve the local model's proposal score.
    """

    schema_version: str = "1.0"
    obligations_implemented_before_repair: RateMetric
    independent_checks_passed_at_first_checkpoint: RateMetric
    production_artifact_defects: list[ProductionArtifactDefect] = Field(
        default_factory=list
    )
    runtime_defects_found: int = Field(default=0, ge=0)
    # `None` means the repair was never measured in files/lines. Defaulting an
    # unmeasured distance to zero would read as "no repair was needed", which is
    # the opposite of what an unrecorded repair means.
    repair_distance_files: int | None = Field(default=None, ge=0)
    repair_distance_lines: int | None = Field(default=None, ge=0)
    model_authored_test_relevance: RateMetric
    ceiling_events: list[CeilingEvent] = Field(default_factory=list)
    model_reasoning_failures: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    attempted_model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    provider_latency_seconds: float = Field(default=0.0, ge=0)
    first_checkpoint_coherent: bool = False
    seconds_to_first_coherent_checkpoint: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_call_accounting(self) -> ModelProposalScorecard:
        if self.attempted_model_calls < self.model_calls:
            raise ValueError(
                "attempted model calls cannot be fewer than successful calls"
            )
        # A coherent checkpoint with no timing is allowed: historical arms
        # recorded the outcome without the clock. The reverse is not, because a
        # timestamp for a checkpoint that never happened is a fabrication.
        if (
            not self.first_checkpoint_coherent
            and self.seconds_to_first_coherent_checkpoint is not None
        ):
            raise ValueError(
                "no coherent first checkpoint was reached, so it has no timestamp"
            )
        return self


class HarnessDetectionScorecard(StrictModel):
    """What the outer system caught, refused, or let through.

    An unsupervised control scores near-zero here by construction, and that is
    the honest reading: it has no detection layer. The number that matters for
    the harness is `defects_escaping_acceptance` — defects found independently
    *after* Apoapsis accepted the result.
    """

    schema_version: str = "1.0"
    defects_detected: RateMetric
    negative_controls_caught: RateMetric
    criteria_with_current_state_evidence: RateMetric
    structured_witness_coverage: RateMetric
    #: Sessions that reached `COMPLETE` without the active slice being done.
    false_complete_count: int = Field(default=0, ge=0)
    #: Claims refused because only a command's friendly label supported them.
    weak_command_name_only_claims_refused: int = Field(default=0, ge=0)
    stale_or_inherited_evidence_rejected: int = Field(default=0, ge=0)
    integrated_defects_caught_before_delivery: int = Field(default=0, ge=0)
    defects_escaping_acceptance: int = Field(default=0, ge=0)
    #: Missing evidence that was nonetheless converted into `COMPLETE`. The
    #: handoff makes this an absolute prohibition, so it is counted on its own
    #: rather than folded into `false_complete_count`.
    missing_evidence_accepted_as_complete: int = Field(default=0, ge=0)


class PairedArmRecord(StrictModel):
    """One arm's frozen result for one case."""

    schema_version: str = "1.0"
    manifest: PairedRunManifest
    proposal: ModelProposalScorecard
    detection: HarnessDetectionScorecard
    #: Outcome measured before any stronger-model or human repair (gate 2).
    independent_case_outcome: CaseOutcome
    #: Outcome after Apoapsis verification and authorized repair (gate 3).
    delivered_case_outcome: CaseOutcome
    independent_checks_failed: list[str] = Field(default_factory=list)
    delivered_regressions: list[str] = Field(default_factory=list)
    repair_actors: list[RepairActorClass] = Field(default_factory=list)
    frontier_repair_calls: int = Field(default=0, ge=0)
    frontier_repair_cost_usd: float = Field(default=0.0, ge=0)
    notes: str = ""

    @property
    def case_id(self) -> str:
        return self.manifest.case_id

    @property
    def arm(self) -> PairedArmKind:
        return self.manifest.arm


class FindingSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class PairedFindingCode(StrEnum):
    MATCHED_MANIFEST_MISMATCH = "MATCHED_MANIFEST_MISMATCH"
    MATCHED_MANIFEST_UNRECORDED = "MATCHED_MANIFEST_UNRECORDED"
    CAPABILITY_REGRESSION = "CAPABILITY_REGRESSION"
    PROPOSAL_CASE_REGRESSION = "PROPOSAL_CASE_REGRESSION"
    PROPOSAL_QUALITY_REGRESSION = "PROPOSAL_QUALITY_REGRESSION"
    DELIVERED_CASE_REGRESSION = "DELIVERED_CASE_REGRESSION"
    ADDITIONAL_DELIVERED_REGRESSION = "ADDITIONAL_DELIVERED_REGRESSION"
    MISSING_EVIDENCE_ACCEPTED_AS_COMPLETE = "MISSING_EVIDENCE_ACCEPTED_AS_COMPLETE"
    FALSE_COMPLETE_NOT_REDUCED = "FALSE_COMPLETE_NOT_REDUCED"
    DEFECT_ESCAPED_ACCEPTANCE = "DEFECT_ESCAPED_ACCEPTANCE"
    NO_DETECTION_ADVANTAGE = "NO_DETECTION_ADVANTAGE"
    INPUT_TOKEN_REGRESSION = "INPUT_TOKEN_REGRESSION"
    UNCLASSIFIED_CEILING_FAILURE = "UNCLASSIFIED_CEILING_FAILURE"
    FRONTIER_REPAIR_NOT_ITEMIZED = "FRONTIER_REPAIR_NOT_ITEMIZED"


class PairedFinding(StrictModel):
    code: PairedFindingCode
    severity: FindingSeverity
    case_id: str | None = None
    detail: str = Field(min_length=1)


class PairedVerdict(StrEnum):
    SUPERIOR = "superior"
    PARITY = "parity"
    REGRESSION = "regression"
    #: The arms are not a matched pair, so neither a win nor a loss can be read
    #: from them. This is a real, common, and useful answer.
    INCOMPARABLE = "incomparable"


class ReleaseGate(StrEnum):
    CAPABILITY_PRESERVATION = "capability_preservation"
    PROPOSAL_NON_INFERIORITY = "proposal_non_inferiority"
    DELIVERED_SUPERIORITY = "delivered_superiority"
    EFFICIENCY = "efficiency"


class GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    #: No evidence either way. Never merged with `PASSED`.
    UNMEASURED = "unmeasured"


class GateResult(StrictModel):
    gate: ReleaseGate
    status: GateStatus
    detail: str = Field(min_length=1)
    findings: list[PairedFinding] = Field(default_factory=list)


class PairedCaseComparison(StrictModel):
    schema_version: str = "1.0"
    case_id: str = Field(min_length=1)
    control: PairedArmRecord
    candidate: PairedArmRecord
    proposal_verdict: PairedVerdict
    delivered_verdict: PairedVerdict
    findings: list[PairedFinding] = Field(default_factory=list)


class PairedCorpusReport(StrictModel):
    """Per-case results and four separately reported gates.

    There is deliberately no overall score field. The handoff forbids combining
    the gates into one average, and the absence of the field is the enforcement.
    """

    schema_version: str = "1.0"
    corpus_id: str = Field(min_length=1)
    generated_at: datetime = Field(default_factory=utc_now)
    control_arm: PairedArmKind
    candidate_arm: PairedArmKind
    cases: list[PairedCaseComparison] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)
    median_control_input_tokens: float | None = None
    median_candidate_input_tokens: float | None = None
    median_control_provider_latency_seconds: float | None = None
    median_candidate_provider_latency_seconds: float | None = None
    frontier_repair_calls: int = Field(default=0, ge=0)
    frontier_repair_cost_usd: float = Field(default=0.0, ge=0)
    recommended_for_default: bool = False

    def gate(self, gate: ReleaseGate) -> GateResult:
        for result in self.gates:
            if result.gate == gate:
                return result
        raise KeyError(gate)


def _rate_value(metric: RateMetric) -> float | None:
    return metric.value if metric.status == MetricStatus.MEASURED else None


def _manifest_findings(
    control: PairedRunManifest, candidate: PairedRunManifest, case_id: str
) -> list[PairedFinding]:
    findings: list[PairedFinding] = []
    for field in MATCHED_MANIFEST_FIELDS:
        left = getattr(control, field)
        right = getattr(candidate, field)
        if left is None or right is None:
            findings.append(
                PairedFinding(
                    code=PairedFindingCode.MATCHED_MANIFEST_UNRECORDED,
                    severity=FindingSeverity.ERROR,
                    case_id=case_id,
                    detail=(
                        f"controlled variable {field!r} is unrecorded in at least "
                        "one arm, so the arms cannot be treated as a matched pair"
                    ),
                )
            )
        elif left != right:
            findings.append(
                PairedFinding(
                    code=PairedFindingCode.MATCHED_MANIFEST_MISMATCH,
                    severity=FindingSeverity.ERROR,
                    case_id=case_id,
                    detail=(
                        f"controlled variable {field!r} differs between arms: "
                        f"control={left!r} candidate={right!r}"
                    ),
                )
            )
    return findings


def _capability_findings(
    control: PairedRunManifest, candidate: PairedRunManifest, case_id: str
) -> list[PairedFinding]:
    findings: list[PairedFinding] = []
    for capability in BaselineCapability:
        if control.capability_status(capability) != CapabilityStatus.PROVIDED:
            continue
        candidate_status = candidate.capability_status(capability)
        if candidate_status == CapabilityStatus.PROVIDED:
            continue
        findings.append(
            PairedFinding(
                code=PairedFindingCode.CAPABILITY_REGRESSION,
                severity=FindingSeverity.ERROR,
                case_id=case_id,
                detail=(
                    f"the control provides {capability.value!r} and the candidate "
                    f"records it as {candidate_status.value!r}"
                ),
            )
        )
    return findings


def _ceiling_findings(record: PairedArmRecord, label: str) -> list[PairedFinding]:
    # A failed call with no ceiling label and no reasoning label is an
    # unattributed failure. It must not be silently charged to the model.
    unattributed = record.proposal.attempted_model_calls - (
        record.proposal.model_calls + record.proposal.model_reasoning_failures
    )
    labelled = len(
        [event for event in record.proposal.ceiling_events if event.finish_reason]
    )
    if unattributed > len(record.proposal.ceiling_events):
        return [
            PairedFinding(
                code=PairedFindingCode.UNCLASSIFIED_CEILING_FAILURE,
                severity=FindingSeverity.WARNING,
                case_id=record.case_id,
                detail=(
                    f"{label}: {unattributed} failed call(s) are neither classified "
                    f"as a ceiling condition ({len(record.proposal.ceiling_events)} "
                    f"recorded, {labelled} with a provider finish reason) nor "
                    "attributed to model reasoning"
                ),
            )
        ]
    return []


def compare_case(
    control: PairedArmRecord, candidate: PairedArmRecord
) -> PairedCaseComparison:
    """Score one matched pair on proposal quality and delivered quality.

    A manifest problem short-circuits both verdicts to `INCOMPARABLE`: reading
    a win off two runs that did not share their controlled variables is exactly
    the mistake the handoff's evidence section is about.
    """

    if control.case_id != candidate.case_id:
        raise ValueError(
            f"cannot pair different cases: {control.case_id!r} vs {candidate.case_id!r}"
        )
    case_id = control.case_id
    findings = _manifest_findings(control.manifest, candidate.manifest, case_id)
    findings.extend(_capability_findings(control.manifest, candidate.manifest, case_id))
    findings.extend(_ceiling_findings(control, "control"))
    findings.extend(_ceiling_findings(candidate, "candidate"))

    if any(
        item.code
        in {
            PairedFindingCode.MATCHED_MANIFEST_MISMATCH,
            PairedFindingCode.MATCHED_MANIFEST_UNRECORDED,
        }
        for item in findings
    ):
        return PairedCaseComparison(
            case_id=case_id,
            control=control,
            candidate=candidate,
            proposal_verdict=PairedVerdict.INCOMPARABLE,
            delivered_verdict=PairedVerdict.INCOMPARABLE,
            findings=findings,
        )

    proposal_verdict = _proposal_verdict(control, candidate, case_id, findings)
    delivered_verdict = _delivered_verdict(control, candidate, case_id, findings)
    return PairedCaseComparison(
        case_id=case_id,
        control=control,
        candidate=candidate,
        proposal_verdict=proposal_verdict,
        delivered_verdict=delivered_verdict,
        findings=findings,
    )


def _proposal_verdict(
    control: PairedArmRecord,
    candidate: PairedArmRecord,
    case_id: str,
    findings: list[PairedFinding],
) -> PairedVerdict:
    regressed = False
    if (
        control.independent_case_outcome == CaseOutcome.PASSED
        and candidate.independent_case_outcome != CaseOutcome.PASSED
    ):
        regressed = True
        findings.append(
            PairedFinding(
                code=PairedFindingCode.PROPOSAL_CASE_REGRESSION,
                severity=FindingSeverity.ERROR,
                case_id=case_id,
                detail=(
                    "the control passed this case before repair and the candidate "
                    f"did not ({candidate.independent_case_outcome.value})"
                ),
            )
        )

    for label, control_metric, candidate_metric in (
        (
            "obligations implemented before repair",
            control.proposal.obligations_implemented_before_repair,
            candidate.proposal.obligations_implemented_before_repair,
        ),
        (
            "independent checks passed at first checkpoint",
            control.proposal.independent_checks_passed_at_first_checkpoint,
            candidate.proposal.independent_checks_passed_at_first_checkpoint,
        ),
    ):
        left = _rate_value(control_metric)
        right = _rate_value(candidate_metric)
        if left is None or right is None or right >= left:
            continue
        regressed = True
        findings.append(
            PairedFinding(
                code=PairedFindingCode.PROPOSAL_QUALITY_REGRESSION,
                severity=FindingSeverity.ERROR,
                case_id=case_id,
                detail=(
                    f"{label}: candidate {right:.3f} is below control {left:.3f}"
                ),
            )
        )

    if regressed:
        return PairedVerdict.REGRESSION
    if (
        candidate.independent_case_outcome == CaseOutcome.PASSED
        and control.independent_case_outcome != CaseOutcome.PASSED
    ):
        return PairedVerdict.SUPERIOR
    return PairedVerdict.PARITY


def _delivered_verdict(
    control: PairedArmRecord,
    candidate: PairedArmRecord,
    case_id: str,
    findings: list[PairedFinding],
) -> PairedVerdict:
    regressed = False
    if candidate.detection.missing_evidence_accepted_as_complete:
        regressed = True
        findings.append(
            PairedFinding(
                code=PairedFindingCode.MISSING_EVIDENCE_ACCEPTED_AS_COMPLETE,
                severity=FindingSeverity.ERROR,
                case_id=case_id,
                detail=(
                    f"{candidate.detection.missing_evidence_accepted_as_complete} "
                    "criteria reached COMPLETE with missing evidence; the handoff "
                    "forbids converting missing evidence into COMPLETE"
                ),
            )
        )
    if (
        control.delivered_case_outcome == CaseOutcome.PASSED
        and candidate.delivered_case_outcome != CaseOutcome.PASSED
    ):
        regressed = True
        findings.append(
            PairedFinding(
                code=PairedFindingCode.DELIVERED_CASE_REGRESSION,
                severity=FindingSeverity.ERROR,
                case_id=case_id,
                detail=(
                    "the control's delivered result passed this case and the "
                    f"candidate's did not ({candidate.delivered_case_outcome.value})"
                ),
            )
        )

    extra = [
        item
        for item in candidate.delivered_regressions
        if item not in set(control.delivered_regressions)
    ]
    if extra:
        regressed = True
        findings.append(
            PairedFinding(
                code=PairedFindingCode.ADDITIONAL_DELIVERED_REGRESSION,
                severity=FindingSeverity.ERROR,
                case_id=case_id,
                detail=(
                    "the candidate introduced regressions the control did not have: "
                    + ", ".join(sorted(extra))
                ),
            )
        )
    if candidate.detection.defects_escaping_acceptance:
        regressed = True
        findings.append(
            PairedFinding(
                code=PairedFindingCode.DEFECT_ESCAPED_ACCEPTANCE,
                severity=FindingSeverity.ERROR,
                case_id=case_id,
                detail=(
                    f"{candidate.detection.defects_escaping_acceptance} defect(s) "
                    "were found independently after Apoapsis accepted the result"
                ),
            )
        )
    if candidate.detection.false_complete_count > control.detection.false_complete_count:
        regressed = True
        findings.append(
            PairedFinding(
                code=PairedFindingCode.FALSE_COMPLETE_NOT_REDUCED,
                severity=FindingSeverity.ERROR,
                case_id=case_id,
                detail=(
                    f"candidate false completions {candidate.detection.false_complete_count} "
                    f"exceed control {control.detection.false_complete_count}"
                ),
            )
        )
    # Quality purchased with additional frontier calls must be itemized. A
    # frontier actor with neither a call count nor a cost is the Crisis Atlas
    # situation exactly: Codex repaired every slice and none of it was priced.
    frontier_used = (
        RepairActorClass.FRONTIER_MODEL in candidate.repair_actors
        or candidate.frontier_repair_calls > 0
    )
    if frontier_used and not candidate.frontier_repair_cost_usd:
        findings.append(
            PairedFinding(
                code=PairedFindingCode.FRONTIER_REPAIR_NOT_ITEMIZED,
                severity=FindingSeverity.WARNING,
                case_id=case_id,
                detail=(
                    f"{candidate.frontier_repair_calls} recorded frontier repair "
                    "call(s) carry no itemized cost; quality bought with frontier "
                    "calls must be itemized"
                ),
            )
        )

    if regressed:
        return PairedVerdict.REGRESSION
    if (
        candidate.delivered_case_outcome == CaseOutcome.PASSED
        and control.delivered_case_outcome != CaseOutcome.PASSED
    ):
        return PairedVerdict.SUPERIOR
    return PairedVerdict.PARITY


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _capability_gate(cases: list[PairedCaseComparison]) -> GateResult:
    findings = [
        item
        for case in cases
        for item in case.findings
        if item.code == PairedFindingCode.CAPABILITY_REGRESSION
    ]
    if not cases:
        return GateResult(
            gate=ReleaseGate.CAPABILITY_PRESERVATION,
            status=GateStatus.UNMEASURED,
            detail="no paired cases were supplied",
        )
    unproven = [
        case
        for case in cases
        if not case.candidate.manifest.capabilities
        or not case.control.manifest.capabilities
    ]
    if unproven:
        return GateResult(
            gate=ReleaseGate.CAPABILITY_PRESERVATION,
            status=GateStatus.UNMEASURED,
            detail=(
                f"{len(unproven)} case(s) record no capability observations for one "
                "or both arms; an unobserved interface is not a preserved one"
            ),
            findings=findings,
        )
    if findings:
        return GateResult(
            gate=ReleaseGate.CAPABILITY_PRESERVATION,
            status=GateStatus.FAILED,
            detail=f"{len(findings)} baseline capability regression(s)",
            findings=findings,
        )
    return GateResult(
        gate=ReleaseGate.CAPABILITY_PRESERVATION,
        status=GateStatus.PASSED,
        detail="the candidate provides every capability the control provides",
    )


def _proposal_gate(cases: list[PairedCaseComparison]) -> GateResult:
    if not cases:
        return GateResult(
            gate=ReleaseGate.PROPOSAL_NON_INFERIORITY,
            status=GateStatus.UNMEASURED,
            detail="no paired cases were supplied",
        )
    incomparable = [
        case for case in cases if case.proposal_verdict == PairedVerdict.INCOMPARABLE
    ]
    if incomparable:
        return GateResult(
            gate=ReleaseGate.PROPOSAL_NON_INFERIORITY,
            status=GateStatus.UNMEASURED,
            detail=(
                f"{len(incomparable)} of {len(cases)} case(s) are not matched pairs"
            ),
            findings=[item for case in incomparable for item in case.findings],
        )
    regressions = [
        case for case in cases if case.proposal_verdict == PairedVerdict.REGRESSION
    ]
    if regressions:
        return GateResult(
            gate=ReleaseGate.PROPOSAL_NON_INFERIORITY,
            status=GateStatus.FAILED,
            detail=(
                f"{len(regressions)} of {len(cases)} case(s) regressed before repair: "
                + ", ".join(case.case_id for case in regressions)
            ),
            findings=[item for case in regressions for item in case.findings],
        )
    return GateResult(
        gate=ReleaseGate.PROPOSAL_NON_INFERIORITY,
        status=GateStatus.PASSED,
        detail=f"no proposal regression across {len(cases)} matched case(s)",
    )


def _delivered_gate(cases: list[PairedCaseComparison]) -> GateResult:
    if not cases:
        return GateResult(
            gate=ReleaseGate.DELIVERED_SUPERIORITY,
            status=GateStatus.UNMEASURED,
            detail="no paired cases were supplied",
        )
    incomparable = [
        case for case in cases if case.delivered_verdict == PairedVerdict.INCOMPARABLE
    ]
    if incomparable:
        return GateResult(
            gate=ReleaseGate.DELIVERED_SUPERIORITY,
            status=GateStatus.UNMEASURED,
            detail=(
                f"{len(incomparable)} of {len(cases)} case(s) are not matched pairs"
            ),
        )
    regressions = [
        case for case in cases if case.delivered_verdict == PairedVerdict.REGRESSION
    ]
    if regressions:
        return GateResult(
            gate=ReleaseGate.DELIVERED_SUPERIORITY,
            status=GateStatus.FAILED,
            detail=(
                f"{len(regressions)} of {len(cases)} delivered result(s) regressed: "
                + ", ".join(case.case_id for case in regressions)
            ),
            findings=[item for case in regressions for item in case.findings],
        )
    # Non-inferiority alone is not enough: the corpus must show the harness
    # catching something the default agent's own completion claim missed.
    detection_advantage = sum(
        case.candidate.detection.integrated_defects_caught_before_delivery
        + case.candidate.detection.weak_command_name_only_claims_refused
        + case.candidate.detection.stale_or_inherited_evidence_rejected
        for case in cases
    )
    if detection_advantage <= 0:
        return GateResult(
            gate=ReleaseGate.DELIVERED_SUPERIORITY,
            status=GateStatus.FAILED,
            detail=(
                "no case caught a defect or unmet obligation the default agent's "
                "own completion claim missed; parity is not superiority"
            ),
            findings=[
                PairedFinding(
                    code=PairedFindingCode.NO_DETECTION_ADVANTAGE,
                    severity=FindingSeverity.ERROR,
                    detail=(
                        "the corpus records zero independently caught defects, "
                        "refused weak claims, or rejected stale evidence"
                    ),
                )
            ],
        )
    superior = [
        case for case in cases if case.delivered_verdict == PairedVerdict.SUPERIOR
    ]
    return GateResult(
        gate=ReleaseGate.DELIVERED_SUPERIORITY,
        status=GateStatus.PASSED,
        detail=(
            f"{len(superior)} case(s) improved, none regressed, and the harness "
            f"caught {detection_advantage} item(s) the default claim missed"
        ),
    )


def _efficiency_gate(
    cases: list[PairedCaseComparison],
    control_median: float | None,
    candidate_median: float | None,
) -> GateResult:
    if not cases or control_median is None or candidate_median is None:
        return GateResult(
            gate=ReleaseGate.EFFICIENCY,
            status=GateStatus.UNMEASURED,
            detail="no paired input-token medians are available",
        )
    # A token median across arms that did not share their controlled variables
    # measures nothing. Reporting "efficiency passed" beside two incomparable
    # runs is exactly the cheap-arm-looks-good failure this module exists to
    # prevent, so the same disqualification applies here as to the other gates.
    incomparable = [
        case
        for case in cases
        if PairedVerdict.INCOMPARABLE
        in {case.proposal_verdict, case.delivered_verdict}
    ]
    if incomparable:
        return GateResult(
            gate=ReleaseGate.EFFICIENCY,
            status=GateStatus.UNMEASURED,
            detail=(
                f"{len(incomparable)} of {len(cases)} case(s) are not matched "
                "pairs, so their token medians are not comparable"
            ),
        )
    findings = [
        item
        for case in cases
        for item in case.findings
        if item.code == PairedFindingCode.UNCLASSIFIED_CEILING_FAILURE
    ]
    if candidate_median >= control_median:
        findings.append(
            PairedFinding(
                code=PairedFindingCode.INPUT_TOKEN_REGRESSION,
                severity=FindingSeverity.ERROR,
                detail=(
                    f"median candidate input tokens {candidate_median:.0f} are not "
                    f"below the control's {control_median:.0f}"
                ),
            )
        )
        return GateResult(
            gate=ReleaseGate.EFFICIENCY,
            status=GateStatus.FAILED,
            detail="the candidate did not remove the control's prompt replay cost",
            findings=findings,
        )
    if findings:
        return GateResult(
            gate=ReleaseGate.EFFICIENCY,
            status=GateStatus.FAILED,
            detail=(
                "ceiling failures are not correctly classified, so a context or "
                "output limit could be misread as a model reasoning failure"
            ),
            findings=findings,
        )
    return GateResult(
        gate=ReleaseGate.EFFICIENCY,
        status=GateStatus.PASSED,
        detail=(
            f"median input tokens {candidate_median:.0f} are below the control's "
            f"{control_median:.0f} and every ceiling failure is classified"
        ),
    )


def score_paired_corpus(
    records: list[PairedArmRecord],
    *,
    corpus_id: str,
    control_arm: PairedArmKind = PairedArmKind.DEFAULT_QWEN_CONTROL,
    candidate_arm: PairedArmKind = PairedArmKind.CAPABILITY_SANDBOX,
) -> PairedCorpusReport:
    """Rescore a frozen corpus without invoking any provider.

    Cases present in only one arm are dropped from the comparison rather than
    scored against nothing; a corpus with no complete pair yields four
    `UNMEASURED` gates, never a pass.
    """

    controls = {
        record.case_id: record for record in records if record.arm == control_arm
    }
    candidates = {
        record.case_id: record for record in records if record.arm == candidate_arm
    }
    cases = [
        compare_case(controls[case_id], candidates[case_id])
        for case_id in sorted(set(controls) & set(candidates))
    ]

    control_inputs = [float(case.control.proposal.input_tokens) for case in cases]
    candidate_inputs = [float(case.candidate.proposal.input_tokens) for case in cases]
    control_median = _median(control_inputs)
    candidate_median = _median(candidate_inputs)

    gates = [
        _capability_gate(cases),
        _proposal_gate(cases),
        _delivered_gate(cases),
        _efficiency_gate(cases, control_median, candidate_median),
    ]
    return PairedCorpusReport(
        corpus_id=corpus_id,
        control_arm=control_arm,
        candidate_arm=candidate_arm,
        cases=cases,
        gates=gates,
        median_control_input_tokens=control_median,
        median_candidate_input_tokens=candidate_median,
        median_control_provider_latency_seconds=_median(
            [case.control.proposal.provider_latency_seconds for case in cases]
        ),
        median_candidate_provider_latency_seconds=_median(
            [case.candidate.proposal.provider_latency_seconds for case in cases]
        ),
        frontier_repair_calls=sum(
            case.candidate.frontier_repair_calls for case in cases
        ),
        frontier_repair_cost_usd=sum(
            case.candidate.frontier_repair_cost_usd for case in cases
        ),
        # Every gate must pass on its own. There is no averaging step here, and
        # `UNMEASURED` never counts as a pass.
        recommended_for_default=bool(cases)
        and all(gate.status == GateStatus.PASSED for gate in gates),
    )
