"""Ceilings expressed as work, not as turns.

> The budget described protocol turns, not engineering work. Twelve Local Power
> turns were not equivalent to the unrestricted control's 62 successful
> model/tool cycles.

A turn count is a proxy for cost that stops tracking cost the moment the
interface changes. Crisis Atlas Slice 2 had twelve turns available and used
one; the unrestricted control used sixty-two and was not extravagant. Neither
number described how much work the owner was willing to pay for.

So the primary ceilings here are wall time, process time, and tokens — things
that mean the same under any interface — plus **no-progress detection**, which
is the one that catches the failure a budget cannot: an agent that is spending
its allowance without moving. The Slice 2C sandbox arm was halted by loop
detection after nine identical calls, having burned 238,617 input tokens.

The call ceiling survives only as an emergency stop, set high. It exists to
bound a runaway, not to shape a session.
"""

from __future__ import annotations

import time
from enum import StrEnum

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel


class BudgetKind(StrEnum):
    WALL_CLOCK = "wall_clock"
    PROCESS_TIME = "process_time"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    NO_PROGRESS = "no_progress"
    #: The emergency stop. Reaching it means something else failed first.
    CALL_CEILING = "call_ceiling"
    DESTRUCTIVE_ACTIONS = "destructive_actions"


class SessionBudget(StrictModel):
    """What the owner is willing to spend on one slice."""

    wall_clock_seconds: float = Field(default=1_800.0, gt=0, le=86_400)
    #: Time spent inside the workcell running commands, as distinct from time
    #: waiting on the model. Reported separately because a session that is slow
    #: because its test suite is slow needs a different fix.
    process_seconds: float = Field(default=1_200.0, gt=0, le=86_400)
    max_input_tokens: int = Field(default=1_000_000, ge=1_000)
    max_output_tokens: int = Field(default=200_000, ge=1_000)
    #: Consecutive turns that changed nothing before the session is stopped.
    #: "Changed nothing" is measured by worktree fingerprint, not by the
    #: model's account of itself.
    max_no_progress_turns: int = Field(default=3, ge=1, le=20)
    #: Identical consecutive tool calls tolerated. The Slice 2C arm made nine.
    max_identical_actions: int = Field(default=3, ge=1, le=20)
    #: Emergency only. High on purpose: a session that hits this has already
    #: failed some other budget's intent.
    emergency_call_ceiling: int = Field(default=400, ge=10, le=10_000)
    #: Destructive actions the workcell will permit before stopping. The clone
    #: is sacrificial, so this is about noticing a rampage, not preventing harm.
    max_destructive_actions: int = Field(default=50, ge=0, le=1_000)

    @model_validator(mode="after")
    def validate_ceiling_is_generous(self) -> SessionBudget:
        # A low call ceiling would recreate the turn-count budget this module
        # exists to replace, by the back door.
        if self.emergency_call_ceiling < 50:
            raise ValueError(
                "the emergency call ceiling is a runaway stop, not a session "
                "shape; setting it below 50 turns it back into the turn budget "
                "that made twelve Local Power turns look like a work allowance"
            )
        return self


class TokenLedger(StrictModel):
    """Token counts, with authority attached.

    Two numbers, and only one of them may stop a session. `input_tokens` and
    `output_tokens` come from the provider's own `usage` events, lifted by
    `WorkcellEventAdapter` -- the same telemetry that classified the Crisis
    Atlas ceiling stops. `estimated_*` are the controller's local guesses, kept
    because they are useful for noticing that the estimator is wrong, and
    barred from every gate because an estimate that stops a session is a
    session stopped by a bug in the estimator.

    `reported` is false until at least one usage event arrives. A ledger with
    no telemetry does not read as zero spend: it reads as unmeasured, and the
    token ceilings say so rather than silently passing.
    """

    reported: bool = False
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    #: Diagnostic only. Never compared against a ceiling, never used to trigger
    #: compaction. See the class docstring.
    estimated_input_tokens: int = Field(default=0, ge=0)
    estimated_output_tokens: int = Field(default=0, ge=0)

    @classmethod
    def from_trace(cls, trace, **estimates) -> TokenLedger:
        """Build from a `WorkcellSessionTrace`'s provider-reported usage."""

        reported = bool(
            trace.input_tokens or trace.output_tokens or trace.cached_input_tokens
        )
        return cls(
            reported=reported,
            input_tokens=trace.input_tokens,
            output_tokens=trace.output_tokens,
            cached_input_tokens=trace.cached_input_tokens,
            **estimates,
        )

    @property
    def estimate_error(self) -> int | None:
        """Signed difference between the estimate and the truth, when known."""

        if not self.reported or not self.estimated_input_tokens:
            return None
        return self.estimated_input_tokens - self.input_tokens


