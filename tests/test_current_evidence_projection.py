"""Deterministic coverage for the current-evidence projection (ADR 0072).

Every case here is built from persisted state and on-disk artifacts only:
no provider, no fake model, no network. The projection's whole job is to
answer "what is true about this task *now*" from exactly those inputs, so
driving it through a full agent run would test the agent, not the
projection.

The shape under test is Crisis Atlas's (`PLAN-E1B90639E58D`, 2026-07-29):
a task whose `report.json` records a failed first stop and whose persisted
state, after a later repair, is `COMPLETE`.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from apoapsis.agent.session import AgentSessionOutcome, AgentSessionResult
from apoapsis.reporting.current_state import (
    EvidenceGeneration,
    EvidenceIntegrity,
    coverage_from_verification_result,
    project_current_task_evidence,
)
from apoapsis.reporting.report import FinalTaskReport, TaskOutcome
from apoapsis.specification.schema import (
    AcceptanceCriterion,
    SourceKind,
    TaskSpecification,
    TraceableStatement,
    utc_now,
)
from apoapsis.verification.results import (
    VerificationCommandResult,
    VerificationResult,
    VerificationStatus,
)
from apoapsis.workflow.acceptance import AcceptanceCoverageStatus
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState

TASK_ID = "TASK-CURRENTSTATE01"


def make_specification(
    task_id: str = TASK_ID,
    *,
    verification_method: str | None = None,
) -> TaskSpecification:
    criteria = []
    if verification_method is not None:
        criteria.append(
            AcceptanceCriterion(
                id="AC-1",
                text="The incident list round-trips through the HTTP API.",
                source=SourceKind.USER,
                source_reference="message-1",
                verification_method=verification_method,
            )
        )
    return TaskSpecification(
        task_id=task_id,
        objective=TraceableStatement(
            text="Serve the dashboard from the local API.",
            source=SourceKind.USER,
            source_reference="message-1",
        ),
        acceptance_criteria=criteria,
    )


def command_result(
    name: str,
    status: VerificationStatus,
    *,
    acceptance: bool = False,
    required: bool = True,
    exit_code: int | None = 0,
) -> VerificationCommandResult:
    return VerificationCommandResult(
        name=name,
        category="test",
        argv=["python", "-m", "unittest"],
        required=required,
        acceptance=acceptance,
        cwd=".",
        status=status,
        exit_code=exit_code,
        duration_seconds=0.1,
    )


def verification_result(
    status: VerificationStatus,
    *,
    task_id: str = TASK_ID,
    commands: list[VerificationCommandResult] | None = None,
) -> VerificationResult:
    started = utc_now()
    if commands is None:
        commands = [
            command_result(
                "unit-tests",
                status,
                exit_code=0 if status == VerificationStatus.PASSED else 1,
            )
        ]
    return VerificationResult(
        task_id=task_id,
        status=status,
        commands=commands,
        started_at=started,
        finished_at=started + timedelta(seconds=1),
        duration_seconds=1.0,
    )


def session_result(
    outcome: AgentSessionOutcome,
    results: list[VerificationResult],
    *,
    stop_reason: str = "session finished",
) -> AgentSessionResult:
    return AgentSessionResult(
        outcome=outcome,
        stop_reason=stop_reason,
        turns=1,
        patch_attempts=1,
        verification_runs=len(results),
        verification_results=results,
    )


class ProjectionTestsBase(unittest.TestCase):
    """A task directory and store with no execution machinery attached.

    `_advance` walks the real `SQLiteTaskStore.transition` API rather than
    writing rows directly, so every fixture below is a state sequence the
    workflow engine would actually permit.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.metadata = self.root / ".apoapsis"
        self.task_directory = self.metadata / "tasks" / TASK_ID
        self.task_directory.mkdir(parents=True)
        self.store = SQLiteTaskStore(self.metadata / "apoapsis.db")

    def create_task(self, specification: TaskSpecification | None = None) -> None:
        self.store.create_task(specification or make_specification())

    def _advance(self, target: WorkflowState, event_type: str, **payload) -> None:
        self.store.transition(
            TASK_ID,
            target,
            actor=WorkflowActor.SYSTEM,
            event_type=event_type,
            payload=payload,
        )

    def reach_implementing(self) -> None:
        self._advance(WorkflowState.SPEC_DRAFTED, "specification_drafted")
        self._advance(WorkflowState.SPEC_APPROVED, "specification_approved")
        self._advance(WorkflowState.REPOSITORY_ANALYZED, "repository_analyzed")
        self._advance(WorkflowState.CONTEXT_COMPILED, "context_compiled")
        self._advance(WorkflowState.ROUTED, "routed")
        self._advance(WorkflowState.IMPLEMENTING, "implementing")

    def write_report(
        self,
        outcome: TaskOutcome,
        *,
        results: list[VerificationResult] | None = None,
        error: str | None = None,
    ) -> None:
        report = FinalTaskReport(
            task_id=TASK_ID,
            outcome=outcome,
            error=error,
            number_of_calls=0,
            input_tokens=0,
            output_tokens=0,
            cached_input_tokens=0,
            estimated_cost_usd=0.0,
            latency_seconds=0.0,
            transmitted_files=0,
            transmitted_lines=0,
            verification_results=results or [],
        )
        (self.task_directory / "report.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )

    def write_artifact(self, filename: str, model) -> None:
        (self.task_directory / filename).write_text(
            model.model_dump_json(indent=2), encoding="utf-8"
        )

    def project(self):
        return project_current_task_evidence(self.root, self.store, TASK_ID)

    # -- fixtures for the four ways a task can currently be COMPLETE ------

    def stop_at_human_review_with_failed_report(self, event_type: str) -> None:
        """The state Crisis Atlas's final slice was in before its repair.

        `report.json` is written here and never again: everything after
        this point is what the projection has to see instead of it.
        """

        self.create_task()
        self.reach_implementing()
        self._advance(WorkflowState.PATCH_READY, "patch_ready")
        self._advance(WorkflowState.VERIFYING, "verification_started")
        self._advance(
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            event_type,
            reason="bounded local coding agent requires escalation",
        )
        self.write_report(
            TaskOutcome.HUMAN_REVIEW_REQUIRED,
            results=[verification_result(VerificationStatus.FAILED)],
            error="bounded local coding agent requires escalation",
        )

    def complete_via_continuation(self, started_event: str) -> None:
        self._advance(WorkflowState.IMPLEMENTING, started_event)
        self._advance(WorkflowState.PATCH_READY, "review_continuation_patch_ready")
        self._advance(
            WorkflowState.VERIFYING, "review_continuation_verification_recorded"
        )
        self._advance(
            WorkflowState.COMPLETE,
            "review_continuation_verification_passed",
            stop_reason="continuation completed the task",
        )

    def complete_via_manual_frontier(self, operation_id: str = "OP-MANUAL-1") -> None:
        self._advance(
            WorkflowState.IMPLEMENTING,
            "manual_frontier_apply_started",
            operation_id=operation_id,
            reason="human-approved manual subscription-frontier patch",
        )
        self._advance(
            WorkflowState.PATCH_READY,
            "manual_frontier_patch_applied",
            operation_id=operation_id,
        )
        self._advance(
            WorkflowState.VERIFYING,
            "manual_frontier_verification_started",
            operation_id=operation_id,
        )
        self._advance(
            WorkflowState.COMPLETE,
            "manual_frontier_verification_passed",
            operation_id=operation_id,
        )


class OriginalReportIsCurrentTests(ProjectionTestsBase):
    def test_untouched_first_stop_reads_the_report(self) -> None:
        """`report.json` is not stale until something supersedes it.

        The report is written at the same instant as the completion event,
        so treating it as current here is correct -- the defect this module
        fixes is only ever about stages that ran *after* it.
        """

        self.create_task()
        self.reach_implementing()
        self._advance(WorkflowState.PATCH_READY, "patch_ready")
        self._advance(WorkflowState.VERIFYING, "verification_started")
        self._advance(WorkflowState.COMPLETE, "verification_passed", attempt=1)
        self.write_report(
            TaskOutcome.COMPLETE,
            results=[verification_result(VerificationStatus.PASSED)],
        )

        evidence = self.project()

        self.assertEqual(evidence.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(
            evidence.evidence_generation, EvidenceGeneration.ORIGINAL_REPORT
        )
        self.assertEqual(evidence.evidence_integrity, EvidenceIntegrity.INTACT)
        self.assertFalse(evidence.supersedes_original_report)
        self.assertTrue(evidence.is_verified_complete)
        self.assertEqual(
            evidence.evidence_sources, [f".apoapsis/tasks/{TASK_ID}/report.json"]
        )

    def test_missing_report_for_original_completion_fails_closed(self) -> None:
        self.create_task()
        self.reach_implementing()
        self._advance(WorkflowState.PATCH_READY, "patch_ready")
        self._advance(WorkflowState.VERIFYING, "verification_started")
        self._advance(WorkflowState.COMPLETE, "verification_passed", attempt=1)

        evidence = self.project()

        self.assertEqual(evidence.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(evidence.evidence_integrity, EvidenceIntegrity.MISSING)
        self.assertEqual(evidence.verification_results, [])
        self.assertFalse(evidence.is_verified_complete)

    def test_mid_flight_task_has_no_outcome(self) -> None:
        self.create_task()
        self.reach_implementing()

        evidence = self.project()

        self.assertIsNone(evidence.outcome)
        self.assertEqual(evidence.evidence_generation, EvidenceGeneration.NONE)
        self.assertFalse(evidence.is_verified_complete)


class SupersededEvidenceTests(ProjectionTestsBase):
    def test_local_continuation_completion_supersedes_human_review_report(
        self,
    ) -> None:
        self.stop_at_human_review_with_failed_report(
            "frontier_escalation_not_configured"
        )
        self.complete_via_continuation("review_local_continuation_started")
        self.write_artifact(
            "agent-session.json",
            session_result(
                AgentSessionOutcome.COMPLETE,
                [verification_result(VerificationStatus.PASSED)],
            ),
        )

        evidence = self.project()

        self.assertEqual(evidence.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(
            evidence.original_report_outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED
        )
        self.assertTrue(evidence.supersedes_original_report)
        self.assertEqual(
            evidence.evidence_generation, EvidenceGeneration.LOCAL_STAGE_SESSION
        )
        self.assertEqual(
            evidence.evidence_sources, [f".apoapsis/tasks/{TASK_ID}/agent-session.json"]
        )
        self.assertTrue(evidence.is_verified_complete)
        self.assertEqual(
            evidence.current_verification_status, VerificationStatus.PASSED
        )

    def test_local_power_sandbox_session_counts_as_the_local_stage(self) -> None:
        """A sandbox run writes `local-power-session.json`, never
        `agent-session.json`. Treating that absence as missing evidence
        would fail delivery closed for a perfectly ordinary configuration.
        """

        self.stop_at_human_review_with_failed_report(
            "review_local_continuation_requires_human"
        )
        self.complete_via_continuation("review_local_continuation_started")
        self.write_artifact(
            "local-power-session.json",
            session_result(
                AgentSessionOutcome.COMPLETE,
                [verification_result(VerificationStatus.PASSED)],
            ),
        )

        evidence = self.project()

        self.assertTrue(evidence.is_verified_complete)
        self.assertEqual(
            evidence.evidence_sources,
            [f".apoapsis/tasks/{TASK_ID}/local-power-session.json"],
        )

    def test_frontier_continuation_completion_reads_the_frontier_session(self) -> None:
        """The same `review_continuation_verification_passed` event is
        written by the local continuation, the frontier continuation, and a
        fresh frontier stage, so the completion event alone cannot identify
        the artifact. The preceding *started* event must decide."""

        self.stop_at_human_review_with_failed_report("bounded_frontier_requires_human")
        self.complete_via_continuation("review_frontier_continuation_started")
        self.write_artifact(
            "frontier-agent-session.json",
            session_result(
                AgentSessionOutcome.COMPLETE,
                [verification_result(VerificationStatus.PASSED)],
            ),
        )
        # A local session file also exists from the earlier stage; it must
        # not be picked up in preference to the frontier one.
        self.write_artifact(
            "agent-session.json",
            session_result(
                AgentSessionOutcome.ESCALATION_REQUIRED,
                [verification_result(VerificationStatus.FAILED)],
            ),
        )

        evidence = self.project()

        self.assertEqual(
            evidence.evidence_generation, EvidenceGeneration.FRONTIER_STAGE_SESSION
        )
        self.assertEqual(
            evidence.evidence_sources,
            [f".apoapsis/tasks/{TASK_ID}/frontier-agent-session.json"],
        )
        self.assertTrue(evidence.is_verified_complete)

    def test_fresh_frontier_stage_completion_reads_the_frontier_session(self) -> None:
        self.stop_at_human_review_with_failed_report(
            "frontier_escalation_not_configured"
        )
        self.complete_via_continuation("review_frontier_stage_started")
        self.write_artifact(
            "frontier-agent-session.json",
            session_result(
                AgentSessionOutcome.COMPLETE,
                [verification_result(VerificationStatus.PASSED)],
            ),
        )

        evidence = self.project()

        self.assertEqual(
            evidence.evidence_generation, EvidenceGeneration.FRONTIER_STAGE_SESSION
        )
        self.assertTrue(evidence.is_verified_complete)

    def test_manual_frontier_repair_supersedes_failed_report(self) -> None:
        """The literal Crisis Atlas case: first report is a failed
        `human_review_required`, a hash-bound manual patch is applied, its
        verification passes, and the task is persistently COMPLETE."""

        self.stop_at_human_review_with_failed_report(
            "frontier_escalation_not_configured"
        )
        self.complete_via_manual_frontier()
        self.write_artifact(
            "manual-frontier-verification-OP-MANUAL-1.json",
            verification_result(
                VerificationStatus.PASSED,
                commands=[
                    command_result("unit-tests", VerificationStatus.PASSED),
                    command_result("web-product", VerificationStatus.PASSED),
                ],
            ),
        )

        evidence = self.project()

        self.assertEqual(evidence.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(
            evidence.original_report_outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED
        )
        self.assertEqual(
            evidence.evidence_generation, EvidenceGeneration.MANUAL_FRONTIER_APPLY
        )
        self.assertTrue(evidence.is_verified_complete)
        self.assertEqual(
            [item["name"] for item in evidence.command_results()],
            ["unit-tests", "web-product"],
        )
        self.assertTrue(
            all(item["status"] == "passed" for item in evidence.command_results())
        )

    def test_verification_retry_that_remains_human_review(self) -> None:
        self.stop_at_human_review_with_failed_report(
            "frontier_escalation_not_configured"
        )
        self._advance(
            WorkflowState.VERIFYING,
            "review_verification_retry_started",
            operation_id="OP-RETRY-1",
            reason="human-authorized verification-only retry",
        )
        self._advance(
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            "review_verification_retry_failed",
            operation_id="OP-RETRY-1",
            reason="configured verification still failed on retry",
        )
        self.write_artifact(
            "review-verification-retry-OP-RETRY-1.json",
            verification_result(
                VerificationStatus.FAILED,
                commands=[command_result("web-product", VerificationStatus.FAILED)],
            ),
        )

        evidence = self.project()

        self.assertEqual(evidence.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)
        self.assertEqual(
            evidence.evidence_generation, EvidenceGeneration.VERIFICATION_RETRY
        )
        self.assertEqual(evidence.evidence_integrity, EvidenceIntegrity.INTACT)
        self.assertFalse(evidence.is_verified_complete)
        # The retry's own commands, not the original report's.
        self.assertEqual(
            [item["name"] for item in evidence.command_results()], ["web-product"]
        )
        self.assertEqual(evidence.reason, "configured verification still failed on retry")

    def test_original_report_is_never_rewritten(self) -> None:
        self.stop_at_human_review_with_failed_report(
            "frontier_escalation_not_configured"
        )
        report_path = self.task_directory / "report.json"
        before = report_path.read_bytes()
        self.complete_via_manual_frontier()
        self.write_artifact(
            "manual-frontier-verification-OP-MANUAL-1.json",
            verification_result(VerificationStatus.PASSED),
        )

        evidence = self.project()

        self.assertTrue(evidence.is_verified_complete)
        self.assertEqual(report_path.read_bytes(), before)
        self.assertEqual(
            json.loads(before)["outcome"], TaskOutcome.HUMAN_REVIEW_REQUIRED.value
        )


class FailClosedTests(ProjectionTestsBase):
    """Missing or malformed newer evidence must never silently become the
    older pass. Delivery can refuse an unproven task; it cannot detect a
    plausible substitution."""

    def _complete_via_manual_frontier_with_passing_report(self) -> None:
        self.create_task()
        self.reach_implementing()
        self._advance(WorkflowState.PATCH_READY, "patch_ready")
        self._advance(WorkflowState.VERIFYING, "verification_started")
        self._advance(
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            "frontier_escalation_not_configured",
            reason="escalation required",
        )
        # Deliberately a *passing* report, so that a fallback to it would
        # look like success rather than like an obvious error.
        self.write_report(
            TaskOutcome.COMPLETE,
            results=[verification_result(VerificationStatus.PASSED)],
        )
        self.complete_via_manual_frontier()

    def test_missing_manual_frontier_artifact_fails_closed(self) -> None:
        self._complete_via_manual_frontier_with_passing_report()

        evidence = self.project()

        self.assertEqual(evidence.evidence_integrity, EvidenceIntegrity.MISSING)
        self.assertEqual(evidence.verification_results, [])
        self.assertIsNone(evidence.current_verification_status)
        self.assertFalse(evidence.is_verified_complete)
        self.assertIn("manual-frontier-verification", evidence.evidence_integrity_detail)

    def test_malformed_manual_frontier_artifact_fails_closed(self) -> None:
        self._complete_via_manual_frontier_with_passing_report()
        (
            self.task_directory / "manual-frontier-verification-OP-MANUAL-1.json"
        ).write_text('{"status": "passed"}', encoding="utf-8")

        evidence = self.project()

        self.assertEqual(evidence.evidence_integrity, EvidenceIntegrity.MALFORMED)
        self.assertEqual(evidence.verification_results, [])
        self.assertFalse(evidence.is_verified_complete)

    def test_completion_event_without_operation_id_fails_closed(self) -> None:
        self.stop_at_human_review_with_failed_report(
            "frontier_escalation_not_configured"
        )
        self._advance(
            WorkflowState.IMPLEMENTING,
            "manual_frontier_apply_started",
            operation_id="OP-MANUAL-1",
        )
        self._advance(WorkflowState.PATCH_READY, "manual_frontier_patch_applied")
        self._advance(WorkflowState.VERIFYING, "manual_frontier_verification_started")
        self._advance(WorkflowState.COMPLETE, "manual_frontier_verification_passed")

        evidence = self.project()

        self.assertEqual(evidence.evidence_integrity, EvidenceIntegrity.MISSING)
        self.assertFalse(evidence.is_verified_complete)
        self.assertIn("operation_id", evidence.evidence_integrity_detail)

    def test_missing_continuation_session_fails_closed(self) -> None:
        self.stop_at_human_review_with_failed_report(
            "frontier_escalation_not_configured"
        )
        self.complete_via_continuation("review_local_continuation_started")

        evidence = self.project()

        self.assertEqual(evidence.evidence_integrity, EvidenceIntegrity.MISSING)
        self.assertEqual(evidence.verification_results, [])
        self.assertFalse(evidence.is_verified_complete)

    def test_malformed_continuation_session_fails_closed(self) -> None:
        self.stop_at_human_review_with_failed_report(
            "frontier_escalation_not_configured"
        )
        self.complete_via_continuation("review_local_continuation_started")
        (self.task_directory / "agent-session.json").write_text(
            "not json at all", encoding="utf-8"
        )

        evidence = self.project()

        self.assertEqual(evidence.evidence_integrity, EvidenceIntegrity.MALFORMED)
        self.assertFalse(evidence.is_verified_complete)

    def test_unmapped_decisive_event_fails_closed(self) -> None:
        """A future completion event this module has not been taught about
        must not inherit the original report's pass by default (the same
        reasoning ADR 0021 applied to stop classification)."""

        self.create_task()
        self.reach_implementing()
        self._advance(WorkflowState.PATCH_READY, "patch_ready")
        self._advance(WorkflowState.VERIFYING, "verification_started")
        self._advance(WorkflowState.COMPLETE, "some_future_completion_event")
        self.write_report(
            TaskOutcome.COMPLETE,
            results=[verification_result(VerificationStatus.PASSED)],
        )

        evidence = self.project()

        self.assertEqual(evidence.evidence_generation, EvidenceGeneration.NONE)
        self.assertEqual(evidence.evidence_integrity, EvidenceIntegrity.MISSING)
        self.assertEqual(evidence.verification_results, [])
        self.assertFalse(evidence.is_verified_complete)

    def test_continuation_completion_without_a_started_event_fails_closed(self) -> None:
        self.stop_at_human_review_with_failed_report(
            "frontier_escalation_not_configured"
        )
        self._advance(WorkflowState.IMPLEMENTING, "unrecognized_stage_started")
        self._advance(WorkflowState.PATCH_READY, "review_continuation_patch_ready")
        self._advance(
            WorkflowState.VERIFYING, "review_continuation_verification_recorded"
        )
        self._advance(
            WorkflowState.COMPLETE, "review_continuation_verification_passed"
        )
        self.write_artifact(
            "agent-session.json",
            session_result(
                AgentSessionOutcome.COMPLETE,
                [verification_result(VerificationStatus.PASSED)],
            ),
        )

        evidence = self.project()

        self.assertEqual(evidence.evidence_generation, EvidenceGeneration.NONE)
        self.assertFalse(evidence.is_verified_complete)


class CoverageProjectionTests(ProjectionTestsBase):
    def test_coverage_recomputed_from_the_immutable_result(self) -> None:
        """Coverage for a superseding stage is derived from the result's
        own `acceptance`/`required` flags (ADR 0018), not from live
        configuration -- an edit made after the run must not change what a
        past run is said to have proven."""

        self.create_task(make_specification(verification_method="web-product"))
        self.reach_implementing()
        self._advance(WorkflowState.PATCH_READY, "patch_ready")
        self._advance(WorkflowState.VERIFYING, "verification_started")
        self._advance(
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            "frontier_escalation_not_configured",
            reason="escalation required",
        )
        self.write_report(TaskOutcome.HUMAN_REVIEW_REQUIRED, error="escalation")
        self.complete_via_manual_frontier()
        self.write_artifact(
            "manual-frontier-verification-OP-MANUAL-1.json",
            verification_result(
                VerificationStatus.PASSED,
                commands=[
                    command_result(
                        "web-product", VerificationStatus.PASSED, acceptance=True
                    )
                ],
            ),
        )

        evidence = self.project()

        self.assertEqual(len(evidence.acceptance_coverage), 1)
        self.assertEqual(
            evidence.acceptance_coverage[0].status, AcceptanceCoverageStatus.PROVEN
        )
        self.assertEqual(evidence.acceptance_coverage[0].criterion_id, "AC-1")

    def test_non_acceptance_command_does_not_prove_a_criterion(self) -> None:
        specification = make_specification(verification_method="web-product")
        result = verification_result(
            VerificationStatus.PASSED,
            commands=[
                command_result("web-product", VerificationStatus.PASSED, acceptance=False)
            ],
        )

        coverage = coverage_from_verification_result(specification, result)

        self.assertEqual(coverage[0].status, AcceptanceCoverageStatus.UNPROVEN)
        self.assertIn("not an approved acceptance check", coverage[0].reason)

    def test_skipped_command_is_not_evidence_of_anything(self) -> None:
        specification = make_specification(verification_method="web-product")
        result = verification_result(
            VerificationStatus.PASSED,
            commands=[
                command_result(
                    "web-product",
                    VerificationStatus.SKIPPED,
                    acceptance=True,
                    required=False,
                    exit_code=None,
                )
            ],
        )

        coverage = coverage_from_verification_result(specification, result)

        self.assertEqual(coverage[0].status, AcceptanceCoverageStatus.UNPROVEN)
        self.assertIn("has not yet been executed", coverage[0].reason)

    def test_coverage_on_the_stop_event_payload_is_preferred(self) -> None:
        """A STRICT rejection serializes the exact coverage that caused the
        stop; reading it back beats recomputing, because it is what the
        harness actually decided on."""

        self.create_task(make_specification(verification_method="web-product"))
        self.reach_implementing()
        self._advance(WorkflowState.PATCH_READY, "patch_ready")
        self._advance(WorkflowState.VERIFYING, "verification_started")
        self._advance(
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            "frontier_escalation_not_configured",
            reason="escalation required",
        )
        self.write_report(TaskOutcome.HUMAN_REVIEW_REQUIRED, error="escalation")
        self._advance(
            WorkflowState.VERIFYING,
            "review_verification_retry_started",
            operation_id="OP-RETRY-9",
        )
        self._advance(
            WorkflowState.HUMAN_REVIEW_REQUIRED,
            "review_verification_retry_incomplete",
            operation_id="OP-RETRY-9",
            reason=(
                "configured verification passed but not every active acceptance "
                "criterion is proven under the strict completion policy"
            ),
            coverage=[
                {
                    "criterion_id": "AC-1",
                    "status": "unproven",
                    "evidence_source": None,
                    "evidence_reference": "web-product",
                    "reason": "no verification command is mapped to this criterion",
                }
            ],
        )
        self.write_artifact(
            "review-verification-retry-OP-RETRY-9.json",
            verification_result(
                VerificationStatus.PASSED,
                commands=[
                    command_result(
                        "web-product", VerificationStatus.PASSED, acceptance=True
                    )
                ],
            ),
        )

        evidence = self.project()

        self.assertEqual(evidence.evidence_integrity, EvidenceIntegrity.INTACT)
        self.assertEqual(
            evidence.acceptance_coverage[0].status, AcceptanceCoverageStatus.UNPROVEN
        )
        self.assertFalse(evidence.is_verified_complete)


if __name__ == "__main__":
    unittest.main()
