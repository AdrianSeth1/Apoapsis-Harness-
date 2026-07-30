"""Slice readiness: what "done" means, replacing "the checks went green".

This is the module that would have stopped Crisis Atlas Slice 2.

That slice asked for `IncidentService`, `ExportService`, and tests. Qwen
proposed one partial file at the wrong package path, created no export service,
and wrote no tests. Apoapsis applied it, ran the inherited checks, saw green,
and terminated the session as `COMPLETE`. The inherited tests stayed green
*precisely because they never imported the new file* — greenness was evidence
that nothing had changed, and it was read as evidence that everything had.

The handoff's correction, which this implements:

> Required commands are necessary evidence. They are not the definition of a
> completed slice.

So a slice is compiled into a `SliceAcceptanceContract` **before any model
spend**, listing every obligation it must discharge. Readiness is then a
question about obligations and current-state evidence, and configured commands
passing is one input to it rather than the whole of it.

The **new-component rule** is the sharp edge:

> A new production component cannot complete solely because inherited tests
> remain green. At least one current-state witness must prove the new path is
> reached.

Merely adding a test file is not enough — a file that exists but never imports
the component proves nothing. Merely preserving inherited green tests is not
enough, for the reason above. What counts is a structured witness whose
*coverage* names the component, or a behavioural witness that drove it through
the product boundary, or an owner's explicit written reason that it is
intentionally unmeasured — and that last one blocks automatic `COMPLETE` rather
than satisfying it.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.delta import CandidateDelta, ChangeKind, PathClass
from apoapsis.workcell.witness import (
    EvidenceClass,
    StructuredWitness,
    WitnessKind,
    WitnessRejection,
    usable_witnesses,
)

CONTRACT_SCHEMA_VERSION = "1.0"


class ObligationKind(StrEnum):
    """The obligation classes the handoff enumerates for a slice contract."""

    #: A production file or component the slice must actually produce.
    PRODUCTION_ARTIFACT = "production_artifact"
    #: A named interface or symbol other slices will consume.
    INTERFACE = "interface"
    #: A test or witness that must exist and must prove something.
    TEST_OR_WITNESS = "test_or_witness"
    #: An edge this slice introduces or consumes, e.g. dashboard -> HTTP API.
    INTEGRATION_EDGE = "integration_edge"
    DOCUMENTATION = "documentation"
    OPERABILITY = "operability"
    #: A deliberate mutation that the evidence must catch.
    NEGATIVE_CONTROL = "negative_control"


class ObligationStatus(StrEnum):
    PROVED = "proved"
    #: Required, and nothing current proves it.
    UNPROVED = "unproved"
    #: The owner wrote down why this is not measured. Blocks automatic
    #: completion rather than satisfying the obligation.
    INTENTIONALLY_UNMEASURED = "intentionally_unmeasured"


class AcceptanceObligation(StrictModel):
    """One thing the slice must discharge, and how it may be discharged."""

    obligation_id: str = Field(min_length=1)
    kind: ObligationKind
    description: str = Field(min_length=1)
    #: Repository-relative paths that must exist in the candidate. For a
    #: production artifact this is the declared package path -- the field that
    #: would have caught Slice 2's wrong-path service.
    required_paths: list[str] = Field(default_factory=list)
    #: Paths that must be *reached* by some witness's coverage, not merely
    #: present. Usually the same as `required_paths` for a new component.
    must_be_exercised: list[str] = Field(default_factory=list)
    #: Criterion identifiers a witness must claim to have proved.
    criteria: list[str] = Field(default_factory=list)
    #: Only an independent witness may discharge this obligation. Used where
    #: model-authored tests would be marking their own homework.
    requires_independent_evidence: bool = False
    #: The owner's explicit reason this is not measured. Its presence is what
    #: turns silence into a visible statement.
    unmeasured_reason: str = ""

    @model_validator(mode="after")
    def validate_dischargeable(self) -> AcceptanceObligation:
        if self.unmeasured_reason:
            return self
        if not (self.required_paths or self.must_be_exercised or self.criteria):
            raise ValueError(
                f"obligation {self.obligation_id!r} names nothing that could "
                "discharge it, so it could never be proved or disproved"
            )
        return self


class SliceAcceptanceContract(StrictModel):
    """Compiled before model spend, so completion has a definition to meet."""

    schema_version: str = CONTRACT_SCHEMA_VERSION
    slice_id: str = Field(min_length=1)
    plan_id: str | None = None
    #: Every active criterion for this slice.
    criteria: list[str] = Field(min_length=1)
    obligations: list[AcceptanceObligation] = Field(min_length=1)
    #: Commands whose passing is necessary but, by itself, never sufficient.
    required_commands: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_criteria_are_covered(self) -> SliceAcceptanceContract:
        claimed = {item for obligation in self.obligations for item in obligation.criteria}
        # An obligation-free criterion can never be proved, which would make
        # the contract unsatisfiable in a way nobody notices until delivery.
        orphaned = sorted(set(self.criteria) - claimed)
        if orphaned:
            raise ValueError(
                "these criteria have no obligation that could prove them: "
                + ", ".join(orphaned)
            )
        return self

    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ObligationResult(StrictModel):
    obligation_id: str
    kind: ObligationKind
    status: ObligationStatus
    detail: str = Field(min_length=1)
    #: Witness ids that discharged it, for the audit trail.
    supporting_witnesses: list[str] = Field(default_factory=list)


class ReadinessBlock(StrEnum):
    OBLIGATION_UNPROVED = "obligation_unproved"
    INTENTIONALLY_UNMEASURED = "intentionally_unmeasured"
    #: A new production component with nothing proving it is reached.
    NEW_COMPONENT_UNEXERCISED = "new_component_unexercised"
    MISSING_REQUIRED_ARTIFACT = "missing_required_artifact"
    REQUIRED_COMMAND_NOT_PASSED = "required_command_not_passed"
    NO_USABLE_WITNESS = "no_usable_witness"


class ReadinessFinding(StrictModel):
    block: ReadinessBlock
    detail: str = Field(min_length=1)
    path: str | None = None
    obligation_id: str | None = None


class SliceReadinessReport(StrictModel):
    schema_version: str = "1.0"
    slice_id: str = Field(min_length=1)
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    ready: bool = False
    obligations: list[ObligationResult] = Field(default_factory=list)
    findings: list[ReadinessFinding] = Field(default_factory=list)
    rejected_witnesses: list[WitnessRejection] = Field(default_factory=list)
    #: New production components this candidate introduced, and whether each is
    #: reached by current-state evidence.
    new_components: list[str] = Field(default_factory=list)
    unexercised_new_components: list[str] = Field(default_factory=list)
    detail: str = Field(min_length=1)


def new_production_components(delta: CandidateDelta) -> list[str]:
    """Production files this candidate added. Modified files are not new.

    Deliberately narrow: the new-component rule is about paths that did not
    exist before, because those are the ones inherited tests structurally
    cannot have been exercising.
    """

    return sorted(
        entry.path
        for entry in delta.entries
        if entry.kind == ChangeKind.ADDED and entry.path_class == PathClass.PRODUCTION
    )


def _exercised_paths(witnesses: list[StructuredWitness]) -> set[str]:
    reached: set[str] = set()
    for witness in witnesses:
        if not witness.passed:
            continue
        reached |= witness.exercised_paths
        # A behavioural or launch witness that drove the product proves the
        # paths its coverage names; without coverage it proves the routes it
        # called, which the obligation layer checks separately.
    return reached


def evaluate_slice_readiness(
    contract: SliceAcceptanceContract,
    delta: CandidateDelta,
    witnesses: list[StructuredWitness],
    *,
    candidate_paths: set[str] | None = None,
    passed_commands: set[str] | None = None,
) -> SliceReadinessReport:
    """Decide whether the slice is done. Green commands are one input, not the answer.

    `candidate_paths` is the full path set of the candidate tree; the delta
    alone cannot tell whether a required artifact that was never touched
    already exists.
    """

    usable, rejected = usable_witnesses(
        witnesses, current_fingerprint=delta.candidate_fingerprint
    )
    present = candidate_paths if candidate_paths is not None else set(delta.paths)
    passed_commands = passed_commands or set()
    reached = _exercised_paths(usable)

    findings: list[ReadinessFinding] = []
    results: list[ObligationResult] = []

    for obligation in contract.obligations:
        if obligation.unmeasured_reason:
            results.append(
                ObligationResult(
                    obligation_id=obligation.obligation_id,
                    kind=obligation.kind,
                    status=ObligationStatus.INTENTIONALLY_UNMEASURED,
                    detail=obligation.unmeasured_reason,
                )
            )
            findings.append(
                ReadinessFinding(
                    block=ReadinessBlock.INTENTIONALLY_UNMEASURED,
                    obligation_id=obligation.obligation_id,
                    detail=(
                        f"{obligation.obligation_id} is intentionally unmeasured "
                        f"({obligation.unmeasured_reason}); this prevents automatic "
                        "completion and routes the slice to human review"
                    ),
                )
            )
            continue

        missing_paths = [item for item in obligation.required_paths if item not in present]
        unexercised = [item for item in obligation.must_be_exercised if item not in reached]
        supporting = [
            witness.witness_id
            for witness in usable
            if witness.passed
            and (
                set(obligation.criteria) & set(witness.criteria_proved)
                or set(obligation.must_be_exercised) & witness.exercised_paths
            )
            and (
                not obligation.requires_independent_evidence
                or witness.evidence_class == EvidenceClass.INDEPENDENT
            )
        ]
        unproved_criteria = [
            criterion
            for criterion in obligation.criteria
            if not any(
                criterion in witness.criteria_proved
                for witness in usable
                if witness.passed
                and (
                    not obligation.requires_independent_evidence
                    or witness.evidence_class == EvidenceClass.INDEPENDENT
                )
            )
        ]

        problems: list[str] = []
        if missing_paths:
            problems.append("missing artifact(s): " + ", ".join(missing_paths))
            for path in missing_paths:
                findings.append(
                    ReadinessFinding(
                        block=ReadinessBlock.MISSING_REQUIRED_ARTIFACT,
                        obligation_id=obligation.obligation_id,
                        path=path,
                        detail=(
                            f"{obligation.obligation_id} requires {path}, which the "
                            "candidate does not contain at that path"
                        ),
                    )
                )
        if unexercised:
            problems.append("never exercised: " + ", ".join(unexercised))
        if unproved_criteria:
            problems.append("unproved criteria: " + ", ".join(unproved_criteria))

        if problems:
            results.append(
                ObligationResult(
                    obligation_id=obligation.obligation_id,
                    kind=obligation.kind,
                    status=ObligationStatus.UNPROVED,
                    detail="; ".join(problems),
                    supporting_witnesses=supporting,
                )
            )
            findings.append(
                ReadinessFinding(
                    block=ReadinessBlock.OBLIGATION_UNPROVED,
                    obligation_id=obligation.obligation_id,
                    detail=f"{obligation.obligation_id}: " + "; ".join(problems),
                )
            )
        else:
            results.append(
                ObligationResult(
                    obligation_id=obligation.obligation_id,
                    kind=obligation.kind,
                    status=ObligationStatus.PROVED,
                    detail="discharged by current-state evidence",
                    supporting_witnesses=supporting,
                )
            )

    # The new-component rule, applied to the delta rather than to the contract,
    # so a component the contract forgot to mention is still caught.
    components = new_production_components(delta)
    unexercised_components = [item for item in components if item not in reached]
    for path in unexercised_components:
        findings.append(
            ReadinessFinding(
                block=ReadinessBlock.NEW_COMPONENT_UNEXERCISED,
                path=path,
                detail=(
                    f"{path} is a new production component and no current-state "
                    "witness proves it is reached. Inherited tests staying green "
                    "is not evidence: they stay green because they never import it."
                ),
            )
        )

    for command in contract.required_commands:
        if command not in passed_commands:
            findings.append(
                ReadinessFinding(
                    block=ReadinessBlock.REQUIRED_COMMAND_NOT_PASSED,
                    detail=f"required command {command!r} has not passed for this state",
                )
            )

    if contract.obligations and not usable:
        findings.append(
            ReadinessFinding(
                block=ReadinessBlock.NO_USABLE_WITNESS,
                detail=(
                    "no witness survived validation, so nothing current proves "
                    "anything about this candidate"
                ),
            )
        )

    ready = not findings
    if ready:
        detail = (
            f"slice {contract.slice_id} is ready: all "
            f"{len(contract.obligations)} obligation(s) discharged by current-state "
            f"evidence, and {len(components)} new component(s) are exercised"
        )
    else:
        detail = (
            f"slice {contract.slice_id} is not ready: {len(findings)} unmet "
            "condition(s). " + "; ".join(item.detail for item in findings[:6])
            + ("; ..." if len(findings) > 6 else "")
        )

    return SliceReadinessReport(
        slice_id=contract.slice_id,
        contract_digest=contract.digest(),
        ready=ready,
        obligations=results,
        findings=findings,
        rejected_witnesses=rejected,
        new_components=components,
        unexercised_new_components=unexercised_components,
        detail=detail,
    )


class SliceNotReady(RuntimeError):
    """The slice may not be completed."""


def require_ready(report: SliceReadinessReport) -> SliceReadinessReport:
    """The raising form.

    ADR 0069 let "all configured checks are green" end a session. This is the
    replacement, and it is deliberately the only door: a caller cannot reach
    completion by ignoring a return value.
    """

    if not report.ready:
        raise SliceNotReady(report.detail)
    return report


class CheckpointOutcome(StrEnum):
    """What a `ready_for_evaluation` checkpoint concluded."""

    #: The candidate was admitted and every obligation is discharged.
    COMPLETE = "complete"
    #: Admitted, but obligations remain. The agent gets another turn.
    CONTINUE = "continue"
    #: The candidate itself was refused; nothing was promoted.
    CANDIDATE_REFUSED = "candidate_refused"
    #: Something is intentionally unmeasured, or evidence is missing in a way
    #: no further model turn can supply.
    HUMAN_REVIEW_REQUIRED = "human_review_required"


class CheckpointDecision(StrictModel):
    schema_version: str = "1.0"
    outcome: CheckpointOutcome
    admitted: bool
    ready: bool
    detail: str = Field(min_length=1)
    #: What to hand back to a repair context. Empty when complete.
    repair_packet: str = ""


def evaluate_checkpoint(
    admission_admitted: bool,
    admission_detail: str,
    readiness: SliceReadinessReport,
) -> CheckpointDecision:
    """Decide what happens when the agent asks to be inspected.

    ADR 0069 ended a session as soon as every configured command was green.
    That is what turned Crisis Atlas Slice 2's single wrong-path file into a
    final result: the checks were green because they never touched it.

    The replacement is this function's shape. Green commands cannot appear
    here at all — they are one input to `evaluate_slice_readiness`, several
    layers down — and the only path to `COMPLETE` runs through an admitted
    candidate *and* a ready slice. `CONTINUE` is the outcome that was missing:
    the agent gets another turn to finish its own stated plan, which is exactly
    what Slice 2 was never given.
    """

    if not admission_admitted:
        return CheckpointDecision(
            outcome=CheckpointOutcome.CANDIDATE_REFUSED,
            admitted=False,
            ready=False,
            detail=f"the candidate was not admitted: {admission_detail}",
            repair_packet=admission_detail,
        )

    if readiness.ready:
        return CheckpointDecision(
            outcome=CheckpointOutcome.COMPLETE,
            admitted=True,
            ready=True,
            detail=readiness.detail,
        )

    # An intentionally unmeasured obligation is not something the agent can
    # fix by trying harder; it is an owner statement that routes the slice to a
    # human. Sending it back for repair would loop.
    if any(
        item.block == ReadinessBlock.INTENTIONALLY_UNMEASURED
        for item in readiness.findings
    ):
        return CheckpointDecision(
            outcome=CheckpointOutcome.HUMAN_REVIEW_REQUIRED,
            admitted=True,
            ready=False,
            detail=(
                "the candidate was admitted, but an obligation is intentionally "
                f"unmeasured, so completion is a human judgement: {readiness.detail}"
            ),
            repair_packet=readiness_packet(readiness),
        )

    return CheckpointDecision(
        outcome=CheckpointOutcome.CONTINUE,
        admitted=True,
        ready=False,
        detail=(
            "the candidate was admitted and the slice is not yet ready; the agent "
            f"gets another turn. {readiness.detail}"
        ),
        repair_packet=readiness_packet(readiness),
    )


def readiness_packet(report: SliceReadinessReport) -> str:
    """A compact statement of what is still missing, for a repair context."""

    if report.ready:
        return "The slice is ready; there is nothing outstanding."
    lines = [
        f"Slice {report.slice_id} is not ready. {len(report.findings)} unmet "
        "condition(s):",
        "",
    ]
    for finding in report.findings:
        location = f" [{finding.path or finding.obligation_id}]" if (
            finding.path or finding.obligation_id
        ) else ""
        lines.append(f"- {finding.block.value}{location}: {finding.detail}")
    if report.rejected_witnesses:
        lines.append("")
        lines.append("Witnesses that could not be used as evidence:")
        for rejection in report.rejected_witnesses:
            lines.append(
                f"- {rejection.witness_id} ({rejection.problem.value}): "
                f"{rejection.detail}"
            )
    return "\n".join(lines)
