from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from apoapsis.specification.schema import StrictModel, utc_now
from apoapsis.workflow.states import WorkflowState


class WorkflowActor(StrEnum):
    SYSTEM = "system"
    USER = "user"
    OPERATOR = "operator"
    VERIFICATION_ENGINE = "verification_engine"


class WorkflowEvent(StrictModel):
    event_id: str = Field(pattern=r"^EVT-[A-Za-z0-9._-]+$")
    sequence: int | None = Field(default=None, ge=1)
    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    event_type: str = Field(min_length=1)
    from_state: WorkflowState | None
    to_state: WorkflowState
    actor: WorkflowActor
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

