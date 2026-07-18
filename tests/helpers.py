from __future__ import annotations

from apoapsis.specification.schema import (
    HardConstraint,
    SourceKind,
    TaskSpecification,
    TraceableStatement,
)


def make_constraint(
    identifier: str = "HC-1", text: str = "Preserve the public API exactly."
) -> HardConstraint:
    return HardConstraint(
        id=identifier,
        text="Do not change public interfaces.",
        verbatim_source=text,
        interpreted_meaning="Existing callers require identical interfaces.",
        source=SourceKind.USER,
        source_reference="message-1",
        verification_method="Run the API snapshot test.",
    )


def make_specification(
    task_id: str = "TASK-TEST-001",
    *,
    constraints: list[HardConstraint] | None = None,
) -> TaskSpecification:
    return TaskSpecification(
        task_id=task_id,
        objective=TraceableStatement(
            text="Add resumable downloads.",
            source=SourceKind.USER,
            source_reference="message-1",
        ),
        hard_constraints=constraints or [],
    )

