from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from apoapsis.context.compiler import ContextPackage
from apoapsis.repository.git import RepositorySnapshot
from apoapsis.specification.schema import (
    AcceptanceCriterion,
    HardConstraint,
    RiskLevel,
    StrictModel,
    utc_now,
)


class ArchitectureDecision(StrictModel):
    decision_id: str = Field(pattern=r"^DEC-[A-Za-z0-9._-]+$")
    title: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    alternatives_considered: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)


class ArchitectureComponent(StrictModel):
    """One named unit of the proposed system (schema 1.1). Advisory, like
    ``ImplementationSlice`` cross-references -- ``validate_plan`` is the
    sole authority on whether ``component_id`` references from slices,
    integration contracts, and hard problems actually resolve."""

    component_id: str = Field(pattern=r"^COMP-[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1)
    responsibility: str = Field(min_length=1)
    owned_paths: list[str] = Field(default_factory=list)
    interfaces_provided: list[str] = Field(default_factory=list)
    interfaces_consumed: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)


class IntegrationContract(StrictModel):
    """One producer/consumer boundary between components (schema 1.1)."""

    contract_id: str = Field(pattern=r"^INT-[A-Za-z0-9._-]+$")
    producer_component_id: str = Field(pattern=r"^COMP-[A-Za-z0-9._-]+$")
    consumer_component_ids: list[str] = Field(default_factory=list)
    interface: str = Field(min_length=1)
    data_flow: str = Field(min_length=1)
    error_behavior: str = Field(min_length=1)
    verification_obligation: str = Field(min_length=1)


class AnticipatedHardProblem(StrictModel):
    """One risk the planner expects to be genuinely difficult, with its
    proposed handling (schema 1.1)."""

    problem_id: str = Field(pattern=r"^HARD-[A-Za-z0-9._-]+$")
    title: str = Field(min_length=1)
    why_hard: str = Field(min_length=1)
    affected_component_ids: list[str] = Field(default_factory=list)
    proposed_solution: str = Field(min_length=1)
    alternatives_considered: list[str] = Field(default_factory=list)
    fallback_or_stop_condition: str = Field(min_length=1)
    risks_if_mishandled: list[str] = Field(default_factory=list)
    validation_plan: str = Field(min_length=1)


class ImplementationSlice(StrictModel):
    """One small, independently verifiable work packet sized for the local
    coding model. All cross-references (dependencies, constraint IDs,
    criterion IDs, verification command names) are advisory proposals from
    the planner -- ``validation.validate_plan`` is the sole deterministic
    authority on whether they are actually well-formed; this schema only
    enforces per-field shape so that an invalid plan can still be imported,
    stored, and inspected with concrete findings rather than rejected
    outright."""

    slice_id: str = Field(pattern=r"^SLICE-[A-Za-z0-9._-]+$")
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    exclusions: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    inherited_constraint_ids: list[str] = Field(default_factory=list)
    acceptance_criterion_ids: list[str] = Field(default_factory=list)
    # Cross-references into the schema 1.1 sections below (advisory, like
    # every other ID list on this model -- validate_plan checks them).
    architecture_component_ids: list[str] = Field(default_factory=list)
    integration_contract_ids: list[str] = Field(default_factory=list)
    hard_problem_ids: list[str] = Field(default_factory=list)
    suggested_paths: list[str] = Field(default_factory=list)
    suggested_symbols: list[str] = Field(default_factory=list)
    context_seeds: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    integration_assumptions: list[str] = Field(default_factory=list)
    interface_contracts: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.UNCLASSIFIED
    local_model_fit_rationale: str = Field(min_length=1)
    stop_conditions: list[str] = Field(default_factory=list)
    # Free-text elaboration (schema 1.1), same trust level as
    # stop_conditions above: ordered advisory strings, not cross-validated.
    implementation_steps: list[str] = Field(default_factory=list)
    failure_cases: list[str] = Field(default_factory=list)
    test_obligations: list[str] = Field(default_factory=list)
    work_brief: str = Field(min_length=1)


class PlanDeliveryContract(StrictModel):
    """How the finished project is installed, launched, and proven ready
    (schema 1.1). Purely descriptive -- Apoapsis never executes any of
    these instructions itself; they document what a human operator does
    after the plan's slices are delivered. Every field defaults empty so
    an ``ArchitecturePlan`` always carries one populated or not, never
    ``None`` -- the planner fills in what it knows."""

    project_kind: str = ""
    primary_documentation_path: str = ""
    install_instructions: str = ""
    launch_or_usage_instructions: str = ""
    test_instructions: str = ""
    readiness_checks: list[str] = Field(default_factory=list)
    required_artifacts: list[str] = Field(default_factory=list)
    credential_setup_instructions: str = ""


