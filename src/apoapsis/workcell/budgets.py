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


class BudgetUsage(StrictModel):
    """What has been spent so far."""

    wall_clock_seconds: float = Field(default=0.0, ge=0)
    process_seconds: float = Field(default=0.0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
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
    check(
        BudgetKind.INPUT_TOKENS,
        usage.input_tokens,
        budget.max_input_tokens,
        f"input tokens {usage.input_tokens:,} reached the "
        f"{budget.max_input_tokens:,} ceiling",
    )
    check(
        BudgetKind.OUTPUT_TOKENS,
        usage.output_tokens,
        budget.max_output_tokens,
        f"output tokens {usage.output_tokens:,} reached the "
        f"{budget.max_output_tokens:,} ceiling",
    )
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
    if breaches:
        return BudgetVerdict(
            within_budget=False,
            breaches=breaches,
            pressure=pressure,
            detail=(
                f"{len(breaches)} budget(s) exhausted: "
                + "; ".join(item.detail for item in breaches)
            ),
        )
    return BudgetVerdict(
        within_budget=True,
        pressure=pressure,
        detail=f"within budget; tightest ceiling is {pressure:.0%} consumed",
    )


class ProgressTracker:
    """Detects a session that is spending without moving.

    Progress is a change in the worktree fingerprint. Not the model saying it
    made progress, and not a turn having occurred: Crisis Atlas Slice 2's
    single turn produced a file, and the sandbox arm's nine identical calls
    produced nothing while looking busy.
    """

    def __init__(self) -> None:
        self.last_fingerprint: str | None = None
        self.consecutive_no_progress = 0
        self._last_action: str | None = None
        self._identical_run = 0
        self.max_identical_run = 0
        self.no_progress_actions: list[str] = []

    def record_turn(self, *, fingerprint: str | None, action_signature: str) -> bool:
        """Record one turn. Returns whether it made progress."""

        if self._last_action == action_signature:
            self._identical_run += 1
        else:
            self._identical_run = 1
            self._last_action = action_signature
        self.max_identical_run = max(self.max_identical_run, self._identical_run)

        moved = fingerprint is not None and fingerprint != self.last_fingerprint
        if moved:
            self.last_fingerprint = fingerprint
            self.consecutive_no_progress = 0
        else:
            self.consecutive_no_progress += 1
            if action_signature not in self.no_progress_actions:
                self.no_progress_actions.append(action_signature)
        return moved


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
