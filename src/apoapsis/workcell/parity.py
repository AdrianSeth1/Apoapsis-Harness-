"""Which slices run the matched control arm, and why that one.

`high_assurance_parity_guard` runs the unrestricted control arm on *every*
slice. It was right to: the question it answered — "is the supervised sandbox no
worse than the native baseline?" — could only be answered by running both, and
it was answered, with six paired 1.0/1.0 slots.

As a standing default it now means every user slice costs two full model
executions forever. That is not what the evidence bought. A question that has
been answered does not need to be re-answered on every slice; it needs to be
*monitored*, which is a sampling problem.

So the policy has three modes and the selection is deterministic. Deterministic
matters more than it sounds: a sampled comparison whose sample was chosen at
random is a comparison nobody can reproduce, and "the parity check happened to
skip the slice that regressed" is exactly the sentence this must never make
true by accident. Selection is a function of the slice's position in its plan,
so the same plan samples the same slices every time, and the audit records
which ones and why.

A sampled slice that regresses escalates exactly as it does today. Sampling
changes how often the question is asked, never what happens to the answer.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from apoapsis.specification.schema import StrictModel


class ParityMode(StrEnum):
    """How often the matched control arm runs."""

    #: Every slice. The pre-ADR-0108 behaviour, one config line away.
    ALWAYS = "always"
    #: The first slice of a plan, then every Nth. The default.
    SAMPLE = "sample"
    #: Never. The qualification evidence stands; nothing re-checks it.
    OFF = "off"


class ParitySelection(StrictModel):
    """Whether this slice runs a control arm, and the reason it was chosen.

    The reason is recorded rather than recomputed by readers. An audit that
    says "no control arm ran" is not evidence of anything; an audit that says
    "no control arm ran because this is slice 3 and the policy samples the
    first slice and every 4th" is.
    """

    mode: ParityMode
    run_control_arm: bool
    #: 1-based position of this slice in its plan, which is what the sampling
    #: rule is a function of.
    slice_position: int = Field(ge=0)
    sample_every: int = Field(ge=1)
    reason: str = Field(min_length=1)


def select_parity(
    *,
    mode: ParityMode,
    slice_position: int,
    sample_every: int = 4,
) -> ParitySelection:
    """Decide, deterministically, whether this slice pays for a control arm.

    `slice_position` is 1-based; 0 means the position could not be established
    (a slice that is not in the plan's own list, which should not happen). That
    case runs the control arm: when the harness cannot tell where it is, the
    safe direction is the more expensive one, not the cheaper one.
    """

    if mode == ParityMode.ALWAYS:
        return ParitySelection(
            mode=mode,
            run_control_arm=True,
            slice_position=slice_position,
            sample_every=sample_every,
            reason="the parity policy is 'always': every slice runs the control arm",
        )
    if mode == ParityMode.OFF:
        return ParitySelection(
            mode=mode,
            run_control_arm=False,
            slice_position=slice_position,
            sample_every=sample_every,
            reason=(
                "the parity policy is 'off': no control arm runs, and the "
                "qualification evidence is not re-checked during this run"
            ),
        )

    if slice_position <= 0:
        return ParitySelection(
            mode=mode,
            run_control_arm=True,
            slice_position=slice_position,
            sample_every=sample_every,
            reason=(
                "this slice's position in the plan could not be established, so "
                "the control arm runs rather than being skipped on a guess"
            ),
        )
    if slice_position == 1:
        return ParitySelection(
            mode=mode,
            run_control_arm=True,
            slice_position=slice_position,
            sample_every=sample_every,
            reason=(
                "the first slice of a plan always runs the control arm, so every "
                "plan has at least one paired comparison of its own"
            ),
        )
    sampled = (slice_position - 1) % sample_every == 0
    return ParitySelection(
        mode=mode,
        run_control_arm=sampled,
        slice_position=slice_position,
        sample_every=sample_every,
        reason=(
            f"slice {slice_position} is sampled: the policy pairs the first "
            f"slice and every {sample_every}th slice after it"
            if sampled
            else (
                f"slice {slice_position} is not sampled: the policy pairs the "
                f"first slice and every {sample_every}th slice after it, so this "
                "slice runs the sandbox arm only"
            )
        ),
    )


class ParityOutcome(StrictModel):
    """What the paired comparison concluded, for one slice.

    Unchanged in substance by sampling: a slice that was *expected* to pair and
    produced no scoreable control is unavailable, and a sandbox candidate that
    proved fewer obligations than its control is a regression. Both stop the
    slice for review exactly as they did when every slice paired. Sampling
    changes how often the question is asked, never what happens to the answer.
    """

    expected: bool
    unavailable: bool
    regression: bool

    @property
    def blocks_completion(self) -> bool:
        return self.unavailable or self.regression


def evaluate_parity(
    *,
    expected: bool,
    control_proved: int | None,
    candidate_proved: int | None,
) -> ParityOutcome:
    """Judge one slice's paired comparison.

    `control_proved` is `None` when no control checkpoint exists -- either
    because this slice was not sampled, or because the control arm failed to
    produce one. Those are different situations and `expected` is what tells
    them apart: an unsampled slice with no control is the policy working, and a
    sampled slice with no control is a comparison that was supposed to happen
    and did not.
    """

    if control_proved is None:
        return ParityOutcome(
            expected=expected, unavailable=expected, regression=False
        )
    if candidate_proved is None:
        # A control exists and the sandbox produced no checkpoint at all. That
        # is already a stop for other reasons; it is not a *parity* regression,
        # and reporting it as one would blame the comparison for the absence.
        return ParityOutcome(expected=expected, unavailable=False, regression=False)
    return ParityOutcome(
        expected=expected,
        unavailable=False,
        regression=candidate_proved < control_proved,
    )


def slice_position(plan: dict, slice_id: str) -> int:
    """1-based position of a slice in its plan, or 0 if it is not listed."""

    for index, item in enumerate(plan.get("slices", []), start=1):
        if item.get("slice_id") == slice_id:
            return index
    return 0


__all__ = [
    "ParityMode",
    "ParityOutcome",
    "ParitySelection",
    "evaluate_parity",
    "select_parity",
    "slice_position",
]
