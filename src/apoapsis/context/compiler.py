from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import Field, model_validator

from apoapsis.config import ContextCompilerConfig
from apoapsis.context.provenance import (
    ContextEvidence,
    EvidenceKind,
    TransmissionPolicy,
)
from apoapsis.repository.git import GitRepository
from apoapsis.specification.schema import StrictModel, TaskSpecification


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_PATH = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z0-9_.-]+[/\\])+"
    r"[A-Za-z0-9_.-]+\.[A-Za-z0-9]+"
)
_DIFF_HUNK = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
_PYTHON_DEFINITION = re.compile(
    r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
)
_STOP_WORDS = {
    "add",
    "after",
    "and",
    "change",
    "current",
    "does",
    "existing",
    "for",
    "from",
    "have",
    "must",
    "not",
    "only",
    "preserve",
    "should",
    "task",
    "that",
    "the",
    "this",
    "user",
    "with",
    "without",
}
_TEXT_SUFFIXES = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".md",
    ".sql",
    ".ini",
    ".cfg",
    ".txt",
}


@dataclass
class _ChangedFile:
    current_lines: set[int] = field(default_factory=set)
    changed_text: list[str] = field(default_factory=list)


class ContextPackage(StrictModel):
    package_version: str = "1.0"
    compiler_version: str = "deterministic-python-v2"
    task_id: str
    specification: TaskSpecification
    head_commit: str
    query_terms: list[str] = Field(default_factory=list)
    retrieval_tools: list[str] = Field(default_factory=list)
    compiler_parameters: dict[str, Any] = Field(default_factory=dict)
    external_research_brief: str | None = None
    research_evidence_ids: list[str] = Field(default_factory=list)
    evidence: list[ContextEvidence] = Field(default_factory=list)
    context_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def derive_digest(self) -> ContextPackage:
        canonical = self.model_dump(
            mode="json", exclude={"context_sha256"}
        )
        digest = hashlib.sha256(
            json.dumps(
                canonical, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        if self.context_sha256 is None:
            self.context_sha256 = digest
        elif self.context_sha256 != digest:
            raise ValueError("context_sha256 does not match package content")
        return self

    @classmethod
    def specification_only(
        cls, specification: TaskSpecification, head_commit: str
    ) -> ContextPackage:
        return cls(
            task_id=specification.task_id,
            specification=specification,
            head_commit=head_commit,
            query_terms=[],
            retrieval_tools=["git"],
            compiler_parameters={"mode": "specification_only"},
            evidence=[],
        )


class ContextCompiler:
    def __init__(
        self,
        config: ContextCompilerConfig | None = None,
        *,
        ripgrep_executable: str = "rg",
    ) -> None:
        self.config = config or ContextCompilerConfig()
        self.ripgrep_executable = ripgrep_executable

    def compile(
        self,
        specification: TaskSpecification,
        repository_root: str | Path,
        *,
        extra_queries: list[str] | None = None,
        preferred_paths: list[str] | None = None,
        preferred_line_anchors: dict[str, int] | None = None,
        external_research_brief: str | None = None,
        research_evidence_ids: list[str] | None = None,
    ) -> ContextPackage:
        repository = GitRepository(repository_root)
        root = repository.root
        head = repository.run(["rev-parse", "HEAD"]).stdout.strip()
        files = self._repository_files(repository)
        text_cache: dict[str, str] = {}
        reasons: dict[str, set[str]] = defaultdict(set)
        combined = self._combined_task_text(specification, extra_queries or [])
        terms = self._query_terms(combined)
        current_diff = repository.run(
            ["diff", "--no-ext-diff", "--unified=3", "HEAD"]
        ).stdout
        changed_files = self._parse_changed_files(current_diff)
        changed_paths = {
            self._normalize_relative(line)
            for line in repository.run(["diff", "--name-only", "HEAD"])
            .stdout.splitlines()
            if line.strip()
        }
        changed_paths = {
            path for path in changed_paths if path in files and not self._excluded(path)
        }
        changed_symbols = self._changed_python_symbols(
            root, files, changed_files, text_cache
        )

        for path in self._explicit_paths(combined):
            if path in files and not self._excluded(path):
                reasons[path].add("explicit path in task or failure")
        for path in preferred_paths or []:
            normalized = self._normalize_relative(path)
            if normalized in files and not self._excluded(normalized):
                reasons[normalized].add("preferred path from current patch")
        line_anchors: dict[str, int] = {}
        for path, line in (preferred_line_anchors or {}).items():
            normalized = self._normalize_relative(path)
            if normalized in files and not self._excluded(normalized) and line >= 1:
                line_anchors[normalized] = line
                reasons[normalized].add(
                    f"normalized failure location at line {line}"
                )
        for path in changed_paths:
            reasons[path].add("current Git diff")

        ripgrep_used = self._ripgrep_search(root, terms, files, reasons)
        selected_symbols = self._symbol_search(
            root, files, terms, reasons, text_cache
        )
        reference_symbols = selected_symbols | changed_symbols
        reference_files = self._symbol_reference_neighbors(
            root, files, reference_symbols, reasons, text_cache
        )
        self._import_neighbors(root, files, reasons, text_cache)
        self._related_tests(
            root,
            files,
            terms,
            reasons,
            text_cache,
            symbol_names=reference_symbols,
        )

        candidate_file_count = len(reasons)
        ordered = sorted(
            reasons,
            key=lambda path: (self._priority(reasons[path]), path),
        )[: self.config.max_files]
        evidence: list[ContextEvidence] = []
        total_chars = 0
        excerpts_truncated_for_char_budget = 0
        files_dropped_for_char_budget = 0
        for index, path in enumerate(ordered):
            content = self._read_text(root, path, text_cache)
            if content is None:
                continue
            excerpt = self._excerpt(
                content, terms, preferred_line=line_anchors.get(path)
            )
            if excerpt is None:
                continue
            start_line, end_line, excerpt_text = excerpt
            remaining = self.config.max_total_chars - total_chars
            if remaining <= 0:
                files_dropped_for_char_budget = len(ordered) - index
                break
            if len(excerpt_text) > remaining:
                excerpt_text = excerpt_text[:remaining]
                end_line = start_line + excerpt_text.count("\n")
                excerpts_truncated_for_char_budget += 1
            evidence.append(
                ContextEvidence(
                    evidence_id=f"EV-{len(evidence) + 1:03d}",
                    kind=self._evidence_kind(path),
                    path=path,
                    start_line=start_line,
                    end_line=end_line,
                    commit=(
                        f"{head}+working-tree" if path in changed_paths else head
                    ),
                    reason_included="; ".join(sorted(reasons[path])),
                    content=excerpt_text,
                    transmission_policy=TransmissionPolicy.CLOUD_ALLOWED,
                )
            )
            total_chars += len(excerpt_text)

        if current_diff and total_chars < self.config.max_total_chars:
            remaining = self.config.max_total_chars - total_chars
            diff_excerpt = current_diff[:remaining]
            evidence.append(
                ContextEvidence(
                    evidence_id=f"EV-{len(evidence) + 1:03d}",
                    kind=EvidenceKind.DIFF,
                    path="<working-tree-diff>",
                    commit=f"{head}+working-tree",
                    reason_included="exact current Git diff",
                    content=diff_excerpt,
                    transmission_policy=TransmissionPolicy.CLOUD_ALLOWED,
                )
            )

        tools = [
            "git",
            "git_diff_symbols",
            "python_ast_symbols",
            "python_ast_references",
            "python_imports",
            "test_discovery",
        ]
        if line_anchors:
            tools.append("failure_line_anchors")
        tools.append("ripgrep" if ripgrep_used else "lexical_fallback")
        parameters = self.config.model_dump(mode="json")
        parameters["candidate_file_count"] = candidate_file_count
        parameters["files_truncated_by_limit"] = max(
            0, candidate_file_count - self.config.max_files
        )
        parameters["files_dropped_for_char_budget"] = files_dropped_for_char_budget
        parameters["excerpts_truncated_for_char_budget"] = (
            excerpts_truncated_for_char_budget
        )
        parameters["changed_symbol_names"] = sorted(changed_symbols)
        parameters["symbol_reference_file_count"] = len(reference_files)
        parameters["failure_line_anchor_count"] = len(line_anchors)
        return ContextPackage(
            task_id=specification.task_id,
            specification=specification,
            head_commit=head,
            query_terms=terms,
            retrieval_tools=tools,
            compiler_parameters=parameters,
            external_research_brief=external_research_brief,
            research_evidence_ids=sorted(research_evidence_ids or []),
            evidence=evidence,
        )

    def _repository_files(self, repository: GitRepository) -> set[str]:
        raw = repository.run(
            ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
        ).stdout
        return {
            self._normalize_relative(path)
            for path in raw.split("\0")
            if path and not self._excluded(self._normalize_relative(path))
        }

    @staticmethod
    def _combined_task_text(
        specification: TaskSpecification, extra_queries: list[str]
    ) -> str:
        parts = [specification.objective.text]
        parts.extend(item.text for item in specification.acceptance_criteria)
        parts.extend(item.verbatim_source for item in specification.hard_constraints)
        parts.extend(extra_queries)
        return "\n".join(parts)

    def _query_terms(self, text: str) -> list[str]:
        words = [
            word.lower()
            for word in _WORD.findall(text)
            if len(word) >= 4 and word.lower() not in _STOP_WORDS
        ]
        frequencies = Counter(words)
        for word, count in list(frequencies.items()):
            if word.endswith("s") and len(word) > 5:
                stem = word[:-1]
                frequencies[stem] = max(frequencies[stem], count)
        ranked = sorted(
            frequencies,
            key=lambda item: (-frequencies[item], -len(item), item),
        )
        selected: list[str] = []
        for word in ranked:
            if word in selected:
                continue
            variants = [word]
            if word.endswith("s") and len(word) > 5:
                stem = word[:-1]
                if stem in frequencies:
                    variants.append(stem)
            remaining = self.config.max_search_terms - len(selected)
            if len(variants) > remaining and remaining == 1:
                variants = [variants[-1]]
            for variant in variants:
                if variant not in selected:
                    selected.append(variant)
                if len(selected) == self.config.max_search_terms:
                    return selected
        return selected

    @staticmethod
    def _explicit_paths(text: str) -> list[str]:
        return sorted(
            {
                ContextCompiler._normalize_relative(match.group(0))
                for match in _PATH.finditer(text)
            }
        )

    def _ripgrep_search(
        self,
        root: Path,
        terms: list[str],
        files: set[str],
        reasons: dict[str, set[str]],
    ) -> bool:
        try:
            for term in terms:
                result = subprocess.run(
                    [
                        self.ripgrep_executable,
                        "-l",
                        "-i",
                        "--fixed-strings",
                        "--glob",
                        "!.git/**",
                        "--glob",
                        "!.apoapsis/**",
                        "--glob",
                        "!.sol/**",
                        "--",
                        term,
                        ".",
                    ],
                    cwd=root,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    shell=False,
                )
                for line in result.stdout.splitlines():
                    path = self._normalize_relative(line.removeprefix("./"))
                    if path in files and not self._excluded(path):
                        reasons[path].add(f"ripgrep term: {term}")
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._lexical_fallback(root, terms, files, reasons, {})
            return False

    def _lexical_fallback(
        self,
        root: Path,
        terms: list[str],
        files: set[str],
        reasons: dict[str, set[str]],
        cache: dict[str, str],
    ) -> None:
        for path in sorted(files):
            content = self._read_text(root, path, cache)
            if content is None:
                continue
            lowered = content.lower()
            for term in terms:
                if term in lowered:
                    reasons[path].add(f"lexical term: {term}")

    def _symbol_search(
        self,
        root: Path,
        files: set[str],
        terms: list[str],
        reasons: dict[str, set[str]],
        cache: dict[str, str],
    ) -> set[str]:
        term_set = set(terms)
        selected_symbols: set[str] = set()
        for path in sorted(item for item in files if item.endswith(".py")):
            content = self._read_text(root, path, cache)
            if content is None:
                continue
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    words = {part.lower() for part in node.name.split("_")}
                    matched = sorted(words & term_set)
                    if matched:
                        selected_symbols.add(node.name)
                        reasons[path].add(
                            f"Python symbol {node.name} matches {matched[0]}"
                        )
        return selected_symbols

    def _symbol_reference_neighbors(
        self,
        root: Path,
        files: set[str],
        symbol_names: set[str],
        reasons: dict[str, set[str]],
        cache: dict[str, str],
    ) -> set[str]:
        """Add a deterministic one-hop neighborhood of Python call sites.

        Only actual ``ast.Call`` targets are considered; an identifier merely
        appearing in a string, comment, assignment, or import is not a call
        edge. Attribute calls use the terminal attribute name so
        ``client.download()`` can reference ``download`` without pretending
        to resolve Python's dynamic dispatch.
        """

        if not symbol_names:
            return set()
        reference_files: set[str] = set()
        for path in sorted(item for item in files if item.endswith(".py")):
            content = self._read_text(root, path, cache)
            if content is None:
                continue
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            called: set[str] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
            matched = sorted(called & symbol_names)
            if matched:
                reasons[path].add(f"Python call reference to {matched[0]}")
                reference_files.add(path)
        return reference_files

    def _import_neighbors(
        self,
        root: Path,
        files: set[str],
        reasons: dict[str, set[str]],
        cache: dict[str, str],
    ) -> None:
        frontier = sorted(path for path in reasons if path.endswith(".py"))
        visited: set[str] = set()
        for _ in range(self.config.max_import_depth):
            next_frontier: set[str] = set()
            for path in frontier:
                if path in visited:
                    continue
                visited.add(path)
                content = self._read_text(root, path, cache)
                if content is None:
                    continue
                try:
                    tree = ast.parse(content)
                except SyntaxError:
                    continue
                modules: set[tuple[str, int]] = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        modules.update((alias.name, 0) for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        modules.add((node.module, node.level))
                for module, level in sorted(modules):
                    relative = module.replace(".", "/")
                    if level:
                        base = PurePosixPath(path).parent
                        for _ in range(level - 1):
                            base = base.parent
                        relative = (base / relative).as_posix()
                        candidates = [
                            f"{relative}.py",
                            f"{relative}/__init__.py",
                        ]
                    else:
                        candidates = [
                            f"{relative}.py",
                            f"{relative}/__init__.py",
                            f"src/{relative}.py",
                            f"src/{relative}/__init__.py",
                        ]
                    for candidate in candidates:
                        if candidate in files and not self._excluded(candidate):
                            reasons[candidate].add(f"imported by {path}")
                            if candidate not in visited:
                                next_frontier.add(candidate)
            frontier = sorted(next_frontier)
            if not frontier:
                break

    def _related_tests(
        self,
        root: Path,
        files: set[str],
        terms: list[str],
        reasons: dict[str, set[str]],
        cache: dict[str, str],
        *,
        symbol_names: set[str] | None = None,
    ) -> None:
        selected_stems = {
            PurePosixPath(path).stem.lower()
            for path in reasons
            if not self._is_test(path)
        }
        symbol_needles = {
            value.lower() for name in (symbol_names or set()) for value in [name, *name.split("_")]
            if len(value) >= 3
        }
        needles = selected_stems | set(terms) | symbol_needles
        for path in sorted(item for item in files if self._is_test(item)):
            content = self._read_text(root, path, cache)
            if content is None:
                continue
            lowered = content.lower()
            matched = sorted(needle for needle in needles if needle in lowered)
            if matched:
                reasons[path].add(f"related test reference: {matched[0]}")

    def _read_text(
        self, root: Path, relative: str, cache: dict[str, str]
    ) -> str | None:
        if relative in cache:
            return cache[relative]
        if Path(relative).suffix.lower() not in _TEXT_SUFFIXES:
            return None
        path = (root / Path(relative)).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return None
        try:
            raw = path.read_bytes()
        except (OSError, PermissionError):
            return None
        if b"\0" in raw or len(raw) > 1_000_000:
            return None
        content = raw.decode("utf-8", errors="replace")
        cache[relative] = content
        return content

    def _excerpt(
        self,
        content: str,
        terms: list[str],
        *,
        preferred_line: int | None = None,
    ) -> tuple[int, int, str] | None:
        lines = content.splitlines(keepends=True)
        if not lines:
            return None
        matches = [
            index
            for index, line in enumerate(lines)
            if any(term in line.lower() for term in terms)
        ]
        anchor = (
            min(len(lines) - 1, max(0, preferred_line - 1))
            if preferred_line is not None
            else (matches[0] if matches else 0)
        )
        start = max(0, anchor - self.config.match_context_lines)
        end = min(len(lines), start + self.config.max_excerpt_lines)
        return start + 1, end, "".join(lines[start:end])

    def _excluded(self, path: str) -> bool:
        normalized = self._normalize_relative(path)
        return any(
            fnmatch.fnmatch(normalized, pattern)
            or fnmatch.fnmatch(PurePosixPath(normalized).name, pattern)
            for pattern in self.config.cloud_excluded_paths
        )

    @staticmethod
    def _priority(reasons: set[str]) -> int:
        joined = " ".join(reasons)
        for index, marker in enumerate(
            [
                "explicit path",
                "failure location",
                "preferred path",
                "current Git diff",
                "Python call reference",
                "symbol",
                "ripgrep",
                "imported",
                "test",
            ]
        ):
            if marker in joined:
                return index
        return 99

    @classmethod
    def _parse_changed_files(cls, diff: str) -> dict[str, _ChangedFile]:
        """Parse changed current-line locations and text from a Git diff.

        This is intentionally narrower than the unified-diff patch parser: it
        consumes Git's own trusted ``git diff`` output solely for retrieval
        hints and never applies a change.
        """

        changed: dict[str, _ChangedFile] = {}
        current_path: str | None = None
        old_path: str | None = None
        current_line: int | None = None
        for raw in diff.splitlines():
            if raw.startswith("diff --git "):
                current_path = None
                old_path = None
                current_line = None
                continue
            if current_line is None and raw.startswith("--- "):
                value = raw[4:]
                old_path = (
                    cls._normalize_relative(value[2:])
                    if value.startswith("a/")
                    else None
                )
                continue
            if current_line is None and raw.startswith("+++ "):
                value = raw[4:]
                current_path = (
                    cls._normalize_relative(value[2:])
                    if value.startswith("b/")
                    else old_path
                )
                current_line = None
                continue
            hunk = _DIFF_HUNK.match(raw)
            if hunk:
                current_line = int(hunk.group("new_start"))
                continue
            if current_path is None or current_line is None or not raw:
                continue
            marker, text = raw[0], raw[1:]
            if marker == " ":
                current_line += 1
            elif marker == "+":
                item = changed.setdefault(current_path, _ChangedFile())
                item.current_lines.add(max(1, current_line))
                item.changed_text.append(text)
                current_line += 1
            elif marker == "-":
                item = changed.setdefault(current_path, _ChangedFile())
                item.current_lines.add(max(1, current_line))
                item.changed_text.append(text)
            elif marker == "\\":
                continue
        return changed

    def _changed_python_symbols(
        self,
        root: Path,
        files: set[str],
        changed_files: dict[str, _ChangedFile],
        cache: dict[str, str],
    ) -> set[str]:
        symbols: set[str] = set()
        for path, changed in sorted(changed_files.items()):
            if not path.endswith(".py") or self._excluded(path):
                continue
            for text in changed.changed_text:
                match = _PYTHON_DEFINITION.match(text)
                if match:
                    symbols.add(match.group(1))
            if path not in files:
                continue
            content = self._read_text(root, path, cache)
            if content is None:
                continue
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(
                    node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    continue
                start = getattr(node, "lineno", 0)
                end = getattr(node, "end_lineno", start)
                if any(start <= line <= end for line in changed.current_lines):
                    symbols.add(node.name)
        return symbols

    @staticmethod
    def _evidence_kind(path: str) -> EvidenceKind:
        if ContextCompiler._is_test(path):
            return EvidenceKind.TEST
        if Path(path).suffix.lower() in {".toml", ".ini", ".cfg", ".yaml", ".yml", ".json"}:
            return EvidenceKind.CONFIGURATION
        return EvidenceKind.FILE_EXCERPT

    @staticmethod
    def _is_test(path: str) -> bool:
        pure = PurePosixPath(path)
        return (
            "tests" in pure.parts
            or pure.name.startswith("test_")
            or pure.stem.endswith("_test")
        )

    @staticmethod
    def _normalize_relative(path: str) -> str:
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized
