"""First-class classification of context and output ceiling conditions.

A response that ends because the conversation no longer fits the server window,
or because it hit the declared output cap, is *not* a model reasoning failure
and is *not* a valid structured proposal. Before this module the two were
indistinguishable in telemetry: the Crisis Atlas unrestricted control's rollover
(prompt 64,409 + completion 1,127 = exactly the 65,536 server context) and the
sliced arm's two 8,192-token output-cap truncations were both recorded as
ordinary invalid model artifacts.

`docs/handoff-2026-07-30-qwen-baseline-preserving-superiority.md` requires these
to be separated so an efficiency or quality comparison cannot silently charge a
harness ceiling to the model's reasoning. Nothing here decides task state; it
only labels an observation.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel


class CeilingStopReason(StrEnum):
    """Why a call ended at an infrastructure limit rather than at a decision."""

    #: The prompt is approaching but has not yet crossed the server window.
    #: Advisory: compaction should start here, before the hard ceiling.
    INPUT_CONTEXT_PRESSURE = "INPUT_CONTEXT_PRESSURE"
    #: Prompt plus completion consumed the whole server context window.
    INPUT_CONTEXT_EXHAUSTED = "INPUT_CONTEXT_EXHAUSTED"
    #: The completion stopped at the declared per-response output cap.
    OUTPUT_CEILING_TRUNCATION = "OUTPUT_CEILING_TRUNCATION"
    #: A tool observation was clipped before it reached the model.
    TOOL_OUTPUT_TRUNCATION = "TOOL_OUTPUT_TRUNCATION"
    #: The provider rejected the request after a context rollover, so the
    #: failure is attributable to the window, not to the provider's health.
    PROVIDER_ERROR_AFTER_ROLLOVER = "PROVIDER_ERROR_AFTER_ROLLOVER"


#: Provider finish reasons that mean "generation was cut off at a limit".
#: OpenAI-compatible servers report `length`; some report `max_tokens` or the
#: Gemini-style `MAX_TOKENS`.
TRUNCATING_FINISH_REASONS: frozenset[str] = frozenset(
    {"length", "max_tokens", "MAX_TOKENS"}
)

#: Fraction of the server context window at which compaction should begin.
#: Qwen Code's own default auto-compaction threshold is 0.70; the handoff is
#: explicit that this is a first experiment point, not an Apoapsis constant.
DEFAULT_CONTEXT_PRESSURE_THRESHOLD = 0.70


class CeilingEvent(StrictModel):
    """One observed ceiling condition, bound to the call that produced it."""

    reason: CeilingStopReason
    request_id: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    context_limit: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    finish_reason: str | None = None
    detail: str = ""


def classify_ceiling_stop_reason(
    *,
    finish_reason: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    context_limit: int | None = None,
    max_output_tokens: int | None = None,
    provider_error: bool = False,
    context_rolled_over: bool = False,
    tool_output_truncated: bool = False,
    pressure_threshold: float = DEFAULT_CONTEXT_PRESSURE_THRESHOLD,
) -> CeilingStopReason | None:
    """Label a call's ceiling condition, or return `None` if there is none.

    Precedence runs from the most decisive signal to the weakest, because a
    single call can satisfy several conditions at once. A request that fails
    immediately after a rollover is a rollover failure even though the previous
    call also reported `length`; a completion that filled the whole window is
    context exhaustion even though the provider still says `length`.

    Returning `None` means the call ended for some other reason. It does *not*
    mean the call succeeded.
    """

    if provider_error:
        return (
            CeilingStopReason.PROVIDER_ERROR_AFTER_ROLLOVER
            if context_rolled_over
            else None
        )
    if tool_output_truncated:
        return CeilingStopReason.TOOL_OUTPUT_TRUNCATION

    truncated = finish_reason in TRUNCATING_FINISH_REASONS
    if truncated:
        consumed = (input_tokens or 0) + (output_tokens or 0)
        if context_limit is not None and consumed >= context_limit:
            # The window, not the per-response cap, is what stopped this call.
            # Charging it to the output cap would justify the wrong fix.
            return CeilingStopReason.INPUT_CONTEXT_EXHAUSTED
        return CeilingStopReason.OUTPUT_CEILING_TRUNCATION

    if (
        context_limit is not None
        and input_tokens is not None
        and context_limit > 0
        and input_tokens / context_limit >= pressure_threshold
    ):
        return CeilingStopReason.INPUT_CONTEXT_PRESSURE
    return None


def is_ceiling_failure(reason: CeilingStopReason | None) -> bool:
    """True when the condition ended useful work rather than merely warning.

    `INPUT_CONTEXT_PRESSURE` is advisory: the call still produced a usable
    response. Every other reason means the artifact is unusable and must not be
    parsed as a proposal or counted as a model reasoning failure.
    """

    return reason is not None and reason != CeilingStopReason.INPUT_CONTEXT_PRESSURE


class FailurePartition(StrictModel):
    """Failures split so a ceiling cannot be reported as model reasoning.

    The handoff forbids a single combined failure count: an interface defect
    that is fixed by compaction and a model that proposed the wrong thing need
    different work, and averaging them hides both.
    """

    ceiling_failures: int = Field(default=0, ge=0)
    model_reasoning_failures: int = Field(default=0, ge=0)
    advisory_pressure_events: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return self.ceiling_failures + self.model_reasoning_failures

    @model_validator(mode="after")
    def validate_partition(self) -> FailurePartition:
        # Defensive: the constructor is the only supported way to build this,
        # but a hand-written or replayed record must not claim a negative split.
        if self.total < 0:
            raise ValueError("a failure partition cannot be negative")
        return self


def partition_failures(reasons: list[CeilingStopReason | None]) -> FailurePartition:
    """Split one call's-worth-per-entry failure labels into their two classes.

    Each entry is the classification of one *failed* call. `None` means the call
    failed for a reason this module does not explain, which is the only class
    that may be attributed to the model's reasoning.
    """

    ceiling = sum(1 for reason in reasons if is_ceiling_failure(reason))
    pressure = sum(
        1 for reason in reasons if reason == CeilingStopReason.INPUT_CONTEXT_PRESSURE
    )
    return FailurePartition(
        ceiling_failures=ceiling,
        # An advisory pressure event did not fail the call, so it is charged to
        # neither class.
        model_reasoning_failures=len(reasons) - ceiling - pressure,
        advisory_pressure_events=pressure,
    )
