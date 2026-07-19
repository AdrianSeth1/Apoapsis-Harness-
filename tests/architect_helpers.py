from __future__ import annotations

from apoapsis.architect.schema import (
    ArchitectureDecision,
    ArchitecturePlan,
    ImplementationSlice,
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
        suggested_paths=(
            ["src/example.py"] if suggested_paths is None else suggested_paths
        ),
        suggested_symbols=["example_function"],
        context_seeds=["example"],
        verification_commands=(
            ["unit-tests"] if verification_commands is None else verification_commands
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
) -> ArchitecturePlan:
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
        slices=[make_slice()] if slices is None else slices,
    )
