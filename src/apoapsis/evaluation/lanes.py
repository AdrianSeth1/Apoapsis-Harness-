from __future__ import annotations

from apoapsis.config import AgentRoute, ApoapsisConfig, ExecutionMode
from apoapsis.evaluation.schemas import EvalLane

_FORCED_ESCALATION_LOCAL_BUDGET = {
    "max_turns": 1,
    "max_patch_attempts": 1,
    "max_verification_runs": 1,
}


def requires_frontier_coder(lane: EvalLane) -> bool:
    return lane in (EvalLane.HYBRID, EvalLane.FORCED_ESCALATION, EvalLane.FRONTIER)


def apply_lane_overlay(config: ApoapsisConfig, lane: EvalLane) -> ApoapsisConfig:
    """Return a deterministic execution overlay for one evaluation lane.

    Only `execution` is ever overridden; `models` always comes from the
    caller's real project configuration so no lane can silently change which
    provider or credentials are used.
    """

    if lane is EvalLane.ONE_SHOT:
        execution = config.execution.model_copy(
            update={"mode": ExecutionMode.ONE_SHOT}
        )
        return config.model_copy(update={"execution": execution})

    if lane is EvalLane.LOCAL:
        execution = config.execution.model_copy(
            update={"mode": ExecutionMode.AGENT, "route": AgentRoute.LOCAL_ONLY}
        )
        return config.model_copy(update={"execution": execution})

    if lane is EvalLane.FRONTIER:
        execution = config.execution.model_copy(
            update={"mode": ExecutionMode.AGENT, "route": AgentRoute.FRONTIER_ONLY}
        )
        return config.model_copy(update={"execution": execution})

    if lane is EvalLane.HYBRID:
        execution = config.execution.model_copy(
            update={
                "mode": ExecutionMode.AGENT,
                "route": AgentRoute.LOCAL_THEN_FRONTIER,
            }
        )
        return config.model_copy(update={"execution": execution})

    if lane is EvalLane.FORCED_ESCALATION:
        constrained_agent = config.execution.agent.model_copy(
            update=_FORCED_ESCALATION_LOCAL_BUDGET
        )
        execution = config.execution.model_copy(
            update={
                "mode": ExecutionMode.AGENT,
                "route": AgentRoute.LOCAL_THEN_FRONTIER,
                "agent": constrained_agent,
            }
        )
        return config.model_copy(update={"execution": execution})

    raise ValueError(f"unsupported evaluation lane: {lane}")
