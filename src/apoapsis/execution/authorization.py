from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from apoapsis.audit.store import AuditArtifact, TaskAuditStore
from apoapsis.config import (
    AgentLoopConfig,
    ApoapsisConfig,
    CompletionPolicy,
    ContextCompilerConfig,
    ExecutionMode,
    effective_config_for_specification,
)
from apoapsis.repository.fingerprint import compute_worktree_fingerprint
from apoapsis.repository.git import GitRepository
from apoapsis.specification.schema import StrictModel, TaskSpecification, utc_now
from apoapsis.workflow.routing import select_agent_route

# Fixed, non-negotiable statements of who decides what -- included in every
# package so the audit record and the UI confirmation both state the
# authority boundary explicitly, rather than leaving it implicit.
AUTHORITY_RULES: tuple[str, ...] = (
    "The harness alone decides local-vs-frontier routing; a model cannot "
    "select or change its own route.",
    "Only the configured, harness-validated verification commands may run; "
    "a model cannot invent or substitute one.",
    "Turn, patch, verification, and context ceilings are enforced by the "
    "harness and cannot be raised by a model.",
    "Completion is decided by the harness's configured completion policy, "
    "never by a model's own claim.",
    "No worktree, commit, merge, or repository mutation happens outside "
    "the harness's own controlled actions.",
)


class ExecutionAuthorizationPackage(StrictModel):
    """An immutable, hashed record of exactly what a 'Start coding'
    confirmation authorizes (ADR 0026).

    Computed once, deterministically, from persisted task/specification
    facts, current repository state, and current configuration -- zero
    model calls, zero side effects beyond reading git state. The UI
    preview and ``prepare_execution_operation`` both build this package
    from the exact same function, and ``run_execution_operation``
    recomputes it fresh immediately before any provider construction,
    worktree mutation, or command execution; a hash mismatch against what
    was authorized (the task, its specification, the repository's tracked/
    untracked state, or execution configuration changed since) is
    rejected before anything irreversible happens.

    Never carries a raw credential or secret environment value -- see
    ``_safe_config_payload``.
    """

    schema_version: str = "1.0"
    operation_id: str = Field(pattern=r"^EXOP-[A-Za-z0-9._-]+$")
    task_id: str = Field(pattern=r"^TASK-[A-Za-z0-9._-]+$")
    task_version: int = Field(ge=1)
    specification_sha256: str = Field(min_length=64, max_length=64)
    repository_root: str = Field(min_length=1)
    repository_head_commit: str = Field(min_length=1)
    worktree_fingerprint: str = Field(min_length=1)
    effective_config_sha256: str = Field(min_length=64, max_length=64)
    predicted_route: str | None = None
    predicted_route_reason: str | None = None
    provider_kinds: dict[str, str] = Field(default_factory=dict)
    model_names: dict[str, str] = Field(default_factory=dict)
    local_agent_budget: AgentLoopConfig
    frontier_agent_budget: AgentLoopConfig
    context_ceilings: ContextCompilerConfig
    completion_policy: CompletionPolicy
    verification_backend: str = Field(min_length=1)
    verification_command_catalog: list[str] = Field(default_factory=list)
    verification_config_sha256: str = Field(min_length=64, max_length=64)
    authority_rules: list[str] = Field(default_factory=lambda: list(AUTHORITY_RULES))
    generated_at: datetime = Field(default_factory=utc_now)
    # Filled in after the rest of the package is built -- see
    # ``build_execution_authorization_package``. Excluded from its own
    # hash input, along with ``generated_at`` (expected to differ between
    # build and a later, otherwise drift-free recomputation) and
    # ``operation_id`` (a fresh operation_id is chosen client-side only
    # once the user actually confirms; excluding it lets the exact same
    # preview hash the confirmation carries be produced again, against
    # the real operation_id, at submission and again at run time --
    # what is authorized is the task/specification/repository/config
    # content, not which specific operation attempt runs it).
    package_sha256: str = ""


