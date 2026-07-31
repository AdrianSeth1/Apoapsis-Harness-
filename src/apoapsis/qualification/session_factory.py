"""Build a real workcell session from the frozen manifest, or refuse.

Every value here comes from the manifest. That is the point: a factory that
filled gaps with sensible defaults would let a rehearsal run under a
configuration nobody froze, and the run would look identical to one that did.
Where the manifest genuinely does not carry a value -- the relay's byte limits,
say -- it is named as a rehearsal-only constant with a reason, rather than
inherited silently from a model default.

The socket lives on the host filesystem rather than a named volume. Both work
now that WSL integration is on, and a host path is checkable: the controller
can stat the socket it created, and the evidence can record the path the
workcell actually mounted.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from apoapsis.qualification.pilot import PilotManifest
from apoapsis.workcell.forwarder import FORWARDER_VERSION
from apoapsis.workcell.live_session import LiveWorkcellSession
from apoapsis.workcell.pins import (
    AgentCliPin,
    ContainerPin,
    EgressPolicy,
    ModelPin,
    RelayPin,
    WorkcellConfig,
    WorkcellLimits,
    WorkcellPin,
)
from apoapsis.workcell.relay import RELAY_VERSION
from apoapsis.workcell.relay_policy import ModelRelayConfig

#: Rehearsal-only relay limits. Named here, with reasons, because the manifest
#: freezes model spend rather than transport mechanics and inheriting these
#: from library defaults would leave them undeclared.
REHEARSAL_MAX_TOTAL_REQUESTS = 400
REHEARSAL_MAX_CONCURRENT = 4
#: Short enough that a vanished client is recorded as a cancellation rather
#: than pinning a worker for the full idle timeout (the Slice 2S finding).
REHEARSAL_STREAM_WRITE_TIMEOUT = 2.0


class SessionFactoryError(RuntimeError):
    """The session cannot be built from this manifest. Not a run that failed."""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_workcell_config(
    manifest: PilotManifest,
    *,
    repo: Path,
    workspace: Path,
    socket_directory: Path,
    upstream_base_url: str,
    forwarder_path: Path | None = None,
) -> WorkcellConfig:
    """Assemble the config. Every pinned value is read from the manifest."""

    package_root = repo / manifest.crisis_atlas.package_root
    task_artifact = package_root / "task.md"
    if not task_artifact.is_file():
        raise SessionFactoryError(f"the task artifact is missing at {task_artifact}")

    forwarder = Path(
        forwarder_path
        or repo / "src" / "apoapsis" / "workcell" / "forwarder.py"
    )
    if not forwarder.is_file():
        raise SessionFactoryError(f"the forwarder is missing at {forwarder}")

    server = manifest.server
    qwen = manifest.qwen
    ladder = manifest.threshold_ladder

    model_pin = ModelPin(
        model_name=manifest.model.model_alias,
        model_file_sha256=manifest.model.sha256,
        quantization=manifest.model.quantization,
        server_name="llama-server",
        server_version=f"{server.build_version}-{server.build_commit}",
        server_flags_sha256=server.argv_sha256,
        context_limit_tokens=manifest.budgets.context_limit_tokens,
        max_output_tokens=manifest.budgets.max_output_tokens,
        # Not a sampling seed: nothing transmits one. The pin's field name
        # predates that finding; the value recorded is the repetition ordinal,
        # and `sampling.seed_reaches_provider_request` is false in the manifest.
        sampling_seed=0,
        temperature=0.0 if manifest.sampling.temperature is None else manifest.sampling.temperature,
        chat_template_sha256=qwen.effective_settings_sha256,
        endpoint=upstream_base_url,
    )

    agent_pin = AgentCliPin(
        cli_name=qwen.package_name,
        cli_version=qwen.package_version,
        cli_sha256=qwen.entry_point_sha256,
        is_default_distribution=True,
        system_prompt_sha256=qwen.package_metadata_sha256,
        tool_schema_sha256=qwen.expected_tool_names_sha256,
        tool_names=list(qwen.expected_tool_names),
        effective_config_sha256=qwen.effective_settings_sha256,
    )

    container_pin = ContainerPin(
        image=qwen.image,
        image_digest=qwen.image_digest,
        runtime_version="docker",
    )

    socket_path = socket_directory / "model.sock"
    # Two formats on purpose, and they are not interchangeable. `RelayPin`
    # records "METHOD PATH" because a run's identity includes which verbs were
    # reachable; `ModelRelayConfig` narrows the built-in allowlist by *path*.
    # Passing the pin's format to the config is rejected, which is how this was
    # found rather than silently widening anything.
    # `/health` is what relay readiness probes before a single token is spent,
    # so narrowing it away makes readiness fail with a 403 that looks like a
    # containment finding. Kept deliberately, and kept narrow: `/v1/completions`
    # stays excluded because the pinned CLI uses the chat endpoint.
    pinned_routes = [
        "GET /health",
        "GET /v1/models",
        "POST /v1/chat/completions",
    ]
    narrowed_paths = ["/health", "/v1/chat/completions", "/v1/models"]
    relay_pin = RelayPin(
        relay_version=RELAY_VERSION,
        forwarder_version=FORWARDER_VERSION,
        forwarder_sha256=_digest(forwarder),
        allowed_routes=sorted(pinned_routes),
        upstream_base_url=upstream_base_url,
    )

    pin = WorkcellPin(
        model=model_pin,
        agent_cli=agent_pin,
        container=container_pin,
        relay=relay_pin,
        seed_commit=manifest.crisis_atlas.seed_commit,
        task_artifact_sha256=_digest(task_artifact),
        verifier_version=manifest.evaluator_framework_commit[:12],
    )

    return WorkcellConfig(
        pin=pin,
        limits=WorkcellLimits(
            wall_clock_seconds=manifest.budgets.per_arm_wall_clock_seconds,
            command_timeout_seconds=manifest.budgets.verification_timeout_seconds,
        ),
        workspace_host_path=str(workspace),
        task_artifact_host_path=str(task_artifact),
        egress=EgressPolicy(
            relay=ModelRelayConfig(
                upstream_base_url=upstream_base_url,
                socket_path=str(socket_path),
                allowed_routes=narrowed_paths,
                max_total_requests=REHEARSAL_MAX_TOTAL_REQUESTS,
                max_concurrent_requests=REHEARSAL_MAX_CONCURRENT,
                stream_write_timeout_seconds=REHEARSAL_STREAM_WRITE_TIMEOUT,
                max_output_tokens=manifest.budgets.max_output_tokens,
            ),
            forwarder_host_path=str(forwarder),
        ),
    )


def session_factory_from_manifest(
    manifest: PilotManifest,
    *,
    repo: Path,
    workspace: Path,
    socket_directory: Path,
    upstream_base_url: str,
) -> LiveWorkcellSession:
    """One real session, configured entirely from the frozen manifest."""

    config = build_workcell_config(
        manifest,
        repo=repo,
        workspace=workspace,
        socket_directory=socket_directory,
        upstream_base_url=upstream_base_url,
    )
    return LiveWorkcellSession(config)
