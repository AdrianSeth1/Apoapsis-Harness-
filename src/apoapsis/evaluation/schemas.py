from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from apoapsis.reporting.report import FinalTaskReport
from apoapsis.specification.schema import StrictModel, utc_now


class EvalLane(StrEnum):
    LOCAL = "local"
    HYBRID = "hybrid"
    FORCED_ESCALATION = "forced-escalation"
    FRONTIER = "frontier"
    ONE_SHOT = "one-shot"


DEFAULT_LANE_ORDER: tuple[EvalLane, ...] = (
    EvalLane.LOCAL,
    EvalLane.HYBRID,
    EvalLane.FORCED_ESCALATION,
    EvalLane.FRONTIER,
    EvalLane.ONE_SHOT,
)


class EvalLaneResult(StrictModel):
    lane: EvalLane
    fixture_path: str | None = None
    report: FinalTaskReport | None = None
    duration_seconds: float = Field(default=0.0, ge=0)
    skipped: bool = False
    skip_reason: str | None = None


class EvalComparisonReport(StrictModel):
    schema_version: str = "1.0"
    run_id: str = Field(min_length=1)
    generated_at: datetime = Field(default_factory=utc_now)
    fixture_source: str
    task_text: str
    lanes: list[EvalLaneResult] = Field(default_factory=list)