class RuntimeDesign(StrictModel):
    """The proposed system's shape at runtime (schema 1.1): entry points,
    configuration surface, secrets handling, external dependencies, and
    operational behavior. Descriptive only, same as
    ``PlanDeliveryContract``; every field defaults empty."""

    entry_points: list[str] = Field(default_factory=list)
    configuration: list[str] = Field(default_factory=list)
    credentials_and_secrets: list[str] = Field(default_factory=list)
    external_services: list[str] = Field(default_factory=list)
    lifecycle: str = ""
    failure_recovery: str = ""
    observability: str = ""


class AcceptanceProofObligation(StrictModel):
    """How one acceptance criterion will be demonstrated (schema 1.1)."""

    criterion_id: str = Field(pattern=r"^AC-[A-Za-z0-9._-]+$")
    proof: str = Field(min_length=1)
    verification_commands: list[str] = Field(default_factory=list)


class EndToEndScenario(StrictModel):
    """One whole-project scenario the finished plan must satisfy end to
    end (schema 1.1), spanning more than one slice."""

    scenario_id: str = Field(pattern=r"^E2E-[A-Za-z0-9._-]+$")
    setup: str = Field(min_length=1)
    behavior: str = Field(min_length=1)
    expected_result: str = Field(min_length=1)
    verification_commands: list[str] = Field(default_factory=list)


class VerificationStrategy(StrictModel):
    """The plan's whole-project verification story (schema 1.1), above and
    beyond each individual slice's own ``verification_commands``. Every
    field defaults empty, same as ``PlanDeliveryContract``/``RuntimeDesign``."""

    whole_project_verification_commands: list[str] = Field(default_factory=list)
    slice_test_strategy: str = ""
    cross_slice_integration_strategy: str = ""
    acceptance_proof_obligations: list[AcceptanceProofObligation] = Field(
        default_factory=list
    )
    end_to_end_scenarios: list[EndToEndScenario] = Field(default_factory=list)


