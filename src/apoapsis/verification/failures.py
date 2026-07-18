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


class NormalizedFailure(StrictModel):
    command_name: str
    argv: list[str] = Field(min_length=1)
    status: VerificationStatus
    exit_code: int | None
    root_error: str
    relevant_error: str


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
        )

