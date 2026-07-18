from __future__ import annotations

import json
import time
from pathlib import Path

from apoapsis.config import ApoapsisConfig
from apoapsis.evaluation.lanes import apply_lane_overlay
from apoapsis.evaluation.schemas import EvalLane, EvalLaneResult
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.research.schemas import ResearchMode
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.vertical_slice import VerticalSliceRunner


def run_eval_lane(
    fixture_root: Path,
    lane: EvalLane,
    config: ApoapsisConfig,
    provider: InstrumentedModelProvider,
    *,
    local_coder_provider: InstrumentedModelProvider | None = None,
    frontier_coder_provider: InstrumentedModelProvider | None = None,
    task_text: str,
) -> EvalLaneResult:
    """Run one deterministic-overlay lane against an already-isolated fixture copy.

    Reuses `VerticalSliceRunner` unchanged: a lane is a configuration overlay,
    not a separate execution engine. Each lane opens its own fresh task store
    rooted at `fixture_root`, never the caller's own project database.
    """

    fixture_root = Path(fixture_root)
    lane_config = apply_lane_overlay(config, lane)
    metadata = fixture_root / ".apoapsis"
    metadata.mkdir(parents=True, exist_ok=True)
    (metadata / "effective-config.json").write_text(
        json.dumps(lane_config.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    store = SQLiteTaskStore(metadata / "apoapsis.db")
    started = time.monotonic()
    report = VerticalSliceRunner(
        fixture_root,
        store,
        provider,
        lane_config,
        local_coder_provider=local_coder_provider,
        frontier_coder_provider=frontier_coder_provider,
        research_mode=ResearchMode.OFF,
    ).run(task_text, approve=lambda specification: True)
    return EvalLaneResult(
        lane=lane,
        fixture_path=str(fixture_root),
        report=report,
        duration_seconds=time.monotonic() - started,
    )
