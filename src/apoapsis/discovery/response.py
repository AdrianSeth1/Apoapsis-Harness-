from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from apoapsis.architect.audit import PlanAuditStore
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.config import ApoapsisConfig
from apoapsis.discovery.errors import (
    ClarificationRoundCeilingExceededError,
    StaleSessionError,
)
from apoapsis.discovery.schema import (
    DiscoverySessionRecord,
    FrontierPlanningRequestPackage,
    FrontierPlanningResponseEnvelope,
    FrontierPlanningResponseKind,
)
from apoapsis.discovery.store import SQLiteDiscoveryStore


def apply_frontier_planning_response(
    root: str | Path,
    discovery_store: SQLiteDiscoveryStore,
    plan_store: SQLitePlanStore,
    config: ApoapsisConfig,
    session: DiscoverySessionRecord,
    package: FrontierPlanningRequestPackage,
    envelope: FrontierPlanningResponseEnvelope,
    raw_payload: dict[str, Any],
) -> DiscoverySessionRecord:
    """The one place either transport (manual or API) hands a validated
    ``FrontierPlanningResponseEnvelope`` to the harness. Requires the
    session to still be exactly ``FRONTIER_PACKAGE_EXPORTED`` at the
    package this envelope answers -- a response to a stale or superseded
    package/session is rejected, never silently applied.

    A ``clarification_questions`` response is rejected outright once the
    configured round ceiling is already reached at this package's round --
    this is a bounded planning workflow, never an unbounded conversation;
    the frontier model must return a complete plan instead. A ``plan``
    response continues through the existing, completely unmodified
    Architect Mode import machinery (``SQLitePlanStore.create_plan``, the
    same function ``architect.importer.import_planner_response`` calls) --
    the same ``apoapsis plan validate``/``apoapsis plan approve`` commands
    then work on the resulting plan exactly as they always have.
    """

    root_path = Path(root).resolve()
    if session.status.value != "frontier_package_exported":
        raise StaleSessionError(
            f"session {session.session_id} is not currently awaiting a "
            f"frontier planning response (status={session.status.value})"
        )
    if session.frontier_package_id != package.package_id:
        raise StaleSessionError(
            f"package {package.package_id} is not the session's current "
            f"outstanding package ({session.frontier_package_id!r})"
        )
    if envelope.session_id != session.session_id:
        raise StaleSessionError(
            f"response session_id {envelope.session_id!r} does not match "
            f"{session.session_id!r}"
        )

    if envelope.kind == FrontierPlanningResponseKind.CLARIFICATION_QUESTIONS:
        if session.frontier_round > config.discovery.max_frontier_clarification_rounds:
            raise ClarificationRoundCeilingExceededError(
                f"session {session.session_id} has reached round "
                f"{session.frontier_round}, exceeding the "
                f"{config.discovery.max_frontier_clarification_rounds} "
                "permitted frontier clarification rounds; the frontier "
                "model must return a complete plan instead"
            )
        assert envelope.clarification_questions is not None
        capped = envelope.clarification_questions[
            : config.discovery.max_clarification_questions
        ]
        return discovery_store.record_frontier_clarification(
            session.session_id, capped, expected_version=session.version
        )

    assert envelope.plan is not None
    plan_id = f"PLAN-{uuid.uuid4().hex[:12].upper()}"
    audit = PlanAuditStore(root_path, plan_id)
    audit.write_json("response.json", raw_payload, kind="planner_response")
    audit.write_json("plan-v1.json", envelope.plan, kind="architecture_plan")
    plan_store.create_plan(plan_id, package.package_id, package.idea_text, envelope.plan)
    return discovery_store.record_frontier_plan(
        session.session_id, plan_id, expected_version=session.version
    )


__all__ = ["apply_frontier_planning_response"]
