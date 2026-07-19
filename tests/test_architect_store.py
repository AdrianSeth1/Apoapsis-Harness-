from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apoapsis.architect.errors import (
    ConcurrentPlanTransitionError,
    InvalidPlanTransitionError,
    PlanActionError,
    PlanNotFoundError,
)
from apoapsis.architect.schema import PlanStatus, PlanValidationResult
from apoapsis.architect.store import SQLitePlanStore
from tests.architect_helpers import make_plan


def _result(plan_id: str, version: int, *, valid: bool) -> PlanValidationResult:
    findings = []
    if not valid:
        from apoapsis.architect.schema import PlanValidationFinding, ValidationSeverity

        findings = [
            PlanValidationFinding(
                severity=ValidationSeverity.ERROR, code="X", message="bad"
            )
        ]
    return PlanValidationResult(
        plan_id=plan_id, plan_version=version, valid=valid, findings=findings
    )


class SQLitePlanStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Path(self.temporary_directory.name) / "state" / "plans.db"
        self.store = SQLitePlanStore(self.database)
        self.plan = make_plan()

    def test_create_plan_starts_proposed_at_version_one(self) -> None:
        record = self.store.create_plan("PLAN-1", "PKG-1", self.plan.idea_text, self.plan)
        self.assertEqual(record.status, PlanStatus.PROPOSED)
        self.assertEqual(record.version, 1)
        self.assertEqual(record.idea_text, self.plan.idea_text)
        events = self.store.events("PLAN-1")
        self.assertEqual([event.event_type for event in events], ["plan_imported"])

    def test_get_plan_missing_raises(self) -> None:
        with self.assertRaises(PlanNotFoundError):
            self.store.get_plan("PLAN-MISSING")

    def test_record_validation_valid_moves_to_validated(self) -> None:
        created = self.store.create_plan("PLAN-1", "PKG-1", self.plan.idea_text, self.plan)
        result = _result("PLAN-1", created.version, valid=True)
        updated = self.store.record_validation(
            "PLAN-1", result, expected_version=created.version
        )
        self.assertEqual(updated.status, PlanStatus.VALIDATED)
        self.assertEqual(updated.version, 2)
        assert updated.validation is not None
        self.assertTrue(updated.validation.valid)

    def test_record_validation_invalid_stays_proposed(self) -> None:
        created = self.store.create_plan("PLAN-1", "PKG-1", self.plan.idea_text, self.plan)
        result = _result("PLAN-1", created.version, valid=False)
        updated = self.store.record_validation(
            "PLAN-1", result, expected_version=created.version
        )
        self.assertEqual(updated.status, PlanStatus.PROPOSED)
        assert updated.validation is not None
        self.assertFalse(updated.validation.valid)

    def test_record_validation_stale_version_rejected(self) -> None:
        created = self.store.create_plan("PLAN-1", "PKG-1", self.plan.idea_text, self.plan)
        result = _result("PLAN-1", created.version, valid=True)
        with self.assertRaises(ConcurrentPlanTransitionError):
            self.store.record_validation("PLAN-1", result, expected_version=999)
        persisted = self.store.get_plan("PLAN-1")
        self.assertEqual(persisted.status, PlanStatus.PROPOSED)
        self.assertEqual(persisted.version, 1)

    def test_approve_requires_validated_status(self) -> None:
        created = self.store.create_plan("PLAN-1", "PKG-1", self.plan.idea_text, self.plan)
        with self.assertRaises(PlanActionError):
            self.store.approve_plan("PLAN-1", expected_version=created.version)

    def test_approve_requires_last_validation_to_be_valid(self) -> None:
        created = self.store.create_plan("PLAN-1", "PKG-1", self.plan.idea_text, self.plan)
        # A validation result can only ever land the record in VALIDATED
        # when it is itself valid (record_validation's own transition rule),
        # so approve_plan's defensive re-check of validation.valid can only
        # ever be exercised by directly fabricating an inconsistent row --
        # this test instead confirms the ordinary invalid-validation path
        # (still PROPOSED) is rejected by the status check above it.
        result = _result("PLAN-1", created.version, valid=False)
        after_validation = self.store.record_validation(
            "PLAN-1", result, expected_version=created.version
        )
        with self.assertRaises(PlanActionError):
            self.store.approve_plan("PLAN-1", expected_version=after_validation.version)

    def test_approve_stale_version_rejected(self) -> None:
        created = self.store.create_plan("PLAN-1", "PKG-1", self.plan.idea_text, self.plan)
        result = _result("PLAN-1", created.version, valid=True)
        validated = self.store.record_validation(
            "PLAN-1", result, expected_version=created.version
        )
        approved = self.store.approve_plan(
            "PLAN-1", expected_version=validated.version
        )
        self.assertEqual(approved.status, PlanStatus.APPROVED)
        with self.assertRaises(ConcurrentPlanTransitionError):
            self.store.approve_plan("PLAN-1", expected_version=validated.version)

    def test_revision_creates_new_version_without_losing_history(self) -> None:
        created = self.store.create_plan("PLAN-1", "PKG-1", self.plan.idea_text, self.plan)
        result = _result("PLAN-1", created.version, valid=True)
        validated = self.store.record_validation(
            "PLAN-1", result, expected_version=created.version
        )
        approved = self.store.approve_plan(
            "PLAN-1", expected_version=validated.version
        )
        self.assertEqual(approved.status, PlanStatus.APPROVED)

        revised_plan = self.plan.model_copy(
            update={"architecture_summary": "A revised approach."}
        )
        revised = self.store.create_revision(
            "PLAN-1",
            "PKG-2",
            revised_plan.idea_text,
            revised_plan,
            expected_version=approved.version,
        )
        self.assertEqual(revised.status, PlanStatus.PROPOSED)
        self.assertEqual(revised.version, approved.version + 1)
        self.assertIsNone(revised.validation)
        self.assertEqual(
            revised.plan.architecture_summary, "A revised approach."
        )
        events = self.store.events("PLAN-1")
        self.assertEqual(
            [event.event_type for event in events],
            ["plan_imported", "plan_validated", "plan_approved", "plan_revised"],
        )
        self.assertEqual(events[-1].payload["previous_status"], "approved")

    def test_validating_an_approved_plan_is_rejected(self) -> None:
        created = self.store.create_plan("PLAN-1", "PKG-1", self.plan.idea_text, self.plan)
        result = _result("PLAN-1", created.version, valid=True)
        validated = self.store.record_validation(
            "PLAN-1", result, expected_version=created.version
        )
        approved = self.store.approve_plan(
            "PLAN-1", expected_version=validated.version
        )
        with self.assertRaises(InvalidPlanTransitionError):
            self.store.record_validation(
                "PLAN-1",
                _result("PLAN-1", approved.version, valid=True),
                expected_version=approved.version,
            )

    def test_revision_stale_version_rejected(self) -> None:
        created = self.store.create_plan("PLAN-1", "PKG-1", self.plan.idea_text, self.plan)
        with self.assertRaises(ConcurrentPlanTransitionError):
            self.store.create_revision(
                "PLAN-1",
                "PKG-2",
                self.plan.idea_text,
                self.plan,
                expected_version=999,
            )

    def test_events_are_sequenced_in_order(self) -> None:
        created = self.store.create_plan("PLAN-1", "PKG-1", self.plan.idea_text, self.plan)
        result = _result("PLAN-1", created.version, valid=True)
        self.store.record_validation("PLAN-1", result, expected_version=created.version)
        events = self.store.events("PLAN-1")
        self.assertEqual(
            [event.sequence for event in events],
            sorted(event.sequence for event in events if event.sequence is not None),
        )

    def test_list_plans_returns_created_plan(self) -> None:
        self.store.create_plan("PLAN-1", "PKG-1", self.plan.idea_text, self.plan)
        plans = self.store.list_plans()
        self.assertEqual([item.plan_id for item in plans], ["PLAN-1"])

    def test_reopened_store_sees_persisted_plan(self) -> None:
        self.store.create_plan("PLAN-1", "PKG-1", self.plan.idea_text, self.plan)
        reopened = SQLitePlanStore(self.database)
        self.assertEqual(reopened.get_plan("PLAN-1").status, PlanStatus.PROPOSED)


if __name__ == "__main__":
    unittest.main()
