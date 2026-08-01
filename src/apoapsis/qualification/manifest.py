"""The Slice 7 qualification manifest: frozen before inference, not after.

Every rule in this module is written down while the outcome is still unknown.
That is the entire point. A non-inferiority bound chosen after seeing which
cases regressed is not a bound, and a scoring boundary drawn after seeing which
side of it the numbers fall on is not a boundary.

Three properties are structural rather than procedural:

**Immutability.** Every model here is `frozen=True`. A manifest cannot be
edited after construction, so "we adjusted the manifest mid-corpus" is not a
thing a caller can do quietly. Fixing an experiment defect means issuing a new
manifest with a new digest and restarting the affected pairs, which the digest
makes visible.

**Two scorecards, and no way to combine them.** There is deliberately no field,
method, or helper anywhere in this module that returns a single number
summarising both. A combined score is how a cheap efficiency win cancels a
quality regression, and the handoff forbids it in words; this forbids it in
types. A test asserts the absence.

**Abstention, never optimism.** Missing evidence, an unclassifiable truncation,
an infrastructure failure, and an incomparable pair are all distinct verdicts,
and none of them is a pass. `CaseVerdict.passed_for_gate` is false for every one
of them.

The source-under-test commit and the commit that carries this manifest are
recorded separately and must differ. Hashing a manifest into the tree it
describes would make the source hash self-referential, and the resulting
digest would be a fact about the bookkeeping rather than about the experiment.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from apoapsis.specification.schema import StrictModel

MANIFEST_SCHEMA_VERSION = "1.0"

_SHA256 = r"^[0-9a-f]{64}$"
_COMMIT = r"^[0-9a-f]{7,40}$"


class Frozen(StrictModel):
    """Immutable by construction, so a manifest cannot drift mid-corpus."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------
# 1. Arms
# --------------------------------------------------------------------------


class ArmKind(StrEnum):
    #: The genuine pinned Qwen Code CLI on its own native agent/tool loop.
    CONTROL = "default_qwen_control"
    #: The identical CLI, supervised by Apoapsis.
    CAPABILITY_SANDBOX = "capability_sandbox"


class ArmSpec(Frozen):
    """One arm. The two differ **only** in supervision.

    `host_authority` is `Literal`-like by validator rather than a bool default:
    neither arm may hold it, and "default" describes the agent interface, not
    the blast radius. The control is hardened exactly as the sandbox is.
    """

    kind: ArmKind
    cli_name: str = Field(min_length=1)
    cli_version: str = Field(min_length=1)
    permission_mode: str = Field(min_length=1)
    computer_use_enabled: bool = False
    tool_search_enabled: bool = False
    #: The realised native tool set, verified against the running CLI before
    #: any inference. A declared tool list that was never observed is a claim.
    realised_tool_names: tuple[str, ...] = Field(min_length=1)
    tool_surface_verified_before_inference: bool = True
    host_authority: bool = False
    supervision: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_no_host_authority(self) -> ArmSpec:
        if self.host_authority:
            raise ValueError(
                "no arm may hold host authority; 'default' describes the agent "
                "interface, not the blast radius"
            )
        if self.computer_use_enabled or self.tool_search_enabled:
            raise ValueError(
                "the verified coding profile disables computer-use and "
                "tool-search; enabling either makes the arms non-identical"
            )
        if not self.tool_surface_verified_before_inference:
            raise ValueError(
                "the realised tool surface must be verified before inference; "
                "an unverified tool list is a claim, not a control"
            )
        if list(self.realised_tool_names) != sorted(self.realised_tool_names):
            raise ValueError("realised_tool_names must be sorted for a stable digest")
        return self


# --------------------------------------------------------------------------
# 2. Corpus
# --------------------------------------------------------------------------