def _sha256_canonical(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_config_payload(config: ApoapsisConfig) -> dict[str, Any]:
    """A JSON-safe rendering of ``config`` with every credential/secret
    stripped -- ``FrontierProviderConfig.api_key_env`` only ever names an
    environment variable (never the secret itself) so it is already safe,
    but ``VerificationCommand.environment`` is a free-form dict a user
    could populate with literal secret values for one command's run, so
    each command's environment dict is replaced by its sorted key names
    only. This payload feeds both the effective-configuration hash and
    the verification-configuration hash below; it is never itself
    returned, logged, or written anywhere -- only its sha256 digest is."""

    payload = config.model_dump(mode="json")
    for command in payload.get("verification", {}).get("commands", []):
        command["environment"] = sorted(command.get("environment", {}).keys())
    return payload


def build_execution_authorization_package(
    project_root: str | Path,
    *,
    operation_id: str,
    task_id: str,
    task_version: int,
    specification: TaskSpecification,
    config: ApoapsisConfig,
) -> ExecutionAuthorizationPackage:
    """Deterministically computes exactly what a 'Start coding' confirmation
    would authorize right now. Called identically by the UI preview, by
    ``prepare_execution_operation`` (which persists the result), and by
    ``run_execution_operation``'s pre-flight recheck -- there is exactly
    one function that decides what 'the same authorization' means."""

    root = Path(project_root).resolve()
    config = effective_config_for_specification(config, specification)
    repository = GitRepository(root)
    head = repository.run(["rev-parse", "HEAD"]).stdout.strip()
    fingerprint = compute_worktree_fingerprint(root)
    frontier_available = config.models.frontier_coder is not None
    predicted_route: str | None = None
    predicted_route_reason: str | None = None
    if config.execution.mode == ExecutionMode.AGENT:
        routing_decision = select_agent_route(
            specification, config.execution, frontier_available=frontier_available
        )
        predicted_route = routing_decision.route.value
        predicted_route_reason = routing_decision.reason

    provider_kinds = {"frontier": config.models.frontier.provider}
    model_names = {"frontier": config.models.frontier.model}
    if config.models.local_coder is not None:
        provider_kinds["local_coder"] = config.models.local_coder.provider
        model_names["local_coder"] = config.models.local_coder.model
    if config.models.frontier_coder is not None:
        provider_kinds["frontier_coder"] = config.models.frontier_coder.provider
        model_names["frontier_coder"] = config.models.frontier_coder.model

    safe_config = _safe_config_payload(config)
    package = ExecutionAuthorizationPackage(
        operation_id=operation_id,
        task_id=task_id,
        task_version=task_version,
        specification_sha256=_sha256_canonical(specification.model_dump(mode="json")),
        repository_root=str(root),
        repository_head_commit=head,
        worktree_fingerprint=fingerprint.digest,
        effective_config_sha256=_sha256_canonical(safe_config),
        predicted_route=predicted_route,
        predicted_route_reason=predicted_route_reason,
        provider_kinds=provider_kinds,
        model_names=model_names,
        local_agent_budget=config.execution.agent,
        frontier_agent_budget=config.execution.frontier_agent,
        context_ceilings=config.context,
        completion_policy=config.execution.completion_policy,
        verification_backend=config.verification.backend.backend.value,
        verification_command_catalog=[
            item.name for item in config.verification.commands
        ],
        verification_config_sha256=_sha256_canonical(safe_config["verification"]),
    )
    package_sha256 = _sha256_canonical(
        package.model_dump(
            mode="json",
            exclude={"package_sha256", "generated_at", "operation_id"},
        )
    )
    return package.model_copy(update={"package_sha256": package_sha256})


def write_execution_authorization_package(
    audit: TaskAuditStore, package: ExecutionAuthorizationPackage
) -> AuditArtifact:
    return audit.write_json(
        f"execution-authorization-{package.operation_id}.json",
        package,
        kind="execution_authorization_package",
    )


__all__ = [
    "AUTHORITY_RULES",
    "ExecutionAuthorizationPackage",
    "build_execution_authorization_package",
    "write_execution_authorization_package",
]
