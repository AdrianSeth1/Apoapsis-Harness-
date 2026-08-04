"""What a run is doing right now, projected from what it has recorded.

The operator's three questions, from MH-9: *what is it doing, how far along is
it, and is context near the window?* Before this, all three were answerable
only by reading several JSON files that mostly did not exist yet, so the UI
showed a spinner and the honest answer was "something".

This module is a pure projection. It takes the events a run appended to its
progress journal and returns a `RunStatus`; it opens no sockets, holds no
state, and asks the running process nothing. That is what makes it testable
from recorded artifacts alone -- a fixture journal produces a status without a
container, a model, or a clock that has to be mocked into agreeing.

It is also why the projection is *allowed to be wrong about the future* and
never about the past. A stage that was entered and never left is reported as
still running even if the process died an hour ago, because that is precisely
what the evidence says. Deciding it "probably failed" would be the projection
inventing a fact the run never recorded, and this project's whole argument is
that the harness reports what it observed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from apoapsis.reporting.operator_schema import OperatorExplanation
from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.progress import (
    PROGRESS_FILENAME,
    ProgressEvent,
    ProgressEventKind,
    RunStage,
    read_progress,
)

#: Stage labels in operator language. MH-9's "naming tax" point: the UI should
#: say "running the model", not `MODEL_RUNNING`, and certainly not `EXOP`.
_STAGE_LABELS: dict[RunStage, str] = {
    RunStage.CONTROLLER_BUILD: "Building the sandbox image",
    RunStage.PREFLIGHT: "Checking the sandbox is sealed",
    RunStage.MODEL_LOADING: "Loading the model",
    RunStage.CONTROL_ARM: "Running the comparison arm",
    RunStage.MODEL_RUNNING: "Writing code",
    RunStage.CHECKPOINT: "Checking the work",
    RunStage.VERIFICATION: "Running the project's own tests",
}


class StageState(StrEnum):
    #: Not reached. Distinct from SKIPPED: a pending stage may still happen.
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    #: Reached the end of the run without ever being entered — the parity
    #: control arm on a slice the sampling policy did not pair, most often.
    #: Reported explicitly so its absence reads as a decision rather than a
    #: gap (ADR 0108's point, applied to the status view).
    SKIPPED = "skipped"


class StageProgress(StrictModel):
    stage: RunStage
    label: str
    state: StageState
    started_at: datetime | None = None
    ended_at: datetime | None = None
    #: Wall-clock seconds the stage took, as the *writer* measured it. Taken
    #: from the journal's own monotonic measurement where available rather
    #: than subtracting timestamps, so a clock adjustment mid-run cannot
    #: produce a negative duration.
    elapsed_seconds: float | None = None
    detail: str | None = None


class ContextPressure(StrictModel):
    """How close the latest exchange came to the model's window.

    `peak_input_tokens` and `last_input_tokens` are both reported because they
    answer different questions: the peak is whether this slice ever came close,
    the last is whether it is close *now*. A slice that compacted mid-run has a
    high peak and a low last, and reporting only one of them would hide the
    compaction entirely.
    """

    calls: int = Field(default=0, ge=0)
    last_input_tokens: int | None = None
    peak_input_tokens: int | None = None
    total_input_tokens: int = Field(default=0, ge=0)
    total_output_tokens: int = Field(default=0, ge=0)
    context_window_tokens: int | None = None
    #: `last_input_tokens / context_window_tokens`, or `None` when either is
    #: unknown. Never invented from a default window: a made-up denominator
    #: makes a made-up percentage, and this number is meant to be trusted.
    window_utilization: float | None = None

    @property
    def near_window(self) -> bool:
        return self.window_utilization is not None and self.window_utilization >= 0.8


class CheckpointSummary(StrictModel):
    attempt: int
    outcome: str
    operator: OperatorExplanation | None = None
    detail: str = ""
    obligations_proved: int | None = None
    obligations_total: int | None = None


class RunStatus(StrictModel):
    """One run, as a page can render it."""

    #: True while the journal shows no RUN_FINISHED event. Says nothing about
    #: whether the process is alive -- see the module docstring.
    running: bool
    outcome: str | None = None
    detail: str = ""
    current_stage: RunStage | None = None
    current_stage_label: str | None = None
    stages: list[StageProgress] = Field(default_factory=list)
    context: ContextPressure = Field(default_factory=ContextPressure)
    last_checkpoint: CheckpointSummary | None = None
    checkpoints_seen: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    updated_at: datetime | None = None
    elapsed_seconds: float | None = None
    #: What the harness has left to spend, passed through from the authorized
    #: budget rather than recomputed here. `None` when the caller did not
    #: supply one, which is honest: this module cannot see a budget it is not
    #: given, and should not guess at one.
    budget_remaining: dict = Field(default_factory=dict)
    #: True when the journal exists but has no events yet -- the run was
    #: authorized and has not written anything. Distinguished from "no journal
    #: at all" so the UI can say "starting" rather than "no data".
    awaiting_first_event: bool = False


def _operator_from_payload(payload: dict) -> OperatorExplanation | None:
    raw = payload.get("operator")
    if not isinstance(raw, dict):
        return None
    try:
        return OperatorExplanation.model_validate(raw)
    except (ValueError, TypeError):
        # A verdict whose operator rendering is malformed still has a usable
        # outcome and detail; dropping the whole checkpoint over the friendly
        # half of it would be the wrong trade.
        return None


def project_run_status(
    events: list[ProgressEvent],
    *,
    context_window_tokens: int | None = None,
    budget_remaining: dict | None = None,
    now: datetime | None = None,
    journal_exists: bool = True,
) -> RunStatus:
    """Project one run's recorded events into a status a page can render."""

    if not events:
        return RunStatus(
            running=journal_exists,
            awaiting_first_event=journal_exists,
            stages=[
                StageProgress(
                    stage=stage,
                    label=_STAGE_LABELS[stage],
                    state=StageState.PENDING,
                )
                for stage in RunStage.sequence()
            ],
            context=ContextPressure(context_window_tokens=context_window_tokens),
            budget_remaining=dict(budget_remaining or {}),
        )

    ordered = sorted(events, key=lambda item: item.sequence)
    entered: dict[RunStage, ProgressEvent] = {}
    left: dict[RunStage, ProgressEvent] = {}
    calls: list[dict] = []
    checkpoints: list[dict] = []
    finished: ProgressEvent | None = None
    started: ProgressEvent | None = None

    for event in ordered:
        if event.kind is ProgressEventKind.RUN_STARTED:
            started = event
        elif event.kind is ProgressEventKind.STAGE_ENTERED and event.stage is not None:
            entered.setdefault(event.stage, event)
        elif event.kind is ProgressEventKind.STAGE_LEFT and event.stage is not None:
            left[event.stage] = event
        elif event.kind is ProgressEventKind.MODEL_CALL:
            calls.append(event.payload)
        elif event.kind is ProgressEventKind.CHECKPOINT_VERDICT:
            checkpoints.append(event.payload)
        elif event.kind is ProgressEventKind.RUN_FINISHED:
            finished = event

    running = finished is None
    # The window the run recorded for itself wins over anything the caller
    # supplied: the caller is reading today's configuration, and the run knows
    # what it actually ran against.
    if started is not None:
        recorded_window = started.payload.get("context_window_tokens")
        if isinstance(recorded_window, int) and recorded_window > 0:
            context_window_tokens = recorded_window
    # A control arm that was never scheduled is not a stage that is "pending"
    # or one that "failed" -- it is a stage the parity policy decided against
    # (ADR 0108). Knowing that up front lets it read as a decision from the
    # first poll rather than only once the run ends.
    control_expected = (
        bool(started.payload.get("parity_arm_expected"))
        if started is not None and started.payload.get("parity_arm_expected") is not None
        else None
    )

    stages: list[StageProgress] = []
    current: RunStage | None = None
    for stage in RunStage.sequence():
        start = entered.get(stage)
        end = left.get(stage)
        if start is None:
            # Never entered. While the run is going it may still happen; once
            # the run is over it demonstrably did not.
            state = StageState.PENDING if running else StageState.SKIPPED
            detail = None
            if stage is RunStage.CONTROL_ARM and control_expected is False:
                state = StageState.SKIPPED
                detail = "not paired with a control arm by the parity policy"
            stages.append(
                StageProgress(
                    stage=stage,
                    label=_STAGE_LABELS[stage],
                    state=state,
                    detail=detail,
                )
            )
            continue
        if end is None:
            state = StageState.RUNNING
            current = stage
        elif end.payload.get("failed"):
            state = StageState.FAILED
        else:
            state = StageState.DONE
        stages.append(
            StageProgress(
                stage=stage,
                label=_STAGE_LABELS[stage],
                state=state,
                started_at=start.at,
                ended_at=end.at if end is not None else None,
                elapsed_seconds=(
                    end.payload.get("elapsed_seconds") if end is not None else None
                ),
                # `failed` first: when a stage both did something notable and
                # then failed, the failure is what the operator needs.
                detail=(
                    (end.payload.get("failed") or end.payload.get("note"))
                    if end is not None
                    else None
                ),
            )
        )

    usable = [
        item for item in calls if isinstance(item.get("input_tokens"), int)
    ]
    last_input = usable[-1]["input_tokens"] if usable else None
    peak_input = max((item["input_tokens"] for item in usable), default=None)
    context = ContextPressure(
        calls=len(calls),
        last_input_tokens=last_input,
        peak_input_tokens=peak_input,
        total_input_tokens=sum(item["input_tokens"] for item in usable),
        total_output_tokens=sum(
            item["output_tokens"]
            for item in calls
            if isinstance(item.get("output_tokens"), int)
        ),
        context_window_tokens=context_window_tokens,
        window_utilization=(
            last_input / context_window_tokens
            if last_input is not None and context_window_tokens
            else None
        ),
    )

    last_checkpoint = None
    if checkpoints:
        payload = checkpoints[-1]
        last_checkpoint = CheckpointSummary(
            attempt=int(payload.get("attempt") or len(checkpoints)),
            outcome=str(payload.get("outcome") or "unknown"),
            operator=_operator_from_payload(payload),
            detail=str(payload.get("detail") or ""),
            obligations_proved=payload.get("obligations_proved"),
            obligations_total=payload.get("obligations_total"),
        )

    started_at = ordered[0].at
    updated_at = ordered[-1].at
    reference = (
        finished.at if finished is not None else (now or datetime.now(timezone.utc))
    )
    elapsed = (reference - started_at).total_seconds()

    return RunStatus(
        running=running,
        outcome=(finished.payload.get("outcome") if finished is not None else None),
        detail=str(finished.payload.get("detail") or "") if finished is not None else "",
        current_stage=current,
        current_stage_label=_STAGE_LABELS[current] if current is not None else None,
        stages=stages,
        context=context,
        last_checkpoint=last_checkpoint,
        checkpoints_seen=len(checkpoints),
        started_at=started_at,
        updated_at=updated_at,
        # Clamped at zero rather than reported negative: a status page showing
        # "-3s elapsed" because the host clock stepped is worse than one
        # showing 0.
        elapsed_seconds=max(0.0, round(elapsed, 3)),
        budget_remaining=dict(budget_remaining or {}),
    )


