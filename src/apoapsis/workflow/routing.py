from __future__ import annotations

from apoapsis.config import AgentRoute, ExecutionConfig
from apoapsis.specification.schema import RiskLevel, StrictModel, TaskSpecification


class RoutingDecision(StrictModel):
    route: AgentRoute
    reason: str
    requested_route: AgentRoute
    risk_level: RiskLevel
    frontier_available: bool


def select_agent_route(
    specification: TaskSpecification,
    execution: ExecutionConfig,
    *,
    frontier_available: bool,
) -> RoutingDecision:
    requested = execution.route
    if requested != AgentRoute.AUTO:
        route = requested
        reason = "explicit configured agent route"
        if route in {AgentRoute.FRONTIER_ONLY, AgentRoute.LOCAL_THEN_FRONTIER}:
            if not frontier_available:
                route = AgentRoute.HUMAN_REVIEW_REQUIRED
                reason = "configured frontier route has no available frontier provider"
        return RoutingDecision(
            route=route,
            reason=reason,
            requested_route=requested,
            risk_level=specification.risk_level,
            frontier_available=frontier_available,
        )

    if specification.risk_level == RiskLevel.CRITICAL:
        route = AgentRoute.HUMAN_REVIEW_REQUIRED
        reason = "critical-risk tasks require an explicit human routing decision"
    elif specification.risk_level == RiskLevel.HIGH:
        route = (
            AgentRoute.LOCAL_THEN_FRONTIER
            if frontier_available
            else AgentRoute.LOCAL_ONLY
        )
        reason = (
            "high-risk task uses the maximum bounded local profile before frontier review"
            if frontier_available
            else "high-risk task uses the maximum bounded local profile"
        )
    elif frontier_available:
        route = AgentRoute.LOCAL_THEN_FRONTIER
        reason = "eligible task uses local-first execution with bounded escalation"
    else:
        route = AgentRoute.LOCAL_ONLY
        reason = "no frontier escalation provider is configured"
    return RoutingDecision(
        route=route,
        reason=reason,
        requested_route=requested,
        risk_level=specification.risk_level,
        frontier_available=frontier_available,
    )
