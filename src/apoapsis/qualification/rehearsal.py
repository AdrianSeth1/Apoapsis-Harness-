"""The rehearsal runner: the executable 7P.3 stopped for want of.

The locked commits held every decision *kernel* -- admission, checkpoint
transitions, acceptance, paired comparability, relay, clone -- and nothing that
sequenced them. A lock cannot authorise a rehearsal it has no runner for, so
7P.3 refused rather than improvising one, and this module is that runner,
written to be bound by digest before anything runs.

It orchestrates and decides nothing. Every verdict-bearing question is
delegated to the kernels that were already qualified; what lives here is
ordering, setup, teardown, evidence and the shape of the report. That division
matters: a runner that re-decided admission or readiness would be a second
implementation of a rule that already exists, and the two would drift.

Four properties are deliberate.

**Six slots in the frozen order, taken from the manifest.** The order is not a
parameter. Reading it from the manifest means a rehearsal cannot quietly run a
friendlier schedule than the one frozen before results existed.

**Teardown is proved, not performed.** `TeardownProof` records what was
actually observed to be gone. A cleanup routine that ran is not evidence; a
directory that no longer exists and a thread that no longer runs are.

**Negative controls are injected, and each names the detector it must trip.**
Passing because some unrelated check happened to fail is not passing, so the
injector records which detector fired and compares it to the one required.

**No model, no network, no container is reachable from here.** The provider is
`FakePilotProvider`, which holds no transport at all.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from apoapsis.qualification.fake_pilot_provider import (
    FakePilotProvider,
    ScriptId,
    script_digest,
)
from apoapsis.specification.schema import StrictModel

_SHA256 = r"^[0-9a-f]{64}$"

REHEARSAL_SCHEMA_VERSION = "1.0"


class RehearsalVerdict(StrEnum):
    PASS_LIVE_PREFLIGHT_AUTHORIZED = "pass_live_preflight_authorized"
    FAIL_REHEARSAL = "fail_rehearsal"
    INCOMPARABLE_CONFIGURATION = "incomparable_configuration"
    NOT_MEASURABLE = "not_measurable"


class StageOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNRUN = "unrun"
    INCONCLUSIVE = "inconclusive"


class StageResult(StrictModel):
    stage: str = Field(min_length=1)
    outcome: StageOutcome
    detail: str = Field(min_length=1)
    evidence: dict[str, str] = Field(default_factory=dict)


class TeardownProof(StrictModel):
    """What was observed absent after a slot, not what cleanup was told to do."""

    worktree_removed: bool
    qwen_home_removed: bool
    evidence_retained: bool
    no_surviving_worker: bool
    no_surviving_relay_stream: bool
    next_slot_cannot_reach_previous: bool

    @property
    def clean(self) -> bool:
        return all(
            (
                self.worktree_removed,
                self.qwen_home_removed,
                self.evidence_retained,
                self.no_surviving_worker,
                self.no_surviving_relay_stream,
                self.next_slot_cannot_reach_previous,
            )
        )


class ArmSlotResult(StrictModel):
    """One of the six scheduled slots."""

    repetition_id: str = Field(min_length=1)
    arm: str = Field(min_length=1)
    order_within_repetition: int = Field(ge=1, le=2)
    script: ScriptId
    seed_commit_verified: bool
    task_bytes_verified: bool
    arm_visible_mounts_verified: bool
    evaluator_only_absent: bool
    provider_requests: int = Field(ge=0)
    relay_observed_requests: int = Field(ge=0)
    candidate_fingerprint: str | None = Field(default=None, pattern=_SHA256)
    checkpoint_outcome: str = Field(min_length=1)
    readiness_blocks: tuple[str, ...] = ()
    satisfied_criteria: tuple[str, ...] = ()
    teardown: TeardownProof
    evidence_path: str = Field(min_length=1)

    @property
    def every_turn_traversed_the_relay(self) -> bool:
        """A provider turn that never crossed the relay bypassed containment."""

        return (
            self.provider_requests > 0
            and self.relay_observed_requests == self.provider_requests
        )


class NegativeControl(StrEnum):
    """Injected faults, each named for the condition it simulates."""

    MANIFEST_LOCK_MISMATCH = "manifest_lock_mismatch"
    WORKCELL_PROFILE_MISMATCH = "workcell_profile_mismatch"
    CONTROLLED_VARIABLE_MISMATCH = "controlled_variable_mismatch"
    MISSING_DURABLE_EVIDENCE = "missing_durable_evidence"
    EVALUATOR_ONLY_EXPOSED = "evaluator_only_exposed"
    STALE_WITNESS_DIGEST = "stale_witness_digest"
    UNMAPPED_OBLIGATION = "unmapped_obligation"
    OUTPUT_CEILING_TRUNCATION = "output_ceiling_truncation"
    INPUT_CONTEXT_EXHAUSTED = "input_context_exhausted"
    PRIOR_ARM_CONTAMINATION = "prior_arm_contamination"
    CHANGED_TASK_BYTES = "changed_task_bytes"
    CHANGED_SERVER_ARGUMENT = "changed_server_argument"
    UNCLASSIFIED_STOP_REASON = "unclassified_stop_reason"
    REPAIR_ENTERING_PROPOSAL_SCORE = "repair_entering_proposal_score"
    REGRESSION_HIDDEN_BY_AGGREGATE = "regression_hidden_by_aggregate"
    ABSENT_REQUIRED_REPETITION = "absent_required_repetition"
    FAKE_EVIDENCE_AS_REAL_QUALIFICATION = "fake_evidence_as_real_qualification"


class NegativeControlResult(StrictModel):
    """One injected fault and the detector that actually caught it.

    `detector_fired` is compared with `required_detector`. A control that is
    refused for the wrong reason is not a passing control: it would leave the
    mapped detector unproven while looking green, which is the shape of every
    defect this pilot has already had to correct.
    """

    control: NegativeControl
    required_detector: str = Field(min_length=1)
    detector_fired: str | None = None
    refused: bool = False

    @property
    def correctly_detected(self) -> bool:
        return self.refused and self.detector_fired == self.required_detector


class TokenAccounting(StrictModel):
    """Session aggregate is the cost; exposed messages are per-call evidence.

    `residual` exists because the two are different measurements of the same
    session and their difference is real -- cache hits, provider-side overhead
    -- rather than an error to be reconciled away. Counting the aggregate as an
    extra call would double-count the whole session, which is why
    `aggregate_counted_as_call` must be false.
    """

    session_aggregate_tokens: int | None = None
    exposed_message_tokens: int | None = None
    residual_tokens: int | None = None
    aggregate_counted_as_call: bool = False
    #: `None`, never 0. A provider that reported nothing did not report zero.
    unmeasured_reason: str | None = None

    @property
    def consistent(self) -> bool:
        if self.session_aggregate_tokens is None or self.exposed_message_tokens is None:
            return self.residual_tokens is None and self.unmeasured_reason is not None
        expected = self.session_aggregate_tokens - self.exposed_message_tokens
        return self.residual_tokens == expected and not self.aggregate_counted_as_call


class PairScore(StrictModel):
    """One repetition, scored independently of the other two.

    Sampling is stochastic across repetitions, so the three are independent
    samples. `aggregate_may_offset_pair_regression` is `False` and there is no
    field that can make it true: a better mean must never hide a pair that got
    worse, which is the per-case release rule.
    """

    repetition_id: str = Field(min_length=1)
    control_proposal_quality: float | None = None
    sandbox_proposal_quality: float | None = None
    sandbox_detection_quality: float | None = None
    repaired_quality_excluded_from_proposal: bool = True
    comparable: bool = True
    incomparable_reason: str | None = None
    aggregate_may_offset_pair_regression: bool = False

    @property
    def regressed(self) -> bool:
        if (
            self.control_proposal_quality is None
            or self.sandbox_proposal_quality is None
        ):
            return False
        return self.sandbox_proposal_quality < self.control_proposal_quality


class RehearsalReport(StrictModel):
    """The structured verdict, binding everything it was produced from."""

    schema_version: str = REHEARSAL_SCHEMA_VERSION
    verdict: RehearsalVerdict
    reason: str = Field(min_length=1)
    manifest_digest: str = Field(pattern=_SHA256)
    lock_digest: str = Field(pattern=_SHA256)
    runner_authority_commit: str = Field(min_length=7)
    runner_module_digests: dict[str, str]
    fake_provider_script_digest: str = Field(pattern=_SHA256)
    controller_image_id: str = Field(min_length=1)
    workcell_image_id: str = Field(min_length=1)
    stages: tuple[StageResult, ...] = ()
    arm_slots: tuple[ArmSlotResult, ...] = ()
    negative_controls: tuple[NegativeControlResult, ...] = ()
    relay_stress_iterations: int = Field(default=0, ge=0)
    relay_stress_passed: bool = False
    token_accounting: TokenAccounting | None = None
    pair_scores: tuple[PairScore, ...] = ()
    evidence_root: str = Field(min_length=1)
    evidence_digest: str = Field(pattern=_SHA256)
    #: A rehearsal never authorises a model call, whatever its verdict.
    authorises_live_inference: bool = False

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()


def decide_verdict(
    *,
    stages: tuple[StageResult, ...],
    arm_slots: tuple[ArmSlotResult, ...],
    negative_controls: tuple[NegativeControlResult, ...],
    relay_stress_passed: bool,
    token_accounting: TokenAccounting | None,
    pair_scores: tuple[PairScore, ...],
) -> tuple[RehearsalVerdict, str]:
    """Turn observations into one of four verdicts.

    The ordering is deliberate. `NOT_MEASURABLE` is checked before
    `FAIL_REHEARSAL`, because a stage that did not run is a different claim
    from one that ran and lost, and reporting the first as the second would
    assert a measurement nobody took.
    """

    unrun = [item.stage for item in stages if item.outcome is StageOutcome.UNRUN]
    if unrun:
        return (
            RehearsalVerdict.NOT_MEASURABLE,
            f"these stages did not run: {sorted(unrun)}",
        )
    inconclusive = [
        item.stage for item in stages if item.outcome is StageOutcome.INCONCLUSIVE
    ]
    if inconclusive:
        return (
            RehearsalVerdict.NOT_MEASURABLE,
            f"these stages could not decide: {sorted(inconclusive)}",
        )

    failed = [item.stage for item in stages if item.outcome is StageOutcome.FAILED]
    if failed:
        return RehearsalVerdict.FAIL_REHEARSAL, f"stages failed: {sorted(failed)}"

    if len(arm_slots) != 6:
        return (
            RehearsalVerdict.NOT_MEASURABLE,
            f"{len(arm_slots)} arm slots were rehearsed; the frozen schedule "
            "has six, and a missing slot is an absent result rather than a "
            "passing one",
        )

    for slot in arm_slots:
        if not slot.every_turn_traversed_the_relay:
            return (
                RehearsalVerdict.FAIL_REHEARSAL,
                f"{slot.repetition_id}/{slot.arm}: "
                f"{slot.provider_requests} provider turns but "
                f"{slot.relay_observed_requests} observed at the relay, so a "
                "turn bypassed containment",
            )
        if not slot.teardown.clean:
            return (
                RehearsalVerdict.FAIL_REHEARSAL,
                f"{slot.repetition_id}/{slot.arm}: teardown left state behind",
            )
        if not slot.evaluator_only_absent:
            return (
                RehearsalVerdict.FAIL_REHEARSAL,
                f"{slot.repetition_id}/{slot.arm}: evaluator-only material was "
                "reachable from an arm",
            )

    missed = [
        str(item.control)
        for item in negative_controls
        if not item.correctly_detected
    ]
    if missed:
        return (
            RehearsalVerdict.FAIL_REHEARSAL,
            f"these injected controls were not caught by their mapped "
            f"detector: {sorted(missed)}",
        )

    if not relay_stress_passed:
        return (
            RehearsalVerdict.FAIL_REHEARSAL,
            "the relay stress requirement was not met; the known intermittent "
            "is still reproducible and must not be carried into a live run",
        )

    if token_accounting is None or not token_accounting.consistent:
        return (
            RehearsalVerdict.NOT_MEASURABLE,
            "token accounting is missing or inconsistent, so cost cannot be "
            "attributed and no comparison is measurable",
        )

    incomparable = [item.repetition_id for item in pair_scores if not item.comparable]
    if incomparable:
        return (
            RehearsalVerdict.INCOMPARABLE_CONFIGURATION,
            f"these pairs are not comparable: {sorted(incomparable)}",
        )

    return (
        RehearsalVerdict.PASS_LIVE_PREFLIGHT_AUTHORIZED,
        "every stage passed, six slots rehearsed, all injected controls caught "
        "by their mapped detector, relay stress met, accounting consistent. "
        "This authorises the live preflight only; live inference remains "
        "unauthorised and every live recheck still applies.",
    )


#: Each injected control and the one detector that must catch it. Written as
#: data so a control cannot silently be marked "caught" by whatever happened to
#: fail first: `NegativeControlResult.correctly_detected` compares against this.
REQUIRED_DETECTORS: dict[NegativeControl, str] = {
    NegativeControl.MANIFEST_LOCK_MISMATCH: "PilotLock.verify_against",
    NegativeControl.WORKCELL_PROFILE_MISMATCH: (
        "StopCondition.CODING_PROFILE_OR_REALISED_TOOLS_DIFFER"
    ),
    NegativeControl.CONTROLLED_VARIABLE_MISMATCH: (
        "StopCondition.REPETITION_CONFIGURATION_DIFFERS"
    ),
    NegativeControl.MISSING_DURABLE_EVIDENCE: (
        "StopCondition.EVIDENCE_STORAGE_NOT_DURABLE"
    ),
    NegativeControl.EVALUATOR_ONLY_EXPOSED: (
        "ResolvedCasePackage.assert_arm_visible_set_is_contained"
    ),
    NegativeControl.STALE_WITNESS_DIGEST: "validate_witness",
    NegativeControl.UNMAPPED_OBLIGATION: "validate_criteria_mapping",
    NegativeControl.OUTPUT_CEILING_TRUNCATION: (
        "CeilingStopReason.OUTPUT_CEILING_TRUNCATION"
    ),
    NegativeControl.INPUT_CONTEXT_EXHAUSTED: (
        "CeilingStopReason.INPUT_CONTEXT_EXHAUSTED"
    ),
    NegativeControl.PRIOR_ARM_CONTAMINATION: "TeardownProof",
    NegativeControl.CHANGED_TASK_BYTES: (
        "StopCondition.SEED_TASK_OR_CONTRACT_BYTES_DIFFER"
    ),
    NegativeControl.CHANGED_SERVER_ARGUMENT: "ServerIdentity.argv_sha256",
    NegativeControl.UNCLASSIFIED_STOP_REASON: (
        "StopCondition.TELEMETRY_CANNOT_BE_CLASSIFIED"
    ),
    NegativeControl.REPAIR_ENTERING_PROPOSAL_SCORE: (
        "RepairPolicy.repair_may_improve_proposal_score"
    ),
    NegativeControl.REGRESSION_HIDDEN_BY_AGGREGATE: (
        "PairScore.aggregate_may_offset_pair_regression"
    ),
    NegativeControl.ABSENT_REQUIRED_REPETITION: "decide_verdict:arm_slot_count",
    NegativeControl.FAKE_EVIDENCE_AS_REAL_QUALIFICATION: (
        "CasePackageValidation.registerable"
    ),
}


class EvidenceWriter:
    """Persists raw records evaluator-side, outside any container.

    Deliberately dumb: it writes bytes and hashes what it wrote. A writer that
    summarised would decide what mattered before anyone had read it, and the
    one thing this project keeps re-learning is that the summary and the
    evidence must stay separable.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_json(self, relative: str, payload: object) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        return path

    def digest(self) -> str:
        fingerprint = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root).as_posix()
            body = hashlib.sha256(path.read_bytes()).hexdigest()
            fingerprint.update(f"{relative}\0{body}\0".encode("utf-8"))
        return fingerprint.hexdigest()