class CaseKind(StrEnum):
    CRISIS_ATLAS = "crisis_atlas"
    FOCUS_ORBIT = "focus_orbit"
    SMALL_BACKEND_CHANGE = "small_backend_change"
    CROSS_FILE_REFACTOR = "cross_file_refactor"
    TEST_REPAIR = "test_repair"
    LAUNCH_OPERABILITY = "launch_operability"
    MISLEADING_INHERITED_SUITE = "misleading_inherited_suite"
    HELD_OUT_REPOSITORY = "held_out_repository"


#: Every kind is required. A corpus missing one is not this experiment.
REQUIRED_CASE_KINDS: frozenset[CaseKind] = frozenset(CaseKind)


class CaseSpec(Frozen):
    """One frozen corpus case. Everything a run needs, fixed before inference."""

    case_id: str = Field(min_length=1)
    kind: CaseKind
    seed_commit: str = Field(pattern=_COMMIT)
    seed_tree_sha256: str = Field(pattern=_SHA256)
    task_text_sha256: str = Field(pattern=_SHA256)
    plan_or_contract_sha256: str | None = Field(default=None, pattern=_SHA256)
    acceptance_criteria_sha256: str = Field(pattern=_SHA256)
    verification_commands_sha256: str = Field(pattern=_SHA256)
    max_output_tokens: int = Field(ge=1)
    wall_clock_budget_seconds: float = Field(gt=0)
    token_budget: int = Field(ge=1)
    #: Required for the release gate. A case may be exploratory only if the
    #: owner froze it that way here, never by later reinterpretation.
    required_for_gate: bool = True
    #: Negative controls expected to be exercised on this case, by id.
    negative_control_ids: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# 3. Controlled variables
# --------------------------------------------------------------------------


class ControlledVariables(Frozen):
    """Everything that must be identical for a pair to be comparable.

    A mismatch in any field makes that pair `INCOMPARABLE`. Nothing here is
    substituted silently: `mismatches_against` returns the field names so the
    refusal names its own cause.
    """

    model_name: str = Field(min_length=1)
    model_file_sha256: str = Field(pattern=_SHA256)
    quantization: str = Field(min_length=1)
    llama_server_binary_sha256: str = Field(pattern=_SHA256)
    llama_server_argv_sha256: str = Field(pattern=_SHA256)
    #: The measured ladder (ADR 0082), never a percentage.
    threshold_ladder_sha256: str = Field(pattern=_SHA256)
    auto_compact_trigger_tokens: int = Field(ge=1)
    context_limit_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    qwen_package_sha256: str = Field(pattern=_SHA256)
    qwen_settings_sha256: str = Field(pattern=_SHA256)
    tool_schema_sha256: str = Field(pattern=_SHA256)
    system_prompt_sha256: str = Field(pattern=_SHA256)
    task_prompt_sha256: str = Field(pattern=_SHA256)
    workcell_image_digest: str = Field(min_length=1)
    verifier_image_digest: str = Field(min_length=1)
    controller_source_commit: str = Field(pattern=_COMMIT)
    worktree_seed_sha256: str = Field(pattern=_SHA256)
    network_policy: str = Field(min_length=1)
    mount_policy_sha256: str = Field(pattern=_SHA256)
    cpu_limit: float = Field(gt=0)
    gpu_allocation: str = Field(min_length=1)
    per_call_budget_tokens: int = Field(ge=1)
    per_case_budget_tokens: int = Field(ge=1)
    verification_config_sha256: str = Field(pattern=_SHA256)
    repair_policy_sha256: str = Field(pattern=_SHA256)
    cold_start: bool

    def mismatches_against(self, other: ControlledVariables) -> tuple[str, ...]:
        mine = self.model_dump(mode="json")
        theirs = other.model_dump(mode="json")
        return tuple(
            sorted(name for name, value in mine.items() if theirs.get(name) != value)
        )


class PairComparability(StrEnum):
    COMPARABLE = "comparable"
    INCOMPARABLE = "incomparable"


