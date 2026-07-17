from __future__ import annotations

import fnmatch
import re
from pathlib import Path, PurePosixPath

from pydantic import Field

from sol.config import PatchPolicyConfig
from sol.patches.parser import ParsedDiff
from sol.specification.schema import StrictModel


class PatchViolation(StrictModel):
    code: str
    message: str
    path: str | None = None


class PatchValidationResult(StrictModel):
    accepted: bool
    files_changed: list[str] = Field(default_factory=list)
    changed_lines: int = Field(ge=0)
    violations: list[PatchViolation] = Field(default_factory=list)

    def require_accepted(self) -> None:
        if not self.accepted:
            summary = "; ".join(item.message for item in self.violations)
            raise PatchPolicyError(summary)


class PatchPolicyError(RuntimeError):
    """A syntactically valid patch violates deterministic repository policy."""


class PatchPolicyValidator:
    def __init__(self, config: PatchPolicyConfig | None = None) -> None:
        self.config = config or PatchPolicyConfig()

    def validate(
        self, parsed: ParsedDiff, repository_root: str | Path
    ) -> PatchValidationResult:
        root = Path(repository_root).resolve()
        violations: list[PatchViolation] = []
        paths = sorted(parsed.paths)
        if len(paths) > self.config.max_files:
            violations.append(
                PatchViolation(
                    code="excessive_file_count",
                    message=(
                        f"patch changes {len(paths)} files; maximum is "
                        f"{self.config.max_files}"
                    ),
                )
            )
        if parsed.changed_lines > self.config.max_changed_lines:
            violations.append(
                PatchViolation(
                    code="excessive_diff_size",
                    message=(
                        f"patch changes {parsed.changed_lines} lines; maximum is "
                        f"{self.config.max_changed_lines}"
                    ),
                )
            )
        for item in parsed.files:
            if item.is_binary:
                violations.append(
                    PatchViolation(
                        code="binary_change",
                        message="binary-file changes are not allowed",
                        path=item.effective_path,
                    )
                )
            if item.is_symlink:
                violations.append(
                    PatchViolation(
                        code="symlink_change",
                        message=(
                            "symlink patches are rejected because their targets "
                            "can escape the repository"
                        ),
                        path=item.effective_path,
                    )
                )
            for path in {item.old_path, item.new_path} - {None}:
                assert path is not None
                if not self._safe_path(path, root):
                    violations.append(
                        PatchViolation(
                            code="path_escape",
                            message=f"repository path escape or forbidden path: {path}",
                            path=path,
                        )
                    )
            path = item.effective_path
            if not path:
                continue
            if (
                not self.config.allow_dependency_changes
                and self._matches(path, self.config.dependency_files)
            ):
                violations.append(
                    PatchViolation(
                        code="unexpected_dependency_change",
                        message=f"unexpected dependency-file change: {path}",
                        path=path,
                    )
                )
            if item.is_deleted and self._is_test(path):
                violations.append(
                    PatchViolation(
                        code="deleted_test",
                        message=f"test deletion is not allowed: {path}",
                        path=path,
                    )
                )
            if self._matches(path, self.config.verification_files):
                violations.append(
                    PatchViolation(
                        code="verification_config_change",
                        message=f"verification configuration is protected: {path}",
                        path=path,
                    )
                )
        unique = {
            (item.code, item.path, item.message): item for item in violations
        }
        ordered = sorted(
            unique.values(), key=lambda item: (item.code, item.path or "")
        )
        return PatchValidationResult(
            accepted=not ordered,
            files_changed=paths,
            changed_lines=parsed.changed_lines,
            violations=ordered,
        )

    @staticmethod
    def _safe_path(path: str, root: Path) -> bool:
        if (
            not path
            or "\\" in path
            or "\0" in path
            or re.match(r"^[A-Za-z]:", path)
            or path.startswith("/")
        ):
            return False
        pure = PurePosixPath(path)
        if (
            not pure.parts
            or ".." in pure.parts
            or pure.parts[0] in {".git", ".sol"}
        ):
            return False
        candidate = (root / Path(*pure.parts)).resolve()
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _matches(path: str, patterns: list[str]) -> bool:
        normalized = path.replace("\\", "/")
        return any(
            fnmatch.fnmatch(normalized, pattern)
            or fnmatch.fnmatch(PurePosixPath(normalized).name, pattern)
            for pattern in patterns
        )

    @staticmethod
    def _is_test(path: str) -> bool:
        pure = PurePosixPath(path)
        return (
            "tests" in pure.parts
            or pure.name.startswith("test_")
            or pure.stem.endswith("_test")
        )