class BudgetUsage(StrictModel):
    """What has been spent so far."""

    wall_clock_seconds: float = Field(default=0.0, ge=0)
    process_seconds: float = Field(default=0.0, ge=0)
    #: Provider-reported only. An unreported ledger leaves the token ceilings
    #: unenforced and says so in the verdict, rather than reading as zero.
    tokens: TokenLedger = Field(default_factory=TokenLedger)
    model_calls: int = Field(default=0, ge=0)
    destructive_actions: int = Field(default=0, ge=0)
    consecutive_no_progress_turns: int = Field(default=0, ge=0)
    max_identical_action_run: int = Field(default=0, ge=0)


class BudgetBreach(StrictModel):
    kind: BudgetKind
    detail: str = Field(min_length=1)
    #: What to tell the agent, if anything. Empty when the stop is not the
    #: agent's to fix -- a wall-clock expiry is not a repair instruction.
    guidance: str = ""


class BudgetVerdict(StrictModel):
    within_budget: bool
    breaches: list[BudgetBreach] = Field(default_factory=list)
    #: Ceilings that could not be checked. Non-empty means the verdict is
    #: partial, and `within_budget=True` means "nothing measurable was
    #: exceeded" rather than "the session is within budget".
    unenforced: list[BudgetKind] = Field(default_factory=list)
    #: Fraction of the tightest budget consumed, for reporting pressure before
    #: it becomes a stop.
    pressure: float = Field(default=0.0, ge=0)
    detail: str = Field(min_length=1)


