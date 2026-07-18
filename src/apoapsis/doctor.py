from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from apoapsis.config import (
    ApoapsisConfig,
    FrontierProviderConfig,
    LocalResearchProviderConfig,
)
from apoapsis.execution.backend import ExecutionBackendName, SandboxUnavailableError
from apoapsis.execution.docker_backend import DockerExecutionBackend
from apoapsis.models.base import ModelOperation
from apoapsis.models.frontier import OpenAICompatibleFrontierProvider
from apoapsis.models.local import OllamaProvider
from apoapsis.models.provider import ModelProvider, ModelRole, ProviderInvocation
from apoapsis.models.telemetry import InstrumentedModelProvider, InstrumentedProviderError
from apoapsis.repository.git import GitCommandError, GitRepository
from apoapsis.specification.schema import StrictModel, utc_now
from apoapsis.verification.runner import VerificationCommand

_MODEL_ROLES = ("frontier", "local_coder", "frontier_coder", "local_research")

_PROBE_PROMPT = 'Respond with exactly {"ok": true} and nothing else.'
_PROBE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


class DoctorCheckStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    SKIPPED = "skipped"


_STATUS_RANK = {
    DoctorCheckStatus.OK: 0,
    DoctorCheckStatus.SKIPPED: 0,
    DoctorCheckStatus.WARNING: 1,
    DoctorCheckStatus.ERROR: 2,
}


class DoctorCheck(StrictModel):
    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    status: DoctorCheckStatus
    detail: str
    remediation: str | None = None


class DoctorReport(StrictModel):
    project_root: str
    generated_at: datetime
    checks: list[DoctorCheck] = Field(default_factory=list)
    overall_status: DoctorCheckStatus


def run_doctor(
    root: Path,
    *,
    probe_providers: bool = False,
    git_executable: str = "git",
    ripgrep_executable: str = "rg",
    provider_overrides: dict[str, InstrumentedModelProvider] | None = None,
) -> DoctorReport:
    """Run read-only diagnostics; never mutates project state or prints secrets."""

    resolved_root = Path(root).resolve()
    checks: list[DoctorCheck] = [
        _check_git(git_executable),
        _check_git_repository(resolved_root, git_executable),
        _check_ripgrep(ripgrep_executable),
        _check_python(),
    ]
    config, config_check = _load_config(resolved_root)
    checks.append(config_check)
    if config is not None:
        checks.extend(_model_checks(config))
        checks.append(_context_check(config))
        checks.extend(_credential_checks(config))
        checks.extend(_ollama_reachability_checks(config))
        checks.extend(_verification_checks(config))
        checks.extend(_verification_backend_checks(config))
        if probe_providers:
            checks.extend(_probe_checks(config, provider_overrides))
    return DoctorReport(
        project_root=str(resolved_root),
        generated_at=utc_now(),
        checks=checks,
        overall_status=_overall_status(checks),
    )


def _overall_status(checks: list[DoctorCheck]) -> DoctorCheckStatus:
    worst = DoctorCheckStatus.OK
    for check in checks:
        if _STATUS_RANK[check.status] > _STATUS_RANK[worst]:
            worst = check.status
    return worst