class PairCheck(Frozen):
    comparability: PairComparability
    mismatched_fields: tuple[str, ...] = ()
    detail: str = Field(min_length=1)

    @property
    def usable_for_gate(self) -> bool:
        return self.comparability is PairComparability.COMPARABLE


def check_pair(
    control: ControlledVariables, sandbox: ControlledVariables
) -> PairCheck:
    """Refuse an incomparable pair before inference or scoring.

    `cold_start` is included deliberately: a warm sandbox against a cold
    control measures the cache, not the harness.
    """

    mismatches = control.mismatches_against(sandbox)
    if mismatches:
        return PairCheck(
            comparability=PairComparability.INCOMPARABLE,
            mismatched_fields=mismatches,
            detail=(
                "the arms differ in more than supervision: "
                f"{', '.join(mismatches)}. This pair cannot be scored, and no "
                "value may be substituted to make it comparable."
            ),
        )
    return PairCheck(
        comparability=PairComparability.COMPARABLE,
        detail="the arms differ only in supervision",
    )


# --------------------------------------------------------------------------
# 4. Scoring boundaries
# --------------------------------------------------------------------------


class ScoreKind(StrEnum):
    #: The first submitted candidate, before any repair of any kind.
    MODEL_PROPOSAL_QUALITY = "model_proposal_quality"
    #: Whether incomplete, unsafe or falsely green candidates were refused
    #: with mapped evidence.
    HARNESS_DEFECT_DETECTION = "harness_defect_detection"
    #: Reported, never merged into either of the above.
    FINAL_DELIVERED_QUALITY = "final_delivered_quality"


class TokenAccounting(Frozen):
    """ADR 0082's four rules, as a shape rather than a paragraph."""

    #: Total cost. The only quantity that includes the residual.
    session_aggregate_input_tokens: int = Field(ge=0)
    session_aggregate_output_tokens: int = Field(ge=0)
    #: Per-call and cache comparisons come from here only.
    exposed_message_input_tokens: int = Field(ge=0)
    exposed_message_cached_input_tokens: int = Field(ge=0)
    exposed_message_count: int = Field(ge=0)
    #: Reported separately, always.
    unattributed_residual_input_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_residual_is_not_a_call(self) -> TokenAccounting:
        if self.session_aggregate_input_tokens < self.exposed_message_input_tokens:
            raise ValueError(
                "the session aggregate cannot be smaller than the exposed "
                "messages it totals; one of the two is not what it claims"
            )
        expected = self.session_aggregate_input_tokens - self.exposed_message_input_tokens
        if self.unattributed_residual_input_tokens != expected:
            raise ValueError(
                "the residual must be exactly aggregate minus exposed; folding "
                "it into either understates real spend or double-counts a total "
                "as another call"
            )
        return self


class ProposalScore(Frozen):
    """Scored on the first submitted candidate, before any repair.

    `repair_applied` exists so the invariant is checkable rather than trusted:
    a proposal score constructed from a repaired candidate is rejected at
    construction, not caught in review.
    """

    case_id: str = Field(min_length=1)
    kind: ScoreKind = ScoreKind.MODEL_PROPOSAL_QUALITY
    obligations_implemented: int = Field(ge=0)
    obligations_required: int = Field(ge=0)
    independent_checks_passed: int = Field(ge=0)
    repair_applied: bool = False

    @model_validator(mode="after")
    def validate_pre_repair(self) -> ProposalScore:
        if self.repair_applied:
            raise ValueError(
                "proposal quality is the first candidate before any Codex, "
                "frontier or human repair; a repair cannot retroactively "
                "improve the model's proposal score"
            )
        if self.kind is not ScoreKind.MODEL_PROPOSAL_QUALITY:
            raise ValueError("ProposalScore may only carry proposal quality")
        return self


# --------------------------------------------------------------------------
# 5. Per-case non-inferiority rule
# --------------------------------------------------------------------------


