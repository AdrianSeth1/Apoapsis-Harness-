"""Enforce a measured token ceiling on an assembled step prompt (MH-2, Task B).

`apoapsis.context.measurement` measures a compiled `ContextPackage` and is
documented as never influencing retrieval, ranking, or truncation. That is
still true: this module does not change it. What this module adds is the thing
that was missing entirely -- something that measures the *assembled prompt*
against the model's *actual* context window before dispatch, and either shrinks
it deterministically or refuses to send it.

Why it was needed: `[context] max_total_chars = 180000` is ~45K tokens at the
project's own 4-chars/token heuristic, against a `context_window_tokens` of
32,768 in the same config file. Nothing reconciled the two, so an oversized
prompt was silently truncated by the server -- which is the worst failure mode
available, because the harness then reasons about a prompt the model never saw.

Shrink order is fixed and deterministic (never "whatever is biggest"):

1. observations -- the append-only ledger view, already bounded elsewhere and
   the cheapest thing to lose, oldest first;
2. evidence excerpts -- dropped lowest-priority first via the compiler's own
   `evidence_reason_priority`, so the least-justified excerpt goes first;
3. session history -- oldest turn first, and a dropped turn is replaced by its
   one-line summary rather than vanishing, because a model that cannot see
   that it already tried something will try it again.

If the prompt still exceeds the ceiling after all three, dispatch is refused
with a named outcome. Truncating further would mean cutting the task
specification or the action protocol, and a model that cannot see its own
protocol cannot produce a valid action.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from pydantic import Field

from apoapsis.context.compiler import ContextPackage, evidence_reason_priority
from apoapsis.context.provenance import ContextEvidence
from apoapsis.specification.schema import StrictModel

#: Identical to `apoapsis.context.measurement._CHARS_PER_TOKEN_ESTIMATE` and
#: `apoapsis.doctor`'s context check, so the three reported numbers never
#: disagree with each other.
CHARS_PER_TOKEN_ESTIMATE = 4

#: Headroom between the computed ceiling and the real window. Covers chat
#: template wrapping, the JSON schema the provider attaches for structured
#: output, and the gap between the 4-chars/token heuristic and a real
#: tokenizer on dense JSON. Deliberately generous: overshooting the window
#: costs a silently truncated prompt, undershooting costs one dropped excerpt.
DEFAULT_SAFETY_MARGIN_TOKENS = 1_024


class PromptMeasurementSource(StrEnum):
    """How a prompt's token count was obtained."""

    HEURISTIC = "chars_per_token_heuristic"
    TOKENIZER = "provider_tokenizer"


class PromptShrinkStage(StrEnum):
    """The deepest shrink stage a prompt required. Ordered by severity."""

    NONE = "none"
    OBSERVATIONS = "observations"
    EVIDENCE = "evidence"
    HISTORY = "history"
    IRREDUCIBLE = "irreducible"


@dataclass(frozen=True)
class PromptWindowLimits:
    """The token ceiling one prompt must fit inside.

    Derived from the model provider's own configuration rather than from the
    context compiler's char budget, which is exactly the disconnect this
    module exists to close.
    """

    context_window_tokens: int
    max_output_tokens: int
    safety_margin_tokens: int = DEFAULT_SAFETY_MARGIN_TOKENS

    def __post_init__(self) -> None:
        if self.prompt_token_ceiling <= 0:
            raise ValueError(
                "prompt token ceiling must be positive: a window of "
                f"{self.context_window_tokens} leaves nothing for a prompt "
                f"after {self.max_output_tokens} output tokens and a "
                f"{self.safety_margin_tokens}-token safety margin"
            )

    @property
    def prompt_token_ceiling(self) -> int:
        """Tokens available to the prompt once output and margin are reserved."""

        return (
            self.context_window_tokens
            - self.max_output_tokens
            - self.safety_margin_tokens
        )

    @classmethod
    def from_provider(
        cls,
        *,
        context_window_tokens: int | None,
        max_output_tokens: int,
        safety_margin_tokens: int = DEFAULT_SAFETY_MARGIN_TOKENS,
    ) -> "PromptWindowLimits | None":
        """Build limits, or `None` when the provider declares no window.

        A provider with no declared `context_window_tokens` is not a provider
        with an infinite one -- it is a provider we cannot make a claim about,
        and inventing a ceiling for it would refuse dispatches for a limit
        nobody configured. Returning `None` disables enforcement explicitly
        instead of guessing.
        """

        if not context_window_tokens:
            return None
        try:
            return cls(
                context_window_tokens=context_window_tokens,
                max_output_tokens=max_output_tokens,
                safety_margin_tokens=safety_margin_tokens,
            )
        except ValueError:
            # A configuration whose output allowance already fills the window
            # is a configuration problem, not a per-turn one. Refusing every
            # dispatch here would report it as a prompt failure on every task;
            # `apoapsis doctor` is where that belongs.
            return None


