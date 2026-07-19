from __future__ import annotations

import os
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field

from apoapsis.execution.backend import (
    ExecutionBackend,
    ExecutionBackendConfig,
    ExecutionBackendName,
)
from apoapsis.execution.docker_backend import DockerExecutionBackend
from apoapsis.execution.host_backend import HostExecutionBackend
from apoapsis.specification.schema import StrictModel
from apoapsis.verification.results import (
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
    description: str = Field(
        default="",
        max_length=500,
        description=(
            "Human-readable summary of what this command validates. Shown "
            "to the specification-extraction model as part of the "
            "deterministic acceptance-command catalog (ADR 0016) so it can "
            "propose a sensible AcceptanceCriterion.verification_method "
            "mapping; purely descriptive, never executed."
        ),
    )
    acceptance: bool = Field(
        default=False,
        description=(
            "Marks this command as an approved acceptance check: strong "
            "enough evidence for an AcceptanceCriterion.verification_method "
            "to name it as proof under the strict completion policy. "
            "Development-only checks stay False (the default) and can "
            "never prove a criterion, even if a model requests them."
        ),
    )


class VerificationConfig(StrictModel):
    commands: list[VerificationCommand] = Field(default_factory=list)
    stop_on_failure: bool = False
    output_limit_chars: int = Field(default=100_000, ge=1_000, le=10_000_000)
    environment_allowlist: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ENVIRONMENT_ALLOWLIST)
    )
    backend: ExecutionBackendConfig = Field(default_factory=ExecutionBackendConfig)

    @classmethod
    def from_toml(cls, path: str | Path) -> VerificationConfig:
        with Path(path).open("rb") as handle:
            raw = tomllib.load(handle)
        verification = raw.get("verification")
        if not isinstance(verification, dict):
            raise ValueError("configuration requires a [verification] section")
        return cls.model_validate(verification)


def build_execution_backend(config: ExecutionBackendConfig) -> ExecutionBackend:
    if config.backend == ExecutionBackendName.HOST:
        return HostExecutionBackend()
    if config.backend == ExecutionBackendName.DOCKER:
        assert config.docker is not None  # enforced by ExecutionBackendConfig validator
        return DockerExecutionBackend(config.docker)
    raise ValueError(f"unsupported execution backend: {config.backend}")


class VerificationRunner:
    def __init__(
        self, config: VerificationConfig, *, backend: ExecutionBackend | None = None
    ) -> None:
        self.config = config
        self.backend = backend or build_execution_backend(config.backend)

    def run(
        self, task_id: str, project_root: str | Path, *, attempt: int = 1
    ) -> VerificationResult:
        started_at = datetime.now(timezone.utc)
        started_clock = time.monotonic()
        context = self.backend.prepare(Path(project_root), task_id, attempt)
        results: list[VerificationCommandResult] = []
        integrity_violations: list[str] = []
        try:
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
                            acceptance=command.acceptance,
                            cwd=context.display_root,
                            status=VerificationStatus.SKIPPED,
                            started_at=now,
                            finished_at=now,
                            duration_seconds=0,
                            backend=self.backend.backend_name,
                        )
                    )
                    continue
                environment = {
                    key: os.environ[key]
                    for key in self.config.environment_allowlist
                    if key in os.environ
                }
                environment.update(command.environment)
                outcome = self.backend.run_command(
                    context, command, environment=environment
                )
                stdout, stdout_cut = self._truncate(outcome.stdout)
                stderr, stderr_cut = self._truncate(outcome.stderr)
                result = VerificationCommandResult(
                    name=command.name,
                    category=command.category,
                    argv=command.argv,
                    required=command.required,
                    acceptance=command.acceptance,
                    cwd=context.display_root,
                    status=outcome.status,
                    exit_code=outcome.exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    output_truncated=stdout_cut or stderr_cut,
                    started_at=outcome.started_at,
                    finished_at=outcome.finished_at,
                    duration_seconds=outcome.duration_seconds,
                    backend=outcome.backend,
                    backend_metadata=outcome.backend_metadata,
                )
                results.append(result)
                if (
                    self.config.stop_on_failure
                    and command.required
                    and result.status != VerificationStatus.PASSED
                ):
                    stop = True
        finally:
            integrity_violations = self.backend.finalize(context)
        finished_at = datetime.now(timezone.utc)
        required_failures = [
            result
            for result in results
            if result.required and result.status != VerificationStatus.PASSED
        ]
        status = (
            VerificationStatus.FAILED
            if required_failures or integrity_violations
            else VerificationStatus.PASSED
        )
        return VerificationResult(
            task_id=task_id,
            status=status,
            commands=results,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=time.monotonic() - started_clock,
            integrity_violations=integrity_violations,
        )

    def _truncate(self, output: str) -> tuple[str, bool]:
        limit = self.config.output_limit_chars
        if len(output) <= limit:
            return output, False
        marker = "\n... output truncated by Apoapsis ...\n"
        head = (limit - len(marker)) // 2
        tail = limit - len(marker) - head
        return output[:head] + marker + output[-tail:], True
