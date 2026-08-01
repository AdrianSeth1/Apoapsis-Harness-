"""Attribute token usage to individual internal calls within one invocation.

Slice 5C reported that one perturbed invocation made a 53,397-token "second
internal call" against roughly 33,431 elsewhere, and could not say why. Read
against the retained evidence, that description is wrong in a way that matters:
53,397 is the `result` event — the CLI's own **session aggregate** — and the
invocation exposed exactly one `assistant` message carrying usage, at 22,433.
There was no second call in the stream. There was a total, a single visible
component, and a 30,964-token difference between them that nothing accounted
for.

The same difference is present in all six stage-7 invocations, tightly grouped
near 10,997 input tokens in five of them. So the residual itself is structural —
the CLI spends provider tokens on traffic it never emits an envelope for — and
only its *size* in `perturbed-1` is the outlier. An instrument that flagged the
residual as an anomaly would fire on every run; one that ignored it would have
missed the finding entirely.

That is why the aggregate is modelled separately from the calls it totals.
`recompute.all_provider_usage` returned every usage block in order and nothing
else, so the `result` total sat in the same list as the message it summed, with
no position, no stop reason, and no cohort. The number was noticed by eye and
then had nowhere to go.

The remedy is not a bigger aggregate. It is to make each internal call a record
that carries what would explain it:

- where it sits in the invocation, because only the first call carries the
  cacheable prefix a stable-prefix control perturbs;
- its stop reason, because a call that ended at the output ceiling and was
  retried explains a doubled input on the next one;
- whether a native compaction event preceded it, because compaction rewrites
  history and the call after it is *expected* to differ; and
- its neighbours, because "anomalous" is a claim about a distribution and
  needs one to be computed against.

`flag_anomalies` and `flag_residual_anomalies` therefore report deviation
rather than asserting a cause. A call whose input is far from its cohort is
flagged `UNEXPLAINED` unless one of the recorded conditions accounts for it, and
an unattributed residual is reported as exactly that — a quantity of tokens the
stream did not attribute. Neither function names a generator for those tokens,
because the event stream does not contain the evidence that would justify one,
and a plausible explanation attached to an anomaly is how a measurement error
becomes part of the record.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.events import _flatten_usage

#: How far an input count may sit from its cohort median before it is flagged.
#: 1.5x is deliberately loose: the point is to catch a 53,397-against-33,431
#: (1.60x) without flagging the ordinary growth of a conversation.
ANOMALY_RATIO = 1.5

#: Below this, ratios are noise -- a 12-token call being 3x a 4-token one is
#: not a finding.
ANOMALY_FLOOR_TOKENS = 1_000


class CallExplanation(str, Enum):
    """Why a call's input deviates, when something recorded accounts for it."""

    #: The first call of an invocation. Carries the prefix; not comparable to
    #: continuations, which have different prompts entirely.
    FIRST_CALL = "first_call"
    #: A native compaction event preceded this call, so history was rewritten.
    FOLLOWS_COMPACTION = "follows_compaction"
    #: The previous call stopped at the output ceiling; this is the retry.
    FOLLOWS_CEILING_STOP = "follows_ceiling_stop"
    #: Deviates, and nothing recorded accounts for it.
    UNEXPLAINED = "unexplained"


#: Stop reasons that mean the response was cut off rather than finished.
_CEILING_STOPS = frozenset({"length", "max_tokens", "MAX_TOKENS", "output_limit"})


class InternalCall(StrictModel):
    """One provider request inside a single CLI invocation."""

    #: Position within the invocation, zero-based. Only index 0 carries the
    #: perturbable prefix.
    index: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None
    #: Native compaction events observed before this call in the event stream.
    compactions_before: int = Field(default=0, ge=0)

    @property
    def stopped_at_ceiling(self) -> bool:
        return self.finish_reason in _CEILING_STOPS


class CallAnomaly(StrictModel):
    """A call whose input deviates from its cohort, and what accounts for it."""

    index: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    cohort_median: float = Field(ge=0)
    ratio: float = Field(ge=0)
    explanation: CallExplanation

    @property
    def is_unexplained(self) -> bool:
        return self.explanation is CallExplanation.UNEXPLAINED


class TokenTotals(StrictModel):
    """A usage triple, used for both the session aggregate and the residual."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


class ResidualStatus(str, Enum):
    """Whether the exposed calls account for the session aggregate."""

    #: No `result` event, so there is nothing to reconcile against.
    NO_AGGREGATE = "no_aggregate"
    #: The exposed calls sum to the aggregate.
    FULLY_ATTRIBUTED = "fully_attributed"
    #: The aggregate exceeds the exposed calls. Tokens were spent on provider
    #: traffic the event stream never surfaced.
    UNATTRIBUTED_RESIDUAL = "unattributed_residual"
    #: The exposed calls exceed the aggregate, which should be impossible and
    #: means one of the two is not what it claims to be.
    OVER_ATTRIBUTED = "over_attributed"


class InvocationDecomposition(StrictModel):
    """Every internal call of one invocation, in order.

    The `result` event is deliberately **not** one of `calls`. It is the CLI's
    own session aggregate — a total over the invocation, including provider
    traffic that never appears as an `assistant` envelope. Counting it
    alongside the messages it totals would compare a sum with its own
    components, which is the same category error the Slice 5C recomputation had
    to undo when a max across all messages compared two different prompts.

    Keeping them apart is what makes the residual computable, and the residual
    is the quantity that actually carries the unexplained Slice 5C tokens.
    """

    calls: list[InternalCall] = Field(default_factory=list)
    anomalies: list[CallAnomaly] = Field(default_factory=list)
    #: The `result` event's totals, when the stream carried one.
    aggregate: TokenTotals | None = None

    @property
    def first_call(self) -> InternalCall | None:
        """The only call comparable across arms of a prefix control.

        Named rather than left to callers to index, because taking a max across
        all calls is precisely the error the Slice 5C recomputation had to undo:
        it compared two different prompts and got the right answer by
        coincidence.
        """

        return self.calls[0] if self.calls else None

    @property
    def unexplained_anomalies(self) -> list[CallAnomaly]:
        return [item for item in self.anomalies if item.is_unexplained]

    @property
    def total_input_tokens(self) -> int:
        return sum(call.input_tokens or 0 for call in self.calls)

    @property
    def exposed(self) -> TokenTotals:
        """What the event stream actually showed."""

        return TokenTotals(
            input_tokens=sum(call.input_tokens or 0 for call in self.calls),
            output_tokens=sum(call.output_tokens or 0 for call in self.calls),
            cached_input_tokens=sum(
                call.cached_input_tokens or 0 for call in self.calls
            ),
        )

    @property
    def residual(self) -> TokenTotals | None:
        """Aggregate minus exposed: tokens the stream never attributed.

        `None` when there is no aggregate to reconcile against, which is not
        the same as a zero residual and must not be rendered as one.
        """

        if self.aggregate is None:
            return None
        exposed = self.exposed
        return TokenTotals(
            input_tokens=self.aggregate.input_tokens - exposed.input_tokens,
            output_tokens=self.aggregate.output_tokens - exposed.output_tokens,
            cached_input_tokens=(
                self.aggregate.cached_input_tokens - exposed.cached_input_tokens
            ),
        )

    @property
    def residual_status(self) -> ResidualStatus:
        residual = self.residual
        if residual is None:
            return ResidualStatus.NO_AGGREGATE
        if residual.input_tokens < 0:
            return ResidualStatus.OVER_ATTRIBUTED
        if residual.input_tokens == 0:
            return ResidualStatus.FULLY_ATTRIBUTED
        return ResidualStatus.UNATTRIBUTED_RESIDUAL


def decompose_invocation(record: dict) -> InvocationDecomposition:
    """Split one headless invocation record into its internal calls.

    Reads the same event shapes the adapter reads -- a usage block either on
    the event or nested in `message.usage` -- and goes through `_flatten_usage`
    so the CLI's `cache_read_input_tokens` spelling is handled in one place
    rather than re-learned here. Slice 5C's live stage read the other spelling
    directly and concluded the server reported no cache telemetry at all.
    """

    calls: list[InternalCall] = []
    aggregate: TokenTotals | None = None
    compactions = 0
    for item in record.get("events", []):
        if not isinstance(item, dict):
            continue
        if _is_compaction(item):
            compactions += 1
            continue
        if str(item.get("type", "")) == "result":
            block = item.get("usage")
            if isinstance(block, dict) and block:
                flat = _flatten_usage(block)
                aggregate = TokenTotals(
                    input_tokens=flat.get("input_tokens") or 0,
                    output_tokens=flat.get("output_tokens") or 0,
                    cached_input_tokens=flat.get("cached_input_tokens") or 0,
                )
            continue
        message = item.get("message")
        message = message if isinstance(message, dict) else {}
        for block, reason in (
            (item.get("usage"), item.get("stop_reason")),
            (message.get("usage"), message.get("stop_reason")),
        ):
            if not isinstance(block, dict) or not block:
                continue
            flat = _flatten_usage(block, finish_reason=reason)
            if not flat.get("input_tokens"):
                continue
            calls.append(
                InternalCall(
                    index=len(calls),
                    input_tokens=flat.get("input_tokens"),
                    output_tokens=flat.get("output_tokens"),
                    cached_input_tokens=flat.get("cached_input_tokens"),
                    finish_reason=(
                        str(reason) if reason is not None else None
                    ),
                    compactions_before=compactions,
                )
            )
            break
    return InvocationDecomposition(
        calls=calls, anomalies=flag_anomalies(calls), aggregate=aggregate
    )


class ResidualAnomaly(StrictModel):
    """An invocation whose unattributed residual deviates from its cohort."""

    label: str = Field(min_length=1)
    residual_input_tokens: int
    cohort_median: float
    ratio: float


def flag_residual_anomalies(
    decompositions: dict[str, InvocationDecomposition],
) -> list[ResidualAnomaly]:
    """Compare unattributed residuals across a set of matched invocations.

    A residual that is present in every invocation of a controlled set is a
    property of the CLI, not an anomaly, and calling it one would be a false
    positive on every run. What is worth flagging is a residual that departs
    from its own cohort, which is only visible across invocations — so this is
    a set-level function and deliberately not a property of one decomposition.

    It reports deviation and nothing else. Naming a generator for these tokens
    would require evidence the event stream does not contain.
    """

    residuals = {
        label: item.residual.input_tokens
        for label, item in decompositions.items()
        if item.residual is not None
        and item.residual_status is ResidualStatus.UNATTRIBUTED_RESIDUAL
    }
    if len(residuals) < 3:
        return []
    median = _median(sorted(residuals.values()))
    if median <= 0:
        return []
    found = []
    for label, tokens in sorted(residuals.items()):
        ratio = tokens / median
        if ANOMALY_RATIO > ratio > 1 / ANOMALY_RATIO:
            continue
        found.append(
            ResidualAnomaly(
                label=label,
                residual_input_tokens=tokens,
                cohort_median=median,
                ratio=ratio,
            )
        )
    return found


def _is_compaction(item: dict) -> bool:
    raw = str(item.get("type", "")).lower()
    return "compact" in raw or "compress" in raw


def flag_anomalies(calls: list[InternalCall]) -> list[CallAnomaly]:
    """Name every call that deviates from its cohort, and why -- or not why.

    The cohort is the *continuation* calls, index 1 onward. The first call is
    excluded from the median rather than merely labelled, because it carries a
    different prompt by construction and including it would move the very
    baseline the deviation is measured against.
    """

    cohort = [
        call.input_tokens
        for call in calls[1:]
        if call.input_tokens and call.input_tokens >= ANOMALY_FLOOR_TOKENS
    ]
    if len(cohort) < 2:
        # One continuation cannot deviate from itself. Reporting nothing here
        # is not the same as reporting no anomaly, and callers that need the
        # distinction have `calls` to look at.
        return []
    median = _median(cohort)
    if median <= 0:
        return []

    found: list[CallAnomaly] = []
    for call in calls[1:]:
        tokens = call.input_tokens
        if not tokens or tokens < ANOMALY_FLOOR_TOKENS:
            continue
        ratio = tokens / median
        if ANOMALY_RATIO > ratio > 1 / ANOMALY_RATIO:
            continue
        found.append(
            CallAnomaly(
                index=call.index,
                input_tokens=tokens,
                cohort_median=median,
                ratio=ratio,
                explanation=_explain(call, calls),
            )
        )
    return found


def _explain(call: InternalCall, calls: list[InternalCall]) -> CallExplanation:
    """Account for a deviation from what was recorded, or decline to.

    Order matters: compaction is checked before a ceiling stop because a
    compaction event rewrites history wholesale and subsumes the smaller
    effect. Nothing here infers a cause from the size of the deviation itself.
    """

    if call.index == 0:
        return CallExplanation.FIRST_CALL
    previous = calls[call.index - 1]
    if call.compactions_before > previous.compactions_before:
        return CallExplanation.FOLLOWS_COMPACTION
    if previous.stopped_at_ceiling:
        return CallExplanation.FOLLOWS_CEILING_STOP
    return CallExplanation.UNEXPLAINED


def _median(values: list[int]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2
