"""MH-9: the progress journal, and the status projected from it.

The projection is a pure function of recorded events, so everything here runs
from fixture journals -- no container, no model, no clock to mock into
agreeing. That is the property under test as much as any single assertion: if
answering "what is it doing right now" needed a live run to verify, it could
not be verified at all.

The journal's own tests are about the two things that are easy to get wrong and
invisible when you do: a reader arriving mid-write, and a writer whose failure
must not take the run down with it.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apoapsis.reporting.run_status import (
    StageState,
    locate_run_journal,
    project_run_status,
    task_run_status,
)
from apoapsis.workcell.progress import (
    PROGRESS_FILENAME,
    ProgressEvent,
    ProgressEventKind,
    ProgressJournal,
    RunStage,
    read_progress,
)

START = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


def clock_from(start: datetime = START):
    """A deterministic clock advancing one second per call."""

    state = {"n": 0}

    def clock() -> datetime:
        state["n"] += 1
        return start + timedelta(seconds=state["n"])

    return clock


class ProgressJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.path = self.root / "evidence" / PROGRESS_FILENAME

    def test_a_journal_records_a_whole_run_in_order(self) -> None:
        journal = ProgressJournal(self.path, clock=clock_from())
        journal.started(run_id="CAP-1", context_window_tokens=65_536)
        with journal.stage(RunStage.PREFLIGHT):
            pass
        with journal.stage(RunStage.MODEL_RUNNING):
            journal.model_call(call=1, input_tokens=1_000, output_tokens=50)
            journal.model_call(call=2, input_tokens=2_000, output_tokens=60)
        journal.checkpoint_verdict(attempt=1, outcome="continue", detail="not ready")
        journal.finished(outcome="complete", detail="done")

        events = read_progress(self.path)
        self.assertEqual([item.sequence for item in events], list(range(1, 10)))
        self.assertEqual(events[0].kind, ProgressEventKind.RUN_STARTED)
        self.assertEqual(events[-1].kind, ProgressEventKind.RUN_FINISHED)

    def test_a_stage_that_raises_is_still_closed_and_says_so(self) -> None:
        journal = ProgressJournal(self.path, clock=clock_from())
        with self.assertRaises(RuntimeError):
            with journal.stage(RunStage.PREFLIGHT):
                raise RuntimeError("the seal probe failed")

        left = [
            item
            for item in read_progress(self.path)
            if item.kind is ProgressEventKind.STAGE_LEFT
        ]
        self.assertEqual(len(left), 1)
        self.assertIn("the seal probe failed", left[0].payload["failed"])

    def test_a_torn_final_line_is_discarded_not_raised_on(self) -> None:
        """The normal case for a file being appended to while it is read."""

        journal = ProgressJournal(self.path, clock=clock_from())
        journal.started(run_id="CAP-1")
        with journal.stage(RunStage.PREFLIGHT):
            pass
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"sequence": 99, "kind": "stage_ent')

        events = read_progress(self.path)
        self.assertEqual([item.sequence for item in events], [1, 2, 3])

    def test_a_journal_that_cannot_be_written_never_raises_into_the_run(self) -> None:
        """Observability must not be able to fail a slice.

        The path is a *file* here, so creating the parent directory fails and
        every subsequent append fails too. The run must proceed regardless.
        """

        blocker = self.root / "not-a-directory"
        blocker.write_text("", encoding="utf-8")
        journal = ProgressJournal(blocker / "sub" / PROGRESS_FILENAME)

        journal.started(run_id="CAP-1")
        with journal.stage(RunStage.MODEL_RUNNING):
            journal.model_call(call=1, input_tokens=10, output_tokens=1)
        journal.finished(outcome="complete")

        self.assertEqual(read_progress(blocker / "sub" / PROGRESS_FILENAME), [])

    def test_reading_a_journal_that_does_not_exist_is_empty_not_an_error(self) -> None:
        self.assertEqual(read_progress(self.root / "nope.jsonl"), [])


class RunStatusProjectionTests(unittest.TestCase):
    """Every case is built from events, exactly as a real journal records them."""

    def journal(self, root: Path) -> ProgressJournal:
        return ProgressJournal(root / PROGRESS_FILENAME, clock=clock_from())

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def events(self) -> list[ProgressEvent]:
        return read_progress(self.root / PROGRESS_FILENAME)

    def test_no_events_yet_reads_as_starting_rather_than_as_no_data(self) -> None:
        status = project_run_status([], journal_exists=True)
        self.assertTrue(status.running)
        self.assertTrue(status.awaiting_first_event)
        # Every stage is listed so the page can show the shape of the run
        # before any of it has happened.
        self.assertEqual(len(status.stages), len(RunStage.sequence()))
        self.assertTrue(all(s.state is StageState.PENDING for s in status.stages))

    def test_no_journal_at_all_is_not_running(self) -> None:
        status = project_run_status([], journal_exists=False)
        self.assertFalse(status.running)
        self.assertFalse(status.awaiting_first_event)

    def test_the_stage_in_flight_is_the_one_entered_and_not_left(self) -> None:
        journal = self.journal(self.root)
        journal.started(run_id="CAP-1", context_window_tokens=65_536)
        with journal.stage(RunStage.PREFLIGHT):
            pass
        journal._append(ProgressEventKind.STAGE_ENTERED, stage=RunStage.MODEL_RUNNING)

        status = project_run_status(self.events())
        self.assertEqual(status.current_stage, RunStage.MODEL_RUNNING)
        self.assertEqual(status.current_stage_label, "Writing code")
        by_stage = {item.stage: item for item in status.stages}
        self.assertEqual(by_stage[RunStage.PREFLIGHT].state, StageState.DONE)
        self.assertEqual(by_stage[RunStage.MODEL_RUNNING].state, StageState.RUNNING)
        self.assertEqual(by_stage[RunStage.CHECKPOINT].state, StageState.PENDING)

    def test_a_run_that_died_mid_stage_still_reports_where_it_was(self) -> None:
        """No RUN_FINISHED and an unclosed stage: exactly a killed process.

        The projection reports it as still running because that is what the
        evidence says. Deciding it "probably died" would be the projection
        inventing a fact the run never recorded.
        """

        journal = self.journal(self.root)
        journal.started(run_id="CAP-1")
        journal._append(ProgressEventKind.STAGE_ENTERED, stage=RunStage.MODEL_LOADING)

        status = project_run_status(self.events())
        self.assertTrue(status.running)
        self.assertEqual(status.current_stage, RunStage.MODEL_LOADING)
        self.assertIsNone(status.outcome)

    def test_context_pressure_reports_the_latest_call_against_the_window(self) -> None:
        journal = self.journal(self.root)
        journal.started(run_id="CAP-1", context_window_tokens=10_000)
        with journal.stage(RunStage.MODEL_RUNNING):
            journal.model_call(call=1, input_tokens=1_000, output_tokens=10)
            journal.model_call(call=2, input_tokens=9_000, output_tokens=20)
            journal.model_call(call=3, input_tokens=2_000, output_tokens=30)

        context = project_run_status(self.events()).context
        self.assertEqual(context.calls, 3)
        # Peak and last are both reported because they answer different
        # questions: "did this slice ever come close" and "is it close now".
        # A slice that compacted has a high peak and a low last, and one
        # number alone would hide the compaction.
        self.assertEqual(context.peak_input_tokens, 9_000)
        self.assertEqual(context.last_input_tokens, 2_000)
        self.assertEqual(context.total_input_tokens, 12_000)
        self.assertEqual(context.total_output_tokens, 60)
        self.assertEqual(context.window_utilization, 0.2)
        self.assertFalse(context.near_window)

    def test_the_recorded_window_beats_whatever_the_caller_supplies(self) -> None:
        """The run knows what it ran against; the caller knows today's config."""

        journal = self.journal(self.root)
        journal.started(run_id="CAP-1", context_window_tokens=65_536)
        with journal.stage(RunStage.MODEL_RUNNING):
            journal.model_call(call=1, input_tokens=32_768, output_tokens=10)

        context = project_run_status(
            self.events(), context_window_tokens=4_096
        ).context
        self.assertEqual(context.context_window_tokens, 65_536)
        self.assertEqual(context.window_utilization, 0.5)

    def test_no_window_anywhere_means_no_percentage_rather_than_a_guess(self) -> None:
        journal = self.journal(self.root)
        journal.started(run_id="CAP-1")
        with journal.stage(RunStage.MODEL_RUNNING):
            journal.model_call(call=1, input_tokens=5_000, output_tokens=10)

        context = project_run_status(self.events()).context
        self.assertEqual(context.last_input_tokens, 5_000)
        self.assertIsNone(context.context_window_tokens)
        self.assertIsNone(context.window_utilization)
        self.assertFalse(context.near_window)

    def test_a_call_without_usage_is_counted_but_not_summed(self) -> None:
        journal = self.journal(self.root)
        journal.started(run_id="CAP-1", context_window_tokens=1_000)
        with journal.stage(RunStage.MODEL_RUNNING):
            journal.model_call(call=1, input_tokens=100, output_tokens=5)
            journal.model_call(call=2, input_tokens=None, output_tokens=None)

        context = project_run_status(self.events()).context
        self.assertEqual(context.calls, 2)
        self.assertEqual(context.total_input_tokens, 100)
        self.assertEqual(context.last_input_tokens, 100)

    def test_near_window_fires_at_eighty_percent(self) -> None:
        journal = self.journal(self.root)
        journal.started(run_id="CAP-1", context_window_tokens=1_000)
        with journal.stage(RunStage.MODEL_RUNNING):
            journal.model_call(call=1, input_tokens=800, output_tokens=1)

        self.assertTrue(project_run_status(self.events()).context.near_window)

    def test_the_last_checkpoint_carries_its_operator_rendering(self) -> None:
        """MH-4's three-part explanation reaches the status view unchanged.

        The UI must not have to know checkpoint vocabulary to render a stop,
        which is the entire point of ADR 0105 -- so the rendering travels with
        the verdict rather than being rebuilt from the outcome name.
        """

        journal = self.journal(self.root)
        journal.started(run_id="CAP-1")
        journal.checkpoint_verdict(
            attempt=1,
            outcome="continue",
            detail="no current-state witness proves index.html is reached",
            operator={
                "attempted": "Run slice SLICE-2.",
                "refusal": "The completion check refused it: nothing proves the new file runs.",
                "next_action": "Retry the slice.",
                "detail": "no current-state witness proves index.html is reached",
            },
            obligations_proved=3,
            obligations_total=7,
        )
        journal.checkpoint_verdict(
            attempt=2,
            outcome="complete",
            detail="every obligation discharged",
            operator={
                "attempted": "Run slice SLICE-2.",
                "refusal": "Nothing refused it.",
                "next_action": "Review the delivered work.",
            },
            obligations_proved=7,
            obligations_total=7,
        )

        status = project_run_status(self.events())
        self.assertEqual(status.checkpoints_seen, 2)
        assert status.last_checkpoint is not None
        self.assertEqual(status.last_checkpoint.attempt, 2)
        self.assertEqual(status.last_checkpoint.outcome, "complete")
        self.assertEqual(status.last_checkpoint.obligations_proved, 7)
        assert status.last_checkpoint.operator is not None
        self.assertEqual(
            status.last_checkpoint.operator.next_action, "Review the delivered work."
        )

    def test_a_malformed_operator_rendering_does_not_lose_the_checkpoint(self) -> None:
        journal = self.journal(self.root)
        journal.started(run_id="CAP-1")
        journal.checkpoint_verdict(
            attempt=1,
            outcome="human_review_required",
            detail="stalled",
            operator={"attempted": "", "nonsense": True},
        )

        status = project_run_status(self.events())
        assert status.last_checkpoint is not None
        self.assertEqual(status.last_checkpoint.outcome, "human_review_required")
        self.assertIsNone(status.last_checkpoint.operator)

    def test_an_unpaired_slice_says_why_its_control_arm_is_absent(self) -> None:
        """"No control arm ran" is not evidence; "and here is why" is (ADR 0108)."""

        journal = self.journal(self.root)
        journal.started(run_id="CAP-1", parity_arm_expected=False)
        with journal.stage(RunStage.MODEL_RUNNING):
            pass

        by_stage = {item.stage: item for item in project_run_status(self.events()).stages}
        control = by_stage[RunStage.CONTROL_ARM]
        self.assertEqual(control.state, StageState.SKIPPED)
        self.assertIn("parity policy", control.detail or "")

    def test_a_failed_stage_is_failed_not_merely_done(self) -> None:
        journal = self.journal(self.root)
        journal.started(run_id="CAP-1")
        with self.assertRaises(RuntimeError):
            with journal.stage(RunStage.PREFLIGHT):
                raise RuntimeError("containment probe reached the network")

        by_stage = {item.stage: item for item in project_run_status(self.events()).stages}
        self.assertEqual(by_stage[RunStage.PREFLIGHT].state, StageState.FAILED)
        self.assertIn("containment probe", by_stage[RunStage.PREFLIGHT].detail or "")

    def test_a_finished_run_reports_its_outcome_and_stops_running(self) -> None:
        journal = self.journal(self.root)
        journal.started(run_id="CAP-1")
        with journal.stage(RunStage.PREFLIGHT):
            pass
        journal.finished(outcome="human_review_required", detail="the stall guard fired")

        status = project_run_status(self.events())
        self.assertFalse(status.running)
        self.assertEqual(status.outcome, "human_review_required")
        self.assertEqual(status.detail, "the stall guard fired")
        # Stages never reached are SKIPPED once the run is over: they
        # demonstrably did not happen, which is different from "not yet".
        by_stage = {item.stage: item for item in status.stages}
        self.assertEqual(by_stage[RunStage.VERIFICATION].state, StageState.SKIPPED)

    def test_elapsed_is_never_negative_when_the_host_clock_steps_back(self) -> None:
        backwards = [
            ProgressEvent(
                sequence=1,
                at=START,
                kind=ProgressEventKind.RUN_STARTED,
                payload={},
            )
        ]
        status = project_run_status(
            backwards, now=START - timedelta(seconds=30)
        )
        self.assertEqual(status.elapsed_seconds, 0.0)

    def test_stage_duration_comes_from_the_writers_own_measurement(self) -> None:
        """Not from subtracting timestamps, so a clock step cannot invent one."""

        journal = self.journal(self.root)
        journal.started(run_id="CAP-1")
        with journal.stage(RunStage.PREFLIGHT):
            pass

        by_stage = {item.stage: item for item in project_run_status(self.events()).stages}
        elapsed = by_stage[RunStage.PREFLIGHT].elapsed_seconds
        self.assertIsNotNone(elapsed)
        self.assertGreaterEqual(elapsed, 0.0)


