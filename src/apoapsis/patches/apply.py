from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from apoapsis.patches.parser import ParsedDiff
from apoapsis.repository.git import GitRepository


class PatchApplicationError(RuntimeError):
    """A validated patch could not be safely applied in the task worktree."""


class GitPatchApplier:
    def __init__(self, *, git_executable: str = "git") -> None:
        self.git_executable = git_executable
        self.last_applied_patch: str | None = None

    def apply(self, parsed: ParsedDiff, worktree: str | Path) -> list[str]:
        root = Path(worktree).resolve()
        repository = GitRepository(root, git_executable=self.git_executable)
        if repository.root != root:
            raise PatchApplicationError("target is not the root of a Git worktree")
        before = self._changed_paths(repository)
        applicable_patch = self._rebase_unique_hunk_headers(parsed.raw, root)
        self.last_applied_patch = applicable_patch
        original_line_endings = self._normalize_target_line_endings(parsed, root)
        try:
            self._git_apply(root, applicable_patch, check_only=True)
            self._git_apply(root, applicable_patch, check_only=False)
        except Exception:
            self._restore_original_bytes(original_line_endings)
            raise
        after = self._changed_paths(repository)
        allowed = before | parsed.paths
        unexpected = after - allowed
        if unexpected:
            self._git_reverse(root, applicable_patch)
            self._restore_original_bytes(original_line_endings)
            raise PatchApplicationError(
                f"patch produced unexpected changed paths: {sorted(unexpected)}"
            )
        return sorted(after)

    @staticmethod
    def _normalize_target_line_endings(
        parsed: ParsedDiff, root: Path
    ) -> dict[Path, bytes]:
        originals: dict[Path, bytes] = {}
        for relative in sorted(parsed.paths):
            path = (root / relative).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if not path.is_file():
                continue
            original = path.read_bytes()
            if b"\0" in original:
                continue
            normalized = original.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            if normalized != original:
                originals[path] = original
                path.write_bytes(normalized)
        return originals

    @staticmethod
    def _restore_original_bytes(originals: dict[Path, bytes]) -> None:
        for path, content in originals.items():
            path.write_bytes(content)

    def _rebase_unique_hunk_headers(self, patch: str, root: Path) -> str:
        """Correct model hunk coordinates only when old context matches uniquely."""

        lines = patch.splitlines()
        result: list[str] = []
        current_path: str | None = None
        source_lines: list[str] = []
        line_delta = 0
        index = 0
        while index < len(lines):
            line = lines[index]
            if line.startswith("diff --git "):
                try:
                    header = shlex.split(line)
                    current_path = (
                        header[2][2:]
                        if len(header) == 4 and header[2].startswith("a/")
                        else None
                    )
                except ValueError:
                    current_path = None
                source_lines = self._source_lines(root, current_path)
                line_delta = 0
                result.append(line)
                index += 1
                continue
            match = re.match(
                r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$",
                line,
            )
            if not match:
                result.append(line)
                index += 1
                continue
            body_end = index + 1
            while body_end < len(lines) and not lines[body_end].startswith(
                ("@@ ", "diff --git ")
            ):
                body_end += 1
            body = lines[index + 1 : body_end]
            old_lines = [
                item[1:]
                for item in body
                if item.startswith((" ", "-"))
            ]
            new_lines = [
                item[1:]
                for item in body
                if item.startswith((" ", "+"))
            ]
            old_start = int(match.group(1))
            matches = self._matching_starts(source_lines, old_lines)
            if len(matches) == 1:
                match_start = matches[0]
                old_start = match_start + 1
                if body and not body[0].startswith(" ") and match_start > 0:
                    body.insert(0, f" {source_lines[match_start - 1]}")
                    old_start -= 1
                content_end = len(body)
                while content_end and body[content_end - 1].startswith("\\"):
                    content_end -= 1
                matched_end = matches[0] + len(old_lines)
                if (
                    content_end
                    and not body[content_end - 1].startswith(" ")
                    and matched_end < len(source_lines)
                ):
                    body.insert(content_end, f" {source_lines[matched_end]}")
                old_lines = [
                    item[1:] for item in body if item.startswith((" ", "-"))
                ]
                new_lines = [
                    item[1:] for item in body if item.startswith((" ", "+"))
                ]
            old_count = len(old_lines)
            new_count = len(new_lines)
            new_start = old_start + line_delta
            result.append(
                f"@@ -{old_start},{old_count} +{new_start},{new_count} @@"
                f"{match.group(5)}"
            )
            line_delta += new_count - old_count
            result.extend(body)
            index = body_end
        return "\n".join(result) + "\n"

    @staticmethod
    def _source_lines(root: Path, relative_path: str | None) -> list[str]:
        if relative_path is None:
            return []
        path = (root / relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return []
        if not path.is_file():
            return []
        return (
            path.read_text(encoding="utf-8", errors="replace")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .splitlines()
        )

    @staticmethod
    def _matching_starts(source: list[str], sought: list[str]) -> list[int]:
        if not sought or len(sought) > len(source):
            return []
        return [
            start
            for start in range(len(source) - len(sought) + 1)
            if source[start : start + len(sought)] == sought
        ]

    def _git_apply(self, root: Path, patch: str, *, check_only: bool) -> None:
        args = [
            self.git_executable,
            "apply",
            "--recount",
            "--whitespace=error-all",
            "--verbose",
        ]
        if check_only:
            args.append("--check")
        result = subprocess.run(
            args,
            cwd=root,
            input=patch.encode("utf-8"),
            check=False,
            capture_output=True,
            timeout=30,
            shell=False,
        )
        if result.returncode != 0:
            raise PatchApplicationError(
                f"git apply {'check' if check_only else 'operation'} failed: "
                f"{result.stderr.decode('utf-8', errors='replace').strip()}"
            )

    def _git_reverse(self, root: Path, patch: str) -> None:
        subprocess.run(
            [self.git_executable, "apply", "--reverse", "--recount"],
            cwd=root,
            input=patch.encode("utf-8"),
            check=False,
            capture_output=True,
            timeout=30,
            shell=False,
        )

    @staticmethod
    def _changed_paths(repository: GitRepository) -> set[str]:
        # Git normally collapses an entirely untracked directory to one
        # porcelain entry such as ``?? tests/``.  Patch policy reasons about
        # files, so that default made a valid patch adding
        # ``tests/__init__.py`` look as though it had unexpectedly changed the
        # directory ``tests/``.  Always request individual untracked files for
        # the before/after path comparison.
        status = repository.run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
        ).stdout
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
