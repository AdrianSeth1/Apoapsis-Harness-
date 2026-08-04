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

from apoapsis.reporting.operator_schema import OperatorExplanation
from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.behaviour import BehaviourKind, BehaviourUnit
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
    #: Symbols that must be *observed* to be exercised, from the coverage
    #: artifact's own symbol data. Only ever populated from an owner-approved
    #: `required_interfaces`, never from the planner's advisory
    #: `suggested_symbols`: advice is not a completion gate.
    required_symbols: list[str] = Field(default_factory=list)
    #: Routes that must have been *called*. A route is exercised by being
    #: requested, which only a launch or behavioural witness can observe.
    required_routes: list[str] = Field(default_factory=list)
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
        if not (
            self.required_paths
            or self.must_be_exercised
            or self.criteria
            or self.required_symbols
            or self.required_routes
        ):
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
    #:
    #: Whether one passed is derived from the witnesses, not supplied
    #: alongside them: a caller-provided set could describe a different tree,
    #: an earlier turn, or a run nobody bound to a fingerprint -- which is the
    #: stale-evidence problem the whole design refuses everywhere else.
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
    #: An addition -- a new file, a new symbol inside a modified file, or a
    #: new route -- with nothing proving it is reached. Slice 4 checked added
    #: files only; Crisis Atlas Slice 3's unreachable export routes lived in a
    #: *modified* file, which a file-level rule cannot see.
    CHANGED_BEHAVIOUR_UNEXERCISED = "changed_behaviour_unexercised"
    MISSING_REQUIRED_ARTIFACT = "missing_required_artifact"
    REQUIRED_COMMAND_NOT_PASSED = "required_command_not_passed"
    NO_USABLE_WITNESS = "no_usable_witness"


#: The rule behind an unexercised addition, stated once per packet rather than
#: once per path. Observed in CAP-4EE9F101146E4556: three new files produced
#: three copies of this sentence in one packet, and a twelve-file checkpoint
#: would have produced twelve -- all of it fed back into a 32K-window model.
UNEXERCISED_EXPLANATION = (
    "These are new in this candidate and nothing proves they are reached. "
    "Inherited tests staying green is not evidence: they stay green because "
    "they never reach them. Add or extend a test that calls each one."
)

#: The heuristic caveat, likewise stated once. It applies to whichever of the
#: listed items were found by route inference rather than by parsing.
HEURISTIC_ROUTE_EXPLANATION = (
    "These routes were found by a heuristic. If one is not a real route, "
    "remove it from the contract rather than widening what counts as covered."
)

MISSING_ARTIFACT_EXPLANATION = (
    "The contract requires these paths and the candidate does not contain "
    "them. Create each one where the contract names it, or change the "
    "contract if the path is wrong."
)


class ReadinessFinding(StrictModel):
    block: ReadinessBlock
    #: What is wrong with *this* item, and only this item.
    detail: str = Field(min_length=1)
    #: The rule this finding is an instance of, identical across every finding
    #: that shares it. Separated from `detail` so a repair packet can state it
    #: once and then list what it applies to: the model needs the rule once and
    #: the paths in full, and repeating the rule per path spends context
    #: without adding information. Empty when the detail is already
    #: self-contained.
    explanation: str = ""
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
    #: Every addition this candidate made, at file, symbol, and route level.
    behaviour_units: list[BehaviourUnit] = Field(default_factory=list)
    #: `unit_id`s that no current-state witness reaches.
    unexercised_behaviour: list[str] = Field(default_factory=list)
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


def _reaches(witnesses: list[StructuredWitness], unit: BehaviourUnit) -> bool:
    """Did any passing witness actually reach this addition?

    Line granularity where the coverage report has it, path granularity where
    it does not, and route matching for a launch or behavioural witness that
    called the route itself. A route is reached by being *called*, which no
    coverage tool reports.
    """

    for witness in witnesses:
        if not witness.passed:
            continue
        if unit.kind == BehaviourKind.NEW_ROUTE and any(
            exchange.route == unit.name for exchange in witness.exchanges
        ):
            return True
        if witness.coverage is not None and witness.coverage.covers(
            unit.path, unit.start_line, unit.end_line
        ):
            return True
    return False


