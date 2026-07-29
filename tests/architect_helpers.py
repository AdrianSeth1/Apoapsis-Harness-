from __future__ import annotations

from apoapsis.architect.schema import (
    ArchitectureComponent,
    ArchitectureDecision,
    ArchitecturePlan,
    ImplementationSlice,
    IntegrationContract,
    PlanDeliveryContract,
    VerificationStrategy,
)
from apoapsis.specification.schema import (
    AcceptanceCriterion,
    HardConstraint,
    SourceKind,
)


def make_slice(
    slice_id: str = "SLICE-1",
    *,
    dependencies: list[str] | None = None,
    inherited_constraint_ids: list[str] | None = None,
    acceptance_criterion_ids: list[str] | None = None,
    verification_commands: list[str] | None = None,
    suggested_paths: list[str] | None = None,
    test_obligations: list[str] | None = None,
    failure_cases: list[str] | None = None,
    integration_contract_ids: list[str] | None = None,
    architecture_component_ids: list[str] | None = None,
) -> ImplementationSlice:
    return ImplementationSlice(
        slice_id=slice_id,
        title=f"Slice {slice_id}",
        objective="Do one small, concrete thing.",
        exclusions=["Do not touch unrelated modules."],
        dependencies=dependencies or [],
        inherited_constraint_ids=(
            ["HC-1"] if inherited_constraint_ids is None else inherited_constraint_ids
        ),
        acceptance_criterion_ids=(
            ["AC-1"] if acceptance_criterion_ids is None else acceptance_criterion_ids
        ),
        integration_contract_ids=integration_contract_ids or [],
        architecture_component_ids=architecture_component_ids or [],
        # `README.md` is present by default because ADR 0076 requires the
        # plan's `primary_documentation_path` to be assigned to a slice, and
        # every fixture plan below names `README.md` as that path.
        suggested_paths=(
            ["src/example.py", "README.md"]
            if suggested_paths is None
            else suggested_paths
        ),
        suggested_symbols=["example_function"],
        context_seeds=["example"],
        verification_commands=(
            ["unit-tests"] if verification_commands is None else verification_commands
        ),
        # Default non-empty: a slice with no test obligation is now an
        # invalid plan (MISSING_TEST_OBLIGATIONS), so a helper that built
        # one by default would make every fixture unapprovable.
        test_obligations=(
            ["Resume offset is honoured."]
            if test_obligations is None
            else test_obligations
        ),
        failure_cases=(
            ["Offset file is missing or corrupt."]
            if failure_cases is None
            else failure_cases
        ),
        integration_assumptions=["The module already exists."],
        interface_contracts=["example_function(x: int) -> int"],
        local_model_fit_rationale="Small, mechanical, single-file change.",
        stop_conditions=["If the module does not exist, stop and escalate."],
        work_brief="Implement the small change described in the objective.",
    )


def make_plan(
    *,
    slices: list[ImplementationSlice] | None = None,
    hard_constraints: list[HardConstraint] | None = None,
    acceptance_criteria: list[AcceptanceCriterion] | None = None,
    verification_strategy: VerificationStrategy | None = None,
    components: list[ArchitectureComponent] | None = None,
    integration_contracts: list[IntegrationContract] | None = None,
    delivery_contract: PlanDeliveryContract | None = None,
) -> ArchitecturePlan:
    # ADR 0074 makes a plan with no whole-project verification command
    # invalid: nothing would ever run against the integrated project and
    # delivery would refuse it. Every fixture therefore declares one by
    # default, which also means the existing delivery tests exercise the
    # new final-verification gate rather than routing around it.
    resolved_slices = [make_slice()] if slices is None else slices
    # ADR 0076 requires `primary_documentation_path` to be assigned to some
    # slice. `README.md` is what `make_slice` provides by default, but a test
    # that supplies its own `suggested_paths` will not have it -- so fall back
    # to a path those slices really do claim, rather than making every such
    # test restate a delivery contract it does not care about.
    assigned = [path for item in resolved_slices for path in item.suggested_paths]
    documentation_path = "README.md" if "README.md" in assigned else (
        assigned[0] if assigned else "README.md"
    )
    return ArchitecturePlan(
        idea_text="Add resumable downloads.",
        architecture_summary="Add an offset-tracking resume layer.",
        decisions=[
            ArchitectureDecision(
                decision_id="DEC-1",
                title="Track offsets in a side file",
                rationale="Simplest persistence with no new dependency.",
            )
        ],
        hard_constraints=(
            [
                HardConstraint(
                    id="HC-1",
                    text="Preserve the current public API.",
                    verbatim_source="Preserve the current public API.",
                    interpreted_meaning="Do not change public signatures.",
                    source=SourceKind.USER,
                    source_reference="idea",
                    verification_method="unit-tests",
                )
            ]
            if hard_constraints is None
            else hard_constraints
        ),
        acceptance_criteria=(
            [
                AcceptanceCriterion(
                    id="AC-1",
                    text="Resumed downloads continue from the correct offset.",
                    source=SourceKind.USER,
                    source_reference="idea",
                )
            ]
            if acceptance_criteria is None
            else acceptance_criteria
        ),
        slices=resolved_slices,
        components=components or [],
        integration_contracts=integration_contracts or [],
        # ADR 0076: a plan must identify where an operator reads first, and
        # must either name a launch command or say why one cannot exist. The
        # controlled fixture is a library change with no launchable entry
        # point, so it states that explicitly rather than inventing a command.
        delivery_contract=(
            PlanDeliveryContract(
                primary_documentation_path=documentation_path,
                launch_not_runnable_reason=(
                    "The controlled fixture is a library change with no "
                    "launchable entry point."
                ),
            )
            if delivery_contract is None
            else delivery_contract
        ),
        verification_strategy=(
            VerificationStrategy(whole_project_verification_commands=["unit-tests"])
            if verification_strategy is None
            else verification_strategy
        ),
    )
