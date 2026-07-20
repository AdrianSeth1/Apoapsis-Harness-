from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from apoapsis.architect.schema import ArchitecturePlan, VerificationCatalogEntry
from apoapsis.config import ApoapsisConfig
from apoapsis.context.compiler import ContextCompiler
from apoapsis.discovery.errors import PackageIntegrityError, PackageNotFoundError
from apoapsis.discovery.schema import (
    ClarificationAnswer,
    ClarificationQuestion,
    FrontierPlanningRequestPackage,
    FrontierPlanningResponseEnvelope,
    IdeaBrief,
)
from apoapsis.repository.git import GitRepository
from apoapsis.specification.schema import SourceKind, TaskSpecification, TraceableStatement


def _sha256_canonical(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def build_frontier_planning_request_package(
    root: str | Path,
    config: ApoapsisConfig,
    *,
    session_id: str,
    idea_text: str,
    idea_brief: IdeaBrief,
    local_questions: list[ClarificationQuestion],
    local_answers: list[ClarificationAnswer],
    frontier_prior_questions: list[ClarificationQuestion],
    frontier_prior_answers: list[ClarificationAnswer],
    frontier_round: int,
    package_id: str | None = None,
) -> FrontierPlanningRequestPackage:
    """Deterministically build the package a frontier model needs to
    propose an ``ArchitecturePlan`` for an already-user-approved idea
    brief. Reuses ``ContextCompiler``/``GitRepository`` exactly like
    Architect Mode's own ``architect.package.build_planner_request_package``
    -- no parallel evidence format -- plus the fields that milestone's
    package intentionally omits: the approved brief and verbatim Q&A,
    active hard constraints (the brief's own ``key_constraints``, already
    verbatim-checked by ``local_model.parse_brief``), and the configured
    Architect Mode ceilings."""

    root_path = Path(root).resolve()
    repository = GitRepository(root_path)
    snapshot = repository.snapshot()
    resolved_id = package_id or f"FPKG-{uuid.uuid4().hex[:12].upper()}"

    ephemeral_specification = TaskSpecification(
        task_id=f"TASK-DISC-{resolved_id[len('FPKG-'):]}",
        objective=TraceableStatement(
            text=idea_brief.summary,
            source=SourceKind.USER,
            source_reference="discovery-idea-brief",
        ),
        hard_constraints=idea_brief.key_constraints,
    )
    compiler = ContextCompiler(config.context)
    context_package = compiler.compile(ephemeral_specification, root_path)

    base = FrontierPlanningRequestPackage(
        package_id=resolved_id,
        session_id=session_id,
        frontier_round=frontier_round,
        idea_text=idea_text,
        idea_brief=idea_brief,
        local_questions=local_questions,
        local_answers=local_answers,
        frontier_prior_questions=frontier_prior_questions,
        frontier_prior_answers=frontier_prior_answers,
        repository=snapshot,
        context=context_package,
        active_hard_constraints=idea_brief.key_constraints,
        verification_catalog=_verification_catalog(config),
        architect_ceilings=config.architect.ceilings.model_dump(mode="json"),
        plan_json_schema=ArchitecturePlan.model_json_schema(),
        response_json_schema=FrontierPlanningResponseEnvelope.model_json_schema(),
        max_clarification_rounds=config.discovery.max_frontier_clarification_rounds,
        max_clarification_questions=config.discovery.max_clarification_questions,
        package_sha256="0" * 64,
    )
    payload = base.model_dump(mode="json", exclude={"package_id", "generated_at", "package_sha256"})
    digest = _sha256_canonical(payload)
    return base.model_copy(update={"package_sha256": digest})


def verify_package_integrity(package: FrontierPlanningRequestPackage) -> bool:
    payload = package.model_dump(
        mode="json", exclude={"package_id", "generated_at", "package_sha256"}
    )
    return _sha256_canonical(payload) == package.package_sha256


def package_path(root: str | Path, package_id: str) -> Path:
    return (
        Path(root).resolve()
        / ".apoapsis"
        / "discovery-planning-packages"
        / package_id
        / "request-package.json"
    )


def load_package(root: str | Path, package_id: str) -> FrontierPlanningRequestPackage:
    path = package_path(root, package_id)
    if not path.is_file():
        raise PackageNotFoundError(f"frontier planning package not found: {package_id}")
    package = FrontierPlanningRequestPackage.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    if not verify_package_integrity(package):
        raise PackageIntegrityError(
            f"frontier planning package {package_id} failed its own integrity "
            "check -- the file on disk does not match its recorded package_sha256"
        )
    return package


__all__ = [
    "build_frontier_planning_request_package",
    "verify_package_integrity",
    "package_path",
    "load_package",
]
