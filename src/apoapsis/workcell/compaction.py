"""Build the handoff capsule under a size bound, and simulate compaction.

**This module is not the live history manager.** It was written as one, and
the Option C probe of the pinned CLI settled that it cannot be: Qwen Code owns
its own conversation history and compacts it itself, at
`context.autoCompactThreshold`. Apoapsis speaks only between native
invocations. So what lives here is (a) the bounded selection of what goes into
a session-handoff capsule, and (b) a simulation of threshold behaviour used to
reason about, and test against, recorded runs. Nothing here decides when the
model's real context is compacted.


The Crisis Atlas unrestricted control died at the ceiling. Its README write
ended with prompt 64,409 + completion 1,127 = exactly the 65,536-token window;
the tool-call JSON was truncated and the next request returned HTTP 500. The
evaluator, not the agent, started a fresh continuation. Slice 2D's own
near-boundary run reached 58,038 tokens — **88.6% of the window** — and still
fired no compaction event.

So the rule is *proactive*: selection starts at a measured fraction of the
window, well before the hard ceiling. Under Option B this governs the capsule,
and the native threshold governs the model.

Two tiers, cheapest first:

1. **Mechanical.** Drop old reasoning blocks, and replace old tool outputs with
   pointers to the artifacts they were spilled to. Costs nothing, loses
   nothing retrievable, and usually suffices.
2. **Semantic.** Only if the remaining history still approaches the threshold.
   It costs a model call and it summarises, which means it can lose things —
   so it is the fallback, not the default.

The threshold is a **configurable experiment point, not a constant**, and its
provenance was previously stated wrongly here. Earlier versions of this file,
the README, and ADR 0080 all said 0.70 "matches Qwen Code's default". It does
not. The setting that carried a percentage threshold,
`model.chatCompression.contextPercentageThreshold`, is marked **REMOVED** in
0.21.1 and "silently ignored (no startup warning)". The live setting is
`context.autoCompactThreshold`, **default 0.85**, and it is not flat: the
bundled docs describe a three-tier warn/auto/hard ladder computed from the
window by `computeThresholds()`, which fires earlier on small windows.

0.70 is therefore an Apoapsis choice with no upstream backing. It is kept as
the *capsule* trigger, deliberately below the native threshold so a capsule is
ready before the model's own compaction fires -- which is a reason, unlike the
one it had. The native value is pinned separately in `pins.NativeContextPin`
rather than reimplemented, because duplicating a ladder we do not control is
how the two drift apart silently.

Truncation is always visible and always reversible. Nothing is dropped without
an artifact pointer the model can follow, because the alternative — silently
discarding the only line that said what went wrong — is the failure this whole
design keeps finding.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel

#: Apoapsis's *capsule* trigger. NOT Qwen's compaction default -- that is
#: `NativeContextPin.auto_compact_threshold`, 0.85 for 0.21.1. Set below the
#: native threshold on purpose: the handoff capsule should exist before the
#: model's own compaction fires, not after it.
DEFAULT_COMPACTION_THRESHOLD = 0.70

#: Below this the window is too small for two-tier compaction to help; the
#: session should be reconfigured rather than compacted into uselessness.
_MINIMUM_USABLE_WINDOW = 8_192


class CompactionTier(StrEnum):
    NONE = "none"
    #: Drop reasoning, replace tool outputs with artifact pointers.
    MECHANICAL = "mechanical"
    #: Summarise. Costs a model call and can lose detail, so it is a fallback.
    SEMANTIC = "semantic"


class SegmentKind(StrEnum):
    """What a piece of history is, which determines how cheaply it can go."""

    #: The state capsule. Never dropped: it is what survives compaction.
    CAPSULE = "capsule"
    #: A model reasoning block. First to go, and cheapest.
    REASONING = "reasoning"
    #: Raw tool output. Replaced by a pointer once spilled.
    TOOL_OUTPUT = "tool_output"
    #: A tool call and its structured result summary. Kept verbatim while
    #: recent: the handoff wants recent tool calls kept as they are.
    TOOL_CALL = "tool_call"
    #: An assistant message that is not reasoning.
    MESSAGE = "message"


class HistorySegment(StrictModel):
    """One addressable piece of conversation history."""

    segment_id: str = Field(min_length=1)
    kind: SegmentKind
    #: Turn number, so recency can be judged without timestamps.
    turn: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    text: str = ""
    #: Where the full content lives if this segment is spilled or already was.
    artifact_pointer: str | None = None


class CompactionDecision(StrictModel):
    tier: CompactionTier
    #: Fraction of the window the history occupied before compacting.
    utilisation_before: float = Field(ge=0)
    utilisation_after: float = Field(ge=0)
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    dropped_segment_ids: list[str] = Field(default_factory=list)
    spilled_segment_ids: list[str] = Field(default_factory=list)
    detail: str = Field(min_length=1)

    @property
    def tokens_saved(self) -> int:
        return max(0, self.tokens_before - self.tokens_after)


class ContextReading(StrictModel):
    """How full the context is, and on whose authority.

    `provider_reported` is the whole point. Compaction changes what the model
    can see, so the decision to compact must rest on the provider's own usage
    accounting rather than on the controller's token estimate.
    """

    input_tokens: int = Field(ge=0)
    provider_reported: bool
    estimated_tokens: int = Field(default=0, ge=0)

    @classmethod
    def from_ledger(cls, ledger) -> ContextReading:
        return cls(
            input_tokens=ledger.input_tokens,
            provider_reported=ledger.reported,
            estimated_tokens=ledger.estimated_input_tokens,
        )


class CompactionPolicy(StrictModel):
    """When to compact and how much history to keep verbatim."""

    context_limit_tokens: int = Field(ge=_MINIMUM_USABLE_WINDOW)
    #: Compaction begins here. Deliberately well below 1.0.
    threshold: float = Field(default=DEFAULT_COMPACTION_THRESHOLD, gt=0, lt=1)
    #: After compacting, aim to be under this. Lower than `threshold` so a
    #: session does not compact, land just under the line, and compact again
    #: on the very next turn.
    target: float = Field(default=0.50, gt=0, lt=1)
    #: Turns of tool calls kept verbatim regardless. The handoff asks for
    #: recent tool calls to survive as they are.
    keep_recent_turns: int = Field(default=2, ge=0, le=20)

    @model_validator(mode="after")
    def validate_target_below_threshold(self) -> CompactionPolicy:
        if self.target >= self.threshold:
            raise ValueError(
                "the compaction target must be below the trigger threshold, or "
                "a session will compact on every turn once it first crosses"
            )
        return self

    def utilisation(self, tokens: int) -> float:
        return tokens / self.context_limit_tokens

    def should_compact(self, tokens: int) -> bool:
        """Threshold test on a token count, without asking where it came from.

        Kept for arithmetic and for replay of recorded usage. A live session
        must use `should_compact_reading`, which refuses an estimate.
        """

        return self.utilisation(tokens) >= self.threshold

    def should_compact_reading(self, reading: ContextReading) -> bool:
        """The live trigger. Only provider-reported usage may fire it.

        An estimate that reads high compacts a session that did not need it,
        and an estimate that reads low is how a run reaches 64,409 tokens
        against a 65,536 window with no compaction event -- which is the
        recorded Crisis Atlas control. Neither failure should be attributable
        to the controller's own arithmetic, so the estimate does not get a
        vote.
        """

        if not reading.provider_reported:
            return False
        return self.should_compact(reading.input_tokens)


def spill_artifact(directory: Path, segment: HistorySegment) -> str:
    """Write a segment's full text where the model can retrieve it.

    Returns the pointer. Content-addressed so the same output spilled twice
    does not become two artifacts, and so a pointer names its bytes.
    """

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(segment.text.encode("utf-8")).hexdigest()[:16]
    path = directory / f"{segment.kind.value}-{digest}.txt"
    if not path.exists():
        path.write_text(segment.text, encoding="utf-8")
    return str(path)


def compact(
    segments: list[HistorySegment],
    policy: CompactionPolicy,
    *,
    spill_directory: Path | None = None,
    current_turn: int | None = None,
) -> tuple[list[HistorySegment], CompactionDecision]:
    """Compact history if it is over the threshold. Mechanical first.

    Returns the surviving segments and what was done. When nothing needs doing,
    the segments come back unchanged and the tier is `NONE` — a caller should
    not have to guess whether compaction ran.
    """

    tokens_before = sum(item.estimated_tokens for item in segments)
    before = policy.utilisation(tokens_before)

    if not policy.should_compact(tokens_before):
        return segments, CompactionDecision(
            tier=CompactionTier.NONE,
            utilisation_before=before,
            utilisation_after=before,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            detail=(
                f"history is at {before:.0%} of the window, below the "
                f"{policy.threshold:.0%} threshold; nothing to do"
            ),
        )

    latest = current_turn if current_turn is not None else max(
        (item.turn for item in segments), default=0
    )
    keep_from = latest - policy.keep_recent_turns

    surviving: list[HistorySegment] = []
    dropped: list[str] = []
    spilled: list[str] = []

    for segment in segments:
        # The capsule is what compaction exists to preserve.
        if segment.kind == SegmentKind.CAPSULE or segment.turn > keep_from:
            surviving.append(segment)
            continue

        if segment.kind == SegmentKind.REASONING:
            # Old reasoning is the cheapest thing to lose: it described how the
            # model got somewhere it has since arrived at.
            dropped.append(segment.segment_id)
            continue

        if segment.kind == SegmentKind.TOOL_OUTPUT:
            pointer = segment.artifact_pointer
            if pointer is None and spill_directory is not None and segment.text:
                pointer = spill_artifact(spill_directory, segment)
            if pointer is None:
                # Nowhere to spill it and no existing pointer. Keeping it is
                # the honest choice: dropping it would make the only record of
                # what a command printed vanish.
                surviving.append(segment)
                continue
            spilled.append(segment.segment_id)
            surviving.append(
                segment.model_copy(
                    update={
                        "text": (
                            f"[output of {segment.segment_id} spilled to "
                            f"{pointer}; read it if you need it]"
                        ),
                        "artifact_pointer": pointer,
                        "estimated_tokens": 24,
                    }
                )
            )
            continue

        surviving.append(segment)

    tokens_after = sum(item.estimated_tokens for item in surviving)
    after = policy.utilisation(tokens_after)
    tier = CompactionTier.MECHANICAL

    if after >= policy.target:
        # Mechanical compaction was not enough. Semantic compaction is the
        # caller's to perform -- it needs a model call -- so this reports that
        # it is required rather than silently doing nothing more.
        return surviving, CompactionDecision(
            tier=CompactionTier.SEMANTIC,
            utilisation_before=before,
            utilisation_after=after,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            dropped_segment_ids=dropped,
            spilled_segment_ids=spilled,
            detail=(
                f"mechanical compaction took the history from {before:.0%} to "
                f"{after:.0%}, still at or above the {policy.target:.0%} target; "
                "semantic compaction is required"
            ),
        )

    return surviving, CompactionDecision(
        tier=tier,
        utilisation_before=before,
        utilisation_after=after,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        dropped_segment_ids=dropped,
        spilled_segment_ids=spilled,
        detail=(
            f"mechanical compaction took the history from {before:.0%} to "
            f"{after:.0%}: {len(dropped)} reasoning block(s) dropped, "
            f"{len(spilled)} tool output(s) spilled to retrievable artifacts"
        ),
    )


class ToolOutputBudget(StrictModel):
    """Per-tool observation ceilings.

    Different tools need different budgets, and one number for all of them
    either starves a test failure or floods the context with a directory
    listing. The values follow the handoff's per-tool guidance.
    """

    search_chars: int = Field(default=4_000, ge=200)
    file_read_chars: int = Field(default=16_000, ge=200)
    test_failure_chars: int = Field(default=12_000, ge=200)
    test_success_chars: int = Field(default=1_000, ge=100)
    shell_chars: int = Field(default=8_000, ge=200)
    #: A background server's accumulated log is the worst offender: it grows
    #: without bound and almost none of it is news.
    background_chars: int = Field(default=2_000, ge=100)

    def for_tool(self, tool_name: str, *, passed: bool | None = None) -> int:
        name = tool_name.lower()
        if name in {"glob", "grep", "grep_search", "search_file_content", "ls"}:
            return self.search_chars
        if name in {"read_file", "read_many_files"}:
            return self.file_read_chars
        if "test" in name:
            return self.test_success_chars if passed else self.test_failure_chars
        if name in {"run_shell_command", "shell", "bash"}:
            return self.shell_chars
        return self.shell_chars


class BoundedObservation(StrictModel):
    """A tool observation clipped to its budget, with the rest retrievable."""

    text: str
    truncated: bool
    original_chars: int = Field(ge=0)
    artifact_pointer: str | None = None

    @model_validator(mode="after")
    def validate_truncation_is_reversible(self) -> BoundedObservation:
        if self.truncated and self.artifact_pointer is None:
            raise ValueError(
                "a truncated observation must name the artifact holding its "
                "full text; truncation that cannot be undone can discard the "
                "only line that said what went wrong"
            )
        return self


def bound_observation(
    text: str,
    *,
    max_chars: int,
    spill_directory: Path | None = None,
    label: str = "output",
) -> BoundedObservation:
    """Clip an observation, keeping the head and the tail.

    Head and tail rather than a prefix, because a command's first lines say
    what it did and its last lines say how it ended, and the failure is almost
    always in one of those two places.
    """

    if len(text) <= max_chars:
        return BoundedObservation(text=text, truncated=False, original_chars=len(text))

    pointer: str | None = None
    if spill_directory is not None:
        directory = Path(spill_directory)
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        path = directory / f"{label}-{digest}.txt"
        if not path.exists():
            path.write_text(text, encoding="utf-8")
        pointer = str(path)

    if pointer is None:
        # Refusing to truncate is safer than truncating irreversibly. The
        # caller gets the whole thing and can decide.
        return BoundedObservation(
            text=text, truncated=False, original_chars=len(text)
        )

    half = max(200, (max_chars - 160) // 2)
    head, tail = text[:half], text[-half:]
    dropped = len(text) - len(head) - len(tail)
    return BoundedObservation(
        text=(
            f"{head}\n\n[... {dropped:,} characters omitted; full output at "
            f"{pointer} ...]\n\n{tail}"
        ),
        truncated=True,
        original_chars=len(text),
        artifact_pointer=pointer,
    )
