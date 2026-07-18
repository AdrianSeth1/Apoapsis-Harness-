from __future__ import annotations

import json
import time
from pathlib import Path

from apoapsis.config import ApoapsisConfig
from apoapsis.audit.store import TaskAuditStore
from apoapsis.evaluation.lanes import apply_lane_overlay
from apoapsis.evaluation.oracle import (
    HeldOutOracleDefinition,
    assert_oracle_withheld,
    run_held_out_oracle,
)
from apoapsis.evaluation.schemas import (
    EvalEvidenceKind,
    EvalLane,
    EvalLaneResult,
)
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
    evidence_kind: EvalEvidenceKind = EvalEvidenceKind.DETERMINISTIC_FAKE,
    held_out_oracle: HeldOutOracleDefinition | None = None,
) -> EvalLaneResult:
    """Run one deterministic-overlay lane against an already-isolated fixture copy.

    Reuses `VerticalSliceRunner` unchanged: a lane is a configuration overlay,
    not a separate execution engine. Each lane opens its own fresh task store
    rooted at `fixture_root`, never the caller's own project database.
    """

    fixture_root = Path(fixture_root)
    if held_out_oracle is not None:
        assert_oracle_withheld(fixture_root, held_out_oracle)
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
    audit_root = fixture_root / ".apoapsis" / "tasks" / report.task_id
    patch_files = list(audit_root.glob("patch-[0-9][0-9][0-9].diff"))
    unsafe_rejections = 0
    for policy_path in audit_root.glob("patch-[0-9][0-9][0-9]-policy.json"):
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if policy.get("accepted") is False:
            unsafe_rejections += 1
    oracle_result = None
    if held_out_oracle is not None:
        oracle_result = run_held_out_oracle(report, lane_config, held_out_oracle)
        artifact_path = (
            f".apoapsis/tasks/{report.task_id}/held-out-oracle.json"
        )
        oracle_result = oracle_result.model_copy(
            update={"audit_artifact": artifact_path}
        )
        TaskAuditStore(fixture_root, report.task_id).write_json(
            "held-out-oracle.json",
            oracle_result,
            kind="held_out_oracle_result",
        )
    return EvalLaneResult(
        lane=lane,
        fixture_path=str(fixture_root),
        report=report,
        duration_seconds=time.monotonic() - started,
        evidence_kind=evidence_kind,
        patch_attempts=len(patch_files),
        unsafe_patch_rejections=unsafe_rejections,
        held_out_oracle=oracle_result,
    )
