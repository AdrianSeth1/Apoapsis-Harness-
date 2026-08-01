"""Everything the workcell binds before a single model token is spent.

The Crisis Atlas rescore refused to compare its two arms because the sliced
arm's seed commit was never written down and its output cap changed mid-run.
That is not a documentation failure to apologise for once; it is the default
outcome unless the pins are a required, hashed object that a run cannot start
without.

So `WorkcellPin` has no optional identity fields. Every version that could
change a result — CLI, model weights, server build and flags, chat template,
system prompt, tool schemas, container image — is required, and
`manifest_digest()` folds them into one value that goes into every evidence
record. A run whose digest differs from another run's is a different
experiment, and the paired scorer will say so.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel
# `pin_capture` imports `pins` only inside a function, so this direction is
# safe; the ladder is defined next to the capture that measures it.
from apoapsis.workcell.pin_capture import ResolvedThresholdLadder
from apoapsis.workcell.relay_policy import ModelRelayConfig

_SHA256_HEX = r"^[0-9a-f]{64}$"
_IMAGE_DIGEST = r"^sha256:[0-9a-f]{64}$"

#: Bumped whenever the set of pinned fields changes, so an old manifest can
#: never be silently compared against a new one.
#:
#: 1.1 adds `AgentCliPin.effective_config_sha256`. Every Slice 2B manifest
#: digest is therefore invalid against a 2C one, which is correct: those runs
#: had an unpinned configuration and are not comparable to runs that do not.
#:
#: 1.2 adds `WorkcellPin.threshold_ladder`. A run recorded before it cannot be
#: compared against one recorded after, and that is the honest outcome: the
#: earlier runs did not know the window they compacted at, and their pinned
#: 0.85 described a percentage rather than a trigger.
PIN_SCHEMA_VERSION = "1.2"


class ModelPin(StrictModel):
    """The weights and the server that serves them."""

    model_name: str = Field(min_length=1)
    model_file_sha256: str = Field(pattern=_SHA256_HEX)
    quantization: str = Field(min_length=1)
    #: The server build, not just "llama.cpp". A different build can change
    #: tokenization, sampling, and stop-reason reporting.
    server_name: str = Field(min_length=1)
    server_version: str = Field(min_length=1)
    #: Hash of the exact argv the server was launched with. Flags such as
    #: batch size, Flash Attention, and KV-cache precision change results.
    server_flags_sha256: str = Field(pattern=_SHA256_HEX)
    #: What the server actually reports, not what the CLI assumes. The
    #: conformance suite compares these two.
    context_limit_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    sampling_seed: int
    temperature: float = Field(ge=0)
    #: The chat template governs whether tool calls survive a round trip at
    #: all. A malformed tool envelope is an adapter defect until proven
    #: otherwise, and that proof starts here.
    chat_template_sha256: str = Field(pattern=_SHA256_HEX)
    endpoint: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_output_fits_context(self) -> ModelPin:
        if self.max_output_tokens > self.context_limit_tokens:
            raise ValueError(
                "max_output_tokens cannot exceed the server context window; "
                "a run pinned this way would report output truncation for what "
                "is actually context exhaustion"
            )
        return self


class AgentCliPin(StrictModel):
    """The coding CLI whose native loop drives the workcell.

    `is_default_distribution` exists so an arm cannot quietly claim to be the
    "default Qwen CLI control" while running a fork. The capability spike
    compares against a control, and the control's identity has to be checkable.
    """

    cli_name: str = Field(min_length=1)
    cli_version: str = Field(min_length=1)
    #: Hash of the installed CLI bundle inside the image.
    cli_sha256: str = Field(pattern=_SHA256_HEX)
    is_default_distribution: bool
    #: Hash of the system prompt the CLI sends, captured from the CLI itself
    #: rather than from Apoapsis. Slice 2 must not substitute its own prompt.
    system_prompt_sha256: str = Field(pattern=_SHA256_HEX)
    #: Hash of the deterministically sorted tool schema list. Reordering the
    #: schemas breaks prompt-cache locality even when the set is unchanged, so
    #: the ordering is part of the identity.
    tool_schema_sha256: str = Field(pattern=_SHA256_HEX)
    tool_names: list[str] = Field(min_length=1)
    #: Hash of the *effective* configuration the CLI resolved for itself --
    #: settings files merged, environment applied, provider entry selected --
    #: not of the file Apoapsis wrote. Slice 2B pinned the bundle and the
    #: declared limits and still could not have detected an edited settings
    #: file, which is the same shape of hole as the unrecorded seed commit that
    #: made the Crisis Atlas arms incomparable. See
    #: `pin_capture.EffectiveCliConfig`.
    effective_config_sha256: str = Field(pattern=_SHA256_HEX)
    #: Line-delimited `stream-json` is the supported adapter input. Scraping
    #: terminal text is not a conformance-testable interface.
    headless_event_format: Literal["stream-json"] = "stream-json"

    @model_validator(mode="after")
    def validate_tool_names_sorted_and_unique(self) -> AgentCliPin:
        if len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("tool_names contains duplicates")
        if list(self.tool_names) != sorted(self.tool_names):
            raise ValueError(
                "tool_names must be sorted; a stable, deterministically ordered "
                "schema list is what makes the prompt prefix cacheable"
            )
        return self


class ContainerPin(StrictModel):
    """The image and the runtime that enforce containment."""

    image: str = Field(min_length=1)
    image_digest: str = Field(pattern=_IMAGE_DIGEST)
    runtime_name: str = Field(default="docker", min_length=1)
    runtime_version: str = Field(min_length=1)
    #: Digest of the prepared dependency layer, when one is reused. Bound into
    #: evidence so a cached layer cannot become invisible state.
    dependency_layer_digest: str | None = Field(default=None, pattern=_IMAGE_DIGEST)


class RelayPin(StrictModel):
    """The egress path's identity.

    The relay and the forwarder are as much a part of the experiment as the
    model: a change to either alters what the agent can reach, how large a
    response can be, and how a cancelled stream behaves. Binding them here means
    a run cannot be compared against one that used different egress code.
    """

    relay_version: str = Field(min_length=1)
    forwarder_version: str = Field(min_length=1)
    #: Hash of the exact forwarder bytes mounted into the container.
    forwarder_sha256: str = Field(pattern=_SHA256_HEX)
    #: Read-only mount point, deliberately outside `/workspace`.
    forwarder_container_path: str = Field(
        default="/opt/apoapsis/forwarder.py", min_length=1
    )
    #: Sorted "METHOD PATH" strings actually permitted for this run.
    allowed_routes: list[str] = Field(min_length=1)
    #: The one upstream the relay will forward to.
    upstream_base_url: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_routes_sorted(self) -> RelayPin:
        if list(self.allowed_routes) != sorted(self.allowed_routes):
            raise ValueError("allowed_routes must be sorted for a stable digest")
        if self.forwarder_container_path.startswith("/workspace"):
            raise ValueError(
                "the forwarder is controller tooling and must be mounted "
                "outside the project worktree"
            )
        return self


class NativeContextPin(StrictModel):
    """Qwen's own context settings, pinned because we delegate to them.

    Under Option B the native loop manages the live conversation, so these are
    not Apoapsis policy -- they are *upstream behaviour a comparison depends
    on*. Pinning them means a run recorded against 0.85 is never silently
    compared with one recorded against something else.

    They are pinned rather than reimplemented. `context.autoCompactThreshold`
    is documented as a ceiling on a three-tier warn/auto/hard ladder computed
    internally by `computeThresholds()`, firing earlier on small windows.
    Duplicating that ladder would give Apoapsis a second, subtly different
    model of when compaction happens, and the two would diverge without anyone
    noticing -- which is the failure mode this whole slice keeps rediscovering.
    """

    #: `context.autoCompactThreshold` as a *percentage*, and nothing more.
    #:
    #: 0.85 is genuinely the pinned build's `DEFAULT_PCT`, measured by executing
    #: its own `computeThresholds`. It is nevertheless **not the trigger**, and
    #: an earlier revision of this comment claiming it was "the resolved
    #: default" invited exactly the misuse that followed: `computeThresholds`
    #: returns `min(pct * window, effectiveWindow - AUTOCOMPACT_BUFFER)`, and at
    #: the pinned 65,536-token window the ceiling governs. The real trigger is
    #: 32,536 -- 49.65% of the window -- while `pct * window` is 55,706.
    #:
    #: Anything predicting when compaction fires must use
    #: `WorkcellPin.threshold_ladder`, not this field. It is retained because
    #: the configured percentage is still part of the run's identity.
    auto_compact_threshold: float = Field(default=0.85, gt=0.0, le=1.0)
    #: `model.chatCompression.maxRecentFilesToRetain`. Pinned because it
    #: materially decides post-compaction continuity: it is how many
    #: recently-touched files Qwen restores into history after compacting, and
    #: therefore how much of the working set survives without the capsule
    #: having to carry it.
    max_recent_files_to_retain: int = Field(default=5, ge=0, le=100)
    #: `model.chatCompression.maxRecentImagesToRetain`. Irrelevant to a coding
    #: run with computer-use disabled, pinned so that stays true on purpose.
    max_recent_images_to_retain: int = Field(default=3, ge=0, le=100)
    #: Whether these were read back from the CLI's resolved settings or are
    #: still this model's defaults. A run recorded on assumed values is not
    #: evidence about the run's actual behaviour.
    resolved_from_cli: bool = False


class WorkcellPin(StrictModel):
    """The complete, required identity of one workcell run."""

    schema_version: str = PIN_SCHEMA_VERSION
    model: ModelPin
    agent_cli: AgentCliPin
    container: ContainerPin
    relay: RelayPin
    #: Qwen's own context management settings, which Option B delegates to.
    native_context: NativeContextPin = Field(default_factory=NativeContextPin)
    #: The compaction ladder the pinned CLI's own `computeThresholds` returns
    #: for this window, executed rather than reimplemented.
    #:
    #: Optional because a manifest written before the ladder was measurable is
    #: still a valid record of its run. It is **not** optional at the point of
    #: use: a consumer that needs a trigger must refuse to synthesise one from
    #: `native_context.auto_compact_threshold`, because that product is the
    #: proportional term alone and overstated the real trigger by 1.71x for the
    #: whole of Slice 5C.
    threshold_ladder: "ResolvedThresholdLadder | None" = None
    #: Commit the disposable clone was made from.
    seed_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    #: Hash of the read-only task artifact mounted outside the project tree.
    task_artifact_sha256: str = Field(pattern=_SHA256_HEX)
    verifier_version: str = Field(min_length=1)

    def manifest_digest(self) -> str:
        """One stable value identifying this exact experiment.

        Computed over the sorted JSON of every pinned field, so adding a field
        in a later schema version changes the digest and prevents a silent
        cross-version comparison.
        """

        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class WorkcellLimits(StrictModel):
    """Resource ceilings the controller enforces, not the prompt.

    These mirror the values the unrestricted Crisis Atlas control ran under, so
    the capability comparison is not confounded by a more generous box.
    """

    cpu_limit: float = Field(default=4.0, gt=0, le=64)
    memory_limit_mb: int = Field(default=2048, ge=256, le=131_072)
    pids_limit: int = Field(default=256, ge=1, le=10_000)
    tmpfs_size_mb: int = Field(default=512, ge=16, le=8_192)
    #: Total budget for the whole agent session.
    wall_clock_seconds: float = Field(default=1_800.0, gt=0, le=21_600)
    #: Per shell action, matching the control's 300-second cap.
    command_timeout_seconds: float = Field(default=300.0, gt=0, le=3_600)
    max_tool_output_chars: int = Field(default=25_000, ge=1_000, le=1_000_000)


class WorkcellConfig(StrictModel):
    """How the controller builds and confines the workcell.

    `network` is `Literal["none"]` rather than a string with a sensible
    default. The model endpoint is reached through a controller-owned socket
    (see `egress`), so there is no legitimate reason for this to ever be
    anything else, and a `Literal` means a configuration file cannot make it so.
    """

    pin: WorkcellPin
    limits: WorkcellLimits = Field(default_factory=WorkcellLimits)
    network: Literal["none"] = "none"
    user: str = Field(default="65532:65532", pattern=r"^[0-9]+:[0-9]+$")
    runtime_executable: str = Field(default="docker", min_length=1)
    #: Absolute host path of the disposable clone. Mounted read-write at
    #: `/workspace`; it is sacrificial, so Git inside it is permitted.
    workspace_host_path: str = Field(min_length=1)
    #: Absolute host path of the approved task document. Mounted read-only at
    #: `/task/task.md`, deliberately outside `/workspace` so it can never be
    #: mistaken for, or committed as, delivered project content.
    task_artifact_host_path: str = Field(min_length=1)
    egress: "EgressPolicy"

    @model_validator(mode="after")
    def validate_task_artifact_outside_workspace(self) -> WorkcellConfig:
        workspace = self.workspace_host_path.replace("\\", "/").rstrip("/")
        artifact = self.task_artifact_host_path.replace("\\", "/")
        if artifact.startswith(workspace + "/"):
            raise ValueError(
                "the task artifact must live outside the workspace clone, or it "
                "will appear in the delivered project tree and in the computed "
                "delta"
            )
        return self


class EgressPolicy(StrictModel):
    """How the model endpoint is reachable when the container has no network.

    The container runs with `--network none`, so it has a loopback interface
    and nothing else. The only way out is a Unix domain socket the controller
    creates, owns, and bind-mounts in; an in-container forwarder exposes it on
    a fixed loopback port for the CLI to use as its OpenAI-compatible base URL.

    This is stronger than an allowlisted host route and it is also more useful:
    every model request crosses a boundary the controller can count, log, and
    stop. There is no route to the host's other ports, no DNS, and nothing to
    reconfigure by prompt — the netns simply has no route.
    """

    #: Controller-side forwarding rule. Names the one upstream, the socket, and
    #: every limit the relay enforces.
    relay: ModelRelayConfig
    #: Host path of the forwarder script, mounted read-only. Immutable
    #: controller tooling: the agent cannot edit it and it is not in the
    #: worktree, so it never enters the computed delta.
    forwarder_host_path: str = Field(min_length=1)
    forwarder_container_path: str = Field(
        default="/opt/apoapsis/forwarder.py", min_length=1
    )
    #: Where the socket's *directory* appears inside the container. Only the
    #: dedicated directory is mounted, never a broad writable host path.
    socket_container_directory: str = Field(default="/run/apoapsis", min_length=1)
    #: Host directory for the ADR 0078 envelope-check socket, mounted the same
    #: way and subject to the same rule.
    #:
    #: A *second* directory rather than a second socket in the first one.
    #: `prepare_socket_directory` requires each socket directory to contain
    #: exactly its own socket, so that mounting it cannot become an unmediated
    #: channel between the controller and the workcell. Sharing one directory
    #: between two relays would have quietly weakened that, and the check
    #: caught the attempt.
    envelope_socket_host_directory: str | None = Field(default=None, min_length=1)
    envelope_socket_container_directory: str = Field(
        default="/run/apoapsis-envelope", min_length=1
    )
    #: Loopback port the in-container forwarder listens on. Inside a
    #: `--network none` namespace this reaches nothing but the forwarder.
    loopback_port: int = Field(default=8080, ge=1, le=65_535)

    @property
    def model_socket_host_path(self) -> str:
        return self.relay.socket_path

    @property
    def socket_host_directory(self) -> str:
        return self.relay.socket_directory

    @property
    def model_socket_container_path(self) -> str:
        name = self.relay.socket_path.replace("\\", "/").rsplit("/", 1)[-1]
        return f"{self.socket_container_directory.rstrip('/')}/{name}"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.loopback_port}/v1"

    @property
    def max_model_requests(self) -> int:
        return self.relay.max_total_requests

    @model_validator(mode="after")
    def validate_tooling_outside_worktree(self) -> EgressPolicy:
        for path in (self.forwarder_container_path, self.socket_container_directory):
            if path.startswith("/workspace"):
                raise ValueError(
                    f"{path!r} is inside the project worktree; controller "
                    "tooling and the relay socket must live outside it"
                )
        return self


WorkcellConfig.model_rebuild()
