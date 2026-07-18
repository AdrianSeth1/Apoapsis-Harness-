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

    def snapshot(self) -> RepositorySnapshot:
        head = self.run(["rev-parse", "HEAD"]).stdout.strip()
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