class PromptWindowMeasurement(StrictModel):
    """A measurement of one assembled prompt against the window ceiling."""

    prompt_chars: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    token_ceiling: int = Field(ge=1)
    context_window_tokens: int = Field(ge=1)
    measured_by: PromptMeasurementSource
    evidence_items: int = Field(ge=0)
    history_turns: int = Field(ge=0)

    @property
    def within_ceiling(self) -> bool:
        return self.prompt_tokens <= self.token_ceiling


class PromptWindowFit(StrictModel):
    """What the guard did to one turn's prompt, recorded on the turn record."""

    before: PromptWindowMeasurement
    after: PromptWindowMeasurement
    stage: PromptShrinkStage = PromptShrinkStage.NONE
    observations_dropped: int = Field(default=0, ge=0)
    evidence_dropped: int = Field(default=0, ge=0)
    history_turns_compacted: int = Field(default=0, ge=0)

    @property
    def fits(self) -> bool:
        return self.after.within_ceiling

    @property
    def shrank(self) -> bool:
        return self.stage is not PromptShrinkStage.NONE


class PromptTooLargeError(Exception):
    """The prompt exceeds the window with nothing left that may be dropped.

    Carries the fit so the caller can record the measurement it refused on,
    rather than reporting only that something was too big.
    """

    def __init__(self, message: str, fit: PromptWindowFit) -> None:
        super().__init__(message)
        self.fit = fit


TokenCounter = Callable[[str], int]
PromptBuilder = Callable[[list[ContextEvidence], list[dict[str, object]]], str]


def replace_evidence(
    package: ContextPackage, evidence: list[ContextEvidence]
) -> ContextPackage:
    """A copy of `package` carrying only `evidence`, with a fresh digest.

    `ContextPackage.context_sha256` is derived from the package's own content
    and validated on construction, so a reduced package must be *rebuilt*
    rather than mutated -- a copy that kept the original digest would be a
    package whose recorded identity no longer described what was transmitted.
    The reduction is also recorded in `compiler_parameters` so the audit
    surface shows that the prompt guard, not the compiler, dropped these
    items.
    """

    parameters = dict(package.compiler_parameters)
    dropped = len(package.evidence) - len(evidence)
    if dropped:
        parameters["prompt_window_evidence_dropped"] = dropped
    return ContextPackage(
        package_version=package.package_version,
        compiler_version=package.compiler_version,
        task_id=package.task_id,
        specification=package.specification,
        head_commit=package.head_commit,
        query_terms=list(package.query_terms),
        retrieval_tools=list(package.retrieval_tools),
        compiler_parameters=parameters,
        external_research_brief=package.external_research_brief,
        research_evidence_ids=list(package.research_evidence_ids),
        evidence=list(evidence),
    )


