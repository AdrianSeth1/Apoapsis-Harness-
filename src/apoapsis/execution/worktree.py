from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field

from apoapsis.repository.git import GitCommandError, GitRepository
from apoapsis.specification.schema import StrictModel


_TASK_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class WorktreeError(RuntimeError):
    """Raised when a managed worktree operation is unsafe or fails."""


class ManagedWorktree(StrictModel):
    task_slug: str
    path: str
    branch: str
    base_commit: str = Field(min_length=1)


class WorktreeManager:
    """Create and remove task worktrees under one controlled directory."""

    def __init__(
        self,
        repository: str | Path,
        *,
        worktree_root: str | Path | None = None,
        git_executable: str = "git",
    ) -> None:
        self.repository = GitRepository(
            repository, git_executable=git_executable
        )
        default_root = self.repository.root / ".apoapsis" / "worktrees"
        self.worktree_root = Path(worktree_root or default_root).resolve()
        if self.worktree_root == self.repository.root:
            raise WorktreeError("worktree root cannot be the repository root")

    def create(
        self,
        task_slug: str,
        *,
        base_ref: str = "HEAD",
        branch: str | None = None,
    ) -> ManagedWorktree:
        slug = self._validate_slug(task_slug)
        branch_name = branch or f"apoapsis/{slug.lower()}"
        self._validate_branch(branch_name)
        path = (self.worktree_root / slug).resolve()
        self._ensure_managed_path(path)
        if path.exists():
            raise WorktreeError(f"worktree path already exists: {path}")
        branch_check = self.repository.run(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            check=False,
        )
        if branch_check.returncode == 0:
            raise WorktreeError(f"branch already exists: {branch_name}")
        base_commit = self.repository.run(
            ["rev-parse", "--verify", f"{base_ref}^{{commit}}"]
        ).stdout.strip()
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        try:
            self.repository.run(
                ["worktree", "add", "-b", branch_name, str(path), base_commit]
            )
        except GitCommandError as exc:
            raise WorktreeError(str(exc)) from exc
        return ManagedWorktree(
            task_slug=slug,
            path=str(path),
            branch=branch_name,
            base_commit=base_commit,
        )

    def describe(self, task_slug: str) -> ManagedWorktree:
        slug = self._validate_slug(task_slug)
        path = (self.worktree_root / slug).resolve()
        self._ensure_managed_path(path)
        if not path.is_dir():
            raise WorktreeError(f"managed worktree does not exist: {path}")
        branch = self.repository.run(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=path
        ).stdout.strip()
        base_commit = self.repository.run(
            ["rev-parse", "HEAD"], cwd=path
        ).stdout.strip()
        return ManagedWorktree(
            task_slug=slug,
            path=str(path),
            branch=branch,
            base_commit=base_commit,
        )

    def cleanup(
        self,
        task_slug: str,
        *,
        force: bool = False,
        delete_branch: bool = False,
    ) -> None:
        managed = self.describe(task_slug)
        path = Path(managed.path)
        status = self.repository.run(
            ["status", "--porcelain=v1"], cwd=path
        ).stdout
        if status.strip() and not force:
            raise WorktreeError(
                "worktree contains uncommitted changes; use force only for an "
                "explicit rollback"
            )

        # Decide everything that can refuse *before* removing anything.
        #
        # This used to remove the worktree first and delete the branch last,
        # so a refusal at the final step left the one state no cleanup path
        # can repair: worktree gone, branch still present. `describe` raises
        # once the directory is missing, and `create` refuses while the
        # branch exists, so the task was wedged until an operator deleted the
        # branch by hand. Ordering the checks first makes cleanup all-or-
        # nothing: it either refuses having changed nothing, or it completes.
        if delete_branch and not force:
            self._require_branch_is_disposable(managed.branch)

        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        try:
            self.repository.run(args)
            self.repository.run(["worktree", "prune"])
            if delete_branch:
                # `-D` unconditionally: for a non-force cleanup the check
                # above already proved no commit is lost, and for a force
                # cleanup the caller has accepted the loss.
                self.repository.run(["branch", "-D", managed.branch])
        except GitCommandError as exc:
            raise WorktreeError(str(exc)) from exc

    def _require_branch_is_disposable(self, branch: str) -> None:
        """Refuse unless every commit on ``branch`` survives its deletion.

        `git branch -d` cannot answer this question in this repository.
        It asks whether the branch is merged into the *currently checked out*
        branch, and Apoapsis never moves the project's checked-out branch --
        `checkpoint_completed_prior_slices` commits on Apoapsis-owned task
        branches precisely so the operator's branch is left alone. Every task
        branch built on an inherited checkpoint is therefore unmerged with
        respect to that branch by construction, and `-d` refused all of them:
        a fresh retry of any slice after the first could not clean up after
        itself (live PLAN-19E795D6DC4B/SLICE-004, 2026-08-02).

        The question worth asking is not "is this merged into HEAD" but "does
        anything else still reach these commits", which is what actually
        decides whether deleting the branch destroys work.
        """

        tip = self.repository.run(["rev-parse", branch], check=False)
        if tip.returncode != 0:
            return
        contained = self.repository.run(
            ["branch", "--format=%(refname:short)", "--contains", tip.stdout.strip()],
            check=False,
        )
        if contained.returncode != 0:
            raise WorktreeError(
                f"cannot determine whether branch {branch} holds unique "
                "commits; refusing to delete it"
            )
        others = {
            line.strip().lstrip("+* ").strip()
            for line in contained.stdout.splitlines()
            if line.strip()
        } - {branch}
        if not others:
            raise WorktreeError(
                f"branch {branch} holds commits no other branch reaches, so "
                "deleting it would discard that work; checkpoint or merge it "
                "first, or use an explicit forced rollback"
            )

    @staticmethod
    def _validate_slug(task_slug: str) -> str:
        if not _TASK_SLUG.fullmatch(task_slug):
            raise WorktreeError(
                "task slug must contain only letters, numbers, '.', '_' or '-'"
            )
        return task_slug

    def _validate_branch(self, branch: str) -> None:
        result = self.repository.run(
            ["check-ref-format", "--branch", branch], check=False
        )
        if result.returncode != 0:
            raise WorktreeError(f"invalid branch name: {branch}")

    def _ensure_managed_path(self, path: Path) -> None:
        try:
            path.relative_to(self.worktree_root)
        except ValueError as exc:
            raise WorktreeError(
                f"path escapes managed worktree root: {path}"
            ) from exc