def _check_git(git_executable: str) -> DoctorCheck:
    found = shutil.which(git_executable)
    if found is None:
        return DoctorCheck(
            name="git",
            category="toolchain",
            status=DoctorCheckStatus.ERROR,
            detail=f"{git_executable!r} was not found on PATH",
            remediation="install Git and ensure it is on PATH",
        )
    try:
        result = subprocess.run(
            [git_executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck(
            name="git",
            category="toolchain",
            status=DoctorCheckStatus.ERROR,
            detail=f"failed to run {git_executable} --version: {exc}",
        )
    if result.returncode != 0:
        return DoctorCheck(
            name="git",
            category="toolchain",
            status=DoctorCheckStatus.ERROR,
            detail=(result.stderr or result.stdout or "git --version failed").strip(),
        )
    return DoctorCheck(
        name="git",
        category="toolchain",
        status=DoctorCheckStatus.OK,
        detail=result.stdout.strip(),
    )


def _check_git_repository(root: Path, git_executable: str) -> DoctorCheck:
    try:
        repo = GitRepository(root, git_executable=git_executable)
    except (GitCommandError, OSError) as exc:
        return DoctorCheck(
            name="git_repository",
            category="toolchain",
            status=DoctorCheckStatus.ERROR,
            detail=f"{root} is not a usable Git repository: {exc}",
            remediation="run this command from inside a Git repository",
        )
    return DoctorCheck(
        name="git_repository",
        category="toolchain",
        status=DoctorCheckStatus.OK,
        detail=f"Git repository root: {repo.root}",
    )


def _check_ripgrep(ripgrep_executable: str) -> DoctorCheck:
    found = shutil.which(ripgrep_executable)
    if found is None:
        return DoctorCheck(
            name="ripgrep",
            category="toolchain",
            status=DoctorCheckStatus.WARNING,
            detail=f"{ripgrep_executable!r} was not found on PATH",
            remediation=(
                "install ripgrep for faster context retrieval; Apoapsis falls "
                "back to a deterministic lexical search without it"
            ),
        )
    return DoctorCheck(
        name="ripgrep",
        category="toolchain",
        status=DoctorCheckStatus.OK,
        detail=f"found at {found}",
    )


def _check_python() -> DoctorCheck:
    detail = f"{sys.version.split()[0]} at {sys.executable}"
    if sys.version_info < (3, 12):
        return DoctorCheck(
            name="python",
            category="toolchain",
            status=DoctorCheckStatus.ERROR,
            detail=detail,
            remediation="Apoapsis requires Python 3.12 or newer",
        )
    return DoctorCheck(
        name="python", category="toolchain", status=DoctorCheckStatus.OK, detail=detail
    )


def _load_config(root: Path) -> tuple[ApoapsisConfig | None, DoctorCheck]:
    config_path = root / ".apoapsis" / "config.toml"
    if not config_path.is_file():
        return None, DoctorCheck(
            name="project_configuration",
            category="toolchain",
            status=DoctorCheckStatus.WARNING,
            detail=f"{config_path} does not exist",
            remediation="run `apoapsis init` in this project",
        )
    try:
        config = ApoapsisConfig.from_toml(config_path)
    except ValueError as exc:
        return None, DoctorCheck(
            name="project_configuration",
            category="toolchain",
            status=DoctorCheckStatus.ERROR,
            detail=f"{config_path} failed to validate: {exc}",
        )
    return config, DoctorCheck(
        name="project_configuration",
        category="toolchain",
        status=DoctorCheckStatus.OK,
        detail=f"loaded {config_path}",
    )


def _model_checks(config: ApoapsisConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    for role in _MODEL_ROLES:
        model_config = getattr(config.models, role)
        if model_config is None:
            checks.append(
                DoctorCheck(
                    name=f"model:{role}",
                    category="model",
                    status=DoctorCheckStatus.SKIPPED,
                    detail="not configured",
                )
            )
            continue
        checks.append(
            DoctorCheck(
                name=f"model:{role}",
                category="model",
                status=DoctorCheckStatus.OK,
                detail=(
                    f"provider={model_config.provider} model={model_config.model} "
                    f"base_url={model_config.base_url} "
                    f"context_window_tokens={model_config.context_window_tokens}"
                ),
            )
        )
    return checks


def _context_check(config: ApoapsisConfig) -> DoctorCheck:
    context = config.context
    coding_models = [
        item
        for item in (
            config.models.frontier,
            config.models.local_coder,
            config.models.frontier_coder,
        )
        if item is not None
    ]
    windows = [
        item.context_window_tokens
        for item in coding_models
        if item.context_window_tokens is not None
    ]
    detail = (
        f"max_files={context.max_files} max_excerpt_lines={context.max_excerpt_lines} "
        f"max_total_chars={context.max_total_chars}"
    )
    if windows:
        smallest = min(windows)
        estimated_tokens = context.max_total_chars / 4
        if estimated_tokens > smallest:
            return DoctorCheck(
                name="context_limits",
                category="context",
                status=DoctorCheckStatus.WARNING,
                detail=(
                    f"{detail}; estimated context budget (~{estimated_tokens:.0f} "
                    f"tokens) may exceed the smallest configured coding context "
                    f"window ({smallest} tokens)"
                ),
                remediation=(
                    "lower context.max_total_chars or raise context_window_tokens"
                ),
            )
    return DoctorCheck(
        name="context_limits", category="context", status=DoctorCheckStatus.OK, detail=detail
    )


def _credential_checks(config: ApoapsisConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    seen_env_vars: set[str] = set()
    for role in _MODEL_ROLES:
        model_config = getattr(config.models, role)
        if model_config is None or model_config.provider != "openai_compatible":
            continue
        env_var = model_config.api_key_env
        if env_var in seen_env_vars:
            continue
        seen_env_vars.add(env_var)
        is_set = bool(os.environ.get(env_var))
        checks.append(
            DoctorCheck(
                name=f"credential:{env_var}",
                category="credentials",
                status=DoctorCheckStatus.OK if is_set else DoctorCheckStatus.ERROR,
                detail=f"{env_var} is set" if is_set else f"{env_var} is not set",
                remediation=(
                    None if is_set else f"set the {env_var} environment variable"
                ),
            )
        )
    return checks


def _ollama_reachability_checks(config: ApoapsisConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    seen_urls: set[str] = set()
    for role in _MODEL_ROLES:
        model_config = getattr(config.models, role)
        if model_config is None or model_config.provider != "ollama":
            continue
        base_url = model_config.base_url
        if base_url in seen_urls:
            continue
        seen_urls.add(base_url)
        checks.append(_probe_ollama_tags(base_url))
    return checks


def _probe_ollama_tags(base_url: str) -> DoctorCheck:
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return DoctorCheck(
            name=f"ollama_reachability:{base_url}",
            category="model",
            status=DoctorCheckStatus.ERROR,
            detail=f"could not reach {url}: {exc}",
            remediation="start the local Ollama service",
        )
    return DoctorCheck(
        name=f"ollama_reachability:{base_url}",
        category="model",
        status=DoctorCheckStatus.OK,
        detail=f"reachable at {url}",
    )


def _verification_checks(config: ApoapsisConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    required = [item for item in config.verification.commands if item.required]
    if not required:
        checks.append(
            DoctorCheck(
                name="verification_commands",
                category="verification",
                status=DoctorCheckStatus.ERROR,
                detail="no required verification command is configured",
                remediation=(
                    "configure at least one [[verification.commands]] with "
                    "required = true"
                ),
            )
        )
    for command in config.verification.commands:
        executable = command.argv[0] if command.argv else None
        found = executable is not None and shutil.which(executable) is not None
        checks.append(
            DoctorCheck(
                name=f"verification_command:{command.name}",
                category="verification",
                status=DoctorCheckStatus.OK if found else DoctorCheckStatus.WARNING,
                detail=(
                    f"argv[0]={executable!r} found on PATH"
                    if found
                    else f"argv[0]={executable!r} was not found on PATH"
                ),
                remediation=(
                    None if found else f"install {executable!r} or adjust the command"
                ),
            )
        )
    return checks


def _verification_backend_checks(config: ApoapsisConfig) -> list[DoctorCheck]:
    backend_config = config.verification.backend
    if backend_config.backend == ExecutionBackendName.HOST:
        return [
            DoctorCheck(
                name="verification_backend",
                category="verification",
                status=DoctorCheckStatus.WARNING,
                detail=(
                    "host backend selected: verification commands run "
                    "directly on the host, unsandboxed"
                ),
                remediation=(
                    'configure [verification.backend] with backend = "docker" '
                    "for sandboxed verification"
                ),
            )
        ]
    assert backend_config.docker is not None
    backend = DockerExecutionBackend(backend_config.docker)
    try:
        backend.preflight()
    except SandboxUnavailableError as exc:
        return [
            DoctorCheck(
                name="docker_sandbox",
                category="verification",
                status=DoctorCheckStatus.ERROR,
                detail=str(exc),
                remediation="resolve the reported Docker issue before relying on the sandbox",
            )
        ]
    return [
        DoctorCheck(
            name="docker_sandbox",
            category="verification",
            status=DoctorCheckStatus.OK,
            detail=(
                "Docker CLI/engine/image preflight passed for "
                f"{backend_config.docker.image}@{backend_config.docker.image_digest}"
            ),
        ),
        _docker_self_test(backend),
    ]


def _docker_self_test(backend: DockerExecutionBackend) -> DoctorCheck:
    command = VerificationCommand(
        name="doctor-self-test",
        category="sandbox",
        argv=backend.config.self_test_argv,
        timeout_seconds=30,
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            (project_root / "probe.txt").write_text(
                "apoapsis doctor self-test\n", encoding="utf-8"
            )
            context = backend.prepare(project_root, "TASK-DOCTORSELFTEST", 1)
            try:
                outcome = backend.run_command(context, command, environment={})
            finally:
                backend.finalize(context)
    except Exception as exc:  # noqa: BLE001 - report any failure as a check
        return DoctorCheck(
            name="docker_self_test",
            category="verification",
            status=DoctorCheckStatus.ERROR,
            detail=f"sandbox self-test failed: {exc}",
        )
    sandboxed = bool(outcome.backend_metadata.get("sandboxed"))
    if outcome.status.value != "passed" or not sandboxed:
        return DoctorCheck(
            name="docker_self_test",
            category="verification",
            status=DoctorCheckStatus.ERROR,
            detail=(
                "sandbox self-test did not pass as sandboxed "
                f"(status={outcome.status.value}, sandboxed={sandboxed})"
            ),
        )
    return DoctorCheck(
        name="docker_self_test",
        category="verification",
        status=DoctorCheckStatus.OK,
        detail=f"self-test container ran in {outcome.duration_seconds:.2f}s",
    )


def _build_probe_adapter(
    model_config: FrontierProviderConfig | LocalResearchProviderConfig,
) -> ModelProvider:
    if model_config.provider == "ollama":
        return OllamaProvider(model_config)
    if model_config.provider == "openai_compatible":
        if isinstance(model_config, FrontierProviderConfig):
            return OpenAICompatibleFrontierProvider(model_config)
        return OpenAICompatibleFrontierProvider(
            FrontierProviderConfig(
                provider="openai_compatible",
                base_url=model_config.base_url,
                model=model_config.model,
                api_key_env=model_config.api_key_env,
                timeout_seconds=min(model_config.timeout_seconds, 3600),
            )
        )
    raise ValueError(f"unsupported provider: {model_config.provider}")


def _probe_checks(
    config: ApoapsisConfig,
    provider_overrides: dict[str, InstrumentedModelProvider] | None,
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    overrides = provider_overrides or {}
    for role in _MODEL_ROLES:
        model_config = getattr(config.models, role)
        if model_config is None:
            continue
        provider = overrides.get(role)
        if provider is None:
            try:
                provider = InstrumentedModelProvider(
                    _build_probe_adapter(model_config),
                    getattr(model_config, "pricing", None),
                )
            except ValueError as exc:
                checks.append(
                    DoctorCheck(
                        name=f"probe:{role}",
                        category="model",
                        status=DoctorCheckStatus.ERROR,
                        detail=f"failed to build provider adapter: {exc}",
                    )
                )
                continue
        cost_note = (
            " (this call may incur hosted-provider cost)"
            if model_config.provider == "openai_compatible"
            else ""
        )
        checks.append(_probe_provider(role, provider, cost_note))
    return checks


def _probe_provider(
    role: str, provider: InstrumentedModelProvider, cost_note: str
) -> DoctorCheck:
    invocation = ProviderInvocation(
        request_id=f"MRQ-{uuid.uuid4().hex}",
        operation=ModelOperation.AGENT_STEP,
        prompt=_PROBE_PROMPT,
        role=ModelRole.CODING_AGENT,
        response_schema=_PROBE_SCHEMA,
        max_output_tokens=64,
    )
    try:
        call = provider.complete(invocation)
    except InstrumentedProviderError as exc:
        return DoctorCheck(
            name=f"probe:{role}",
            category="model",
            status=DoctorCheckStatus.ERROR,
            detail=f"provider probe failed{cost_note}: {exc}",
        )
    try:
        json.loads(call.output.content)
        structured_ok = True
    except json.JSONDecodeError:
        structured_ok = False
    detail = f"responded in {call.telemetry.latency_seconds:.2f}s{cost_note}"
    if not structured_ok:
        detail += "; response was not valid structured JSON output"
    return DoctorCheck(
        name=f"probe:{role}",
        category="model",
        status=DoctorCheckStatus.OK if structured_ok else DoctorCheckStatus.WARNING,
        detail=detail,
    )