def evaluate_budget(budget: SessionBudget, usage: BudgetUsage) -> BudgetVerdict:
    """Check every ceiling and report all of them, not the first.

    All of them, because an owner reading a stopped session wants to know
    whether it ran out of time, ran out of tokens, or stopped moving -- and
    those have different answers.
    """

    breaches: list[BudgetBreach] = []
    ratios: list[float] = []
    unenforced: list[BudgetKind] = []

    def check(
        kind: BudgetKind, used: float, limit: float, detail: str, guidance: str = ""
    ) -> None:
        ratios.append(used / limit if limit else 0.0)
        if used >= limit:
            breaches.append(BudgetBreach(kind=kind, detail=detail, guidance=guidance))

    check(
        BudgetKind.WALL_CLOCK,
        usage.wall_clock_seconds,
        budget.wall_clock_seconds,
        f"wall clock {usage.wall_clock_seconds:.0f}s reached the "
        f"{budget.wall_clock_seconds:.0f}s ceiling",
    )
    check(
        BudgetKind.PROCESS_TIME,
        usage.process_seconds,
        budget.process_seconds,
        f"in-workcell process time {usage.process_seconds:.0f}s reached the "
        f"{budget.process_seconds:.0f}s ceiling",
    )
    if usage.tokens.reported:
        check(
            BudgetKind.INPUT_TOKENS,
            usage.tokens.input_tokens,
            budget.max_input_tokens,
            f"input tokens {usage.tokens.input_tokens:,} reached the "
            f"{budget.max_input_tokens:,} ceiling",
        )
        check(
            BudgetKind.OUTPUT_TOKENS,
            usage.tokens.output_tokens,
            budget.max_output_tokens,
            f"output tokens {usage.tokens.output_tokens:,} reached the "
            f"{budget.max_output_tokens:,} ceiling",
        )
    else:
        # No provider usage event has arrived. The estimates are present and
        # are deliberately not substituted: a session stopped on an estimate is
        # a session stopped by the estimator's error, and a token ceiling that
        # silently passes on missing data is worse than one that abstains.
        unenforced.extend([BudgetKind.INPUT_TOKENS, BudgetKind.OUTPUT_TOKENS])
    check(
        BudgetKind.NO_PROGRESS,
        usage.consecutive_no_progress_turns,
        budget.max_no_progress_turns,
        f"{usage.consecutive_no_progress_turns} consecutive turn(s) changed "
        "nothing in the worktree",
        guidance=(
            "Your last few turns left the worktree unchanged. Read what is "
            "still outstanding and take a different action; repeating the "
            "previous one will not change the result."
        ),
    )
    check(
        BudgetKind.CALL_CEILING,
        usage.model_calls,
        budget.emergency_call_ceiling,
        f"model calls {usage.model_calls} reached the emergency ceiling of "
        f"{budget.emergency_call_ceiling}; this is a runaway stop and some "
        "other budget should have caught it first",
    )
    check(
        BudgetKind.DESTRUCTIVE_ACTIONS,
        usage.destructive_actions,
        budget.max_destructive_actions,
        f"{usage.destructive_actions} destructive action(s) reached the "
        f"{budget.max_destructive_actions} ceiling",
    )

    if usage.max_identical_action_run >= budget.max_identical_actions:
        breaches.append(
            BudgetBreach(
                kind=BudgetKind.NO_PROGRESS,
                detail=(
                    f"the same action was repeated {usage.max_identical_action_run} "
                    "times in a row"
                ),
                guidance=(
                    "You repeated an identical action several times. It will "
                    "keep returning the same result; try a different approach."
                ),
            )
        )

    pressure = max(ratios) if ratios else 0.0
    unmeasured_note = (
        ""
        if not unenforced
        else (
            "; token ceilings UNENFORCED -- no provider usage event has been "
            "reported, and the local estimate is not permitted to stop a session"
        )
    )
    if breaches:
        return BudgetVerdict(
            within_budget=False,
            breaches=breaches,
            unenforced=unenforced,
            pressure=pressure,
            detail=(
                f"{len(breaches)} budget(s) exhausted: "
                + "; ".join(item.detail for item in breaches)
                + unmeasured_note
            ),
        )
    return BudgetVerdict(
        within_budget=True,
        unenforced=unenforced,
        pressure=pressure,
        detail=(
            f"no measurable ceiling exceeded; tightest is {pressure:.0%} consumed"
            + unmeasured_note
        ),
    )


class ProgressKind(StrEnum):
    """The three ways a turn can advance authoritative state."""

    WORKTREE = "worktree_changed"
    OBLIGATION = "obligation_discharged"
    EVIDENCE = "evidence_produced"
    NONE = "none"


class TurnObservation(StrictModel):
    """What the controller observed about one turn.

    Every field is controller-owned. The model's account of the turn is
    deliberately not among them.
    """

    action_signature: str = Field(min_length=1)
    worktree_fingerprint: str | None = None
    #: Obligation ids the readiness evaluator now considers discharged that it
    #: did not before.
    discharged_obligations: list[str] = Field(default_factory=list)
    #: Witness ids or artifact hashes the controller produced this turn. A
    #: debugging turn that edits nothing but yields a new coverage artifact or
    #: a new failure diagnosis has advanced the session's knowledge, and
    #: counting it as no-progress would punish exactly the behaviour the
    #: unrestricted control did well.
    evidence_artifacts: list[str] = Field(default_factory=list)


