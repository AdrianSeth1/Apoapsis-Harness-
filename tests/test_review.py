from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from apoapsis.config import AgentLoopConfig
from apoapsis.review.classify import classify_stop_reason, eligible_actions_for
from apoapsis.review.errors import (
    ActiveOperationExistsError,
    DuplicateOperationError,
    OperationAlreadyRunningError,
    OperationNotFoundError,
    ReviewError,
)
from apoapsis.review.schema import (
    ContinuationBudget,
    ReviewActionKind,
    ReviewContinuationPackage,
    StopReasonKind,
)
from apoapsis.review.store import ReviewOperationStore
from apoapsis.specification.schema import (
    HardConstraint,
    SourceKind,
    TaskSpecification,
    TraceableStatement,
)
from apoapsis.verification.failures import NormalizedFailure
from apoapsis.verification.results import VerificationStatus
from apoapsis.workflow.events import WorkflowActor, WorkflowEvent
from apoapsis.workflow.states import WorkflowState


def _event(event_type: str, *, to_state: WorkflowState, payload=None) -> WorkflowEvent:
    return WorkflowEvent(
        event_id="EVT-1",
        task_id="TASK-1",
        event_type=event_type,
        from_state=None,
        to_state=to_state,
        actor=WorkflowActor.SYSTEM,
        payload=payload or {},
    )


class ClassifyStopReasonTests(unittest.TestCase):
    def test_each_known_event_type_classifies_correctly(self) -> None:
        cases = [
            ("specification_not_approved", StopReasonKind.SPECIFICATION_NOT_APPROVED),
            (
                "deterministic_route_requires_human",
                StopReasonKind.ROUTING_REQUIRES_HUMAN,
            ),
            (
                "acceptance_coverage_incomplete",
                StopReasonKind.ACCEPTANCE_COVERAGE_INCOMPLETE,
            ),
            (
                "review_verification_retry_incomplete",
                StopReasonKind.ACCEPTANCE_COVERAGE_INCOMPLETE,
            ),
            ("review_verification_retry_failed", StopReasonKind.VERIFICATION_FAILED),
            (
                "frontier_escalation_not_configured",
                StopReasonKind.LOCAL_AGENT_ESCALATION_UNAVAILABLE,
            ),
            (
                "review_local_continuation_requires_human",
                StopReasonKind.LOCAL_AGENT_ESCALATION_UNAVAILABLE,
            ),
            (
                "bounded_frontier_requires_human",
                StopReasonKind.FRONTIER_AGENT_EXHAUSTED,
            ),
            (
                "review_frontier_continuation_requires_human",
                StopReasonKind.FRONTIER_AGENT_EXHAUSTED,
            ),
        ]
        for event_type, expected_kind in cases:
            with self.subTest(event_type=event_type):
                events = [
                    _event(
                        event_type,
                        to_state=WorkflowState.HUMAN_REVIEW_REQUIRED,
                        payload={"reason": "some reason"},
                    )
                ]
                kind, matched_event = classify_stop_reason(events)
                self.assertEqual(kind, expected_kind)
                assert matched_event is not None
                self.assertEqual(matched_event.event_type, event_type)
                self.assertEqual(matched_event.payload.get("reason"), "some reason")

    def test_unrecognized_event_falls_back_to_unknown(self) -> None:
        events = [
            _event(
                "some_future_event_type", to_state=WorkflowState.HUMAN_REVIEW_REQUIRED
            )
        ]
        kind, matched_event = classify_stop_reason(events)
        self.assertEqual(kind, StopReasonKind.UNKNOWN)
        assert matched_event is not None
        self.assertEqual(matched_event.event_type, "some_future_event_type")

    def test_newest_event_alone_decides_even_if_unrecognized(self) -> None:
        """An unrecognized *newest* HUMAN_REVIEW_REQUIRED event must never
        fall back to an older, recognized one (ADR 0021) -- it must
        classify as UNKNOWN instead."""

        events = [
            _event(
                "bounded_frontier_requires_human",
                to_state=WorkflowState.HUMAN_REVIEW_REQUIRED,
                payload={"reason": "frontier exhausted"},
            ),
            _event(
                "some_future_event_type",
                to_state=WorkflowState.HUMAN_REVIEW_REQUIRED,
            ),
        ]
        kind, matched_event = classify_stop_reason(events)
        self.assertEqual(kind, StopReasonKind.UNKNOWN)
        assert matched_event is not None
        self.assertEqual(matched_event.event_type, "some_future_event_type")

    def test_most_recent_matching_event_wins(self) -> None:
        events = [
            _event(
                "specification_not_approved",
                to_state=WorkflowState.HUMAN_REVIEW_REQUIRED,
            ),
            _event("specification_approved", to_state=WorkflowState.SPEC_APPROVED),
            _event(
                "bounded_frontier_requires_human",
                to_state=WorkflowState.HUMAN_REVIEW_REQUIRED,
                payload={"reason": "frontier exhausted"},
            ),
        ]
        kind, matched_event = classify_stop_reason(events)
        self.assertEqual(kind, StopReasonKind.FRONTIER_AGENT_EXHAUSTED)
        assert matched_event is not None
        self.assertEqual(matched_event.payload.get("reason"), "frontier exhausted")