class TaskRunStatusOnDiskTests(unittest.TestCase):
    """The locator half: finding the journal a task's latest attempt wrote."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.task_id = "TASK-ABC123"
        self.sandbox = (
            self.root / ".apoapsis" / "tasks" / self.task_id / "capability-sandbox"
        )

    def write_attempt(self, run_id: str, *, journal: bool, mtime: float) -> Path:
        evidence = self.sandbox / run_id / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        path = evidence / PROGRESS_FILENAME
        if journal:
            payload = {
                "sequence": 1,
                "at": START.isoformat(),
                "kind": "run_started",
                "stage": None,
                "payload": {"run_id": run_id, "context_window_tokens": 65_536},
            }
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            import os

            os.utime(path, (mtime, mtime))
        return path

    def test_a_task_that_never_ran_is_reported_as_not_running(self) -> None:
        status = task_run_status(self.root, self.task_id)
        self.assertFalse(status.running)
        self.assertEqual(status.stages, [])
        self.assertIsNone(locate_run_journal(self.root, self.task_id))

    def test_the_newest_attempt_with_a_journal_wins(self) -> None:
        """Aborted attempts leave directories with no journal (F14).

        Seventeen `CAP-*` directories for five executions was the observed
        case. Picking the newest *directory* would show an empty page whenever
        the most recent attempt died before writing anything.
        """

        self.write_attempt("CAP-OLD", journal=True, mtime=1_000.0)
        self.write_attempt("CAP-NEW", journal=True, mtime=2_000.0)
        self.write_attempt("CAP-ABORTED", journal=False, mtime=3_000.0)

        located = locate_run_journal(self.root, self.task_id)
        assert located is not None
        self.assertIn("CAP-NEW", str(located))

        status = task_run_status(self.root, self.task_id)
        self.assertTrue(status.running)
        self.assertEqual(status.context.context_window_tokens, 65_536)


class RunStatusRouteTests(unittest.TestCase):
    """The route's own hazard: it lives under the task-detail prefix.

    `/api/tasks/<id>` matches everything beginning with that prefix, so a
    naive ordering makes `/api/tasks/<id>/run-status` return a task detail for
    a task whose id ends in "/run-status" -- a 404 that reads like a missing
    task rather than a routing bug.
    """

    def test_the_run_status_suffix_is_matched_before_the_task_detail_prefix(
        self,
    ) -> None:
        import inspect

        from apoapsis.ui import server as server_module

        source = inspect.getsource(
            server_module.ApoapsisUIRequestHandler._handle_api_get
        )
        run_status_at = source.index('endswith("/run-status")')
        task_detail_at = source.index('path.startswith("/api/tasks/"):\n')
        self.assertLess(
            run_status_at,
            task_detail_at,
            "the /run-status route must be tested before the task-detail prefix",
        )

    def test_the_service_answers_for_a_task_that_never_ran(self) -> None:
        """The UI polls this for tasks that may not have started."""

        from apoapsis.ui.application import ApoapsisUIService

        root = Path(tempfile.mkdtemp())
        service = ApoapsisUIService(root)
        payload = service.task_run_status("TASK-NEVER-RAN")
        self.assertFalse(payload["running"])
        self.assertEqual(payload["stages"], [])


if __name__ == "__main__":
    unittest.main()