class CaseVerdict(StrEnum):
    """Every way a case can end. Only one of them is a pass."""

    SANDBOX_BETTER = "sandbox_better"
    TIE = "tie"
    SANDBOX_WORSE = "sandbox_worse"
    #: Evidence exists but does not support a comparison.
    NOT_MEASURABLE = "not_measurable"
    #: Required evidence was never produced.
    MISSING_EVIDENCE = "missing_evidence"
    #: A response or tool output hit a ceiling and could not be classified.
    UNCLASSIFIED_TRUNCATION = "unclassified_truncation"
    #: The run did not complete for reasons outside either agent.
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    #: Controlled variables differed.
    INCOMPARABLE = "incomparable"

    @property
    def passed_for_gate(self) -> bool:
        """Abstention is never a pass.

        A tie passes: non-inferiority asks that the sandbox not score *below*
        the control. Everything else either regressed or failed to produce a
        comparison, and neither may be read as success.
        """

        return self in (CaseVerdict.SANDBOX_BETTER, CaseVerdict.TIE)

    @property
    def is_abstention(self) -> bool:
        return self in (
            CaseVerdict.NOT_MEASURABLE,
            CaseVerdict.MISSING_EVIDENCE,
            CaseVerdict.UNCLASSIFIED_TRUNCATION,
            CaseVerdict.INFRASTRUCTURE_FAILURE,
            CaseVerdict.INCOMPARABLE,
        )


class NonInferiorityRule(Frozen):
    """Frozen before any output is seen."""

    #: The sandbox must not score below control on first-proposal acceptance
    #: quality for any required case.
    per_case_floor: str = (
        "Capability Sandbox first-proposal acceptance quality must be >= the "
        "matched control for every required case."
    )
    aggregate_proposal_quality_must_not_fall: bool = True
    #: At least one material improvement in final quality or defect detection.
    material_improvement_required: bool = True
    #: A regression on any required case blocks rollout regardless of averages.
    per_case_regression_blocks_rollout: bool = True
    #: Efficiency cannot buy a quality regression.
    efficiency_subordinate_to_quality: bool = True
    permitted_failure_or_ceiling_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    false_completions_permitted: int = Field(default=0, ge=0)


class GateResult(Frozen):
    promoted: bool
    blocking_case_ids: tuple[str, ...] = ()
    abstaining_case_ids: tuple[str, ...] = ()
    detail: str = Field(min_length=1)


def evaluate_gate(
    verdicts: dict[str, CaseVerdict],
    *,
    required_case_ids: frozenset[str],
    aggregate_proposal_quality_fell: bool,
    material_improvement_observed: bool,
    rule: NonInferiorityRule,
) -> GateResult:
    """Per-case first, and an average can never rescue a regressed case.

    Written as an explicit loop over cases rather than any comparison of means.
    There is no arithmetic here that could let one case's improvement offset
    another's regression, because there is no arithmetic across cases at all.
    """

    missing = tuple(sorted(required_case_ids - set(verdicts)))
    blocking = list(missing)
    abstaining = []
    for case_id in sorted(required_case_ids & set(verdicts)):
        verdict = verdicts[case_id]
        if verdict.is_abstention:
            abstaining.append(case_id)
            blocking.append(case_id)
        elif not verdict.passed_for_gate:
            blocking.append(case_id)

    if blocking:
        return GateResult(
            promoted=False,
            blocking_case_ids=tuple(sorted(set(blocking))),
            abstaining_case_ids=tuple(abstaining),
            detail=(
                "per-case gate failed; a better aggregate cannot excuse a "
                "regressed or unmeasured required case"
            ),
        )
    if rule.aggregate_proposal_quality_must_not_fall and aggregate_proposal_quality_fell:
        return GateResult(
            promoted=False,
            detail="aggregate proposal quality fell",
        )
    if rule.material_improvement_required and not material_improvement_observed:
        return GateResult(
            promoted=False,
            detail=(
                "no material improvement in final quality or defect detection; "
                "parity alone does not justify a default change"
            ),
        )
    return GateResult(promoted=True, detail="every frozen gate passed")