def locate_run_journal(project_root: Path | str, task_id: str) -> Path | None:
    """The progress journal of this task's most recent sandbox attempt.

    A task accumulates one `CAP-*` directory per attempt, including attempts
    that died before writing anything (F14 in the review counted seventeen for
    one task). The newest one that actually has a journal is the run an
    operator means by "what is it doing"; picking the newest directory
    outright would show an empty page whenever the latest attempt aborted
    early.
    """

    sandbox_root = (
        Path(project_root) / ".apoapsis" / "tasks" / task_id / "capability-sandbox"
    )
    if not sandbox_root.is_dir():
        return None
    candidates = [
        path
        for path in sandbox_root.glob(f"*/evidence/{PROGRESS_FILENAME}")
        if path.is_file()
    ]
    if not candidates:
        return None
    # Ordered by the attempt directory's own name where possible, and by mtime
    # otherwise. `CAP-` ids are random rather than sequential, so mtime is the
    # only ordering the filesystem actually carries.
    return max(candidates, key=lambda path: path.stat().st_mtime)


def task_run_status(
    project_root: Path | str,
    task_id: str,
    *,
    context_window_tokens: int | None = None,
    budget_remaining: dict | None = None,
    now: datetime | None = None,
) -> RunStatus:
    """Read this task's journal from disk and project it.

    Returns a status with `running=False` and no stages when the task has
    never started a sandbox attempt, rather than raising: "this task is not
    running" is a legitimate answer to "what is it doing", and the UI polls
    this endpoint for tasks that may not have started yet.
    """

    journal = locate_run_journal(project_root, task_id)
    if journal is None:
        return RunStatus(
            running=False,
            stages=[],
            context=ContextPressure(context_window_tokens=context_window_tokens),
            budget_remaining=dict(budget_remaining or {}),
        )
    return project_run_status(
        read_progress(journal),
        context_window_tokens=context_window_tokens,
        budget_remaining=budget_remaining,
        now=now,
    )


__all__ = [
    "CheckpointSummary",
    "ContextPressure",
    "RunStatus",
    "StageProgress",
    "StageState",
    "locate_run_journal",
    "project_run_status",
    "task_run_status",
]
