from __future__ import annotations

import uuid
from pathlib import Path

from apoapsis.architect.schema import (
    ArchitecturePlan,
    PlannerRequestPackage,
    VerificationCatalogEntry,
)
from apoapsis.config import ApoapsisConfig, ContextCompilerConfig
from apoapsis.context.compiler import ContextCompiler
from apoapsis.repository.git import GitRepository
from apoapsis.specification.schema import SourceKind, TaskSpecification, TraceableStatement


def _verification_catalog(config: ApoapsisConfig) -> list[VerificationCatalogEntry]:
    return [
        VerificationCatalogEntry(
            name=command.name,
            category=command.category,
            description=command.description,
            acceptance_designated=command.acceptance,
        )
        for command in sorted(config.verification.commands, key=lambda item: item.name)
    ]


def _documentation_references(root: Path) -> list[str]:
    references: list[str] = []
    if (root / "HANDOFF.md").is_file():
        references.append("HANDOFF.md")
    adr_dir = root / "docs" / "adr"
    if adr_dir.is_dir():
        references.extend(
            sorted(
                path.relative_to(root).as_posix()
                for path in adr_dir.glob("*.md")
            )
        )
    return references


def build_planner_request_package(
    root: Path,
    idea_text: str,
    config: ApoapsisConfig,
    *,
    context_config: ContextCompilerConfig | None = None,
) -> PlannerRequestPackage:
    """Build the reproducible package a strong external model needs to
    propose an ``ArchitecturePlan`` for ``idea_text``. Every field is
    deterministic given the current repository state and configuration --
    running this twice against an unchanged worktree yields the same
    ``package_sha256`` (modulo ``generated_at``/``package_id``)."""

    repository = GitRepository(root)
    snapshot = repository.snapshot()
    package_id = f"PKG-{uuid.uuid4().hex[:12].upper()}"

    ephemeral_specification = TaskSpecification(
        task_id=f"TASK-ARCH-{package_id[len('PKG-'):]}",
        objective=TraceableStatement(
            text=idea_text,
            source=SourceKind.USER,
            source_reference="architect-idea",
        ),
    )
    compiler = ContextCompiler(context_config or config.context)
    context_package = compiler.compile(ephemeral_specification, root)

    return PlannerRequestPackage(
        package_id=package_id,
        idea_text=idea_text,
        repository=snapshot,
        context=context_package,
        documentation_references=_documentation_references(root),
        verification_catalog=_verification_catalog(config),
        plan_json_schema=ArchitecturePlan.model_json_schema(),
    )
