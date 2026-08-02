from __future__ import annotations

import os
import hashlib
import sys
import tempfile
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


# Environment Apoapsis imposes on every verification command it owns, before
# the allowlisted host environment and any explicitly configured per-command
# environment are layered on top.
#
# `PYTHONDONTWRITEBYTECODE=1` stops a Python verification run from writing
# `__pycache__/*.pyc` into the task worktree. Without it the harness's own
# check mutates the tree it is measuring: live task TASK-EF33C00E5BD4
# (2026-07-26) reported two `.pyc` files as changed alongside the single
# source file the model edited. The Local Power sandbox already set this for
# model-requested shell commands; ordinary configured verification did not.
# This is prevention, not concealment -- ADR 0063's changed-path classifier
# still keeps reviewer output correct for repositories where byproducts are
# produced some other way.
HARNESS_VERIFICATION_ENVIRONMENT = {
    "PYTHONDONTWRITEBYTECODE": "1",
}


# Flags whose *next* argv element is a value, not a test path. Used only to
# skip over that value when reading positional discovery roots.
_PYTEST_VALUE_FLAGS = frozenset(
    {"-k", "-m", "-p", "-o", "-c", "-n", "--rootdir", "--junitxml", "--deselect"}
)


def _normalize_discovery_root(raw: str) -> str:
    """Normalize a configured discovery root to a comparable repository-
    relative directory, or return "" if it is not one.

    Absolute paths and parent traversals are rejected rather than guessed
    at: this value is only ever used to *compare* against a plan's
    ``suggested_paths``, so an unnormalizable root must produce no claim
    rather than a wrong one.
    """

    candidate = raw.strip().replace("\\", "/").strip("\"'")
    if not candidate or candidate in {".", "./"}:
        return ""
    while candidate.startswith("./"):
        candidate = candidate[2:]
    candidate = candidate.rstrip("/")
    if not candidate or candidate.startswith("/") or candidate.startswith(".."):
        return ""
    if len(candidate) > 1 and candidate[1] == ":":  # Windows drive letter
        return ""
    return candidate


def derive_discovery_roots(argv: list[str]) -> list[str]:
    """Best-effort, read-only inference of the directories a configured test
    command scans, from its own ``argv``.

    Apoapsis already knows this: ``unittest discover -s tests`` names its
    start directory outright. The planner does not, because
    ``VerificationCatalogEntry`` is deliberately name-only and never carries
    argv (ADR 0016). Deriving the root here lets the harness tell a planner
    *where* tests must live without ever transmitting executable content --
    a repository-relative directory is data of the same kind as
    ``suggested_paths``, not a shell grant.

    Returns ``[]`` when the command's layout cannot be read confidently.
    Absent information is not evidence: a caller must treat an empty result
    as "no claim", never as "no tests required".
    """

    tokens = [str(item) for item in argv]
    roots: list[str] = []

    if "unittest" in tokens and "discover" in tokens:
        explicit = False
        for index, token in enumerate(tokens):
            if token in {"-s", "--start-directory"}:
                if index + 1 < len(tokens):
                    root = _normalize_discovery_root(tokens[index + 1])
                    explicit = True
                    if root:
                        roots.append(root)
            elif token.startswith("--start-directory="):
                explicit = True
                root = _normalize_discovery_root(token.split("=", 1)[1])
                if root:
                    roots.append(root)
        if not explicit:
            # `unittest discover` with no -s discovers from the working
            # directory, which is the repository root: every path is
            # already in scope, so no single root can be claimed.
            return []

    elif any(token == "pytest" or token.endswith("/pytest") for token in tokens):
        start = next(
            index
            for index, token in enumerate(tokens)
            if token == "pytest" or token.endswith("/pytest")
        )
        skip_next = False
        for token in tokens[start + 1 :]:
            if skip_next:
                skip_next = False
                continue
            if token.startswith("-"):
                skip_next = token in _PYTEST_VALUE_FLAGS
                continue
            root = _normalize_discovery_root(token.split("::", 1)[0])
            if root:
                roots.append(root)

    deduplicated: list[str] = []
    for root in roots:
        if root not in deduplicated:
            deduplicated.append(root)
    return deduplicated


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
    discovery_roots: list[str] = Field(
        default_factory=list,
        description=(
            "Repository-relative directories this command collects tests "
            "from. Optional: when empty it is inferred from `argv`. Set it "
            "explicitly when the layout cannot be read from argv (a wrapper "
            "script, a Makefile target, a custom runner). Descriptive data, "
            "never executed -- it is shown to planning models so a plan can "
            "put a slice's tests where this command will actually find "
            "them, and is compared against a plan's suggested_paths."
        ),
    )

    def resolved_discovery_roots(self) -> list[str]:
        """The directories this command scans: the explicit configuration
        if set, otherwise whatever `argv` reveals. Empty means unknown."""

        explicit = [
            normalized
            for normalized in (
                _normalize_discovery_root(root) for root in self.discovery_roots
            )
            if normalized
        ]
        if explicit:
            return explicit
        return derive_discovery_roots(self.argv)


