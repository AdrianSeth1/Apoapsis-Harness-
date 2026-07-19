"""Shared, owner-scoped operation-lease discipline (ADR 0025).

Reused by ``review.store.ReviewOperationStore``,
``intake.store.IntakeOperationStore``, and
``execution.operation_store.ExecutionOperationStore`` -- one coherent
mechanism rather than three independent copies, because a subtle
concurrency bug here has the same shape and the same consequences in all
three. Every helper here operates on a caller-supplied table name and an
already-open ``sqlite3.Connection``; callers own transaction boundaries
(``BEGIN IMMEDIATE``/commit/rollback) exactly as they already did before
this module existed.

The core idea: a ``RUNNING`` operation is owned by a unique, randomly
generated ``lease_owner_id`` and carries a ``lease_expires_at`` deadline.
The worker that claims the lease renews it on a fixed wall-clock interval
via :class:`LeaseHeartbeat`, independent of how long the underlying model
call or agent turn actually takes -- so a healthy operation that runs
longer than any single lease duration never looks stale, as long as its
owning process is alive and renewing. Every state-changing SQL statement
below is a single, atomically-guarded ``UPDATE ... WHERE`` -- never a
separate read-then-write -- so two callers racing to claim, renew, finish,
or recover the same row can never both succeed.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable

DEFAULT_LEASE_DURATION = timedelta(minutes=5)
DEFAULT_HEARTBEAT_INTERVAL = timedelta(minutes=1)


class LeaseLostError(RuntimeError):
    """Raised when a caller's lease on an operation was lost -- reclaimed
    by recovery, or never actually held -- before it could record a
    terminal outcome. The caller must treat its own completed work as
    orphaned from the ledger's perspective and must not retry the same
    mutation; the operation is already authoritatively AMBIGUOUS (or was
    claimed by someone else) and only explicit human review resolves it.
    """


def new_owner_id() -> str:
    """A unique identifier for one worker's attempt to run one operation.

    Generated fresh every time an operation is claimed (never reused
    across attempts, even retries of the same ``operation_id``), so a
    lease conflict can only ever mean "a different attempt now holds
    this," never "the same attempt is racing itself."
    """

    return f"LEASE-{uuid.uuid4().hex}"


def ensure_lease_columns(connection: sqlite3.Connection, table: str) -> None:
    """Additive migration: add the two lease columns to an existing
    operations table if they are not already present. Safe to call on
    every store construction, including against a brand-new table that
    was just created with the columns already in its schema (in which
    case this is a no-op) -- never destructive, never drops or rewrites
    existing rows. Existing terminal records remain fully readable;
    existing ``RUNNING`` records simply gain ``NULL`` lease columns,
    which :func:`expire_lease_to_ambiguous` treats as unconditionally
    stale (fail closed -- see that function's docstring).
    """

    existing = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if "lease_owner_id" not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN lease_owner_id TEXT")
    if "lease_expires_at" not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN lease_expires_at TEXT")


def claim_lease(
    connection: sqlite3.Connection,
    table: str,
    operation_id: str,
    *,
    owner_id: str,
    lease_duration: timedelta,
    now: datetime,
    from_status: str,
    to_status: str,
) -> bool:
    """Atomically transitions ``operation_id`` from ``from_status`` to
    ``to_status`` while claiming the lease for ``owner_id``. Returns
    ``True`` iff exactly one row matched and was updated -- the caller
    must treat ``False`` as "the claim did not happen" (the row may not
    exist, or may already be in some other status) and raise its own
    richer, status-specific error after a follow-up read.
    """

    expires_at = (now + lease_duration).isoformat()
    cursor = connection.execute(
        f"""
        UPDATE {table}
        SET status = ?, updated_at = ?, lease_owner_id = ?, lease_expires_at = ?
        WHERE operation_id = ? AND status = ?
        """,
        (to_status, now.isoformat(), owner_id, expires_at, operation_id, from_status),
    )
    return cursor.rowcount == 1


def renew_lease(
    connection: sqlite3.Connection,
    table: str,
    operation_id: str,
    *,
    owner_id: str,
    lease_duration: timedelta,
    now: datetime,
    running_status: str,
) -> bool:
    """Atomically extends the lease deadline, but only while ``owner_id``
    still holds it and the row is still ``running_status``. Renewal
    succeeds even if the previous deadline has technically already
    passed, as long as nobody else has acted on the row yet (recovery has
    not won the race) -- deliberately, so a heartbeat tick that is merely
    a little late can still keep a healthy operation alive. Returns
    ``True`` iff the lease was renewed.
    """

    expires_at = (now + lease_duration).isoformat()
    cursor = connection.execute(
        f"""
        UPDATE {table}
        SET lease_expires_at = ?
        WHERE operation_id = ? AND status = ? AND lease_owner_id = ?
        """,
        (expires_at, operation_id, running_status, owner_id),
    )
    return cursor.rowcount == 1


def release_lease(
    connection: sqlite3.Connection,
    table: str,
    operation_id: str,
    *,
    owner_id: str,
    running_status: str,
    to_status: str,
    now: datetime,
    extra_assignments: dict[str, object] | None = None,
) -> bool:
    """Atomically transitions ``operation_id`` from ``running_status`` to
    a terminal ``to_status`` (``SUCCEEDED``/``PENDING_SPECIFICATION_
    APPROVAL``/``FAILED``, depending on the caller), but only while
    ``owner_id`` still holds the lease. This is the "only the owning
    lease may mark success/failure" guarantee: if recovery already moved
    the row to ``AMBIGUOUS`` (or a different owner has since claimed it,
    which cannot happen under this module's own claim semantics but is
    still guarded here defensively), this returns ``False`` and the
    caller must raise :class:`LeaseLostError` rather than pretend its
    result was recorded.

    ``extra_assignments`` lets each store set its own additional columns
    (``result_summary``, ``error``, ``report_path``, ...) in the same
    atomic statement, without this shared module needing to know their
    names in advance.
    """

    extra_assignments = extra_assignments or {}
    extra_sql = "".join(f", {column} = ?" for column in extra_assignments)
    cursor = connection.execute(
        f"""
        UPDATE {table}
        SET status = ?, updated_at = ?{extra_sql}
        WHERE operation_id = ? AND status = ? AND lease_owner_id = ?
        """,
        (
            to_status,
            now.isoformat(),
            *extra_assignments.values(),
            operation_id,
            running_status,
            owner_id,
        ),
    )
    return cursor.rowcount == 1


def expire_lease_to_ambiguous(
    connection: sqlite3.Connection,
    table: str,
    operation_id: str,
    *,
    running_status: str,
    ambiguous_status: str,
    now: datetime,
    note: str,
) -> bool:
    """Atomically moves a ``RUNNING`` operation to the terminal
    ``AMBIGUOUS`` status, but only if its lease has actually expired as
    of ``now`` -- checked in the exact same statement as the transition,
    so no read-then-write race is possible between "recovery decided this
    looks stale" and "the original owner renews it." A row whose lease
    columns are both ``NULL`` (a legacy ``RUNNING`` row written before
    this migration, which never had a lease at all) is treated as
    unconditionally expired -- fail closed, per ADR 0025: there is no
    signal to prove a legacy row's owner is still alive, so it is always
    eligible for recovery regardless of how recently it was touched.
    Returns ``True`` iff exactly one row was updated.
    """

    cursor = connection.execute(
        f"""
        UPDATE {table}
        SET status = ?, updated_at = ?, error = COALESCE(error, ?)
        WHERE operation_id = ? AND status = ?
          AND (lease_expires_at IS NULL OR lease_expires_at < ?)
        """,
        (
            ambiguous_status,
            now.isoformat(),
            note,
            operation_id,
            running_status,
            now.isoformat(),
        ),
    )
    return cursor.rowcount == 1


class LeaseHeartbeat:
    """A deterministic, wall-clock-driven background lease renewal
    ticker, independent of model/agent behavior: it renews on a fixed
    interval regardless of whether the underlying work has made any
    progress, so a long-but-healthy operation is never misclassified as
    crashed merely because a single model call or agent turn took a long
    time. Runs on its own daemon thread with its own callable rather than
    sharing the caller's SQLite connection (SQLite connections are not
    safe to use across threads).

    Tests inject a short ``interval``/``lease_duration`` (milliseconds,
    not real minutes) rather than mocking away the thread entirely, so
    the real threading and locking code path is still exercised without
    ever sleeping for a production lease duration.
    """

    def __init__(
        self,
        renew: Callable[[], bool],
        *,
        interval: timedelta = DEFAULT_HEARTBEAT_INTERVAL,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._renew = renew
        self._interval_seconds = interval.total_seconds()
        self._stop_event = threading.Event()
        self._lease_lost_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._sleep_fn = sleep_fn

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost_event.is_set()

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                renewed = self._renew()
            except Exception:
                renewed = False
            if not renewed:
                self._lease_lost_event.set()
                return


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_LEASE_DURATION",
    "LeaseHeartbeat",
    "LeaseLostError",
    "claim_lease",
    "ensure_lease_columns",
    "expire_lease_to_ambiguous",
    "new_owner_id",
    "release_lease",
    "renew_lease",
]
