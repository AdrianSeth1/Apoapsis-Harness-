from __future__ import annotations

from apoapsis.config import AgentRoute, ApoapsisConfig, CompletionPolicy, ExecutionMode
from apoapsis.evaluation.schemas import EvalLane

_FORCED_ESCALATION_LOCAL_BUDGET = {
    "max_turns": 1,
    "max_patch_attempts": 1,
    "max_verification_runs": 1,
}

# Every lane explicitly selects BASELINE, regardless of what the caller's
# real project configuration selects. `apoapsis init` now defaults ordinary
# product runs to STRICT (ADR 0016), but the evaluation harness measures
# false-success rate against the held-out oracle -- a measurement that stays
# comparable across runs and models only if completion here means exactly
# "configured verification passed," never "and every acceptance criterion
# happened to be mapped and proven too." This is a deliberate, audited
# override recorded on every persisted `FinalTaskReport.completion_policy`
# and in the comparison Markdown, not silent inheritance.
_EVALUATION_COMPLETION_POLICY = CompletionPolicy.BASELINE


def requires_frontier_coder(lane: EvalLane) -> bool:
    return lane in (EvalLane.HYBRID, EvalLane.FORCED_ESCALATION, EvalLane.FRONTIER)


def apply_lane_overlay(config: ApoapsisConfig, lane: EvalLane) -> ApoapsisConfig:
    """Return a deterministic execution overlay for one evaluation lane.

    Only `execution` is ever overridden; `models` always comes from the
    caller's real project configuration so no lane can silently change which
    provider or credentials are used. `completion_policy` is always forced
    to the explicit evaluation-baseline selection above.
    """

    if lane is EvalLane.ONE_SHOT:
        execution = config.execution.model_copy(
            update={
                "mode": ExecutionMode.ONE_SHOT,
                "completion_policy": _EVALUATION_COMPLETION_POLICY,
            }
        )
        return config.model_copy(update={"execution": execution})

    if lane is EvalLane.LOCAL:
        execution = config.execution.model_copy(
            update={
                "mode": ExecutionMode.AGENT,
                "route": AgentRoute.LOCAL_ONLY,
                "completion_policy": _EVALUATION_COMPLETION_POLICY,
            }
        )
        return config.model_copy(update={"execution": execution})

    if lane is EvalLane.FRONTIER:
        execution = config.execution.model_copy(
            update={
                "mode": ExecutionMode.AGENT,
                "route": AgentRoute.FRONTIER_ONLY,
                "completion_policy": _EVALUATION_COMPLETION_POLICY,
            }
        )
        return config.model_copy(update={"execution": execution})

    if lane is EvalLane.HYBRID:
        execution = config.execution.model_copy(
            update={
                "mode": ExecutionMode.AGENT,
                "route": AgentRoute.LOCAL_THEN_FRONTIER,
                "completion_policy": _EVALUATION_COMPLETION_POLICY,
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
                "completion_policy": _EVALUATION_COMPLETION_POLICY,
            }
        )
        return config.model_copy(update={"execution": execution})

    raise ValueError(f"unsupported evaluation lane: {lane}")
