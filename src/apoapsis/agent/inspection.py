from __future__ import annotations

import json
import re
import subprocess
from difflib import unified_diff
from pathlib import Path, PurePosixPath

from apoapsis.context.provenance import (
    ContextEvidence,
    EvidenceKind,
    TransmissionPolicy,
)
from apoapsis.repository.git import GitRepository


class AgentInspectionError(RuntimeError):
    """A requested repository inspection was unsafe or could not be completed."""


class RepositoryInspector:
    """Read-only, bounded repository actions executed without a shell."""

    def __init__(
        self,
        worktree: str | Path,
        *,
        max_search_results: int,
        max_read_lines: int,
        max_chars: int,
        ripgrep_executable: str = "rg",
    ) -> None:
        self.root = Path(worktree).resolve()
        self.repository = GitRepository(self.root)
        if self.repository.root != self.root:
            raise AgentInspectionError("inspection target must be a Git worktree root")
        self.max_search_results = max_search_results
        self.max_read_lines = max_read_lines
        self.max_chars = max_chars
        self.ripgrep_executable = ripgrep_executable

    def search(
        self, query: str, path_glob: str | None = None
    ) -> list[ContextEvidence]:
        query = query.strip()
        if not query:
            raise AgentInspectionError("search query must not be empty")
        if path_glob is not None:
            self._validate_glob(path_glob)
        args = [
            self.ripgrep_executable,
            "--json",
            "--fixed-strings",
            "--color",
            "never",
            "--glob",
            "!.git/**",
            "--glob",
            "!.apoapsis/**",
            "--glob",
            "!.sol/**",
        ]
        if path_glob:
            args.extend(["--glob", path_glob])
        args.extend([query, "."])
        try:
            completed = subprocess.run(
                args,
                cwd=self.root,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentInspectionError(f"repository search failed: {exc}") from exc
        if completed.returncode not in {0, 1}:
            raise AgentInspectionError(
                f"repository search failed: {completed.stderr.strip()}"
            )
        matches: dict[tuple[str, int], str] = {}
        allowed = self._repository_files()
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            raw_path = ((data.get("path") or {}).get("text") or "")
            relative = self._normalize_path(raw_path)
            line_number = int(data.get("line_number") or 0)
            content = ((data.get("lines") or {}).get("text") or "")
            if relative in allowed and line_number > 0:
                matches[(relative, line_number)] = content.rstrip("\r\n")
        changed = self._changed_paths()
        head = self._head()
        evidence: list[ContextEvidence] = []
        used_chars = 0
        for (path, line_number), content in sorted(matches.items()):
            if len(evidence) >= self.max_search_results:
                break
            remaining = self.max_chars - used_chars
            if remaining <= 0:
                break
            content = content[:remaining]
            evidence.append(
                ContextEvidence(
                    evidence_id=f"EV-AGENT-SEARCH-{len(evidence) + 1:03d}",
                    kind=self._evidence_kind(path),
                    path=path,
                    start_line=line_number,
                    end_line=line_number,
                    commit=(
                        f"{head}+working-tree" if path in changed else head
                    ),
                    reason_included=f"agent literal search for {query!r}",
                    content=content,
                    transmission_policy=TransmissionPolicy.CLOUD_ALLOWED,
                )
            )
            used_chars += len(content)
        return evidence

    def read(
        self, path: str, start_line: int = 1, end_line: int | None = None
    ) -> ContextEvidence:
        relative = self._validate_path(path)
        if relative not in self._repository_files():
            raise AgentInspectionError(f"path is not a repository file: {relative}")
        destination = (self.root / Path(*PurePosixPath(relative).parts)).resolve()
        raw = destination.read_bytes()
        if b"\0" in raw:
            raise AgentInspectionError(f"binary files cannot be read: {relative}")
        content = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
        lines = content.replace("\r", "\n").splitlines()
        if start_line > len(lines):
            raise AgentInspectionError(
                f"start_line {start_line} exceeds {len(lines)} lines in {relative}"
            )
        requested_end = end_line or (start_line + self.max_read_lines - 1)
        if requested_end < start_line:
            raise AgentInspectionError("end_line must not precede start_line")
        actual_end = min(
            requested_end,
            start_line + self.max_read_lines - 1,
            len(lines),
        )
        excerpt = "\n".join(lines[start_line - 1 : actual_end])
        excerpt = excerpt[: self.max_chars]
        actual_end = start_line + excerpt.count("\n")
        head = self._head()
        return ContextEvidence(
            evidence_id="EV-AGENT-READ-001",
            kind=self._evidence_kind(relative),
            path=relative,
            start_line=start_line,
            end_line=actual_end,
            commit=(
                f"{head}+working-tree"
                if relative in self._changed_paths()
                else head
            ),
            reason_included="explicit agent file read",
            content=excerpt,
            transmission_policy=TransmissionPolicy.CLOUD_ALLOWED,
        )

    def diff(self) -> ContextEvidence | None:
        content = self.repository.run(
            ["diff", "--no-ext-diff", "--unified=5", "HEAD"]
        ).stdout
        if not content:
            return None
        head = self._head()
        return ContextEvidence(
            evidence_id="EV-AGENT-DIFF-001",
            kind=EvidenceKind.DIFF,
            path="<working-tree-diff>",
            commit=f"{head}+working-tree",
            reason_included="agent requested the exact current Git diff",
            content=content[: self.max_chars],
            transmission_policy=TransmissionPolicy.CLOUD_ALLOWED,
        )

    def replacement_patch(
        self, path: str, old_text: str, new_text: str
    ) -> str:
        relative = self._validate_path(path)
        if relative not in self._repository_files():
            raise AgentInspectionError(f"path is not a repository file: {relative}")
        destination = (self.root / Path(*PurePosixPath(relative).parts)).resolve()
        raw = destination.read_bytes()
        if b"\0" in raw:
            raise AgentInspectionError(f"binary files cannot be edited: {relative}")
        current = raw.decode("utf-8", errors="strict").replace("\r\n", "\n")
        current = current.replace("\r", "\n")
        occurrences = current.count(old_text)
        if occurrences != 1:
            raise AgentInspectionError(
                f"replace_text old_text must occur exactly once in {relative}; "
                f"found {occurrences} matches"
            )
        # Model-generated source edits commonly indent otherwise blank lines.
        # Canonicalize only whitespace-only lines; meaningful trailing whitespace
        # remains subject to the existing strict Git whitespace policy.
        new_text = "\n".join(
            "" if line.strip() == "" else line
            for line in new_text.split("\n")
        )
        updated = current.replace(old_text, new_text, 1)
        body = "".join(
            unified_diff(
                current.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
                n=3,
                lineterm="\n",
            )
        )
        if not body:
            raise AgentInspectionError("replace_text did not change the file")
        return f"diff --git a/{relative} b/{relative}\n{body}"

    def has_changes(self) -> bool:
        return bool(self._changed_paths())

    def changed_paths(self) -> list[str]:
        return sorted(self._changed_paths())

    def _repository_files(self) -> set[str]:
        raw = self.repository.run(
            ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
        ).stdout
        return {
            normalized
            for item in raw.split("\0")
            if item
            for normalized in [self._normalize_path(item)]
            if self._path_is_safe(normalized)
        }

    def _changed_paths(self) -> set[str]:
        raw = self.repository.run(
            ["status", "--porcelain=v1", "-z"]
        ).stdout
        return {
            self._normalize_path(item[3:])
            for item in raw.split("\0")
            if len(item) > 3
        }

    def _head(self) -> str:
        return self.repository.run(["rev-parse", "HEAD"]).stdout.strip()

    def _validate_path(self, path: str) -> str:
        normalized = self._normalize_path(path)
        if not self._path_is_safe(normalized):
            raise AgentInspectionError(f"unsafe repository path: {path}")
        destination = (self.root / Path(*PurePosixPath(normalized).parts)).resolve()
        try:
            destination.relative_to(self.root)
        except ValueError as exc:
            raise AgentInspectionError(f"repository path escape: {path}") from exc
        return normalized

    @staticmethod
    def _normalize_path(path: str) -> str:
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    @staticmethod
    def _path_is_safe(path: str) -> bool:
        if (
            not path
            or "\0" in path
            or path.startswith("/")
            or re.match(r"^[A-Za-z]:", path)
        ):
            return False
        parts = PurePosixPath(path).parts
        return bool(parts) and ".." not in parts and parts[0] not in {
            ".git",
            ".apoapsis",
            ".sol",
        }

    def _validate_glob(self, path_glob: str) -> None:
        if (
            not self._path_is_safe(path_glob)
            or path_glob.startswith("!")
            or "\\" in path_glob
        ):
            raise AgentInspectionError(f"unsafe path_glob: {path_glob}")

    @staticmethod
    def _evidence_kind(path: str) -> EvidenceKind:
        pure = PurePosixPath(path)
        if (
            "tests" in pure.parts
            or pure.name.startswith("test_")
            or pure.stem.endswith("_test")
        ):
            return EvidenceKind.TEST
        return EvidenceKind.FILE_EXCERPT
