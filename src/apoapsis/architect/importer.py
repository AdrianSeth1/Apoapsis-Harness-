from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from apoapsis.architect.audit import PlanAuditStore
from apoapsis.architect.errors import PlanImportError
from apoapsis.architect.schema import (
    PlanRecord,
    PlannerRequestPackage,
    PlannerResponseEnvelope,
)
from apoapsis.architect.store import SQLitePlanStore


def import_planner_response(
    root: Path, plan_store: SQLitePlanStore, raw_payload: dict[str, Any]
) -> PlanRecord:
    """Import a manually-pasted planner response as a brand new plan.

    Rejects a response whose ``request_package_sha256`` does not match the
    stored request package's own hash exactly -- this is the sole
    integrity check tying an imported plan back to the exact package it
    was generated from, so a tampered or mismatched response can never be
    silently accepted.
    """

    try:
        envelope = PlannerResponseEnvelope.model_validate(raw_payload)
    except ValidationError as exc:
        raise PlanImportError(f"planner response is invalid: {exc}") from exc

    package_path = (
        root / ".apoapsis" / "plan-packages" / envelope.package_id / "request-package.json"
    )
    if not package_path.is_file():
        raise PlanImportError(
            f"no exported request package found for {envelope.package_id}; "
            "run 'apoapsis plan export' first"
        )
    package = PlannerRequestPackage.model_validate_json(
        package_path.read_text(encoding="utf-8")
    )
    if package.package_sha256 != envelope.request_package_sha256:
        raise PlanImportError(
            "response request_package_sha256 does not match the stored "
            "request package; refusing to import"
        )

    plan_id = f"PLAN-{uuid.uuid4().hex[:12].upper()}"
    audit = PlanAuditStore(root, plan_id)
    audit.write_json("response.json", raw_payload, kind="planner_response")
    audit.write_json("plan-v1.json", envelope.plan, kind="architecture_plan")
    return plan_store.create_plan(
        plan_id, envelope.package_id, package.idea_text, envelope.plan
    )


__all__ = ["import_planner_response"]
