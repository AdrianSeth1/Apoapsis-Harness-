from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel
from apoapsis.verification.results import (
    VerificationCommandResult,
    VerificationResult,
    VerificationStatus,
)


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ROOT_MARKER = re.compile(
    r"(?:AssertionError|[A-Za-z]+(?:Error|Exception)\b|\bFAILED\b|\berror:)",
    re.IGNORECASE,
)
_TRACEBACK_LOCATION = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)')
_COLON_LOCATION = re.compile(
    r"(?m)(?:^|\s)(?P<path>[A-Za-z0-9_./\\-]+\.py):"
    r"(?P<line>\d+)(?::\d+)?(?:\b|:)"
)


class FailureLocation(StrictModel):
    path: str = Field(min_length=1)
    line: int = Field(ge=1)


class NormalizedFailure(StrictModel):
    command_name: str
    argv: list[str] = Field(min_length=1)
    status: VerificationStatus
    exit_code: int | None
    root_error: str
    relevant_error: str
    locations: list[FailureLocation] = Field(default_factory=list)


class FailureNormalizer:
    def extract(
        self,
        result: VerificationResult,
        worktree: str | Path,
        *,
        max_chars: int = 8_000,
    ) -> tuple[VerificationCommandResult, NormalizedFailure]:
        failed = next(
            (
                item
                for item in result.commands
                if item.required and item.status != VerificationStatus.PASSED
            ),
            None,
        )
        if failed is None:
            raise ValueError("verification result has no failed required command")
        combined = "\n".join(part for part in [failed.stdout, failed.stderr] if part)
        normalized = _ANSI.sub("", combined).replace("\r\n", "\n").replace("\r", "\n")
        locations = self._locations(normalized, Path(worktree).resolve())
        normalized = normalized.replace(str(Path(worktree).resolve()), "<WORKTREE>")
        lines: list[str] = []
        previous = None
        for raw_line in normalized.splitlines():
            line = raw_line.rstrip()
            if line == previous and line:
                continue
            lines.append(line)
            previous = line
        meaningful = [line for line in lines if line.strip()]
        roots = [line.strip() for line in meaningful if _ROOT_MARKER.search(line)]
        root_error = roots[-1] if roots else (
            meaningful[-1] if meaningful else f"command exited with {failed.exit_code}"
        )
        relevant = "\n".join(lines)
        if len(relevant) > max_chars:
            half = (max_chars - 40) // 2
            relevant = (
                relevant[:half]
                + "\n... normalized failure truncated ...\n"
                + relevant[-half:]
            )
        return failed, NormalizedFailure(
            command_name=failed.name,
            argv=failed.argv,
            status=failed.status,
            exit_code=failed.exit_code,
            root_error=root_error,
            relevant_error=relevant,
            locations=locations,
        )

    @staticmethod
    def _locations(output: str, worktree: Path) -> list[FailureLocation]:
        """Extract validated repository-relative traceback locations.

        Locations are retrieval hints only. Every candidate is resolved below
        the task worktree before it can influence context selection, so an
        error mentioning an arbitrary absolute path cannot request that file.
        """

        raw_locations: list[tuple[str, str]] = []
        for pattern in (_TRACEBACK_LOCATION, _COLON_LOCATION):
            raw_locations.extend(
                (match.group("path"), match.group("line"))
                for match in pattern.finditer(output)
            )
        selected: list[FailureLocation] = []
        seen: set[tuple[str, int]] = set()
        for raw_path, raw_line in raw_locations:
            candidate = Path(raw_path)
            resolved = candidate.resolve() if candidate.is_absolute() else (worktree / candidate).resolve()
            try:
                relative = resolved.relative_to(worktree).as_posix()
            except ValueError:
                continue
            key = (relative, int(raw_line))
            if key in seen:
                continue
            seen.add(key)
            selected.append(FailureLocation(path=relative, line=key[1]))
        return selected