class ArchitecturePlan(StrictModel):
    """The planner's entire proposal. Deliberately has no status, approval,
    execution, shell, or filesystem field of any kind -- ``extra="forbid"``
    means a planner response that attempts to smuggle one in (for example
    ``"status": "approved"``) fails validation outright rather than being
    silently accepted or ignored. Approval/execution status lives only on
    the harness-owned ``PlanRecord``, never here."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    # "1.1" additively covers the richer sections below (components,
    # integration_contracts, anticipated_hard_problems, delivery_contract,
    # runtime_design, verification_strategy) plus the matching slice-level
    # fields on ImplementationSlice; "1.0" plans remain valid unchanged
    # since every new field defaults to empty.
    schema_version: Literal["1.0", "1.1"] = "1.1"
    idea_text: str = Field(min_length=1)
    architecture_summary: str = Field(min_length=1)
    decisions: list[ArchitectureDecision] = Field(default_factory=list)
    hard_constraints: list[HardConstraint] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    slices: list[ImplementationSlice] = Field(default_factory=list)
    components: list[ArchitectureComponent] = Field(default_factory=list)
    integration_contracts: list[IntegrationContract] = Field(default_factory=list)
    anticipated_hard_problems: list[AnticipatedHardProblem] = Field(
        default_factory=list
    )
    delivery_contract: PlanDeliveryContract = Field(
        default_factory=PlanDeliveryContract
    )
    runtime_design: RuntimeDesign = Field(default_factory=RuntimeDesign)
    verification_strategy: VerificationStrategy = Field(
        default_factory=VerificationStrategy
    )


class PlanStatus(StrEnum):
    """Harness-owned plan lifecycle. Never set from parsing planner JSON --
    only ``SQLitePlanStore`` transitions a record between these values."""

    PROPOSED = "proposed"
    VALIDATED = "validated"
    APPROVED = "approved"
    SUPERSEDED = "superseded"
    EXECUTED = "executed"


class PlanActor(StrEnum):
    SYSTEM = "system"
    USER = "user"
    OPERATOR = "operator"


class PlanEvent(StrictModel):
    event_id: str = Field(pattern=r"^EVT-[A-Za-z0-9._-]+$")
    sequence: int | None = Field(default=None, ge=1)
    plan_id: str = Field(pattern=r"^PLAN-[A-Za-z0-9._-]+$")
    event_type: str = Field(min_length=1)
    from_status: PlanStatus | None
    to_status: PlanStatus
    actor: PlanActor
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ValidationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class PlanValidationFinding(StrictModel):
    severity: ValidationSeverity
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    slice_id: str | None = None


class PlanValidationResult(StrictModel):
    plan_id: str = Field(pattern=r"^PLAN-[A-Za-z0-9._-]+$")
    plan_version: int = Field(ge=1)
    valid: bool
    findings: list[PlanValidationFinding] = Field(default_factory=list)
    validated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_flag_matches_findings(self) -> PlanValidationResult:
        has_errors = any(
            item.severity == ValidationSeverity.ERROR for item in self.findings
        )
        if self.valid == has_errors:
            raise ValueError(
                "valid must be False if and only if an error finding is present"
            )
        return self


class VerificationCatalogEntry(StrictModel):
    """Name-only descriptor of a configured verification command -- mirrors
    ``specification.extractor``'s acceptance-command catalog shape exactly
    (never argv/environment), so the planner package can never transmit
    executable shell content, only command names to choose from."""

    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    description: str = ""
    acceptance_designated: bool = False


PLAN_AUTHORITY_RULES: list[str] = [
    "You (the planner) may only return JSON matching PLAN_JSON_SCHEMA below.",
    "You have no shell, filesystem, Git, network, or workflow-transition "
    "authority; nothing you write executes anything.",
    "You cannot mark a plan approved, validated, or executed. That status "
    "is decided solely by the Apoapsis harness after a human explicitly "
    "approves it.",
    "verification_commands entries must name commands from "
    "VERIFICATION_CATALOG only; inventing a command name is rejected by "
    "deterministic validation, never executed as a request.",
    "suggested_paths are advisory hints for a local coding model, not a "
    "grant to write outside the repository; paths must be repository- "
    "relative and are rejected if they escape it.",
    "This package and your response are both retained verbatim as an "
    "immutable audit record before any further action is taken.",
]


class PlannerRequestPackage(StrictModel):
    """Everything a strong external model needs to propose an
    ``ArchitecturePlan``, and nothing more: no credentials, no execution
    path, no ambient authority. Built once by ``architect.package`` and
    written to disk before it ever leaves Apoapsis."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    package_version: Literal["1.0"] = "1.0"
    package_id: str = Field(pattern=r"^PKG-[A-Za-z0-9._-]+$")
    idea_text: str = Field(min_length=1)
    repository: RepositorySnapshot
    context: ContextPackage
    documentation_references: list[str] = Field(default_factory=list)
    verification_catalog: list[VerificationCatalogEntry] = Field(default_factory=list)
    plan_json_schema: dict[str, Any]
    authority_rules: list[str] = Field(default_factory=lambda: list(PLAN_AUTHORITY_RULES))
    generated_at: datetime = Field(default_factory=utc_now)
    package_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def derive_digest(self) -> PlannerRequestPackage:
        canonical = self.model_dump(mode="json", exclude={"package_sha256"})
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        if self.package_sha256 is None:
            self.package_sha256 = digest
        elif self.package_sha256 != digest:
            raise ValueError("package_sha256 does not match package content")
        return self


class PlannerResponseEnvelope(StrictModel):
    """The manual copy-paste response wrapper a human saves after running
    the exported package through Claude, Codex, Fabel, or another model.
    ``request_package_sha256`` must match the stored package's own
    ``package_sha256`` exactly, or import is rejected."""

    schema_version: Literal["1.0"] = "1.0"
    package_id: str = Field(pattern=r"^PKG-[A-Za-z0-9._-]+$")
    request_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan: ArchitecturePlan


class PlanRecord(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    plan_id: str = Field(pattern=r"^PLAN-[A-Za-z0-9._-]+$")
    # Accepts both a manually-exported PlannerRequestPackage id (PKG-...,
    # ADR 0019) and a discovery-flow FrontierPlanningRequestPackage id
    # (FPKG-..., ADR 0032) -- widened additively so
    # SQLitePlanStore.create_plan() is genuinely reused by both origins
    # rather than duplicated.
    package_id: str = Field(pattern=r"^(PKG|FPKG)-[A-Za-z0-9._-]+$")
    idea_text: str = Field(min_length=1)
    plan: ArchitecturePlan
    validation: PlanValidationResult | None = None
    status: PlanStatus
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