class TurnProgress(StrictModel):
    made_progress: bool
    kinds: list[ProgressKind] = Field(default_factory=list)
    detail: str = Field(min_length=1)


class ProgressTracker:
    """Detects a session that is spending without advancing state.

    Progress is **authoritative state advancement**: a changed worktree
    fingerprint, an obligation the readiness evaluator newly considers
    discharged, or a new controller-observed evidence artifact. Any one of the
    three counts.

    Model narration alone is never progress. That is the distinction the Slice
    2C sandbox arm's nine identical calls turned on -- they read as productive
    and changed nothing observable -- and it is also why the definition is not
    just "the worktree changed": a turn that runs a failing test and produces a
    coverage artifact naming the failure has moved the session forward without
    touching a file.
    """

    def __init__(self) -> None:
        self.last_fingerprint: str | None = None
        self.consecutive_no_progress = 0
        self._last_action: str | None = None
        self._identical_run = 0
        self.max_identical_run = 0
        self.no_progress_actions: list[str] = []
        self._seen_obligations: set[str] = set()
        self._seen_artifacts: set[str] = set()

    def record_turn(self, observation: TurnObservation) -> TurnProgress:
        """Record one turn and say whether -- and how -- it advanced state."""

        if self._last_action == observation.action_signature:
            self._identical_run += 1
        else:
            self._identical_run = 1
            self._last_action = observation.action_signature
        self.max_identical_run = max(self.max_identical_run, self._identical_run)

        kinds: list[ProgressKind] = []

        fingerprint = observation.worktree_fingerprint
        if fingerprint is not None and fingerprint != self.last_fingerprint:
            self.last_fingerprint = fingerprint
            kinds.append(ProgressKind.WORKTREE)

        # "Newly" discharged, not "currently" discharged: an obligation that
        # was already satisfied three turns ago is not this turn's work.
        fresh_obligations = set(observation.discharged_obligations) - self._seen_obligations
        if fresh_obligations:
            self._seen_obligations |= fresh_obligations
            kinds.append(ProgressKind.OBLIGATION)

        fresh_artifacts = set(observation.evidence_artifacts) - self._seen_artifacts
        if fresh_artifacts:
            self._seen_artifacts |= fresh_artifacts
            kinds.append(ProgressKind.EVIDENCE)

        if kinds:
            self.consecutive_no_progress = 0
            return TurnProgress(
                made_progress=True,
                kinds=kinds,
                detail="; ".join(
                    {
                        ProgressKind.WORKTREE: "the worktree changed",
                        ProgressKind.OBLIGATION: (
                            f"{len(fresh_obligations)} obligation(s) newly discharged"
                        ),
                        ProgressKind.EVIDENCE: (
                            f"{len(fresh_artifacts)} new evidence artifact(s)"
                        ),
                    }[kind]
                    for kind in kinds
                ),
            )

        self.consecutive_no_progress += 1
        if observation.action_signature not in self.no_progress_actions:
            self.no_progress_actions.append(observation.action_signature)
        return TurnProgress(
            made_progress=False,
            kinds=[ProgressKind.NONE],
            detail=(
                "the worktree is unchanged, no obligation was newly discharged, "
                "and no new evidence artifact was produced"
            ),
        )


class SessionClock:
    """Wall clock and in-workcell process time, kept apart.

    Separately, because "the session took 30 minutes" and "the session spent 25
    of those minutes running the test suite" call for different fixes, and one
    number cannot say which happened.
    """

    def __init__(self) -> None:
        self._started = time.monotonic()
        self.process_seconds = 0.0

    def add_process_time(self, seconds: float) -> None:
        self.process_seconds += max(0.0, seconds)

    @property
    def wall_clock_seconds(self) -> float:
        return time.monotonic() - self._started

    def usage(self, **overrides) -> BudgetUsage:
        payload = {
            "wall_clock_seconds": self.wall_clock_seconds,
            "process_seconds": self.process_seconds,
        }
        payload.update(overrides)
        return BudgetUsage(**payload)