# --------------------------------------------------------------------------
# 6. Negative controls
# --------------------------------------------------------------------------


class DetectorLayer(StrEnum):
    ADMISSION = "admission"
    WITNESS_EMISSION = "witness_emission"
    READINESS = "readiness"
    INTEGRATED_VERIFICATION = "integrated_verification"
    STRICT_WEB_CHECK = "strict_web_check"
    CEILING_CLASSIFIER = "ceiling_classifier"
    DELIVERY = "delivery"


class NegativeControl(Frozen):
    """A deliberate mutation, its mapped detector, and its required evidence.

    `allowed_secondary_detectors` is empty by default and must be frozen
    explicitly. Detection by an unrelated check is not detection by the mapped
    layer: a mutation caught by accident tells you nothing about whether the
    layer meant to catch it works.
    """

    control_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    expected_detector: DetectorLayer
    allowed_secondary_detectors: tuple[DetectorLayer, ...] = ()
    required_evidence: str = Field(min_length=1)
    failure_classification: str = Field(min_length=1)

    def satisfied_by(self, layer: DetectorLayer) -> bool:
        return layer is self.expected_detector or layer in self.allowed_secondary_detectors


def _control(control_id, description, detector, evidence, classification):
    return NegativeControl(
        control_id=control_id,
        description=description,
        expected_detector=detector,
        required_evidence=evidence,
        failure_classification=classification,
    )


FROZEN_NEGATIVE_CONTROLS: tuple[NegativeControl, ...] = (
    _control(
        "NC-01-localstorage-persistence",
        "Replace API persistence with localStorage.",
        DetectorLayer.INTEGRATED_VERIFICATION,
        "restart-persistence witness showing state absent after server restart",
        "undetected_persistence_regression",
    ),
    _control(
        "NC-02-discarded-query",
        "Discard URL query parameters while leaving the route reachable.",
        DetectorLayer.INTEGRATED_VERIFICATION,
        "route witness recording request query and normalised response assertion",
        "undetected_filter_regression",
    ),
    _control(
        "NC-03-static-sample-incidents",
        "Return static sample incidents instead of backend state.",
        DetectorLayer.INTEGRATED_VERIFICATION,
        "mutation-then-read witness proving the read reflects the write",
        "undetected_fake_backend",
    ),
    _control(
        "NC-04-root-404",
        "Root route returns 404 while an internal API path still works.",
        DetectorLayer.STRICT_WEB_CHECK,
        "launch witness recording the bound address and the root response status",
        "undetected_broken_entrypoint",
    ),
    _control(
        "NC-05-route-shadowing",
        "Shadow a specific export route with a broad route.",
        DetectorLayer.INTEGRATED_VERIFICATION,
        "route witness proving the specific path was exercised, not merely reachable",
        "undetected_route_shadowing",
    ),
    _control(
        "NC-06-restart-loss",
        "Make restart persistence fail.",
        DetectorLayer.INTEGRATED_VERIFICATION,
        "restart witness with an explicit post-restart read",
        "undetected_restart_regression",
    ),
    _control(
        "NC-07-nondeterministic-export",
        "Make JSON or Markdown export nondeterministic.",
        DetectorLayer.INTEGRATED_VERIFICATION,
        "two export artifacts with recorded hashes from one unchanged state",
        "undetected_nondeterminism",
    ),
    _control(
        "NC-08-witness-removed",
        "Remove the only test/witness reaching a new or changed component.",
        DetectorLayer.READINESS,
        "behaviour-unit coverage showing the component unexercised",
        "undetected_unmeasured_component",
    ),
    _control(
        "NC-09-inaccessible-control",
        "Leave an inaccessible form control or blocking alert in the UI.",
        DetectorLayer.STRICT_WEB_CHECK,
        "accessibility witness naming the unlabelled control or blocking dialog",
        "undetected_accessibility_regression",
    ),
    _control(
        "NC-10-output-ceiling-truncation",
        "Terminate a model response exactly at the configured output ceiling.",
        DetectorLayer.CEILING_CLASSIFIER,
        "stop reason classified as OUTPUT_CEILING_TRUNCATION, never model failure",
        "misclassified_truncation",
    ),
)


