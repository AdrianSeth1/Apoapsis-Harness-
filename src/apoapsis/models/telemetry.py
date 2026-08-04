from __future__ import annotations

import time
import hashlib
from datetime import datetime, timezone

from pydantic import Field

from apoapsis.config import ProviderPricing
from apoapsis.models.base import ModelOperation, TokenUsage
from apoapsis.models.provider import (
    ModelProvider,
    ModelRole,
    ProviderInvocation,
    ProviderOutput,
)
from apoapsis.specification.schema import StrictModel


class RelayObservedModelUsage(StrictModel):
    """What a contained agent's own model traffic cost, read off the relay.

    The Capability Sandbox does not call a provider through the harness: a
    native CLI inside the workcell talks to the model server, and the only
    component that sees those exchanges is the controller-owned relay. So this
    is deliberately *not* a `ProviderCallTelemetry` -- there is no prompt hash,
    no per-call request id, and no harness-side latency to report, and
    manufacturing one call record per exchange would put invented fields in the
    audit surface. It is a separate, honestly-shaped summary of observed usage.

    `calls` is the number of exchanges that *reported* usage, which is the
    denominator every total here needs: "1.9M input tokens over 46 of 46
    exchanges" and "over 2 of 46" are different evidence, and only the second
    is a telemetry gap.
    """

    #: Where the numbers came from, so a reader never has to guess whether a
    #: total was measured or estimated.
    source: str = Field(default="capability_sandbox_relay", min_length=1)
    calls: int = Field(default=0, ge=0)
    #: Exchanges the relay forwarded, whether or not they reported usage. When
    #: this exceeds `calls`, some traffic went unmeasured and the totals below
    #: are a floor rather than a sum.
    exchanges_observed: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    #: The largest single prompt observed. The number that answers "did context
    #: approach the window?", which no sum can answer.
    peak_input_tokens: int = Field(default=0, ge=0)
    #: Project-relative path of the per-call series written by the sandbox, for
    #: readers who need growth across the slice rather than its total.
    series_artifact: str | None = None

    @property
    def fully_measured(self) -> bool:
        return self.exchanges_observed == self.calls


class ProviderCallTelemetry(StrictModel):
    request_id: str
    response_id: str | None = None
    operation: ModelOperation
    role: ModelRole | None = None
    provider: str
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    cache_hit: bool
    estimated_cost_usd: float = Field(ge=0)
    prompt_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    model_digest: str | None = None
    thinking_tokens: int | None = Field(default=None, ge=0)
    prompt_evaluation_seconds: float | None = Field(default=None, ge=0)
    generation_seconds: float | None = Field(default=None, ge=0)
    model_load_seconds: float | None = Field(default=None, ge=0)
    structured_output_valid: bool | None = None
    retry_count: int = Field(default=0, ge=0)
    succeeded: bool = True
    error: str | None = None
    started_at: datetime
    finished_at: datetime
    latency_seconds: float = Field(ge=0)


class InstrumentedCall(StrictModel):
    output: ProviderOutput
    telemetry: ProviderCallTelemetry


class InstrumentedProviderError(RuntimeError):
    def __init__(
        self, message: str, telemetry: ProviderCallTelemetry
    ) -> None:
        self.telemetry = telemetry
        super().__init__(message)


class InstrumentedModelProvider:
    def __init__(
        self, provider: ModelProvider, pricing: ProviderPricing | None = None
    ) -> None:
        self.provider = provider
        self.pricing = pricing or ProviderPricing()

    @property
    def provider_name(self) -> str:
        return self.provider.provider_name

    @property
    def model_name(self) -> str:
        return self.provider.model_name

    def complete(self, invocation: ProviderInvocation) -> InstrumentedCall:
        started_at = datetime.now(timezone.utc)
        started_clock = time.monotonic()
        prompt_sha256 = hashlib.sha256(
            invocation.prompt.encode("utf-8")
        ).hexdigest()
        try:
            output = self.provider.complete(invocation)
        except Exception as exc:
            latency = time.monotonic() - started_clock
            telemetry = ProviderCallTelemetry(
                request_id=invocation.request_id,
                operation=invocation.operation,
                role=invocation.role,
                provider=self.provider_name,
                model=self.model_name,
                input_tokens=0,
                output_tokens=0,
                cached_input_tokens=0,
                cache_hit=False,
                estimated_cost_usd=0,
                prompt_sha256=prompt_sha256,
                succeeded=False,
                error=f"{type(exc).__name__}: {exc}",
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                latency_seconds=latency,
            )
            raise InstrumentedProviderError(str(exc), telemetry) from exc
        latency = time.monotonic() - started_clock
        finished_at = datetime.now(timezone.utc)
        usage = output.usage
        metadata = output.provider_metadata
        cached = min(usage.cached_input_tokens, usage.input_tokens)
        uncached = usage.input_tokens - cached
        cost = (
            uncached * self.pricing.input_per_million_usd
            + cached * self.pricing.cached_input_per_million_usd
            + usage.output_tokens * self.pricing.output_per_million_usd
        ) / 1_000_000
        priced_usage = TokenUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=cached,
            estimated_cost_usd=cost,
        )
        output = output.model_copy(update={"usage": priced_usage})
        return InstrumentedCall(
            output=output,
            telemetry=ProviderCallTelemetry(
                request_id=invocation.request_id,
                response_id=output.response_id,
                operation=invocation.operation,
                role=invocation.role,
                provider=self.provider_name,
                model=output.model,
                input_tokens=priced_usage.input_tokens,
                output_tokens=priced_usage.output_tokens,
                cached_input_tokens=priced_usage.cached_input_tokens,
                cache_hit=priced_usage.cached_input_tokens > 0,
                estimated_cost_usd=cost,
                prompt_sha256=prompt_sha256,
                model_digest=metadata.get("model_digest"),
                thinking_tokens=metadata.get("thinking_tokens"),
                prompt_evaluation_seconds=metadata.get(
                    "prompt_evaluation_seconds"
                ),
                generation_seconds=metadata.get("generation_seconds"),
                model_load_seconds=metadata.get("model_load_seconds"),
                structured_output_valid=metadata.get("structured_output_valid"),
                retry_count=int(metadata.get("retry_count") or 0),
                succeeded=True,
                started_at=started_at,
                finished_at=finished_at,
                latency_seconds=latency,
            ),
        )