def evaluate_slice_readiness(
    contract: SliceAcceptanceContract,
    delta: CandidateDelta,
    witnesses: list[StructuredWitness],
    *,
    candidate_paths: set[str] | None = None,
    behaviour_units: list[BehaviourUnit] | None = None,
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
    reached = _exercised_paths(usable)
    # Derived, never supplied. A command counts as passed only if a *usable*
    # witness -- current, fingerprint-bound, substantive -- says it passed.
    passed_commands = {item.command_name for item in usable if item.passed}
    observed_symbols: set[str] = set()
    observed_routes: set[str] = set()
    for witness in usable:
        if witness.passed:
            observed_symbols |= witness.exercised_symbols
            observed_routes |= witness.exercised_routes

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
        missing_symbols = [
            item for item in obligation.required_symbols if item not in observed_symbols
        ]
        missing_routes = [
            item for item in obligation.required_routes if item not in observed_routes
        ]
        supporting = [
            witness.witness_id
            for witness in usable
            if witness.passed
            and (
                set(obligation.criteria) & set(witness.criteria_proved)
                or set(obligation.must_be_exercised) & witness.exercised_paths
                or set(obligation.required_symbols) & witness.exercised_symbols
                or set(obligation.required_routes) & witness.exercised_routes
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
                        detail=f"{obligation.obligation_id} requires {path}",
                        explanation=MISSING_ARTIFACT_EXPLANATION,
                    )
                )
        if unexercised:
            problems.append("never exercised: " + ", ".join(unexercised))
        if missing_symbols:
            problems.append(
                "interface(s) never observed being exercised: "
                + ", ".join(missing_symbols)
            )
        if missing_routes:
            problems.append(
                "route(s) never called by any witness: " + ", ".join(missing_routes)
            )
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

    # The changed-behaviour rule, applied to the delta rather than to the
    # contract, so an addition the contract forgot to mention is still caught.
    #
    # `behaviour_units` is supplied by the checkpoint loop, which has both
    # trees and can parse them. Without it this falls back to added production
    # files, which is Slice 4's original rule and strictly weaker.
    units = behaviour_units
    if units is None:
        units = [
            BehaviourUnit(
                kind=BehaviourKind.NEW_FILE,
                path=path,
                name=path,
                start_line=1,
                end_line=1,
            )
            for path in new_production_components(delta)
        ]
    unexercised_units = [item for item in units if not _reaches(usable, item)]
    for unit in unexercised_units:
        findings.append(
            ReadinessFinding(
                block=ReadinessBlock.CHANGED_BEHAVIOUR_UNEXERCISED,
                path=unit.path,
                # The label only. "Is new in this candidate" and the rule
                # behind it are stated once, above, by the explanation; a
                # `new_file` unit whose name is its own path is named once
                # rather than twice.
                detail=(
                    unit.path
                    if unit.kind == BehaviourKind.NEW_FILE
                    else f"{unit.unit_id} ({unit.kind.value})"
                ),
                explanation=(
                    UNEXERCISED_EXPLANATION
                    + (f" {HEURISTIC_ROUTE_EXPLANATION}" if unit.heuristic else "")
                ),
            )
        )

    for command in contract.required_commands:
        if command not in passed_commands:
            findings.append(
                ReadinessFinding(
                    block=ReadinessBlock.REQUIRED_COMMAND_NOT_PASSED,
                    detail=(
                f"required command {command!r} has no current, usable, "
                "fingerprint-bound witness reporting that it passed"
            ),
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
            f"evidence, and {len(units)} behaviour unit(s) are exercised"
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
        behaviour_units=units,
        unexercised_behaviour=[item.unit_id for item in unexercised_units],
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
    #: The same verdict, written for the operator: what was attempted, what
    #: refused it, and the one next action. `detail` above stays exactly as it
    #: was -- this is an additional rendering, not a replacement, and the UI
    #: shows this first with `detail` behind it.
    operator: OperatorExplanation | None = None


def _operator(outcome: CheckpointOutcome, detail: str) -> OperatorExplanation:
    """Build the operator rendering for a verdict.

    Imported inside the function on purpose: `reporting.operator` imports
    `CheckpointOutcome` from this module to build its table, so a module-level
    import here would be a cycle. The table lives there because that is where
    the *operator vocabulary* lives -- checkpoint verdicts, session outcomes
    and review stop reasons are written in one voice or they read as three
    different products.
    """

    from apoapsis.reporting.operator import explain_checkpoint

    return explain_checkpoint(outcome, detail)


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
            operator=_operator(
                CheckpointOutcome.CANDIDATE_REFUSED,
                f"the candidate was not admitted: {admission_detail}",
            ),
        )

    if readiness.ready:
        return CheckpointDecision(
            outcome=CheckpointOutcome.COMPLETE,
            admitted=True,
            ready=True,
            detail=readiness.detail,
            operator=_operator(CheckpointOutcome.COMPLETE, readiness.detail),
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
            operator=_operator(
                CheckpointOutcome.HUMAN_REVIEW_REQUIRED, readiness.detail
            ),
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
        operator=_operator(CheckpointOutcome.CONTINUE, readiness.detail),
    )


def readiness_packet(report: SliceReadinessReport) -> str:
    """A compact statement of what is still missing, for a repair context.

    Grouped by explanation, not listed per finding. This text is fed straight
    back into a 32K-window model, and the previous shape repeated each rule
    once per path: CAP-4EE9F101146E4556's packet carried three copies of the
    unexercised-behaviour paragraph for three files, and a twelve-file
    checkpoint would have carried twelve. Same information, a fraction of the
    tokens, and easier for a small model to act on -- one rule, then the list
    it applies to, is how a person would write it.

    Grouping preserves order: the first time an explanation appears fixes its
    position, so a packet does not reshuffle between turns for a model that is
    reading it against the previous one.
    """

    if report.ready:
        return "The slice is ready; there is nothing outstanding."
    lines = [
        f"Slice {report.slice_id} is not ready. {len(report.findings)} unmet "
        "condition(s):",
        "",
    ]

    groups: dict[tuple[str, str], list[ReadinessFinding]] = {}
    for finding in report.findings:
        groups.setdefault((finding.block.value, finding.explanation), []).append(
            finding
        )

    for (block, explanation), findings in groups.items():
        if explanation:
            lines.append(f"{block} ({len(findings)}): {explanation}")
            for finding in findings:
                location = finding.path or finding.obligation_id
                lines.append(
                    f"  - {finding.detail}"
                    + (f" [{location}]" if location and location not in finding.detail
                       else "")
                )
        else:
            # No shared rule to hoist: these details stand alone, so grouping
            # them under a heading would add a line rather than remove one.
            for finding in findings:
                location = f" [{finding.path or finding.obligation_id}]" if (
                    finding.path or finding.obligation_id
                ) else ""
                lines.append(f"- {block}{location}: {finding.detail}")
        lines.append("")

    if report.rejected_witnesses:
        lines.append("Witnesses that could not be used as evidence:")
        for rejection in report.rejected_witnesses:
            lines.append(
                f"- {rejection.witness_id} ({rejection.problem.value}): "
                f"{rejection.detail}"
            )
    return "\n".join(lines).rstrip() + "\n"
