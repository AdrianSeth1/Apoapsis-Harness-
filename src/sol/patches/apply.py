from __future__ import annotations

import subprocess
from pathlib import Path

from sol.patches.parser import ParsedDiff
from sol.repository.git import GitRepository


class PatchApplicationError(RuntimeError):
    """A validated patch could not be safely applied in the task worktree."""


class GitPatchApplier:
    def __init__(self, *, git_executable: str = "git") -> None:
        self.git_executable = git_executable

    def apply(self, parsed: ParsedDiff, worktree: str | Path) -> list[str]:
        root = Path(worktree).resolve()
        repository = GitRepository(root, git_executable=self.git_executable)
        if repository.root != root:
            raise PatchApplicationError("target is not the root of a Git worktree")
        before = self._changed_paths(repository)
        self._git_apply(root, parsed.raw, check_only=True)
        self._git_apply(root, parsed.raw, check_only=False)
        after = self._changed_paths(repository)
        allowed = before | parsed.paths
        unexpected = after - allowed
        if unexpected:
            self._git_reverse(root, parsed.raw)
            raise PatchApplicationError(
                f"patch produced unexpected changed paths: {sorted(unexpected)}"
            )
        return sorted(after)

    def _git_apply(self, root: Path, patch: str, *, check_only: bool) -> None:
        args = [self.git_executable, "apply", "--recount", "--whitespace=error-all"]
        if check_only:
            args.append("--check")
        result = subprocess.run(
            args,
            cwd=root,
            input=patch,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
        )
        if result.returncode != 0:
            raise PatchApplicationError(
                f"git apply {'check' if check_only else 'operation'} failed: "
                f"{result.stderr.strip()}"
            )

    def _git_reverse(self, root: Path, patch: str) -> None:
        subprocess.run(
            [self.git_executable, "apply", "--reverse", "--recount"],
            cwd=root,
            input=patch,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
        )

    @staticmethod
    def _changed_paths(repository: GitRepository) -> set[str]:
        status = repository.run(["status", "--porcelain=v1", "-z"]).stdout
        entries = [entry for entry in status.split("\0") if entry]
        paths: set[str] = set()
        index = 0
        while index < len(entries):
            entry = entries[index]
            code = entry[:2]
            path = entry[3:]
            paths.add(path.replace("\\", "/"))
            if "R" in code or "C" in code:
                index += 1
                if index < len(entries):
                    paths.add(entries[index].replace("\\", "/"))
            index += 1
        return paths

