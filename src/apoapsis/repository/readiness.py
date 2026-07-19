from __future__ import annotations

from pathlib import Path

from apoapsis.repository.git import GitRepository


class DirtyParentRepositoryError(RuntimeError):
    """Raised when the parent repository has uncommitted tracked changes
    or untracked files at the moment execution is about to start (ADR
    0026).

    ``VerticalSliceRunner._run_from_approved`` compiles the agent's
    initial context by reading directly from the parent checkout, but
    ``WorktreeManager.create()`` creates the task's isolated worktree from
    clean HEAD, carrying none of that uncommitted state. If the parent
    checkout is dirty, the two disagree about what the repository
    contains: the context could describe file content the worktree the
    agent actually edits does not have. Failing closed here is the only
    safe response -- Apoapsis never stashes, resets, deletes, or commits
    a user's uncommitted work automatically."""


def require_clean_parent_repository(project_root: str | Path) -> None:
    snapshot = GitRepository(project_root).snapshot()
    if snapshot.is_clean:
        return
    changed = ", ".join(snapshot.changed_files[:20])
    more = (
        f" (and {len(snapshot.changed_files) - 20} more)"
        if len(snapshot.changed_files) > 20
        else ""
    )
    raise DirtyParentRepositoryError(
        "the parent repository has uncommitted tracked changes or "
        "untracked files, so the context the agent would see (compiled "
        "from the parent checkout) would not match the isolated worktree "
        "it will actually edit (created fresh from clean HEAD). Commit or "
        "stash these changes yourself first -- Apoapsis will not modify "
        f"them automatically. Changed: {changed}{more}"
    )


__all__ = ["DirtyParentRepositoryError", "require_clean_parent_repository"]
