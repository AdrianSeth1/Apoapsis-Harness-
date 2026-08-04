from __future__ import annotations

import os
import shutil
import sqlite3
import stat
from pathlib import Path

from apoapsis.specification.schema import (
    HardConstraint,
    SourceKind,
    TaskSpecification,
    TraceableStatement,
)


def remove_tree(path: str | Path) -> None:
    """Delete a directory tree that may contain a Git repository.

    Git writes its loose objects and packfiles read-only. On POSIX the
    containing directory's write bit is what governs unlink, so
    ``shutil.rmtree`` succeeds; on Windows the read-only attribute is
    enforced on the file itself and ``os.unlink`` raises
    ``PermissionError: [WinError 5]``. A test that deletes a project
    directory to simulate the user moving or deleting it therefore passed on
    Linux and failed on Windows for a reason with nothing to do with what it
    asserts.

    Clearing the attribute and retrying is what Git's own tooling does. This
    is a test helper on purpose: the product never deletes a user's
    repository, so it has no need of this and should not grow the ability.
    """

    def _clear_readonly_and_retry(func, target, _exc_info) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=_clear_readonly_and_retry)


def force_operation_status(
    database_path: str | Path,
    table: str,
    operation_id: str,
    *,
    status: str,
) -> None:
    """Test-only: directly overwrites an operation row's status and clears
    its lease columns, simulating an out-of-band write that none of the
    real store methods can produce (e.g. a bookkeeping call crashing right
    after the real work it was recording already landed). Clearing the
    lease columns matches ADR 0025's fail-closed handling of legacy rows
    written before the lease migration -- unconditionally eligible for
    recovery regardless of ``now``."""

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            f"UPDATE {table} SET status = ?, lease_owner_id = NULL, "
            "lease_expires_at = NULL WHERE operation_id = ?",
            (status, operation_id),
        )
        connection.commit()
    finally:
        connection.close()


def make_constraint(
    identifier: str = "HC-1", text: str = "Preserve the public API exactly."
) -> HardConstraint:
    return HardConstraint(
        id=identifier,
        text="Do not change public interfaces.",
        verbatim_source=text,
        interpreted_meaning="Existing callers require identical interfaces.",
        source=SourceKind.USER,
        source_reference="message-1",
        verification_method="Run the API snapshot test.",
    )


def make_specification(
    task_id: str = "TASK-TEST-001",
    *,
    constraints: list[HardConstraint] | None = None,
) -> TaskSpecification:
    return TaskSpecification(
        task_id=task_id,
        objective=TraceableStatement(
            text="Add resumable downloads.",
            source=SourceKind.USER,
            source_reference="message-1",
        ),
        hard_constraints=constraints or [],
    )

