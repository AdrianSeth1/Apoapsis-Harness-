"""An append-only record of what a run is doing, written while it does it.

Every other artifact this package writes is a *result*: `checkpoint.json` after
a checkpoint, `model-usage-series.json` after the arm finishes, `result.json`
at the end. They are the right shape for an audit and the wrong shape for a
person watching a run, because none of them exists yet at the moment the
question "what is it doing right now?" is asked.

So this is deliberately the one artifact written *during* the work. It is a
journal, not a state file: events are appended and never rewritten, so a reader
that arrives late reconstructs the whole run, a reader that arrives mid-run
sees everything up to now, and a run that dies leaves its own last known
position behind instead of a stale "running" flag nobody cleared.

Two properties matter more than they look:

**Reads happen mid-write.** A polled status endpoint will routinely read this
file while a line is half-flushed. `read_progress` therefore discards a
trailing partial line rather than raising -- a torn last line is the normal
case, not corruption.

**Writing must never break the run.** A journal is an observability aid. If
the disk is full or the path is gone, the correct behaviour is to lose the
journal and finish the slice, not to fail a slice that was otherwise going to
succeed. Every write here is best-effort for that reason, and the projection
side treats absent events as "not observed" rather than "did not happen".
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterator

from pydantic import Field

from apoapsis.specification.schema import StrictModel

#: The journal's filename wherever a run writes one. Fixed rather than
#: configurable: the status projection has to find it without being told, and
#: a run that renamed it would simply be invisible.
PROGRESS_FILENAME = "progress.jsonl"


class RunStage(StrEnum):
    """The pipeline an operator is actually watching, in execution order.

    Named for what the operator sees rather than for the functions that
    implement it: `CONTROLLER_BUILD` is "building the sandbox image", not
    `docker build -f docker/controller.Dockerfile`. The order here is the
    order they are displayed in, and `RunStage.sequence()` is the only place
    that ordering is stated.
    """

    CONTROLLER_BUILD = "controller_build"
    PREFLIGHT = "preflight"
    MODEL_LOADING = "model_loading"
    CONTROL_ARM = "control_arm"
    MODEL_RUNNING = "model_running"
    CHECKPOINT = "checkpoint"
    VERIFICATION = "verification"

    @classmethod
    def sequence(cls) -> tuple["RunStage", ...]:
        return (
            cls.CONTROLLER_BUILD,
            cls.PREFLIGHT,
            cls.MODEL_LOADING,
            cls.CONTROL_ARM,
            cls.MODEL_RUNNING,
            cls.CHECKPOINT,
            cls.VERIFICATION,
        )


class ProgressEventKind(StrEnum):
    #: Written once, first, carrying the facts a reader needs to interpret
    #: everything after it -- most importantly the context window the agent
    #: actually runs against. Recorded rather than looked up later so the
    #: percentage a status page shows is measured against the window this run
    #: really used, not against whatever the configuration says today.
    RUN_STARTED = "run_started"
    STAGE_ENTERED = "stage_entered"
    STAGE_LEFT = "stage_left"
    #: One model exchange the relay observed, carrying its usage. Emitted per
    #: call rather than summed at the end, because "is context near the
    #: window?" is a question about the *latest* call and cannot be answered
    #: by a total.
    MODEL_CALL = "model_call"
    CHECKPOINT_VERDICT = "checkpoint_verdict"
    RUN_FINISHED = "run_finished"


class ProgressEvent(StrictModel):
    """One appended line.

    `sequence` is assigned by the writer and is the ordering of record: wall
    clock is reported but never ordered on, because two events inside the same
    millisecond are common and a clock that steps backwards should not be able
    to reorder a run's history.
    """

    sequence: int = Field(ge=1)
    at: datetime
    kind: ProgressEventKind
    stage: RunStage | None = None
    #: Free-form per-kind detail. Deliberately not a union of typed payloads:
    #: this is a diagnostic journal whose consumers must tolerate events they
    #: do not recognise, including ones written by a newer version.
    payload: dict = Field(default_factory=dict)


class ProgressJournal:
    """Appends events for one run. Never raises into the run it observes."""

    def __init__(self, path: Path | str, *, clock=None) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = time.monotonic
        self._disabled = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._disabled = True
        # Resume rather than restart. A run has two writers: the launch script
        # records the controller-image build (which happens on the host,
        # before this process exists), and the controller records everything
        # after. Starting at 1 here would give two events the same sequence
        # and let the projection order them arbitrarily -- and sequence is
        # exactly what the projection orders on, precisely so it does not have
        # to trust a clock shared across a container boundary.
        self._sequence = max(
            (item.sequence for item in read_progress(self.path)), default=0
        )

    def _append(
        self,
        kind: ProgressEventKind,
        *,
        stage: RunStage | None = None,
        payload: dict | None = None,
    ) -> None:
        if self._disabled:
            return
        self._sequence += 1
        event = ProgressEvent(
            sequence=self._sequence,
            at=self._clock(),
            kind=kind,
            stage=stage,
            payload=payload or {},
        )
        line = json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n"
        try:
            # Opened and closed per event rather than held: a run can be killed
            # at any moment, and an fd holding buffered lines loses them. One
            # append per event is a few microseconds against a slice that takes
            # minutes, and it means the journal on disk is never behind.
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # Losing the journal must never lose the slice. One failure
            # disables it for the rest of the run rather than retrying per
            # event on a disk that is not coming back.
            self._disabled = True

    def started(
        self,
        *,
        run_id: str | None = None,
        slice_id: str | None = None,
        context_window_tokens: int | None = None,
        parity_arm_expected: bool | None = None,
    ) -> None:
        self._append(
            ProgressEventKind.RUN_STARTED,
            payload={
                "run_id": run_id,
                "slice_id": slice_id,
                "context_window_tokens": context_window_tokens,
                "parity_arm_expected": parity_arm_expected,
            },
        )

    @contextmanager
    def stage(self, stage: RunStage, **detail) -> Iterator[None]:
        """Bracket one pipeline stage.

        Leaves a `STAGE_LEFT` even when the body raises, and records that it
        raised. A stage that is entered and never left is exactly how a
        crashed run should look -- the projection reports it as still running,
        which is what was true at the moment the process died.
        """

        self._append(ProgressEventKind.STAGE_ENTERED, stage=stage, payload=dict(detail))
        started = self._monotonic()
        failed: str | None = None
        try:
            yield
        except BaseException as exc:  # noqa: BLE001 - re-raised immediately
            failed = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._append(
                ProgressEventKind.STAGE_LEFT,
                stage=stage,
                payload={
                    "elapsed_seconds": round(self._monotonic() - started, 3),
                    **({"failed": failed} if failed is not None else {}),
                },
            )

    def model_call(
        self,
        *,
        call: int,
        input_tokens: int | None,
        output_tokens: int | None,
        cached_input_tokens: int | None = None,
        arm: str | None = None,
    ) -> None:
        self._append(
            ProgressEventKind.MODEL_CALL,
            stage=RunStage.MODEL_RUNNING,
            payload={
                "call": call,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": cached_input_tokens,
                "arm": arm,
            },
        )

    def checkpoint_verdict(
        self,
        *,
        attempt: int,
        outcome: str,
        detail: str,
        operator: dict | None = None,
        obligations_proved: int | None = None,
        obligations_total: int | None = None,
    ) -> None:
        self._append(
            ProgressEventKind.CHECKPOINT_VERDICT,
            stage=RunStage.CHECKPOINT,
            payload={
                "attempt": attempt,
                "outcome": outcome,
                "detail": detail,
                # Carried verbatim from the verdict so the status view can show
                # ADR 0105's three-part rendering without re-deriving it, and
                # without the UI needing to know checkpoint vocabulary at all.
                "operator": operator,
                "obligations_proved": obligations_proved,
                "obligations_total": obligations_total,
            },
        )

    def finished(self, *, outcome: str, detail: str = "") -> None:
        self._append(
            ProgressEventKind.RUN_FINISHED,
            payload={"outcome": outcome, "detail": detail},
        )


def read_progress(path: Path | str) -> list[ProgressEvent]:
    """Read a journal, tolerating the writer being mid-append.

    Three things are skipped rather than raised on, because all three are
    normal for a file being written concurrently: a torn final line, a line
    that is not JSON, and an event whose shape this version does not
    understand. A journal is evidence about a run, not a contract with it; a
    status page that crashed because a run was writing would be worse than
    useless.
    """

    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return []
    events: list[ProgressEvent] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(ProgressEvent.model_validate_json(stripped))
        except (ValueError, TypeError):
            continue
    events.sort(key=lambda item: item.sequence)
    return events


__all__ = [
    "PROGRESS_FILENAME",
    "ProgressEvent",
    "ProgressEventKind",
    "ProgressJournal",
    "RunStage",
    "read_progress",
]