# --------------------------------------------------------------------------
# 7. Crisis Atlas must-pass contract
# --------------------------------------------------------------------------

CRISIS_ATLAS_MUST_PASS: tuple[str, ...] = (
    "implements every slice artifact in its declared package",
    "refuses Slice 2 after only the partial wrong-path service",
    "proves both service classes through current-state evidence",
    "starts the canonical server and exercises the actual routes",
    "uses the HTTP API from the dashboard",
    "creates an incident whose state survives reload and server restart",
    "correctly filters status through both API and browser UI",
    "round-trips status, timeline events, and action items",
    "produces deterministic JSON and Markdown exports",
    "passes the strict web-product check or records only reviewed, explained "
    "dynamic-analysis limitations",
    "catches the localStorage and discarded-query negative controls",
    "passes final integrated verification",
    "records any Codex/human repair as an authoritative plan checkpoint",
    "keeps Report, task state, final verification, and delivery.json consistent",
    "produces a clean ZIP with accurate usage instructions and no Git metadata, "
    "runtime database, model logs, or credentials",
)


class KnownPartialProposal(Frozen):
    """The Crisis Atlas Slice 2 first proposal, frozen as a fixture.

    The one candidate this whole programme exists to refuse: a single partial
    file at the wrong package path, no export service, no tests, and every
    inherited check green because none of them touch it.
    """

    changed_paths: tuple[str, ...] = ("services/incident_service.py",)
    declared_artifacts: tuple[str, ...] = (
        "app/services/incident_service.py",
        "app/services/export_service.py",
    )
    tests_added: int = 0
    inherited_checks_green: bool = True

    @property
    def may_complete(self) -> bool:
        """Always false, and not by policy lookup.

        Declared artifacts are absent from the change set and no new component
        is exercised. Green inherited checks are irrelevant, which is precisely
        the lesson.
        """

        return False


# --------------------------------------------------------------------------
# 8. Phase 0 provenance
# --------------------------------------------------------------------------


class SuiteResult(Frozen):
    commit: str = Field(pattern=_COMMIT)
    failed: int = Field(ge=0)
    passed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    subtests_passed: int = Field(default=0, ge=0)
    note: str = ""


class RepairClass(StrEnum):
    BASELINE_RULER_REPAIR = "baseline_ruler_repair"
    OBSOLETE_TEST_MECHANISM = "obsolete_test_mechanism"


class Phase0Provenance(Frozen):
    suite_history: tuple[SuiteResult, ...]
    baseline_ruler_repairs: tuple[str, ...]
    obsolete_test_mechanisms: tuple[str, ...]
    qualification_platform: str = "Linux + Python 3.12"
    windows_status: str = Field(min_length=1)
    #: Stated explicitly so no later reading can promote a calibration fix into
    #: evidence about the harness.
    counts_as_capability_sandbox_win: bool = False

    @model_validator(mode="after")
    def validate_not_a_win(self) -> Phase0Provenance:
        if self.counts_as_capability_sandbox_win:
            raise ValueError(
                "Phase 0 repairs calibrated the ruler; counting them toward the "
                "defect-detection claim would count calibration as measurement"
            )
        return self


# --------------------------------------------------------------------------
# 9. Stop conditions
# --------------------------------------------------------------------------


