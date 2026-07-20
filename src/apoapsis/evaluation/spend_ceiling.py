from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from apoapsis.config import ApoapsisConfig, ProviderPricing
from apoapsis.evaluation.lanes import requires_frontier_coder
from apoapsis.evaluation.schemas import EvalLane
from apoapsis.models.provider import ProviderInvocation
from apoapsis.models.telemetry import InstrumentedCall, InstrumentedModelProvider

# Matches `context/measurement.py`'s and `doctor.py`'s own rough
# chars-per-token estimate, reused here so every part of Apoapsis that has
# to guess a token count without a real tokenizer guesses the same way.
_CHARS_PER_TOKEN_ESTIMATE = 4


class HostedSpendCeilingExceededError(RuntimeError):
    """A hosted call was refused, or a completed hosted call pushed
    cumulative spend past an explicitly configured ceiling.

    Never caught to silently continue, retry with a cheaper request, or
    fall back to a different model -- the caller must explicitly raise the
    ceiling (or stop requesting hosted lanes) to proceed.
    """


def estimate_worst_case_call_cost_usd(
    *, prompt_chars: int, max_output_tokens: int, pricing: ProviderPricing
) -> float:
    """A deliberately pessimistic upper bound on one call's cost: the full
    input estimate at the uncached input price (never the cheaper cached
    price) plus the full configured `max_output_tokens` ceiling, even
    though a real response is almost always shorter. Never an average or a
    prediction -- a ceiling this estimate clears is guaranteed not to be
    exceeded by a real call with the same prompt and token ceiling, for any
    output the model actually returns."""

    input_tokens_estimate = -(-prompt_chars // _CHARS_PER_TOKEN_ESTIMATE)
    return (
        input_tokens_estimate * pricing.input_per_million_usd
        + max_output_tokens * pricing.output_per_million_usd
    ) / 1_000_000


def estimate_worst_case_run_cost_usd(
    config: ApoapsisConfig, lanes: Iterable[EvalLane]
) -> float:
    """A deliberately pessimistic upper bound on an entire evaluation run's
    hosted spend, computed only from configuration -- no call is made and
    no prompt has to exist yet. Assumes every configured
    `frontier_agent.max_turns` in every hosted-requiring lane is spent on
    a full-price call at the configured context budget and
    `frontier_coder.max_output_tokens` ceiling. Returns 0.0 if no
    requested lane requires a hosted frontier coder, or none is
    configured."""

    frontier_coder = config.models.frontier_coder
    if frontier_coder is None:
        return 0.0
    hosted_lane_count = sum(1 for lane in lanes if requires_frontier_coder(lane))
    if hosted_lane_count == 0:
        return 0.0
    per_call = estimate_worst_case_call_cost_usd(
        prompt_chars=config.context.max_total_chars,
        max_output_tokens=frontier_coder.max_output_tokens,
        pricing=frontier_coder.pricing,
    )
    max_calls_per_lane = config.execution.frontier_agent.max_turns
    return per_call * max_calls_per_lane * hosted_lane_count


@dataclass
class SpendLedger:
    """A shared, mutable running total for one evaluation invocation.

    Two independent checks, not one: `refuse_if_worst_case_exceeds` runs
    strictly *before* a call is attempted (nothing spent yet, spend is
    never mutated by a refusal), and `record_actual` runs strictly *after*
    a call completes, using the real recorded cost -- a hard backstop in
    case a worst-case pre-call estimate ever turns out to have been wrong.
    Either check failing raises `HostedSpendCeilingExceededError` and
    leaves `spent_usd` exactly where it was (a refused call is never
    partially recorded).
    """

    ceiling_usd: float
    spent_usd: float = 0.0
    calls_recorded: int = 0
    calls_refused: int = 0
    # Sticky once True: sourced by the caller (e.g. an `apoapsis eval`
    # lane loop) to stop starting further lanes even if the exception
    # raised for the call that tripped this was caught and converted into
    # an ordinary per-task failure report somewhere inside the run --
    # exceeding the ceiling must always halt the whole invocation, not
    # just fail whichever single call happened to trip it.
    exceeded: bool = False

    def __post_init__(self) -> None:
        if self.ceiling_usd < 0:
            raise ValueError("ceiling_usd must not be negative")

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.ceiling_usd - self.spent_usd)

    def refuse_if_worst_case_exceeds(self, worst_case_cost_usd: float) -> None:
        prospective = self.spent_usd + worst_case_cost_usd
        if prospective > self.ceiling_usd:
            self.calls_refused += 1
            self.exceeded = True
            raise HostedSpendCeilingExceededError(
                f"refusing hosted call: its worst-case cost estimate "
                f"(${worst_case_cost_usd:.4f}) would bring cumulative spend to "
                f"${prospective:.4f}, exceeding the configured "
                f"${self.ceiling_usd:.4f} ceiling (${self.spent_usd:.4f} already "
                f"spent across {self.calls_recorded} call(s))"
            )

    def record_actual(self, actual_cost_usd: float) -> None:
        self.spent_usd += actual_cost_usd
        self.calls_recorded += 1
        if self.spent_usd > self.ceiling_usd:
            self.exceeded = True
            raise HostedSpendCeilingExceededError(
                f"hosted spend ceiling exceeded after a completed call: "
                f"cumulative spend is now ${self.spent_usd:.4f}, exceeding the "
                f"configured ${self.ceiling_usd:.4f} ceiling"
            )


class SpendCeilingModelProvider:
    """Wraps an `InstrumentedModelProvider`, enforcing a shared
    `SpendLedger` on every call: refuses before a call whose worst-case
    cost would already exceed the remaining ceiling, and re-checks the
    real recorded cost after every completed call as a hard backstop.
    Never retries with a cheaper request, truncates output, or silently
    substitutes a different model to stay under budget -- refusal is the
    only response to an exceeded ceiling.
    """

    def __init__(
        self,
        inner: InstrumentedModelProvider,
        ledger: SpendLedger,
        *,
        default_max_output_tokens: int,
    ) -> None:
        self.inner = inner
        self.ledger = ledger
        self.default_max_output_tokens = default_max_output_tokens

    @property
    def provider_name(self) -> str:
        return self.inner.provider_name

    @property
    def model_name(self) -> str:
        return self.inner.model_name

    @property
    def pricing(self) -> ProviderPricing:
        return self.inner.pricing

    def complete(self, invocation: ProviderInvocation) -> InstrumentedCall:
        worst_case = estimate_worst_case_call_cost_usd(
            prompt_chars=len(invocation.prompt),
            max_output_tokens=(
                invocation.max_output_tokens or self.default_max_output_tokens
            ),
            pricing=self.inner.pricing,
        )
        self.ledger.refuse_if_worst_case_exceeds(worst_case)
        call = self.inner.complete(invocation)
        self.ledger.record_actual(call.telemetry.estimated_cost_usd)
        return call


__all__ = [
    "HostedSpendCeilingExceededError",
    "SpendCeilingModelProvider",
    "SpendLedger",
    "estimate_worst_case_call_cost_usd",
    "estimate_worst_case_run_cost_usd",
]
