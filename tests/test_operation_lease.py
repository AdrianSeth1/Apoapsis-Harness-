from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

from apoapsis.execution.operation_errors import ExecutionOperationError
from apoapsis.execution.operation_schema import ExecutionOperationStatus
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.intake.errors import IntakeError
from apoapsis.intake.schema import IntakeOperationStatus
from apoapsis.intake.store import IntakeOperationStore
from apoapsis.operations.lease import (
    LeaseHeartbeat,
    LeaseLostError,
    claim_lease,
    ensure_lease_columns,
    expire_lease_to_ambiguous,
    new_owner_id,
    release_lease,
    renew_lease,
)
from apoapsis.review.errors import ReviewError
from apoapsis.review.schema import ReviewActionKind, ReviewOperationStatus
from apoapsis.review.store import ReviewOperationStore
from apoapsis.specification.schema import utc_now
from apoapsis.workflow.engine import SQLiteTaskStore


class LeaseModuleTests(unittest.TestCase):
    """Direct tests of the shared, table-name-parameterized lease
    primitives (ADR 0025), independent of any of the three domain
    stores that reuse them. A minimal ad hoc table stands in for a real
    operations table -- the module only ever reads/writes the four
    columns it owns (``status``, ``updated_at``, ``lease_owner_id``,
    ``lease_expires_at``) plus one caller-chosen extra column."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "lease.db"
        self.connection = sqlite3.connect(self.database_path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            """
            CREATE TABLE ops (
                operation_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT
            );
            """
        )
        ensure_lease_columns(self.connection, "ops")
        self.connection.execute(
            "INSERT INTO ops (operation_id, status, updated_at) VALUES (?, ?, ?)",
            ("OP-1", "recorded", utc_now().isoformat()),
        )

    def test_ensure_lease_columns_is_additive_and_idempotent(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(ops)").fetchall()
        }
        self.assertIn("lease_owner_id", columns)
        self.assertIn("lease_expires_at", columns)
        # Calling again against a table that already has the columns must
        # not raise or duplicate anything.
        ensure_lease_columns(self.connection, "ops")

    def test_claim_lease_only_one_of_two_racing_owners_wins(self) -> None:
        now = utc_now()
        owner_a, owner_b = new_owner_id(), new_owner_id()
        won_a = claim_lease(
            self.connection,
            "ops",
            "OP-1",
            owner_id=owner_a,
            lease_duration=timedelta(minutes=5),
            now=now,
            from_status="recorded",
            to_status="running",
        )
        won_b = claim_lease(
            self.connection,
            "ops",
            "OP-1",
            owner_id=owner_b,
            lease_duration=timedelta(minutes=5),
            now=now,
            from_status="recorded",
            to_status="running",
        )
        self.assertTrue(won_a)
        self.assertFalse(won_b)
        row = self.connection.execute(
            "SELECT status, lease_owner_id FROM ops WHERE operation_id = ?", ("OP-1",)
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["lease_owner_id"], owner_a)

    def test_renew_lease_extends_deadline_only_for_the_owning_owner(self) -> None:
        now = utc_now()
        owner = new_owner_id()
        claim_lease(
            self.connection,
            "ops",
            "OP-1",
            owner_id=owner,
            lease_duration=timedelta(minutes=5),
            now=now,
            from_status="recorded",
            to_status="running",
        )
        later = now + timedelta(minutes=4)
        renewed_by_stranger = renew_lease(
            self.connection,
            "ops",
            "OP-1",
            owner_id=new_owner_id(),
            lease_duration=timedelta(minutes=5),
            now=later,
            running_status="running",
        )
        self.assertFalse(renewed_by_stranger)
        renewed_by_owner = renew_lease(
            self.connection,
            "ops",
            "OP-1",
            owner_id=owner,
            lease_duration=timedelta(minutes=5),
            now=later,
            running_status="running",
        )
        self.assertTrue(renewed_by_owner)
        row = self.connection.execute(
            "SELECT lease_expires_at FROM ops WHERE operation_id = ?", ("OP-1",)
        ).fetchone()
        self.assertEqual(row["lease_expires_at"], (later + timedelta(minutes=5)).isoformat())

    def test_renew_lease_succeeds_even_if_deadline_already_passed_when_uncontested(
        self,
    ) -> None:
        # A heartbeat tick that fires a little late (the deadline has
        # technically already elapsed) must still succeed as long as
        # nobody else has acted on the row -- deliberately, so a merely
        # slow tick does not cause a healthy operation to lose its lease.
        now = utc_now()
        owner = new_owner_id()
        claim_lease(
            self.connection,
            "ops",
            "OP-1",
            owner_id=owner,
            lease_duration=timedelta(seconds=1),
            now=now,
            from_status="recorded",
            to_status="running",
        )
        late = now + timedelta(minutes=10)
        renewed = renew_lease(
            self.connection,
            "ops",
            "OP-1",
            owner_id=owner,
            lease_duration=timedelta(minutes=5),
            now=late,
            running_status="running",
        )
        self.assertTrue(renewed)

    def test_release_lease_only_succeeds_for_the_owning_owner(self) -> None:
        now = utc_now()
        owner = new_owner_id()
        claim_lease(
            self.connection,
            "ops",
            "OP-1",
            owner_id=owner,
            lease_duration=timedelta(minutes=5),
            now=now,
            from_status="recorded",
            to_status="running",
        )
        released_by_stranger = release_lease(
            self.connection,
            "ops",
            "OP-1",
            owner_id=new_owner_id(),
            running_status="running",
            to_status="succeeded",
            now=now,
        )
        self.assertFalse(released_by_stranger)
        released_by_owner = release_lease(
            self.connection,
            "ops",
            "OP-1",
            owner_id=owner,
            running_status="running",
            to_status="succeeded",
            now=now,
        )
        self.assertTrue(released_by_owner)

    def test_expire_lease_to_ambiguous_only_after_genuine_expiry(self) -> None:
        now = utc_now()
        owner = new_owner_id()
        claim_lease(
            self.connection,
            "ops",
            "OP-1",
            owner_id=owner,
            lease_duration=timedelta(minutes=5),
            now=now,
            from_status="recorded",
            to_status="running",
        )
        still_healthy = expire_lease_to_ambiguous(
            self.connection,
            "ops",
            "OP-1",
            running_status="running",
            ambiguous_status="ambiguous",
            now=now + timedelta(minutes=4),
            note="stale",
        )
        self.assertFalse(still_healthy)
        genuinely_expired = expire_lease_to_ambiguous(
            self.connection,
            "ops",
            "OP-1",
            running_status="running",
            ambiguous_status="ambiguous",
            now=now + timedelta(minutes=6),
            note="stale",
        )
        self.assertTrue(genuinely_expired)
        row = self.connection.execute(
            "SELECT status FROM ops WHERE operation_id = ?", ("OP-1",)
        ).fetchone()
        self.assertEqual(row["status"], "ambiguous")

    def test_legacy_running_row_without_lease_data_is_unconditionally_expired(
        self,
    ) -> None:
        # A row written before the lease migration has NULL lease columns
        # -- there is no way to prove such an owner is still alive, so it
        # must fail closed and be reclaimable regardless of `now`.
        self.connection.execute(
            "UPDATE ops SET status = 'running' WHERE operation_id = ?", ("OP-1",)
        )
        expired = expire_lease_to_ambiguous(
            self.connection,
            "ops",
            "OP-1",
            running_status="running",
            ambiguous_status="ambiguous",
            now=utc_now(),
            note="legacy row",
        )
        self.assertTrue(expired)


class LeaseHeartbeatTests(unittest.TestCase):
    def test_heartbeat_renews_repeatedly_on_its_own_interval(self) -> None:
        calls = []

        def renew() -> bool:
            calls.append(utc_now())
            return True

        heartbeat = LeaseHeartbeat(renew, interval=timedelta(milliseconds=20))
        heartbeat.start()
        try:
            time.sleep(0.15)
        finally:
            heartbeat.stop()
        self.assertGreaterEqual(len(calls), 3)
        self.assertFalse(heartbeat.lease_lost)

    def test_heartbeat_flags_lease_lost_and_stops_when_renew_fails(self) -> None:
        def renew() -> bool:
            return False

        heartbeat = LeaseHeartbeat(renew, interval=timedelta(milliseconds=10))
        heartbeat.start()
        try:
            time.sleep(0.1)
        finally:
            heartbeat.stop()
        self.assertTrue(heartbeat.lease_lost)

    def test_heartbeat_flags_lease_lost_when_renew_raises(self) -> None:
        def renew() -> bool:
            raise RuntimeError("connection lost")

        heartbeat = LeaseHeartbeat(renew, interval=timedelta(milliseconds=10))
        heartbeat.start()
        try:
            time.sleep(0.1)
        finally:
            heartbeat.stop()
        self.assertTrue(heartbeat.lease_lost)


class _CrossStoreLeaseSemanticsBase:
    """Shared behavior verified identically against review, intake, and
    execution operation stores (ADR 0025's "one coherent, shared
    discipline" requirement) -- each concrete subclass only supplies the
    handful of domain-specific names/factories that differ."""

    operation_status: type
    running_status_value: str
    operation_id_prefix: str

    def setUp(self) -> None:  # type: ignore[override]
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.store = self._make_store()
        self.task_store = SQLiteTaskStore(self.root / "apoapsis.db")

    def _operation_id(self, suffix: str) -> str:
        return f"{self.operation_id_prefix}-{suffix}"

    def _make_store(self):
        raise NotImplementedError

    def _create_operation(self, operation_id: str) -> None:
        raise NotImplementedError

    def _mark_terminal_success(self, operation_id: str, *, owner_id: str) -> None:
        raise NotImplementedError

    def _recover(self, *, now):
        raise NotImplementedError

    def test_long_healthy_operation_survives_many_former_boundaries(self) -> None:
        operation_id = self._operation_id("LONG-HEALTHY")
        self._create_operation(operation_id)
        owner_id = new_owner_id()
        start = utc_now()
        self.store.mark_running(
            operation_id,
            owner_id=owner_id,
            lease_duration=timedelta(minutes=5),
            now=start,
        )
        # Simulate a heartbeat renewing every few minutes while an injected
        # clock sails past several former 15-minute "running_expiry"
        # windows -- the operation must never be misclassified as
        # crashed as long as it keeps renewing.
        moment = start
        for _ in range(6):
            moment = moment + timedelta(minutes=4)
            renewed = self.store.renew_lease(
                operation_id,
                owner_id=owner_id,
                lease_duration=timedelta(minutes=5),
                now=moment,
            )
            self.assertTrue(renewed)
            report = self._recover(now=moment)
            self.assertEqual(report.ambiguous_operation_ids, [])
        self.assertEqual(
            self.store.get(operation_id).status.value, self.running_status_value
        )

    def test_heartbeat_stopping_leads_recovery_to_mark_ambiguous(self) -> None:
        operation_id = self._operation_id("CRASHED")
        self._create_operation(operation_id)
        owner_id = new_owner_id()
        start = utc_now()
        self.store.mark_running(
            operation_id,
            owner_id=owner_id,
            lease_duration=timedelta(minutes=5),
            now=start,
        )
        # The owning process crashes here -- no further renewal happens.
        past_expiry = start + timedelta(minutes=6)
        report = self._recover(now=past_expiry)
        self.assertEqual(report.ambiguous_operation_ids, [operation_id])
        self.assertEqual(
            self.store.get(operation_id).status.value, "ambiguous"
        )

    def test_old_owner_cannot_mark_success_after_recovery_wins(self) -> None:
        operation_id = self._operation_id("RACE")
        self._create_operation(operation_id)
        owner_id = new_owner_id()
        start = utc_now()
        self.store.mark_running(
            operation_id,
            owner_id=owner_id,
            lease_duration=timedelta(minutes=5),
            now=start,
        )
        past_expiry = start + timedelta(minutes=6)
        report = self._recover(now=past_expiry)
        self.assertEqual(report.ambiguous_operation_ids, [operation_id])
        # The original owner, unaware recovery already won, now tries to
        # report its (possibly genuinely completed) result -- it must be
        # rejected, never silently overwriting AMBIGUOUS.
        with self.assertRaises(LeaseLostError):
            self._mark_terminal_success(operation_id, owner_id=owner_id)
        self.assertEqual(
            self.store.get(operation_id).status.value, "ambiguous"
        )

    def test_duplicate_claim_of_the_same_recorded_operation_is_rejected(self) -> None:
        operation_id = self._operation_id("DUP-CLAIM")
        self._create_operation(operation_id)
        first_owner = new_owner_id()
        self.store.mark_running(operation_id, owner_id=first_owner)
        second_owner = new_owner_id()
        with self.assertRaises(Exception):
            self.store.mark_running(operation_id, owner_id=second_owner)
        # Only the first owner's lease is recorded -- the second, rejected
        # attempt must never have touched the row.
        record = self.store.get(operation_id)
        self.assertEqual(record.lease_owner_id, first_owner)
        self.assertEqual(record.status.value, self.running_status_value)


class ReviewLeaseSemanticsTests(_CrossStoreLeaseSemanticsBase, unittest.TestCase):
    running_status_value = ReviewOperationStatus.RUNNING.value
    operation_id_prefix = "RVOP"

    def _make_store(self) -> ReviewOperationStore:
        return ReviewOperationStore(self.root / "review-operations.db")

    def _create_operation(self, operation_id: str) -> None:
        self.store.create(
            operation_id,
            "TASK-1",
            ReviewActionKind.ABANDON,
            expected_task_version=1,
        )

    def _mark_terminal_success(self, operation_id: str, *, owner_id: str) -> None:
        self.store.mark_succeeded(operation_id, owner_id=owner_id, result_summary="done")

    def _recover(self, *, now):
        from apoapsis.review.recovery import recover_stale_operations

        return recover_stale_operations(self.task_store, self.store, now=now)


class IntakeLeaseSemanticsTests(_CrossStoreLeaseSemanticsBase, unittest.TestCase):
    running_status_value = IntakeOperationStatus.RUNNING.value
    operation_id_prefix = "INOP"

    def _make_store(self) -> IntakeOperationStore:
        return IntakeOperationStore(self.root / "intake-operations.db")

    def _create_operation(self, operation_id: str) -> None:
        self.store.create(
            operation_id,
            "TASK-1",
            "Add resumable downloads.",
            request_sha256="0" * 64,
            expected_task_version=1,
            provider_role="local_coder",
        )

    def _mark_terminal_success(self, operation_id: str, *, owner_id: str) -> None:
        self.store.mark_pending_approval(
            operation_id,
            owner_id=owner_id,
            result_summary="done",
            audit_artifact_locations=[],
        )

    def _recover(self, *, now):
        from apoapsis.intake.recovery import recover_stale_intake_operations

        return recover_stale_intake_operations(self.task_store, self.store, now=now)


class ExecutionLeaseSemanticsTests(_CrossStoreLeaseSemanticsBase, unittest.TestCase):
    running_status_value = ExecutionOperationStatus.RUNNING.value
    operation_id_prefix = "EXOP"

    def _make_store(self) -> ExecutionOperationStore:
        return ExecutionOperationStore(self.root / "execution-operations.db")

    def _create_operation(self, operation_id: str) -> None:
        self.store.create(
            operation_id,
            "TASK-1",
            expected_task_version=1,
            expected_repository_head="0" * 40,
        )

    def _mark_terminal_success(self, operation_id: str, *, owner_id: str) -> None:
        self.store.mark_succeeded(operation_id, owner_id=owner_id, result_summary="done")

    def _recover(self, *, now):
        from apoapsis.execution.operation_recovery import (
            recover_stale_execution_operations,
        )

        return recover_stale_execution_operations(self.task_store, self.store, now=now)


if __name__ == "__main__":
    unittest.main()