class VerificationConfig(StrictModel):
    commands: list[VerificationCommand] = Field(default_factory=list)
    stop_on_failure: bool = False
    output_limit_chars: int = Field(default=100_000, ge=1_000, le=10_000_000)
    environment_allowlist: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ENVIRONMENT_ALLOWLIST)
    )
    backend: ExecutionBackendConfig = Field(default_factory=ExecutionBackendConfig)
    auto_install_dependencies: bool = True
    dependency_install_timeout_seconds: float = Field(default=600.0, gt=0, le=3600)

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
            environment = dict(HARNESS_VERIFICATION_ENVIRONMENT)
            environment.update(
                {
                    key: os.environ[key]
                    for key in self.config.environment_allowlist
                    if key in os.environ
                }
            )
            dependency_command, dependency_environment = self._dependency_install(
                Path(project_root), task_id
            )
            environment.update(dependency_environment)
            stop = False
            if dependency_command is not None:
                outcome = self.backend.run_command(
                    context, dependency_command, environment=environment
                )
                stdout, stdout_cut = self._truncate(outcome.stdout)
                stderr, stderr_cut = self._truncate(outcome.stderr)
                dependency_result = VerificationCommandResult(
                    name=dependency_command.name,
                    category=dependency_command.category,
                    argv=dependency_command.argv,
                    required=True,
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
                    backend_metadata={
                        **outcome.backend_metadata,
                        "harness_selected": True,
                        "install_scripts_allowed": True,
                    },
                )
                results.append(dependency_result)
                stop = dependency_result.status != VerificationStatus.PASSED
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
                command_environment = dict(environment)
                command_environment.update(command.environment)
                outcome = self.backend.run_command(
                    context, command, environment=command_environment
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

    def _dependency_install(
        self, project_root: Path, task_id: str
    ) -> tuple[VerificationCommand | None, dict[str, str]]:
        if not self.config.auto_install_dependencies:
            return None, {}
        requirements = [
            path
            for path in sorted(project_root.glob("requirements*.txt"))
            if any(
                line.strip() and not line.lstrip().startswith("#")
                for line in path.read_text(encoding="utf-8").splitlines()
            )
        ]
        pyproject = project_root / "pyproject.toml"
        pyproject_has_dependencies = False
        if pyproject.is_file():
            with pyproject.open("rb") as handle:
                project_table = tomllib.load(handle).get("project", {})
            pyproject_has_dependencies = bool(
                isinstance(project_table, dict)
                and (
                    project_table.get("dependencies")
                    or project_table.get("optional-dependencies")
                )
            )
        manifest = (
            requirements[0]
            if requirements
            else pyproject if pyproject_has_dependencies else None
        )
        if manifest is None:
            return None, {}
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()[:16]
        if self.backend.backend_name == "host":
            target = (
                Path(tempfile.gettempdir())
                / "apoapsis-dependencies"
                / task_id
                / digest
            ).resolve()
            target.mkdir(parents=True, exist_ok=True)
            target_arg = str(target)
        else:
            target_arg = f".apoapsis-dependencies/{digest}"
        argv = [
            sys.executable if self.backend.backend_name == "host" else "python",
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--target",
            target_arg,
        ]
        if manifest == pyproject:
            argv.append(".")
        else:
            argv.extend(["-r", manifest.name])
        command = VerificationCommand(
            name="dependency-install",
            category="dependencies",
            argv=argv,
            timeout_seconds=self.config.dependency_install_timeout_seconds,
            required=True,
            description="Harness-selected installation of declared project dependencies.",
        )
        return command, {"PYTHONPATH": target_arg}

    def _truncate(self, output: str) -> tuple[str, bool]:
        limit = self.config.output_limit_chars
        if len(output) <= limit:
            return output, False
        marker = "\n... output truncated by Apoapsis ...\n"
        head = (limit - len(marker)) // 2
        tail = limit - len(marker) - head
        return output[:head] + marker + output[-tail:], True
