from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field

from apoapsis.models.base import ModelOperation, TokenUsage
from apoapsis.specification.schema import StrictModel, utc_now


class ProviderError(RuntimeError):
    """A provider request or response failed before a usable proposal existed."""


class ModelRole(StrEnum):
    FRONTIER_IMPLEMENTATION = "FRONTIER_IMPLEMENTATION"
    CODING_AGENT = "CODING_AGENT"
    LOCAL_CODING_AGENT = "LOCAL_CODING_AGENT"
    FRONTIER_CODING_AGENT = "FRONTIER_CODING_AGENT"
    LOCAL_RESEARCH_MODEL = "LOCAL_RESEARCH_MODEL"
    LOCAL_DISCOVERY_MODEL = "LOCAL_DISCOVERY_MODEL"
    FRONTIER_PLANNING_MODEL = "FRONTIER_PLANNING_MODEL"


class ProviderInvocation(StrictModel):
    request_id: str = Field(pattern=r"^MRQ-[A-Za-z0-9._-]+$")
    operation: ModelOperation
    prompt: str = Field(min_length=1)
    role: ModelRole = ModelRole.FRONTIER_IMPLEMENTATION
    response_schema: dict[str, Any] | None = None
    think: bool | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0, le=3600)


class ProviderOutput(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    response_id: str = Field(min_length=1)
    content: str
    model: str = Field(min_length=1)
    finish_reason: str = Field(min_length=1)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


@runtime_checkable
class ModelProvider(Protocol):
    """Narrow provider boundary: one prompt in, one untrusted proposal out."""

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def complete(self, invocation: ProviderInvocation) -> ProviderOutput: ...
