from __future__ import annotations

import asyncio
from pathlib import Path

from apoapsis.discovery.errors import BriefNotApprovedError
from apoapsis.discovery.schema import DiscoverySessionRecord, DiscoveryStatus
from apoapsis.discovery.store import SQLiteDiscoveryStore
from apoapsis.research.engine import ResearchEngine
from apoapsis.research.factory import build_research_engine
from apoapsis.research.schemas import ResearchMode
from apoapsis.specification.schema import (
    SourceKind,
    TaskSpecification,
    TraceableStatement,
)


def discovery_research_specification(
    session: DiscoverySessionRecord,
) -> TaskSpecification:
    """Translate an approved discovery brief into the same typed task shape
    the quarantined Research Mode engine already consumes.

    This is advisory input only.  It creates no workflow task and grants no
    execution authority.
    """

    if not session.brief_approved or session.idea_brief is None:
        raise BriefNotApprovedError(
            f"session {session.session_id} has no approved idea brief yet"
        )
    return TaskSpecification(
        task_id=f"TASK-DISC-{session.session_id.removeprefix('DISC-')}",
        objective=TraceableStatement(
            text=session.idea_brief.summary,
            source=SourceKind.USER,
            source_reference="discovery-idea-brief",
        ),
        hard_constraints=session.idea_brief.key_constraints,
    )


def run_discovery_research_step(
    root: str | Path,
    discovery_store: SQLiteDiscoveryStore,
    config,
    session_id: str,
    *,
    expected_version: int,
    requested_mode: ResearchMode,
    research_engine: ResearchEngine | None = None,
) -> DiscoverySessionRecord:
    if requested_mode == ResearchMode.OFF:
        raise ValueError("research operations require a non-OFF research mode")
    session = discovery_store.get_session(session_id)
    if session.status != DiscoveryStatus.BRIEF_APPROVED:
        raise BriefNotApprovedError(
            f"research requires an approved idea brief, found {session.status.value}"
        )
    specification = discovery_research_specification(session)
    owned_fetch_process = None
    engine = research_engine
    if engine is None:
        engine, owned_fetch_process = build_research_engine(root, config)
    try:
        execution = asyncio.run(engine.execute(specification, requested_mode))
    finally:
        if owned_fetch_process is not None:
            owned_fetch_process.close()
    outcome = execution.outcome
    return discovery_store.record_research_result(
        session_id,
        requested_mode=requested_mode,
        triggered=outcome is not None,
        brief=outcome.brief if outcome is not None else None,
        evidence_ids=(
            [item.evidence_id for item in outcome.evidence]
            if outcome is not None
            else []
        ),
        audit_directory=execution.audit_directory,
        telemetry=outcome.telemetry if outcome is not None else None,
        expected_version=expected_version,
    )


__all__ = [
    "discovery_research_specification",
    "run_discovery_research_step",
]
