from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel


class GitCommandError(RuntimeError):
    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.args_run = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"git command failed ({returncode}): git {' '.join(args)}: "
            f"{stderr.strip()}"
        )


class RepositoryHasNoCommitsError(RuntimeError):
    """Raised when an operation needs a resolvable `HEAD` but the repository
    has no commits yet (an "unborn" branch).

    Almost everything Apoapsis does is anchored to a base commit: context
    compilation, worktree isolation, fingerprints, planning packages, and
    every audit record that names the code a decision was made against.
    `git rev-parse HEAD` fails with an `ambiguous argument 'HEAD'` message
    that says nothing about the actual problem, so this error is raised
    instead, naming the fix.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = str(root)
        super().__init__(
            f"repository {self.root} has no commits yet, so there is no base "
            "commit to anchor this work to. Make one commit first (for "
            "example `git add -A` then `git commit -m \"initial commit\"`) "
            "and retry."
        )


def _is_unborn_head_error(error: GitCommandError) -> bool:
    stderr = error.stderr.lower()
    return "ambiguous argument 'head'" in stderr or "unknown revision" in stderr


class RepositorySnapshot(StrictModel):
    root: str
    head_commit: str
    branch: str | None
    is_clean: bool
    changed_files: list[str] = Field(default_factory=list)


class GitRepository:
    def __init__(
        self,
        path: str | Path,
        *,
        git_executable: str = "git",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.path = Path(path).resolve()
        self.git_executable = git_executable
        self.timeout_seconds = timeout_seconds
        root = self.run(["rev-parse", "--show-toplevel"]).stdout.strip()
        self.root = Path(root).resolve()

    def run(
        self,
        args: list[str],
        *,
        cwd: str | Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [self.git_executable, *args],
            cwd=Path(cwd).resolve() if cwd else self.path,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            shell=False,
        )
        if check and completed.returncode != 0:
            raise GitCommandError(args, completed.returncode, completed.stderr)
        return completed

    def has_commits(self) -> bool:
        """True when `HEAD` resolves to a commit. Never raises for the
        ordinary "no commits yet" case, so callers can check cheaply before
        starting work."""

        return self.run(["rev-parse", "--verify", "--quiet", "HEAD"], check=False).returncode == 0

    def head_commit(self) -> str:
        """The current `HEAD` commit, or a legible error explaining that the
        repository has no commits yet."""

        try:
            return self.run(["rev-parse", "HEAD"]).stdout.strip()
        except GitCommandError as exc:
            if _is_unborn_head_error(exc):
                raise RepositoryHasNoCommitsError(self.root) from exc
            raise

    def snapshot(self) -> RepositorySnapshot:
        head = self.head_commit()
        branch_result = self.run(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False
        )
        branch = (
            branch_result.stdout.strip()
            if branch_result.returncode == 0
            else None
        )
        status = self.run(["status", "--porcelain=v1", "-z"]).stdout
        entries = [entry for entry in status.split("\0") if entry]
        changed_files = [entry[3:] if len(entry) > 3 else entry for entry in entries]
        return RepositorySnapshot(
            root=str(self.root),
            head_commit=head,
            branch=branch,
            is_clean=not entries,
            changed_files=changed_files,
        )