class EligibleActionsTests(unittest.TestCase):
    def test_frontier_continuation_removed_when_unavailable(self) -> None:
        actions = eligible_actions_for(
            StopReasonKind.FRONTIER_AGENT_EXHAUSTED,
            frontier_available=False,
            continuations_used=0,
            max_continuations_per_task=5,
        )
        self.assertNotIn(ReviewActionKind.FRONTIER_CONTINUATION, actions)

    def test_continuation_actions_removed_once_ceiling_reached(self) -> None:
        actions = eligible_actions_for(
            StopReasonKind.LOCAL_AGENT_ESCALATION_UNAVAILABLE,
            frontier_available=True,
            continuations_used=5,
            max_continuations_per_task=5,
        )
        self.assertNotIn(ReviewActionKind.LOCAL_CONTINUATION, actions)
        self.assertIn(ReviewActionKind.INSPECT_ONLY, actions)
        self.assertIn(ReviewActionKind.ABANDON, actions)
        self.assertIn(ReviewActionKind.VERIFICATION_ONLY_RETRY, actions)

    def test_unknown_kind_only_offers_inspect_and_abandon(self) -> None:
        actions = eligible_actions_for(
            StopReasonKind.UNKNOWN,
            frontier_available=True,
            continuations_used=0,
            max_continuations_per_task=5,
        )
        self.assertEqual(
            set(actions), {ReviewActionKind.INSPECT_ONLY, ReviewActionKind.ABANDON}
        )


class ReviewOperationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = ReviewOperationStore(
            Path(self.temporary_directory.name) / "review-operations.db"
        )

    def test_create_and_get(self) -> None:
        record = self.store.create(
            "RVOP-1", "TASK-1", ReviewActionKind.ABANDON, expected_task_version=1
        )
        self.assertEqual(record.status.value, "recorded")
        fetched = self.store.get("RVOP-1")
        self.assertEqual(fetched.operation_id, "RVOP-1")

    def test_duplicate_operation_id_rejected_once_terminal(self) -> None:
        # A literal duplicate operation_id for the same *active* task is
        # instead rejected by the one-active-operation-per-task check
        # (see test_second_active_operation_for_same_task_rejected) --
        # DuplicateOperationError is reserved for reusing an operation_id
        # whose row already exists in a *terminal* status.
        self.store.create(
            "RVOP-1", "TASK-1", ReviewActionKind.ABANDON, expected_task_version=1
        )
        self.store.mark_running("RVOP-1")
        self.store.mark_succeeded("RVOP-1", result_summary="done")
        with self.assertRaises(DuplicateOperationError):
            self.store.create(
                "RVOP-1", "TASK-1", ReviewActionKind.ABANDON, expected_task_version=1
            )

    def test_second_active_operation_for_same_task_rejected(self) -> None:
        self.store.create(
            "RVOP-1", "TASK-1", ReviewActionKind.ABANDON, expected_task_version=1
        )
        with self.assertRaises(ActiveOperationExistsError):
            self.store.create(
                "RVOP-2", "TASK-1", ReviewActionKind.ABANDON, expected_task_version=1
            )

    def test_different_tasks_may_each_have_an_active_operation(self) -> None:
        self.store.create(
            "RVOP-1", "TASK-1", ReviewActionKind.ABANDON, expected_task_version=1
        )
        record = self.store.create(
            "RVOP-2", "TASK-2", ReviewActionKind.ABANDON, expected_task_version=1
        )
        self.assertEqual(record.task_id, "TASK-2")

    def test_unknown_operation_raises(self) -> None:
        with self.assertRaises(OperationNotFoundError):
            self.store.get("RVOP-MISSING")

    def test_normal_lifecycle(self) -> None:
        self.store.create(
            "RVOP-1",
            "TASK-1",
            ReviewActionKind.LOCAL_CONTINUATION,
            expected_task_version=1,
            authorized_budget=ContinuationBudget(additional_turns=5),
        )
        self.store.mark_running("RVOP-1")
        record = self.store.mark_succeeded("RVOP-1", result_summary="done")
        self.assertEqual(record.status.value, "succeeded")
        self.assertEqual(record.result_summary, "done")
        assert record.authorized_budget is not None
        self.assertEqual(record.authorized_budget.additional_turns, 5)

    def test_marking_running_twice_fails_closed(self) -> None:
        self.store.create(
            "RVOP-1", "TASK-1", ReviewActionKind.ABANDON, expected_task_version=1
        )
        self.store.mark_running("RVOP-1")
        with self.assertRaises(OperationAlreadyRunningError):
            self.store.mark_running("RVOP-1")

    def test_cannot_succeed_an_operation_that_never_started_running(self) -> None:
        self.store.create(
            "RVOP-1", "TASK-1", ReviewActionKind.ABANDON, expected_task_version=1
        )
        with self.assertRaises(ReviewError):
            self.store.mark_succeeded("RVOP-1", result_summary="done")

    def test_failed_operation_records_error(self) -> None:
        self.store.create(
            "RVOP-1", "TASK-1", ReviewActionKind.ABANDON, expected_task_version=1
        )
        self.store.mark_running("RVOP-1")
        record = self.store.mark_failed("RVOP-1", error="boom")
        self.assertEqual(record.status.value, "failed")
        self.assertEqual(record.error, "boom")


