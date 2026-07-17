from __future__ import annotations

import os
import subprocess
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field

from sol.specification.schema import StrictModel
from sol.verification.results import (
    VerificationCommandResult,
    VerificationResult,
    VerificationStatus,
)


DEFAULT_ENVIRONMENT_ALLOWLIST = [
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOME",
    "VIRTUAL_ENV",
]


class VerificationCommand(StrictModel):
    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    required: bool = True
    environment: dict[str, str] = Field(default_factory=dict)


class VerificationConfig(StrictModel):
    commands: list[VerificationCommand] = Field(default_factory=list)
    stop_on_failure: bool = False
    output_limit_chars: int = Field(default=100_000, ge=1_000, le=10_000_000)
    environment_allowlist: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ENVIRONMENT_ALLOWLIST)
    )

    @classmethod
    def from_toml(cls, path: str | Path) -> VerificationConfig:
        with Path(path).open("rb") as handle:
            raw = tomllib.load(handle)
        verification = raw.get("verification")
        if not isinstance(verification, dict):
            raise ValueError("configuration requires a [verification] section")
        return cls.model_validate(verification)


class VerificationRunner:
    def __init__(self, config: VerificationConfig) -> None:
        self.config = config

    def run(
        self, task_id: str, project_root: str | Path
    ) -> VerificationResult:
        root = Path(project_root).resolve()
        if not root.is_dir():
            raise ValueError(f"project root does not exist: {root}")
        started_at = datetime.now(timezone.utc)
        started_clock = time.monotonic()
        results: list[VerificationCommandResult] = []
        stop = False
        for command in self.config.commands:
            if stop:
                now = datetime.now(timezone.utc)
                results.append(
                    VerificationCommandResult(
                        name=command.name,
                        category=command.category,
                        argv=command.argv,
                        required=command.required,
                        cwd=str(root),
                        status=VerificationStatus.SKIPPED,
                        started_at=now,
                        finished_at=now,
                        duration_seconds=0,
                    )
                )
                continue
            result = self._run_command(command, root)
            results.append(result)
            if (
                self.config.stop_on_failure
                and command.required
                and result.status != VerificationStatus.PASSED
            ):
                stop = True
        finished_at = datetime.now(timezone.utc)
        required_failures = [
            result
            for result in results
            if result.required and result.status != VerificationStatus.PASSED
        ]
        status = (
            VerificationStatus.FAILED
            if required_failures
            else VerificationStatus.PASSED
        )
        return VerificationResult(
            task_id=task_id,
            status=status,
            commands=results,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=time.monotonic() - started_clock,
        )

    def _run_command(
        self, command: VerificationCommand, root: Path
    ) -> VerificationCommandResult:
        started_at = datetime.now(timezone.utc)
        started_clock = time.monotonic()
        environment = {
            key: os.environ[key]
            for key in self.config.environment_allowlist
            if key in os.environ
        }
        environment.update(command.environment)
        exit_code: int | None = None
        stdout = ""
        stderr = ""
        status = VerificationStatus.ERROR
        try:
            completed = subprocess.run(
                command.argv,
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=command.timeout_seconds,
                shell=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            status = (
                VerificationStatus.PASSED
                if completed.returncode == 0
                else VerificationStatus.FAILED
            )
        except subprocess.TimeoutExpired as exc:
            stdout = self._to_text(exc.stdout)
            stderr = self._to_text(exc.stderr)
            status = VerificationStatus.TIMED_OUT
        except OSError as exc:
            stderr = str(exc)
            status = VerificationStatus.ERROR
        duration = time.monotonic() - started_clock
        stdout, stdout_cut = self._truncate(stdout)
        stderr, stderr_cut = self._truncate(stderr)
        return VerificationCommandResult(
            name=command.name,
            category=command.category,
            argv=command.argv,
            required=command.required,
            cwd=str(root),
            status=status,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            output_truncated=stdout_cut or stderr_cut,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            duration_seconds=duration,
        )

    def _truncate(self, output: str) -> tuple[str, bool]:
        limit = self.config.output_limit_chars
        if len(output) <= limit:
            return output, False
        marker = "\n... output truncated by SOL ...\n"
        head = (limit - len(marker)) // 2
        tail = limit - len(marker) - head
        return output[:head] + marker + output[-tail:], True

    @staticmethod
    def _to_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