def estimate_tokens(text: str) -> int:
    """The project-wide 4-chars/token heuristic, rounded up."""

    return -(-len(text) // CHARS_PER_TOKEN_ESTIMATE)


def _measure(
    prompt: str,
    *,
    limits: PromptWindowLimits,
    evidence: list[ContextEvidence],
    history: list[dict[str, object]],
    count_tokens: TokenCounter | None,
) -> PromptWindowMeasurement:
    source = PromptMeasurementSource.HEURISTIC
    tokens = estimate_tokens(prompt)
    if count_tokens is not None:
        try:
            counted = count_tokens(prompt)
        except Exception:
            # An exact count is an improvement, never a dependency: a local
            # `/tokenize` endpoint that is slow, down, or newly incompatible
            # must not be able to stop a session that the heuristic can
            # measure perfectly well.
            counted = None
        if isinstance(counted, int) and counted >= 0:
            tokens = counted
            source = PromptMeasurementSource.TOKENIZER
    return PromptWindowMeasurement(
        prompt_chars=len(prompt),
        prompt_tokens=tokens,
        token_ceiling=limits.prompt_token_ceiling,
        context_window_tokens=limits.context_window_tokens,
        measured_by=source,
        evidence_items=len(evidence),
        history_turns=len(history),
    )


def _summarize_turn(turn: dict[str, object]) -> dict[str, object]:
    """Collapse one history turn to the line that says what happened.

    Keeps `turn`, `action`, `accepted`, and a clipped `summary`. Everything
    else -- evidence ids, ledger sizes, verification bookkeeping -- is
    reconstructible from the audit store and is not what a model needs in
    order to avoid repeating itself.
    """

    summary = turn.get("summary")
    compacted: dict[str, object] = {
        "turn": turn.get("turn"),
        "action": turn.get("action"),
        "accepted": turn.get("accepted"),
        "compacted": True,
    }
    if isinstance(summary, str):
        compacted["summary"] = summary[:200]
    return compacted


def _drop_order(evidence: list[ContextEvidence]) -> list[int]:
    """Indices of `evidence`, least valuable first.

    Sorted by descending compiler priority, then by descending position, so
    ties break toward the later (less prominent) item and the whole ordering
    is a pure function of the input.
    """

    ranked = [
        (evidence_reason_priority(item.reason_included), index)
        for index, item in enumerate(evidence)
    ]
    ranked.sort(key=lambda pair: (-pair[0], -pair[1]))
    return [index for _priority, index in ranked]


def fit_prompt_to_window(
    build: PromptBuilder,
    *,
    evidence: list[ContextEvidence],
    history: list[dict[str, object]],
    limits: PromptWindowLimits,
    is_observation: Callable[[ContextEvidence], bool] = lambda _item: False,
    count_tokens: TokenCounter | None = None,
) -> tuple[str, PromptWindowFit]:
    """Return the prompt to dispatch and what it cost to make it fit.

    `build` must be a pure function of the evidence list and history list it
    is handed: the guard calls it repeatedly with progressively smaller
    inputs, and any hidden state would make the measurement describe a prompt
    other than the one dispatched.

    Raises `PromptTooLargeError` when the prompt still exceeds the ceiling
    after every permitted reduction.
    """

    kept_evidence = list(evidence)
    kept_history = list(history)
    prompt = build(kept_evidence, kept_history)
    before = _measure(
        prompt,
        limits=limits,
        evidence=kept_evidence,
        history=kept_history,
        count_tokens=count_tokens,
    )
    if before.within_ceiling:
        return prompt, PromptWindowFit(before=before, after=before)

    stage = PromptShrinkStage.NONE
    observations_dropped = 0
    evidence_dropped = 0
    history_compacted = 0
    measurement = before

    # Stage 1: observations, oldest first. The ledger is ordered
    # chronologically, so the first surviving observation is always the
    # oldest one, and dropping it re-indexes the rest -- hence recomputing
    # the position each pass rather than iterating a stale index list.
    while not measurement.within_ceiling:
        victims = [
            index for index, item in enumerate(kept_evidence) if is_observation(item)
        ]
        if not victims:
            break
        stage = PromptShrinkStage.OBSERVATIONS
        oldest = victims[0]
        kept_evidence = [
            item for position, item in enumerate(kept_evidence) if position != oldest
        ]
        observations_dropped += 1
        prompt = build(kept_evidence, kept_history)
        measurement = _measure(
            prompt,
            limits=limits,
            evidence=kept_evidence,
            history=kept_history,
            count_tokens=count_tokens,
        )

    # Stage 2: evidence excerpts, lowest compiler priority first.
    while not measurement.within_ceiling and kept_evidence:
        stage = PromptShrinkStage.EVIDENCE
        victim = _drop_order(kept_evidence)[0]
        kept_evidence = [
            item for position, item in enumerate(kept_evidence) if position != victim
        ]
        evidence_dropped += 1
        prompt = build(kept_evidence, kept_history)
        measurement = _measure(
            prompt,
            limits=limits,
            evidence=kept_evidence,
            history=kept_history,
            count_tokens=count_tokens,
        )

    # Stage 3: history, oldest turn first, each collapsed to its one line.
    position = 0
    while not measurement.within_ceiling and position < len(kept_history):
        if kept_history[position].get("compacted") is True:
            position += 1
            continue
        stage = PromptShrinkStage.HISTORY
        kept_history = [
            _summarize_turn(item) if index == position else item
            for index, item in enumerate(kept_history)
        ]
        history_compacted += 1
        position += 1
        prompt = build(kept_evidence, kept_history)
        measurement = _measure(
            prompt,
            limits=limits,
            evidence=kept_evidence,
            history=kept_history,
            count_tokens=count_tokens,
        )

    fit = PromptWindowFit(
        before=before,
        after=measurement,
        stage=stage if measurement.within_ceiling else PromptShrinkStage.IRREDUCIBLE,
        observations_dropped=observations_dropped,
        evidence_dropped=evidence_dropped,
        history_turns_compacted=history_compacted,
    )
    if not measurement.within_ceiling:
        raise PromptTooLargeError(
            "prompt exceeds the model context window and cannot be reduced "
            f"further: {measurement.prompt_tokens} tokens "
            f"({measurement.measured_by.value}) against a ceiling of "
            f"{limits.prompt_token_ceiling} "
            f"(window {limits.context_window_tokens} - output "
            f"{limits.max_output_tokens} - margin "
            f"{limits.safety_margin_tokens}). Dropped "
            f"{observations_dropped} observation(s), {evidence_dropped} "
            f"evidence excerpt(s), and compacted {history_compacted} history "
            "turn(s); what remains is the task specification and action "
            "protocol, which cannot be cut without making a valid response "
            "impossible.",
            fit,
        )
    return prompt, fit


__all__ = [
    "CHARS_PER_TOKEN_ESTIMATE",
    "DEFAULT_SAFETY_MARGIN_TOKENS",
    "PromptMeasurementSource",
    "PromptShrinkStage",
    "PromptTooLargeError",
    "PromptWindowFit",
    "PromptWindowLimits",
    "PromptWindowMeasurement",
    "TokenCounter",
    "estimate_tokens",
    "fit_prompt_to_window",
    "replace_evidence",
]
