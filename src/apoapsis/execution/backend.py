from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel
from apoapsis.verification.results import VerificationStatus

if TYPE_CHECKING:
    from apoapsis.verification.runner import VerificationCommand


class SandboxUnavailableError(RuntimeError):
    """A configured execution backend cannot run right now.

    Never caught to silently fall back to a different backend; the caller
    must explicitly reconfigure `[verification.backend]` to change backends.
    """


class ExecutionBackendName(StrEnum):
    HOST = "host"
    DOCKER = "docker"


class DockerBackendConfig(StrictModel):
    image: str = Field(min_length=1)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cpu_limit: float = Field(default=2.0, gt=0, le=64)
    memory_limit_mb: int = Field(default=2048, ge=64, le=131_072)
    pids_limit: int = Field(default=256, ge=1, le=10_000)
    tmpfs_size_mb: int = Field(default=256, ge=16, le=8_192)
    wall_clock_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    environment_allowlist: list[str] = Field(default_factory=list)
    network: Literal["none"] = "none"
    user: str = Field(default="65532:65532", pattern=r"^[0-9]+:[0-9]+$")
    docker_executable: str = Field(default="docker", min_length=1)
    self_test_argv: list[str] = Field(default_factory=lambda: ["true"])


class ExecutionBackendConfig(StrictModel):
    backend: ExecutionBackendName = ExecutionBackendName.HOST
    docker: DockerBackendConfig | None = None

    @model_validator(mode="after")
    def require_docker_config_when_selected(self) -> ExecutionBackendConfig:
        if self.backend == ExecutionBackendName.DOCKER and self.docker is None:
            raise ValueError(
                "backend 'docker' requires [verification.backend.docker] configuration"
            )
        return self


class RawCommandOutcome(StrictModel):
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    status: VerificationStatus
    started_at: datetime
    finished_at: datetime
    duration_seconds: float = Field(ge=0)
    backend: str
    backend_metadata: dict[str, object] = Field(default_factory=dict)


@dataclass
class ExecutionContext:
    root: Path
    display_root: str
    extra: dict[str, object] = field(default_factory=dict)


class ExecutionBackend(Protocol):
    """Narrow seam between deterministic verification sequencing and where
    a preconfigured argument vector actually runs. Only Apoapsis selects the
    backend and the argv; a model never sees or chooses either."""

    backend_name: str

    def prepare(
        self, project_root: Path, task_id: str, attempt: int
    ) -> ExecutionContext: ...

    def run_command(
        self,
        context: ExecutionContext,
        command: "VerificationCommand",
        *,
        environment: dict[str, str],
    ) -> RawCommandOutcome: ...

    def finalize(self, context: ExecutionContext) -> list[str]:
        """Return paths that changed unexpectedly during the run, if any."""
        ...
