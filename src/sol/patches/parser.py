from __future__ import annotations

import shlex
from dataclasses import dataclass


class UnifiedDiffError(RuntimeError):
    """The provider output is not a supported Git unified diff."""


@dataclass(frozen=True)
class ParsedDiffFile:
    old_path: str | None
    new_path: str | None
    added_lines: int
    deleted_lines: int
    is_new: bool
    is_deleted: bool
    is_binary: bool
    is_symlink: bool

    @property
    def effective_path(self) -> str:
        return self.new_path or self.old_path or ""


@dataclass(frozen=True)
class ParsedDiff:
    raw: str
    files: tuple[ParsedDiffFile, ...]

    @property
    def changed_lines(self) -> int:
        return sum(item.added_lines + item.deleted_lines for item in self.files)

    @property
    def paths(self) -> set[str]:
        return {item.effective_path for item in self.files if item.effective_path}


class UnifiedDiffParser:
    def parse(self, content: str) -> ParsedDiff:
        normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise UnifiedDiffError("provider returned an empty patch")
        if normalized.startswith("```") or not normalized.startswith("diff --git "):
            raise UnifiedDiffError(
                "patch must contain only a Git unified diff beginning with diff --git"
            )
        normalized += "\n"
        starts = [
            index
            for index, line in enumerate(normalized.splitlines(keepends=True))
            if line.startswith("diff --git ")
        ]
        lines = normalized.splitlines(keepends=True)
        files: list[ParsedDiffFile] = []
        for position, start in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(lines)
            files.append(self._parse_file(lines[start:end]))
        if not files:
            raise UnifiedDiffError("patch does not contain any file diffs")
        return ParsedDiff(raw=normalized, files=tuple(files))

    def _parse_file(self, lines: list[str]) -> ParsedDiffFile:
        try:
            header = shlex.split(lines[0].strip())
        except ValueError as exc:
            raise UnifiedDiffError("invalid diff --git header") from exc
        if len(header) != 4 or header[:2] != ["diff", "--git"]:
            raise UnifiedDiffError("invalid diff --git header")
        old_path = self._strip_prefix(header[2], "a/")
        new_path = self._strip_prefix(header[3], "b/")
        is_new = any(line.startswith("new file mode ") for line in lines)
        is_deleted = any(line.startswith("deleted file mode ") for line in lines)
        is_binary = any(
            line.startswith("GIT binary patch") or line.startswith("Binary files ")
            for line in lines
        )
        is_symlink = any(
            line.startswith(("new file mode 120000", "new mode 120000"))
            for line in lines
        )
        for line in lines:
            if line.startswith("--- ") and line[4:].strip() == "/dev/null":
                old_path = None
                is_new = True
            elif line.startswith("+++ ") and line[4:].strip() == "/dev/null":
                new_path = None
                is_deleted = True
        added = sum(
            1
            for line in lines
            if line.startswith("+") and not line.startswith("+++")
        )
        deleted = sum(
            1
            for line in lines
            if line.startswith("-") and not line.startswith("---")
        )
        if not is_binary and not any(line.startswith("@@ ") for line in lines):
            raise UnifiedDiffError(
                f"file diff for {new_path or old_path} does not contain a hunk"
            )
        return ParsedDiffFile(
            old_path=old_path,
            new_path=new_path,
            added_lines=added,
            deleted_lines=deleted,
            is_new=is_new,
            is_deleted=is_deleted,
            is_binary=is_binary,
            is_symlink=is_symlink,
        )

    @staticmethod
    def _strip_prefix(path: str, prefix: str) -> str:
        if not path.startswith(prefix):
            raise UnifiedDiffError(f"diff path must begin with {prefix}: {path}")
        return path[len(prefix) :]
