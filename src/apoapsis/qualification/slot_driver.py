"""One arm slot: a real workcell, the real CLI, and observation afterwards.

`_apply_script` is gone from the evidence path. It wrote the candidate files
with Python, which proved that Python can write files and nothing about the
system under test. Here the scripted provider emits native tool calls, the
genuine Qwen CLI consumes them, and Qwen writes the candidate into a worktree
the controller then inspects. If the tool surface is not what the manifest
expects, if the CLI refuses the call, or if the envelope does not survive the
relay, no candidate appears and the slot fails -- which is the point.

The controller never takes the agent's word for anything. Every fact recorded
about a slot is read from the worktree, from `/proc/self/mounts` inside the
container, from the relay's counters, or from the runtime's process list.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from apoapsis.qualification.fake_pilot_provider import ScriptId
from apoapsis.qualification.fake_provider_server import FakeProviderServer
from apoapsis.qualification.observation import (
    MountObservation,
    RuntimeResidue,
    TeardownObservation,
    observe_mounts,
    observe_residue,
    observe_teardown,
)
from apoapsis.qualification.session_factory import build_workcell_config
from apoapsis.workcell.live_session import LiveWorkcellSession

WORKCELL_UID = 65532

def controller_address() -> str:
    """Address an upstream served by the controller process itself.

    The workcell reaches the controller-owned relay over a Unix socket.  The
    relay, not the workcell, makes the HTTP request to the upstream, so a fake
    provider in the same controller process is always loopback.  Resolving the
    controller hostname happened to work from a native WSL controller but, in
    a host-networked controller container, resolved to Docker Desktop's host
    gateway and silently bypassed the local fake provider.
    """

    return "127.0.0.1"


#: The declared placeholder Qwen's OpenAI-compatible client validates against.
#:
#: It is public, non-secret, and written here in the source rather than read
#: from anywhere: it authenticates nothing, grants no external access, and is
#: accepted only because Qwen 0.21.1 refuses to start without a non-empty value
#: -- "Missing API key for OpenAI-compatible auth. Set
#: settings.security.auth.apiKey, or set the 'OPENAI_API_KEY' environment
#: variable." The only endpoint it is ever sent to is the loopback forwarder,
#: which reaches the controller-owned relay and nothing else; the relay does not
#: inspect it. It is never sourced from the host environment, and no host
#: credential reaches the workcell by this or any other route.
#:
#: See ADR 0090. The security property is credential exclusion, not the absence
#: of all authentication-shaped configuration -- the latter is unsatisfiable
#: against the pinned CLI, and a property nothing can satisfy protects nothing.
LOCAL_PLACEHOLDER_API_KEY = "apoapsis-local-nonsecret-placeholder"


def qwen_settings(
    *, model_alias: str, loopback_port: int, context_window: int, max_output: int
) -> dict:
    """The pinned coding profile. `yolo`, no computer use, no tool search."""

    return {
        "selectedAuthType": "openai",
        "security": {
            "auth": {
                "selectedType": "openai",
                # In the settings file, never in the environment. The workcell
                # image supplies no token-like environment variable and the
                # `no-token-environment` containment probe stays exactly as it
                # was; this value lives in a file the manifest binds by digest,
                # so changing it changes the run's identity.
                "apiKey": LOCAL_PLACEHOLDER_API_KEY,
            }
        },
        "telemetry": {"enabled": False},
        "usageStatisticsEnabled": False,
        "providerProtocol": "openai",
        "tools": {
            "computerUse": {"enabled": False},
            "toolSearch": {"enabled": False},
            "approvalMode": "yolo",
        },
        "modelProviders": {
            "openai": [
                {
                    "id": model_alias,
                    "name": model_alias,
                    "baseUrl": f"http://127.0.0.1:{loopback_port}/v1",
                    "generationConfig": {
                        "contextWindowSize": context_window,
                        "samplingParams": {"max_tokens": max_output},
                    },
                }
            ]
        },
    }


def hand_over(path: Path) -> None:
    """Give the worktree to the workcell user.

    The controller runs as root, so everything it creates is root-owned and the
    workcell (uid 65532) cannot write to it. Qwen diagnosed exactly this during
    the live smoke -- it tried shell redirection, `cp`, `touch`, Python writes
    and `nsenter` before reporting the ownership rather than claiming success.
    Handing the tree over is the controller's job.
    """

    for item in [path, *path.rglob("*")]:
        os.chown(item, WORKCELL_UID, WORKCELL_UID)


def write_settings(session, settings: dict) -> str:
    """Write settings at the QWEN_HOME *root*, and return their digest.

    Not under `.qwen/`. Putting them there produced "QWEN_HOME points to
    /tmp/qwen-home but no settings.json was found there", after which the CLI
    used its own 64,000-token default and the relay refused the request for
    exceeding the pinned ceiling.
    """

    import hashlib

    body = json.dumps(settings)
    code, _out, err = session.exec(
        [
            "sh",
            "-c",
            "mkdir -p /tmp/qwen-home && cat > /tmp/qwen-home/settings.json "
            "<< 'SETTINGS_EOF'\n" + body + "\nSETTINGS_EOF\n"
            "test -s /tmp/qwen-home/settings.json",
        ]
    )
    if code != 0:
        raise RuntimeError(f"could not write Qwen settings: {err[:200]}")
    return hashlib.sha256(body.encode()).hexdigest()


def run_qwen(
    session,
    prompt: str,
    *,
    timeout_seconds: float = 900.0,
    continue_session: bool = False,
    stream_json: bool = False,
):
    """Run the genuine CLI headless and return its streams."""

    command = (
        "cd /workspace && "
        "HOME=/tmp/qwen-home QWEN_HOME=/tmp/qwen-home "
        "QWEN_CODE_SUPPRESS_YOLO_WARNING=1 "
        f"timeout {int(timeout_seconds)} node "
        "/usr/local/lib/node_modules/@qwen-code/qwen-code/cli-entry.js "
        f"--yolo {'--continue ' if continue_session else ''}"
        f"{'--output-format stream-json ' if stream_json else ''}"
        f"--prompt {json.dumps(prompt)} "
        "> /tmp/qwen-stdout.log 2>/tmp/qwen-stderr.log; echo EXIT=$?"
    )
    code, out, _err = session.exec(
        ["sh", "-c", command], timeout_seconds=timeout_seconds + 120
    )
    _, stdout, _ = session.exec(["sh", "-c", "cat /tmp/qwen-stdout.log"])
    _, stderr, _ = session.exec(["sh", "-c", "cat /tmp/qwen-stderr.log"])
    return code, out.strip(), stdout, stderr


class SlotObservation:
    """Everything one slot was observed to do. Plain object; the model lives
    in `rehearsal.ArmSlotResult` and is built from this."""

    def __init__(self) -> None:
        self.mounts: MountObservation | None = None
        self.residue: RuntimeResidue | None = None
        self.teardown: TeardownObservation | None = None
        self.relay_before = 0
        self.relay_after = 0
        self.provider_requests = 0
        self.created_paths: tuple[str, ...] = ()
        self.qwen_stdout = ""
        self.qwen_stderr = ""
        self.settings_sha256 = ""
        self.seed_commit_observed = ""
        self.task_bytes_sha256 = ""
        #: Read off the wire, from the `tools` array the CLI itself sent.
        self.observed_tool_names: tuple[str, ...] = ()
        self.observed_tool_schema: list | None = None
        #: Relay records for this slot whose transfer never completed. Any entry
        #: disqualifies the slot's candidate.
        self.incomplete_relay_responses: tuple[str, ...] = ()
        self.kept_workspace: Path | None = None
        self.error: str | None = None
        self.checkpoints: tuple[object, ...] = ()
        #: What the model actually cost, as the relay read it off the wire.
        #: `calls_with_usage` is the denominator: totals with no count behind
        #: them cannot be told apart from a run that reported nothing, which is
        #: exactly how the live path came to publish `input_tokens: 0`.
        self.calls_with_usage = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_input_tokens = 0
        self.peak_input_tokens = 0
        #: `(call index, input tokens, output tokens)`, in order, so context
        #: growth across the slice is plottable rather than only totalled.
        self.usage_series: tuple[tuple[int, int, int], ...] = ()


def execute_slot(
    manifest,
    *,
    repo: Path,
    seed_repository: Path,
    base: Path,
    repetition_id: str,
    arm: str,
    script: ScriptId | None,
    evidence_dir: Path,
    previous_slot_paths: tuple[Path, ...] = (),
    max_output_tokens: int,
    qwen_timeout_seconds: float = 900.0,
    seed_files: dict[str, str] | None = None,
    keep_workspace: bool = False,
    live_upstream_base_url: str | None = None,
    supervisor: Callable[[Path, int], tuple[object, str | None]] | None = None,
    continuation_limit: int = 0,
    stream_json: bool = False,
    task_text_override: str | None = None,
    write_workspace_task: bool = True,
    inject_stream_usage_options: bool = False,
    usage_observer: Callable[[int, object], None] | None = None,
) -> SlotObservation:
    """Run one slot end to end in a real `--network none` workcell."""

    observation = SlotObservation()
    slot_root = base / f"slot-{repetition_id}-{arm}"
    workspace = slot_root / "workspace"
    qwen_home = slot_root / "qwen-home"
    sockets = slot_root / "sockets"
    for path in (workspace.parent, sockets, qwen_home):
        path.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # A fresh clone per slot. Cloning rather than copying keeps the seed's own
    # identity checkable inside the slot.
    # `safe.directory` is required, not cosmetic: the seed is mounted from the
    # host and is owned by another uid, so Git refuses it as "dubious
    # ownership" and exits 128. Scoped to the clone commands rather than set
    # globally, so nothing else in the controller inherits a relaxed rule.
    # Both the repository and its `.git` are named: `-c safe.directory=*` is
    # not honoured for this check in every Git version, and the error names
    # `/seed/.git` specifically. Repeated `-c` builds the multi-valued key.
    # Passed through the environment rather than with `-c`. `git clone` from a
    # local path forks `git upload-pack`, and `-c` does not reach that child --
    # which is why three `-c safe.directory` variants all still failed with
    # "dubious ownership in repository at '/seed/.git'". GIT_CONFIG_* is
    # inherited by the child, and is scoped to these calls rather than written
    # into any global config.
    # Copy first, take ownership, then ask Git -- rather than cloning across
    # the ownership boundary. `git clone` from the read-only mount forks
    # `git upload-pack`, and neither `-c safe.directory` nor `GIT_CONFIG_*`
    # reached that child: three variants all failed identically on "dubious
    # ownership in repository at '/seed/.git'". Copying makes the question
    # disappear instead of relaxing a security check to work around it, and
    # the commit is still read from the slot's own copy rather than asserted.
    shutil.copytree(seed_repository, workspace)
    for item in [workspace, *workspace.rglob("*")]:
        os.chown(item, 0, 0)
    head = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if head.returncode != 0:
        observation.error = f"could not read the seed commit: {head.stderr[:300]}"
        return observation
    observation.seed_commit_observed = head.stdout.strip()
    # A plain copy brings gitignored state a clone never would: the seed
    # carries a `.apoapsis/` directory from earlier evaluation runs, and eight
    # of its files turned up in the first slot's worktree. Cleaning to the
    # committed tree is what makes "a fresh clone of the seed" true.
    subprocess.run(  # noqa: S603
        ["git", "clean", "-xdff", "--quiet"],
        cwd=workspace,
        capture_output=True,
        timeout=120,
    )
    subprocess.run(  # noqa: S603
        ["git", "reset", "--hard", "--quiet", observation.seed_commit_observed],
        cwd=workspace,
        capture_output=True,
        timeout=120,
    )
    shutil.rmtree(workspace / ".git", ignore_errors=True)

    package_root = repo / manifest.crisis_atlas.package_root
    task_text = (
        task_text_override
        if task_text_override is not None
        else (package_root / "task.md").read_text(encoding="utf-8")
    )
    import hashlib

    observation.task_bytes_sha256 = hashlib.sha256(task_text.encode()).hexdigest()
    if write_workspace_task:
        (workspace / "TASK.md").write_text(task_text, encoding="utf-8")
    # Extra fixtures, used by the capability probe: the marker it must read.
    for relative, body in (seed_files or {}).items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    hand_over(workspace)

    forwarder = slot_root / "forwarder.py"
    shutil.copyfile(repo / "src/apoapsis/workcell/forwarder.py", forwarder)
    # Host-resolvable copy, for the same reason as the forwarder: the daemon
    # resolves bind-mount sources, and the package path exists only inside the
    # controller. Mounted from `/opt/apoapsis/docs/...` it arrives as an empty
    # directory and `/task/task.md` is not a file.
    task_artifact = slot_root / "task.md"
    task_artifact.write_text(task_text, encoding="utf-8")

    if (script is None) == (live_upstream_base_url is None):
        raise ValueError("exactly one of script or live_upstream_base_url is required")
    provider = None
    if script is not None:
        provider = FakeProviderServer(
            script,
            model_name=manifest.model.model_alias,
            host="0.0.0.0",
            transcript_path=evidence_dir / "provider-transcript.json",
        )
        provider.start()
    try:
        if provider is not None:
            port = provider.base_url.rsplit(":", 1)[1]
            upstream = f"http://{controller_address()}:{port}"
        else:
            upstream = str(live_upstream_base_url)
        config = build_workcell_config(
            manifest,
            repo=repo,
            workspace=workspace,
            socket_directory=sockets,
            upstream_base_url=upstream,
            forwarder_path=forwarder,
            task_artifact_path=task_artifact,
            inject_stream_usage_options=inject_stream_usage_options,
        )
        session = LiveWorkcellSession(config, usage_observer=usage_observer)
        with session:
            observation.settings_sha256 = write_settings(
                session,
                qwen_settings(
                    model_alias=manifest.model.model_alias,
                    loopback_port=config.egress.loopback_port,
                    context_window=manifest.budgets.context_limit_tokens,
                    max_output=max_output_tokens,
                ),
            )
            exit_code, stderr = session.start_forwarder()
            if exit_code != 0:
                observation.error = f"forwarder did not start: {stderr[:200]}"
                return observation

            # Observed from inside the container, before any work happens.
            observation.mounts = observe_mounts(session)
            observation.relay_before = session.relay_request_count()

            prompt = (
                f"Read {'TASK.md in the current directory' if write_workspace_task else '/task/task.md'} and implement exactly "
                "what it specifies. Create the files it names at the paths it "
                "names. Do not ask questions; make the changes."
            )
            stdout_parts: list[str] = []
            stderr_parts: list[str] = []
            checkpoints: list[object] = []
            deadline = time.monotonic() + qwen_timeout_seconds
            for turn in range(continuation_limit + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    observation.error = "the frozen per-arm wall-clock budget expired"
                    break
                _, _wrapper, stdout, stderr_log = run_qwen(
                    session,
                    prompt,
                    timeout_seconds=remaining,
                    continue_session=turn > 0,
                    stream_json=stream_json,
                )
                stdout_parts.append(stdout)
                stderr_parts.append(stderr_log)
                if supervisor is None:
                    break
                checkpoint, next_prompt = supervisor(workspace, turn)
                checkpoints.append(checkpoint)
                if next_prompt is None:
                    break
                prompt = next_prompt
            observation.checkpoints = tuple(checkpoints)
            observation.qwen_stdout = "\n".join(stdout_parts)
            observation.qwen_stderr = "\n".join(stderr_parts)
            observation.relay_after = session.relay_request_count()
            # Read while the session is still alive: the relay's stats go with
            # it, and a total collected after teardown is a total nobody has.
            relay_stats = session.relay.stats
            observation.calls_with_usage = relay_stats.requests_with_usage
            observation.input_tokens = relay_stats.input_tokens
            observation.output_tokens = relay_stats.output_tokens
            observation.cached_input_tokens = relay_stats.cached_input_tokens
            observation.peak_input_tokens = relay_stats.peak_input_tokens
            observation.usage_series = relay_stats.usage_series

            # A turn whose response never completed cannot have produced a
            # proposal, whatever appeared in the worktree. Recorded as a slot
            # error so the checkpoint is never reached: scoring a candidate
            # assembled from a truncated stream is the substitution the whole
            # telemetry taxonomy exists to refuse, and it would look exactly
            # like a short answer to anyone reading the files afterwards.
            observation.incomplete_relay_responses = session.incomplete_relay_responses()
            if observation.incomplete_relay_responses:
                observation.error = (
                    "the relay recorded "
                    f"{len(observation.incomplete_relay_responses)} incomplete "
                    "response(s), so no candidate from this slot may be scored: "
                    + "; ".join(observation.incomplete_relay_responses[:3])
                )
            (evidence_dir / "qwen-stdout.log").write_text(
                observation.qwen_stdout, encoding="utf-8"
            )
            (evidence_dir / "qwen-stderr.log").write_text(
                observation.qwen_stderr, encoding="utf-8"
            )

        # Outside the context manager: the container is gone, so residue is a
        # real question rather than a formality.
        observation.residue = observe_residue(
            container_name_fragment=config.pin.container.image.split(":")[0],
            socket_path=Path(config.egress.relay.socket_path),
            relay=session.relay,
        )
    finally:
        if provider is not None:
            observation.observed_tool_names = provider.observed_tool_names
            observation.observed_tool_schema = provider.observed_tool_schema
            provider.stop()
            observation.provider_requests = provider.request_count
        else:
            observation.provider_requests = observation.relay_after - observation.relay_before

    observation.created_paths = tuple(
        sorted(
            path.relative_to(workspace).as_posix()
            for path in workspace.rglob("*")
            if path.is_file()
        )
    )
    # Preserve what Qwen produced before the worktree is destroyed.
    produced = evidence_dir / "produced-worktree"
    produced.mkdir(parents=True, exist_ok=True)
    for relative in observation.created_paths:
        source = workspace / relative
        if source.stat().st_size < 100_000:
            target = produced / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    if keep_workspace:
        # The capability probe is judged from the worktree, so it survives to
        # be inspected. Teardown is then reported honestly as not-clean rather
        # than claimed, which is the whole point of observing it.
        observation.kept_workspace = workspace
    else:
        shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(qwen_home, ignore_errors=True)
    observation.teardown = observe_teardown(
        worktree=workspace,
        qwen_home=qwen_home,
        evidence=evidence_dir,
        residue=observation.residue or RuntimeResidue(observation_failed="not observed"),
        previous_slot_paths=previous_slot_paths,
    )
    return observation