def prove_teardown(
    *,
    worktree: Path,
    qwen_home: Path,
    evidence: Path,
    surviving_workers: int,
    surviving_relay_streams: int,
    next_slot_paths: tuple[Path, ...] = (),
) -> TeardownProof:
    """Observe what is gone. Running cleanup is not evidence that it worked."""

    return TeardownProof(
        worktree_removed=not worktree.exists(),
        qwen_home_removed=not qwen_home.exists(),
        evidence_retained=evidence.exists() and any(evidence.rglob("*")),
        no_surviving_worker=surviving_workers == 0,
        no_surviving_relay_stream=surviving_relay_streams == 0,
        next_slot_cannot_reach_previous=not any(
            path.exists() and path.is_relative_to(worktree)
            for path in next_slot_paths
        ),
    )


def scheduled_slots(manifest) -> tuple[tuple[str, str, int], ...]:
    """The six slots, in the frozen order, read from the manifest.

    Order is read rather than passed, so a rehearsal cannot run a friendlier
    schedule than the one frozen before any result existed.
    """

    slots: list[tuple[str, str, int]] = []
    for pair in manifest.paired_executions:
        for execution in sorted(
            pair.executions, key=lambda item: item.order_within_repetition
        ):
            slots.append(
                (
                    pair.repetition.repetition_id,
                    str(execution.arm),
                    execution.order_within_repetition,
                )
            )
    return tuple(slots)


def script_for(arm: str, shape: str) -> ScriptId:
    """Which script a slot runs. Both arms see the same candidate shape."""

    return (
        ScriptId.INCOMPLETE_PROPOSAL
        if shape == "incomplete"
        else ScriptId.COMPLETE_PROPOSAL
    )


def rehearsal_provider(script: ScriptId) -> FakePilotProvider:
    provider = FakePilotProvider(script)
    if provider.reaches_network:  # pragma: no cover - structural guard
        raise RuntimeError("the rehearsal provider must not reach a network")
    return provider


def fake_provider_script_digest() -> str:
    return script_digest()
