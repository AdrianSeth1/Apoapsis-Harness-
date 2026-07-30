"""Docker lifecycle for a long-lived, hardened, disposable workcell.

`DockerExecutionBackend` runs one command per container and throws it away.
That is right for verification and wrong for a coding agent: the whole point of
Slice 2 is a *persistent* shell and working directory, which means one
container that lives for the session.

So this controller does `create` → `start` → many `exec` → `freeze` → `destroy`,
and every step is fail-closed:

* preflight refuses to start unless the pinned image digest is already present;
* every container it creates carries a run-id label, and it will never kill or
  remove a container whose label it cannot verify;
* `destroy` records what it actually cleaned up, including whether the process
  tree and the controller-owned socket really went away.

Cold and warm timings are recorded separately, because "the model was fast"
and "the image was already warm" are different claims.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field

if TYPE_CHECKING:
    from apoapsis.workcell.relay_preflight import RelayReadinessReport

from apoapsis.execution.backend import SandboxUnavailableError
from apoapsis.specification.schema import StrictModel, utc_now
from apoapsis.workcell.platform_support import assess_socket_support
from apoapsis.workcell.pins import WorkcellConfig

_RUNTIME_CALL_TIMEOUT_SECONDS = 30.0
_RUN_ID_LABEL = "apoapsis.run_id"
_MANAGED_LABEL = "apoapsis.managed"
_WORKCELL_LABEL = "apoapsis.workcell"


class WorkcellExecResult(StrictModel):
    argv: list[str] = Field(min_length=1)
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = Field(default=0.0, ge=0)
    timed_out: bool = False


class WorkcellTimings(StrictModel):
    """Timings kept apart so a warm cache cannot be reported as model speed."""

    #: Container create + start, excluding any image pull (there is none).
    container_start_seconds: float = Field(default=0.0, ge=0)
    #: First readiness probe. This is the cold cost.
    readiness_seconds: float = Field(default=0.0, ge=0)
    #: Whether the image and dependency layer were already resident.
    warm_start: bool = False
    #: Wall time of the agent session itself.
    agent_session_seconds: float = Field(default=0.0, ge=0)
    #: Time spent freezing and destroying.
    teardown_seconds: float = Field(default=0.0, ge=0)


class CleanupRecord(StrictModel):
    """What teardown actually achieved, not what it attempted."""

    container_removed: bool = False
    #: Fails closed: if ownership cannot be verified by label, nothing is
    #: killed and this says so.
    ownership_verified: bool = False
    process_tree_terminated: bool = False
    model_socket_removed: bool = False
    workspace_retained_for_admission: bool = True
    residue: list[str] = Field(default_factory=list)

    @property
    def clean(self) -> bool:
        return (
            self.container_removed
            and self.ownership_verified
            and self.process_tree_terminated
            and self.model_socket_removed
            and not self.residue
        )


class WorkcellRunRecord(StrictModel):
    schema_version: str = "1.0"
    run_id: str = Field(min_length=1)
    container_name: str = Field(min_length=1)
    workcell_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    timings: WorkcellTimings = Field(default_factory=WorkcellTimings)
    cleanup: CleanupRecord = Field(default_factory=CleanupRecord)
    #: Digest of the worktree at freeze time, computed by the controller
    #: outside the model's trust domain.
    frozen_worktree_fingerprint: str | None = None


class WorkcellController:
    """Owns the container. The model owns nothing outside it."""

    def __init__(self, config: WorkcellConfig) -> None:
        self.config = config
        self.run_id = uuid.uuid4().hex
        self.container_name = f"apoapsis-workcell-{self.run_id[:12]}"
        self.record = WorkcellRunRecord(
            run_id=self.run_id,
            container_name=self.container_name,
            workcell_manifest_digest=config.pin.manifest_digest(),
        )

    # -- preflight ---------------------------------------------------------

    def preflight(self) -> None:
        """Refuse to start unless a real, pinned, hardened run is possible."""

        executable = self.config.runtime_executable
        if shutil.which(executable) is None:
            raise SandboxUnavailableError(
                f"container runtime {executable!r} was not found on PATH"
            )
        info = self._run_runtime(["info", "--format", "{{.OSType}}"])
        if info.returncode != 0 or info.stdout.strip() != "linux":
            raise SandboxUnavailableError(
                "the container runtime is not running Linux containers "
                f"(reported {info.stdout.strip()!r})"
            )
        reference = self.image_reference
        if self._run_runtime(["image", "inspect", reference]).returncode != 0:
            raise SandboxUnavailableError(
                f"pinned image {reference} is not present locally; Apoapsis "
                f"never pulls automatically. Run:\n    {executable} pull "
                f"{reference}\nthen retry."
            )
        workspace = Path(self.config.workspace_host_path)
        if not workspace.is_dir():
            raise SandboxUnavailableError(
                f"the disposable clone {workspace} does not exist"
            )
        artifact = Path(self.config.task_artifact_host_path)
        if not artifact.is_file():
            raise SandboxUnavailableError(
                f"the read-only task artifact {artifact} does not exist"
            )
        forwarder = Path(self.config.egress.forwarder_host_path)
        if not forwarder.is_file():
            raise SandboxUnavailableError(
                f"the forwarder tooling {forwarder} does not exist"
            )
        expected = self.config.pin.relay.forwarder_sha256
        actual = _file_sha256(forwarder)
        if actual != expected:
            raise SandboxUnavailableError(
                f"the forwarder at {forwarder} hashes to {actual}, but the run "
                f"manifest pins {expected}. The egress code is part of the "
                "experiment's identity; refusing to run a different one."
            )
        assessment = assess_socket_support(self.config.egress.model_socket_host_path)
        if not assessment.usable:
            raise SandboxUnavailableError(
                assessment.detail
                + (
                    "\n\nTry:\n- " + "\n- ".join(assessment.remedies)
                    if assessment.remedies
                    else ""
                )
            )

    @property
    def image_reference(self) -> str:
        return f"{self.config.pin.container.image}@{self.config.pin.container.image_digest}"

    # -- lifecycle ---------------------------------------------------------

    def build_create_argv(self) -> list[str]:
        """The exact `create` argv. Separated so tests can assert on it.

        Every hardening flag here is enforced by the runtime, not by the
        prompt, which is the whole basis on which ADR 0077 permits a shell.
        """

        limits = self.config.limits
        egress = self.config.egress
        return [
            self.config.runtime_executable,
            "create",
            "--name",
            self.container_name,
            "--label",
            f"{_MANAGED_LABEL}=true",
            "--label",
            f"{_WORKCELL_LABEL}=true",
            "--label",
            f"{_RUN_ID_LABEL}={self.run_id}",
            "--pull=never",
            # No route to anything. The model endpoint arrives via the
            # controller-owned socket below, so there is nothing to allowlist.
            "--network",
            self.config.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(limits.pids_limit),
            "--memory",
            f"{limits.memory_limit_mb}m",
            "--cpus",
            str(limits.cpu_limit),
            "--user",
            self.config.user,
            "--tmpfs",
            f"/tmp:size={limits.tmpfs_size_mb}m",
            # Writable: the clone is sacrificial and Git inside it is fine.
            "-v",
            f"{self.config.workspace_host_path}:/workspace:rw",
            # Read-only and deliberately outside /workspace, so the approved
            # task can never be rewritten or committed as project content.
            "-v",
            f"{self.config.task_artifact_host_path}:/task/task.md:ro",
            # The only egress. A *dedicated* directory containing nothing but
            # the socket -- mounting a broad writable host path would hand the
            # workcell a channel the relay does not mediate.
            "-v",
            f"{egress.socket_host_directory}:{egress.socket_container_directory}:rw",
            # Immutable controller tooling, read-only and outside /workspace so
            # the agent cannot edit it and it never enters the computed delta.
            "-v",
            f"{egress.forwarder_host_path}:{egress.forwarder_container_path}:ro",
            "-e",
            f"OPENAI_BASE_URL={egress.base_url}",
            "-e",
            f"APOAPSIS_MODEL_SOCKET={egress.model_socket_container_path}",
            "-w",
            "/workspace",
            self.image_reference,
            # Sleep as PID 1 so the container is a persistent shell host; every
            # agent action arrives as an `exec` into this namespace.
            "sleep",
            "infinity",
        ]

    def start(self, *, warm_start: bool = False) -> None:
        self.preflight()
        clock = time.monotonic()
        created = self._run_runtime(self.build_create_argv()[1:])
        if created.returncode != 0:
            raise SandboxUnavailableError(
                f"could not create the workcell: {created.stderr.strip()}"
            )
        started = self._run_runtime(["start", self.container_name])
        if started.returncode != 0:
            self.destroy()
            raise SandboxUnavailableError(
                f"could not start the workcell: {started.stderr.strip()}"
            )
        self.record.timings.container_start_seconds = time.monotonic() - clock
        self.record.timings.warm_start = warm_start

        probe_clock = time.monotonic()
        readiness = self.exec(["true"], timeout_seconds=30.0)
        self.record.timings.readiness_seconds = time.monotonic() - probe_clock
        if readiness.exit_code != 0:
            self.destroy()
            raise SandboxUnavailableError(
                "the workcell started but did not become ready"
            )

    def exec(
        self, argv: list[str], *, timeout_seconds: float | None = None
    ) -> WorkcellExecResult:
        """Run one command inside the persistent container.

        Never through a host shell: `argv` is passed as a list, and any shell
        the agent wants runs *inside* the workcell where it is contained.
        """

        effective = timeout_seconds or self.config.limits.command_timeout_seconds
        full = [
            self.config.runtime_executable,
            "exec",
            "--user",
            self.config.user,
            "-w",
            "/workspace",
            self.container_name,
            *argv,
        ]
        clock = time.monotonic()
        try:
            completed = subprocess.run(
                full,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return WorkcellExecResult(
                argv=argv,
                exit_code=None,
                stdout=_to_text(exc.stdout),
                stderr=_to_text(exc.stderr),
                duration_seconds=time.monotonic() - clock,
                timed_out=True,
            )
        except OSError as exc:
            return WorkcellExecResult(
                argv=argv,
                exit_code=None,
                stderr=str(exc),
                duration_seconds=time.monotonic() - clock,
            )
        return WorkcellExecResult(
            argv=argv,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - clock,
        )

    def freeze(self) -> str | None:
        """Pause the container and fingerprint the worktree from outside.

        The fingerprint is computed by the controller against the host-side
        clone, never read from a command the model could have influenced.
        """

        self._run_runtime(["pause", self.container_name])
        fingerprint = _worktree_fingerprint(Path(self.config.workspace_host_path))
        self.record.frozen_worktree_fingerprint = fingerprint
        return fingerprint

    def destroy(self) -> CleanupRecord:
        """Tear down, and record what actually happened.

        Fails closed on ownership: a container whose run-id label cannot be
        confirmed is left alone. Killing an unidentified container is worse
        than leaking one.
        """

        clock = time.monotonic()
        cleanup = self.record.cleanup
        cleanup.ownership_verified = self._verify_ownership()
        if not cleanup.ownership_verified:
            cleanup.residue.append(
                f"container {self.container_name!r} could not be confirmed as "
                "ours by run-id label; it was left untouched"
            )
        else:
            self._run_runtime(["kill", self.container_name])
            removed = self._run_runtime(["rm", "-f", self.container_name])
            cleanup.container_removed = removed.returncode == 0
            cleanup.process_tree_terminated = cleanup.container_removed
            if not cleanup.container_removed:
                cleanup.residue.append(
                    f"container {self.container_name!r} could not be removed: "
                    f"{removed.stderr.strip()}"
                )

        socket_path = Path(self.config.egress.model_socket_host_path)
        try:
            socket_path.unlink(missing_ok=True)
            cleanup.model_socket_removed = not socket_path.exists()
        except OSError as exc:
            cleanup.residue.append(f"model socket could not be removed: {exc}")

        # The workspace is deliberately *not* deleted here. Delta admission
        # runs against it after teardown; destroying it would destroy the
        # candidate.
        cleanup.workspace_retained_for_admission = Path(
            self.config.workspace_host_path
        ).is_dir()

        self.record.timings.teardown_seconds = time.monotonic() - clock
        self.record.finished_at = datetime.now(timezone.utc)
        return cleanup

    # -- internals ---------------------------------------------------------

    def _verify_ownership(self) -> bool:
        inspected = self._run_runtime(
            [
                "inspect",
                "--format",
                '{{ index .Config.Labels "' + _RUN_ID_LABEL + '" }}',
                self.container_name,
            ]
        )
        if inspected.returncode != 0:
            return False
        return inspected.stdout.strip() == self.run_id

    def _run_runtime(self, args: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self.config.runtime_executable, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_RUNTIME_CALL_TIMEOUT_SECONDS,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailableError(
                f"{self.config.runtime_executable} {' '.join(args)} failed: {exc}"
            ) from exc


def check_relay_readiness(
    controller: "WorkcellController",
    *,
    relay_requests_before: int,
    relay_requests_after: int,
) -> "RelayReadinessReport":
    """Run the three readiness probes inside the started container.

    Separated from `WorkcellController` so it can be exercised against a fake
    exec function: the classification logic is worth testing without Docker,
    and the Docker part is a thin loop over `controller.exec`.
    """

    from apoapsis.workcell.relay_preflight import (
        ReadinessStep,
        build_probe_argv,
        classify_probe_output,
        evaluate_readiness,
        one_token_payload,
    )

    egress = controller.config.egress
    base = f"http://127.0.0.1:{egress.loopback_port}"
    plan = [
        (ReadinessStep.FORWARDER_LISTENING, "GET", f"{base}/health", ""),
        (ReadinessStep.HEALTH_ROUND_TRIP, "GET", f"{base}/v1/models", ""),
        (
            ReadinessStep.ONE_TOKEN_COMPLETION,
            "POST",
            f"{base}/v1/chat/completions",
            one_token_payload(controller.config.pin.model.model_name),
        ),
    ]
    results = []
    for step, method, url, payload in plan:
        outcome = controller.exec(
            build_probe_argv(
                method=method, url=url, payload=payload, timeout_seconds=60.0
            ),
            timeout_seconds=90.0,
        )
        result = classify_probe_output(
            step,
            stdout=outcome.stdout,
            exit_code=outcome.exit_code,
            duration=outcome.duration_seconds,
        )
        results.append(result)
        if result.status.value == "failed":
            # Stop at the first failure. Running the token-spending step after
            # the health route already failed spends a request to learn nothing.
            break
    return evaluate_readiness(
        results,
        relay_requests_observed=max(0, relay_requests_after - relay_requests_before),
    )


def _worktree_fingerprint(root: Path) -> str | None:
    """SHA-256 over the sorted relative paths and content digests.

    Computed host-side, outside the model's trust domain, so a model that
    rewrites Git history inside the sacrificial clone changes nothing that
    Apoapsis relies on.
    """

    import hashlib
    import os

    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    entries: list[tuple[str, str]] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in sorted(files):
            path = Path(current) / name
            try:
                content = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            entries.append((path.relative_to(root).as_posix(), content))
    for relative, content in sorted(entries):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def load_workcell_config(path: Path) -> WorkcellConfig:
    """Load a pinned workcell configuration from JSON.

    Every pin is required by the schema, so a partially specified file fails
    here rather than producing an unidentifiable run.
    """

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxUnavailableError(
            f"could not read the workcell configuration {path}: {exc}"
        ) from exc
    return WorkcellConfig.model_validate(payload)
