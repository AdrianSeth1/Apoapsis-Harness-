from __future__ import annotations

import uuid
from pathlib import Path

from apoapsis.architect.store import SQLitePlanStore
from apoapsis.config import ApoapsisConfig
from apoapsis.discovery.audit import DiscoveryAuditStore
from apoapsis.discovery.errors import (
    AnswerMismatchError,
    BriefNotApprovedError,
)
from apoapsis.discovery.frontier_package import (
    build_frontier_planning_request_package,
)
from apoapsis.discovery.local_model import (
    build_local_provider,
    propose_clarification_questions,
    propose_idea_brief,
)
from apoapsis.discovery.manual import write_frontier_planning_artifacts
from apoapsis.discovery.schema import (
    ClarificationAnswer,
    DiscoverySessionRecord,
    FrontierPlanningRequestPackage,
)
from apoapsis.discovery.store import SQLiteDiscoveryStore


def start_session(discovery_store: SQLiteDiscoveryStore, idea_text: str) -> DiscoverySessionRecord:
    session_id = f"DISC-{uuid.uuid4().hex[:12].upper()}"
    return discovery_store.create_session(session_id, idea_text)


def _validate_answers(
    answers: list[ClarificationAnswer], questions: list, *, label: str
) -> None:
    question_ids = {item.question_id for item in questions}
    answer_ids = {item.question_id for item in answers}
    if answer_ids - question_ids:
        raise AnswerMismatchError(
            f"{label} answers reference unknown question ids: "
            f"{sorted(answer_ids - question_ids)}"
        )


def propose_local_clarification_questions(
    root: str | Path,
    discovery_store: SQLiteDiscoveryStore,
    config: ApoapsisConfig,
    session_id: str,
    *,
    expected_version: int,
) -> DiscoverySessionRecord:
    session = discovery_store.get_session(session_id)
    provider = build_local_provider(config.models.frontier)
    audit = DiscoveryAuditStore(root, session_id)
    questions = propose_clarification_questions(
        provider,
        config.models.frontier,
        audit,
        session.idea_text,
        max_questions=config.discovery.max_clarification_questions,
    )
    return discovery_store.record_local_questions(
        session_id, questions, expected_version=expected_version
    )


def record_local_answers(
    discovery_store: SQLiteDiscoveryStore,
    session_id: str,
    answers: list[ClarificationAnswer],
    *,
    expected_version: int,
) -> DiscoverySessionRecord:
    session = discovery_store.get_session(session_id)
    _validate_answers(answers, session.local_questions, label="local")
    return discovery_store.record_local_answers(
        session_id, answers, expected_version=expected_version
    )


def propose_idea_brief_step(
    root: str | Path,
    discovery_store: SQLiteDiscoveryStore,
    config: ApoapsisConfig,
    session_id: str,
    *,
    expected_version: int,
) -> DiscoverySessionRecord:
    session = discovery_store.get_session(session_id)
    provider = build_local_provider(config.models.frontier)
    audit = DiscoveryAuditStore(root, session_id)
    brief = propose_idea_brief(
        provider, config.models.frontier, audit, session.idea_text, session.local_answers
    )
    return discovery_store.record_idea_brief(
        session_id, brief, expected_version=expected_version
    )


def approve_idea_brief_step(
    discovery_store: SQLiteDiscoveryStore, session_id: str, *, expected_version: int
) -> DiscoverySessionRecord:
    return discovery_store.approve_idea_brief(session_id, expected_version=expected_version)


def export_frontier_planning_package(
    root: str | Path,
    discovery_store: SQLiteDiscoveryStore,
    config: ApoapsisConfig,
    session_id: str,
    *,
    transport: str,
    expected_version: int,
) -> tuple[DiscoverySessionRecord, FrontierPlanningRequestPackage, str, str]:
    """Builds and writes an immutable ``FrontierPlanningRequestPackage``
    (JSON + self-contained Markdown) and records it as the session's
    current outstanding package. Requires an explicitly approved idea
    brief -- never exports before that approval."""

    session = discovery_store.get_session(session_id)
    if not session.brief_approved or session.idea_brief is None:
        raise BriefNotApprovedError(
            f"session {session_id} has no approved idea brief yet"
        )
    package = build_frontier_planning_request_package(
        root,
        config,
        session_id=session_id,
        idea_text=session.idea_text,
        idea_brief=session.idea_brief,
        local_questions=session.local_questions,
        local_answers=session.local_answers,
        frontier_prior_questions=session.frontier_questions,
        frontier_prior_answers=session.frontier_answers,
        frontier_round=session.frontier_round + 1,
    )
    json_path, markdown_path = write_frontier_planning_artifacts(root, package)
    updated = discovery_store.record_frontier_package(
        session_id,
        package.package_id,
        transport,
        expected_version=expected_version,
        next_round=package.frontier_round,
    )
    return updated, package, json_path, markdown_path


def record_frontier_answers(
    discovery_store: SQLiteDiscoveryStore,
    session_id: str,
    answers: list[ClarificationAnswer],
    *,
    expected_version: int,
) -> DiscoverySessionRecord:
    session = discovery_store.get_session(session_id)
    _validate_answers(answers, session.frontier_questions, label="frontier")
    return discovery_store.record_frontier_answers(
        session_id, answers, expected_version=expected_version
    )


__all__ = [
    "start_session",
    "propose_local_clarification_questions",
    "record_local_answers",
    "propose_idea_brief_step",
    "approve_idea_brief_step",
    "export_frontier_planning_package",
    "record_frontier_answers",
]