class StopCondition(StrEnum):
    IDENTITY_OR_PROFILE_FAILURE = "identity_profile_readiness_containment_conformance_failed"
    CONTROLLED_VARIABLE_DIFFERS = "controlled_variable_differs"
    EVIDENCE_NOT_DURABLE = "evidence_cannot_be_written_durably"
    UNMAPPED_ACCEPTANCE_OBLIGATION = "acceptance_obligations_unmapped"
    ASYMMETRIC_TASK_INFORMATION = "arms_received_different_task_information"
    UNCLASSIFIABLE_CEILING = "ceiling_or_truncation_could_not_be_classified"
    ARM_CONTAMINATION = "an_earlier_arm_contaminated_the_next"
    SOURCE_OR_SEED_CHANGED = "source_or_seed_changed_after_freezing"


FROZEN_STOP_CONDITIONS: tuple[StopCondition, ...] = tuple(StopCondition)


# --------------------------------------------------------------------------
# The manifest
# --------------------------------------------------------------------------


class QualificationManifest(Frozen):
    """The frozen experiment. Immutable, hashed, and self-checking."""

    schema_version: str = MANIFEST_SCHEMA_VERSION
    manifest_id: str = Field(min_length=1)
    #: The tree under test.
    source_under_test_commit: str = Field(pattern=_COMMIT)
    #: The commit that carries this manifest. Recorded separately and required
    #: to differ, so the source hash never becomes self-referential.
    manifest_commit: str | None = Field(default=None, pattern=_COMMIT)
    arms: tuple[ArmSpec, ...] = Field(min_length=2, max_length=2)
    corpus: tuple[CaseSpec, ...] = Field(min_length=1)
    repetitions_per_case: int = Field(ge=3)
    controlled_variables: ControlledVariables
    non_inferiority: NonInferiorityRule
    negative_controls: tuple[NegativeControl, ...] = Field(min_length=10)
    crisis_atlas_must_pass: tuple[str, ...] = Field(min_length=15)
    phase0: Phase0Provenance
    stop_conditions: tuple[StopCondition, ...] = Field(min_length=8)

    @model_validator(mode="after")
    def validate_frozen_experiment(self) -> QualificationManifest:
        kinds = {arm.kind for arm in self.arms}
        if kinds != {ArmKind.CONTROL, ArmKind.CAPABILITY_SANDBOX}:
            raise ValueError(
                "exactly two arms are permitted: the control and the "
                "Capability Sandbox. There is no legacy harness arm"
            )
        present = {case.kind for case in self.corpus}
        absent = REQUIRED_CASE_KINDS - present
        if absent:
            raise ValueError(
                f"corpus is missing required case kinds: "
                f"{sorted(item.value for item in absent)}"
            )
        mapped = {item.control_id for item in self.negative_controls}
        referenced = {
            control_id for case in self.corpus for control_id in case.negative_control_ids
        }
        if not referenced.issubset(mapped):
            raise ValueError(
                f"corpus references unmapped negative controls: "
                f"{sorted(referenced - mapped)}"
            )
        if self.manifest_commit is not None and (
            self.manifest_commit == self.source_under_test_commit
        ):
            raise ValueError(
                "the manifest commit must differ from the source under test; "
                "otherwise the source hash describes the bookkeeping"
            )
        return self

    def digest(self) -> str:
        """One stable value identifying this frozen experiment.

        `manifest_commit` is excluded: it cannot be known while the manifest is
        being written, and including it would make the digest change when the
        manifest is committed -- so the artifact and the committed artifact
        would disagree by construction.
        """

        payload = self.model_dump(mode="json", exclude={"manifest_commit"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def required_case_ids(self) -> frozenset[str]:
        return frozenset(case.case_id for case in self.corpus if case.required_for_gate)

    @property
    def paired_executions(self) -> int:
        """Pairs implied by the corpus and repetitions."""

        return len(self.corpus) * self.repetitions_per_case

    @property
    def arm_runs(self) -> int:
        return self.paired_executions * len(self.arms)