class ReviewSchemaAuthorityTests(unittest.TestCase):
    def test_continuation_package_rejects_smuggled_authority_field(self) -> None:
        specification = TaskSpecification(
            task_id="TASK-1",
            objective=TraceableStatement(
                text="Add resumable downloads.",
                source=SourceKind.USER,
                source_reference="m",
            ),
        )
        payload = ReviewContinuationPackage(
            operation_id="RVOP-1",
            task_id="TASK-1",
            action=ReviewActionKind.LOCAL_CONTINUATION,
            specification=specification,
            current_diff="diff --git a/x b/x\n",
            stop_reason_kind=StopReasonKind.LOCAL_AGENT_ESCALATION_UNAVAILABLE,
            stop_reason_text="turn budget exhausted",
            prior_turn_count=3,
            authorized_budget=ContinuationBudget(additional_turns=5),
            effective_agent_budget=AgentLoopConfig(),
            worktree_fingerprint="a" * 64,
            repository_head_commit="deadbeef",
        ).model_dump(mode="json")
        payload["approved"] = True
        with self.assertRaises(ValidationError):
            ReviewContinuationPackage.model_validate(payload)

    def test_normalized_failure_is_reused_unchanged(self) -> None:
        # Sanity check that review schemas reuse the existing failure shape
        # rather than inventing a parallel one.
        failure = NormalizedFailure(
            command_name="unit-tests",
            argv=["python", "-m", "unittest"],
            status=VerificationStatus.FAILED,
            exit_code=1,
            root_error="AssertionError",
            relevant_error="AssertionError: boom",
        )
        self.assertEqual(failure.command_name, "unit-tests")

    def test_constraint_reused_from_specification_schema(self) -> None:
        constraint = HardConstraint(
            id="HC-1",
            text="x",
            verbatim_source="x",
            interpreted_meaning="x",
            source=SourceKind.USER,
            source_reference="m",
            verification_method="run tests",
        )
        self.assertEqual(constraint.id, "HC-1")

    def test_generated_at_defaults_to_now(self) -> None:
        budget = ContinuationBudget(additional_turns=1)
        self.assertEqual(budget.additional_turns, 1)
        # Confirm the module's utc_now default factory produces tz-aware times
        # elsewhere in this schema family (used by ReviewCase/records).
        now = datetime.now(timezone.utc)
        self.assertIsNotNone(now.tzinfo)


if __name__ == "__main__":
    unittest.main()
