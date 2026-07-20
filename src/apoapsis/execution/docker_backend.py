from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat as stat_module
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from apoapsis.execution.backend import (
    DockerBackendConfig,
    ExecutionContext,
    RawCommandOutcome,
    SandboxUnavailableError,
)
from apoapsis.verification.results import VerificationStatus

if TYPE_CHECKING:
    from apoapsis.verification.runner import VerificationCommand


_EXCLUDED_DIR_NAMES = {".git", ".apoapsis", ".sol"}
_DOCKER_CALL_TIMEOUT_SECONDS = 30.0
_RUN_ID_LABEL = "apoapsis.run_id"
_MANAGED_LABEL = "apoapsis.managed"


class DockerExecutionBackend:
    """Runs configured verification commands inside a locked-down, network-
    disabled Linux container via the `docker` CLI.

    Fails closed: any preflight problem raises `SandboxUnavailableError`
    with a precise diagnostic. There is no code path here that runs a
    command directly on the host -- switching backends requires the caller
    to explicitly reconfigure `[verification.backend]`.
    """

    backend_name = "docker"

    def __init__(self, config: DockerBackendConfig) -> None:
        self.config = config

    def prepare(
        self, project_root: Path, task_id: str, attempt: int
    ) -> ExecutionContext:
        self.preflight()
        project_root = Path(project_root).resolve()
        task_slug = task_id.removeprefix("TASK-").lower()
        sandbox_root = (
            project_root
            / ".apoapsis"
            / "sandbox"
            / task_slug
            / f"attempt-{attempt:03d}"
        )
        workspace = sandbox_root / "workspace"
        if sandbox_root.exists():
            shutil.rmtree(sandbox_root)
        sandbox_root.mkdir(parents=True, exist_ok=True)
        skipped_reparse_points = _copy_workspace(project_root, workspace)
        before_manifest = _content_manifest(workspace)
        return ExecutionContext(
            root=workspace,
            display_root=str(project_root),
            extra={
                "task_slug": task_slug,
                "attempt": attempt,
                "workspace": workspace,
                "sandbox_root": sandbox_root,
                "before_manifest": before_manifest,
                "skipped_reparse_points": skipped_reparse_points,
            },
        )

    def run_command(
        self,
        context: ExecutionContext,
        command: "VerificationCommand",
        *,
        environment: dict[str, str],
    ) -> RawCommandOutcome:
        run_id = uuid.uuid4().hex
        container_name = (
            f"apoapsis-verify-{context.extra['task_slug']}-"
            f"{context.extra['attempt']:03d}-{run_id[:8]}"
        )
        workspace = context.extra["workspace"]
        argv = self._build_argv(container_name, run_id, workspace, command, environment)
        effective_timeout = min(
            command.timeout_seconds, self.config.wall_clock_timeout_seconds
        )
        started_at = datetime.now(timezone.utc)
        started_clock = time.monotonic()
        exit_code: int | None = None
        stdout = ""
        stderr = ""
        status = VerificationStatus.ERROR
        timeout_cleanup: str | None = None
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                shell=False,
                check=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            status = (
                VerificationStatus.PASSED
                if exit_code == 0
                else VerificationStatus.FAILED
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _to_text(exc.stdout)
            stderr = _to_text(exc.stderr)
            status = VerificationStatus.TIMED_OUT
            timeout_cleanup = self._kill_and_remove(container_name, run_id)
        except OSError as exc:
            stderr = str(exc)
            status = VerificationStatus.ERROR
        duration = time.monotonic() - started_clock
        metadata: dict[str, object] = {
            "sandboxed": True,
            "image": self.config.image,
            "image_digest": self.config.image_digest,
            "container_name": container_name,
            "run_id": run_id,
            "cpu_limit": self.config.cpu_limit,
            "memory_limit_mb": self.config.memory_limit_mb,
            "pids_limit": self.config.pids_limit,
            "network": self.config.network,
        }
        if timeout_cleanup is not None:
            metadata["timeout_cleanup"] = timeout_cleanup
        return RawCommandOutcome(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            status=status,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            duration_seconds=duration,
            backend=self.backend_name,
            backend_metadata=metadata,
        )

    def finalize(self, context: ExecutionContext) -> list[str]:
        workspace = context.extra["workspace"]
        before = context.extra["before_manifest"]
        after = _content_manifest(workspace)
        changed = sorted(
            path for path, digest in before.items() if after.get(path) != digest
        )
        shutil.rmtree(context.extra["sandbox_root"], ignore_errors=True)
        return changed

    def preflight(self) -> None:
        """Raise `SandboxUnavailableError` unless a real sandboxed run is
        possible right now. Reused verbatim by `apoapsis doctor`."""

        docker = self.config.docker_executable
        if shutil.which(docker) is None:
            raise SandboxUnavailableError(
                f"Docker CLI {docker!r} was not found on PATH"
            )
        version = self._run_docker(["info", "--format", "{{.ServerVersion}}"])
        if version.returncode != 0:
            raise SandboxUnavailableError(
                "Docker engine did not respond to 'docker info' -- is Docker "
                f"Desktop running? ({version.stderr.strip() or version.stdout.strip()})"
            )
        os_type = self._run_docker(["info", "--format", "{{.OSType}}"])
        reported = os_type.stdout.strip()
        if os_type.returncode != 0 or reported != "linux":
            raise SandboxUnavailableError(
                "Docker engine is not running Linux containers (reported "
                f"OSType={reported!r}); switch Docker Desktop to Linux containers"
            )
        reference = f"{self.config.image}@{self.config.image_digest}"
        inspected = self._run_docker(["image", "inspect", reference])
        if inspected.returncode != 0:
            present_digests = self._locally_present_digests(self.config.image)
            if present_digests:
                raise SandboxUnavailableError(
                    f"pinned image {reference} does not match any locally present "
                    f"digest for {self.config.image!r} (found: "
                    f"{', '.join(present_digests)}); Apoapsis never pulls or retags "
                    "automatically. Either re-pin "
                    "[verification.backend.docker].image_digest to one of the "
                    f"digests above, or run:\n    docker pull {reference}\n"
                    "(this requires network access) to fetch the exact configured "
                    "digest, then retry."
                )
            raise SandboxUnavailableError(
                f"pinned image {reference} is not present locally; Apoapsis "
                "never pulls automatically. Run:\n"
                f"    docker pull {reference}\n"
                "(this requires network access), then retry."
            )

    def _locally_present_digests(self, image: str) -> list[str]:
        """Read-only lookup of the digests actually present locally for
        `image` (name/tag only, no digest pinned) -- lets preflight
        distinguish "this image was never pulled at all" from "this image
        is present, but not at the configured digest" without ever pulling,
        retagging, or otherwise mutating anything."""

        inspected = self._run_docker(
            ["image", "inspect", image, "--format", "{{json .RepoDigests}}"]
        )
        if inspected.returncode != 0:
            return []
        try:
            digests = json.loads(inspected.stdout.strip() or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(digests, list):
            return []
        return [value for value in digests if isinstance(value, str)]

    def _run_docker(self, args: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self.config.docker_executable, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_DOCKER_CALL_TIMEOUT_SECONDS,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailableError(
                f"docker {' '.join(args)} failed: {exc}"
            ) from exc

    def _verify_ownership(self, container_name: str, run_id: str) -> bool:
        """True only if `container_name` exists and carries the exact
        `apoapsis.run_id` label this backend attached when it created it."""

        inspected = self._run_docker(
            [
                "inspect",
                "--format",
                "{{ index .Config.Labels \"" + _RUN_ID_LABEL + "\" }}",
                container_name,
            ]
        )
        if inspected.returncode != 0:
            return False
        return inspected.stdout.strip() == run_id

    def _kill_and_remove(self, container_name: str, run_id: str) -> str:
        """Best-effort cleanup after a timeout -- but only ever against the
        exact container this call created. Fails closed: if ownership
        cannot be verified via the run-id label, the container is left
        untouched rather than killed or removed."""

        if not self._verify_ownership(container_name, run_id):
            return "ownership_unverified_left_running"
        self._run_docker(["kill", container_name])
        self._run_docker(["rm", "-f", container_name])
        return "removed"

    def _build_argv(
        self,
        container_name: str,
        run_id: str,
        workspace: Path,
        command: "VerificationCommand",
        environment: dict[str, str],
    ) -> list[str]:
        argv = [
            self.config.docker_executable,
            "run",
            "--rm",
            "--name",
            container_name,
            "--label",
            f"{_MANAGED_LABEL}=true",
            "--label",
            f"{_RUN_ID_LABEL}={run_id}",
            "--pull=never",
            "--network",
            self.config.network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.config.pids_limit),
            "--memory",
            f"{self.config.memory_limit_mb}m",
            "--cpus",
            str(self.config.cpu_limit),
            "--user",
            self.config.user,
            "--tmpfs",
            f"/tmp:size={self.config.tmpfs_size_mb}m",
            "-v",
            f"{workspace}:/workspace:rw",
            "-w",
            "/workspace",
        ]
        for key in self.config.environment_allowlist:
            if key in environment:
                argv.extend(["-e", f"{key}={environment[key]}"])
        argv.append(f"{self.config.image}@{self.config.image_digest}")
        argv.extend(command.argv)
        return argv


def _is_reparse_point(entry: "os.DirEntry[str]") -> bool:
    """True for a symlink, Windows junction, or any other reparse point.

    `os.path.islink`/`DirEntry.is_symlink` do NOT detect Windows directory
    junctions (they carry `IO_REPARSE_TAG_MOUNT_POINT`, not
    `IO_REPARSE_TAG_SYMLINK`), so this also checks the Windows-only
    `st_file_attributes` bit that is set for every reparse-point type,
    junctions included. If the entry cannot be stat'd at all, the
    exception propagates -- failing the whole copy closed rather than
    silently treating an unreadable entry as an ordinary file or directory.
    """

    info = entry.stat(follow_symlinks=False)
    if stat_module.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", None)
    return attributes is not None and bool(
        attributes & stat_module.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _copy_workspace(source: Path, destination: Path) -> int:
    """Copy `source` into `destination` for isolated verification.

    Excludes `.git`/`.apoapsis`/`.sol`. Never follows or copies symlinks,
    Windows junctions, or any other reparse point -- entries are walked
    manually (not via `os.walk`, which does not honor `followlinks=False`
    for junctions) so nothing is ever recursed into before it has been
    confirmed not to be a reparse point. Returns the number of entries
    skipped for this reason.
    """

    skipped = 0

    def walk(current_source: Path, current_destination: Path) -> None:
        nonlocal skipped
        current_destination.mkdir(parents=True, exist_ok=True)
        with os.scandir(current_source) as entries:
            for entry in entries:
                if entry.name in _EXCLUDED_DIR_NAMES:
                    continue
                if _is_reparse_point(entry):
                    skipped += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    walk(Path(entry.path), current_destination / entry.name)
                elif entry.is_file(follow_symlinks=False):
                    shutil.copy2(entry.path, current_destination / entry.name)
                else:
                    # Not a reparse point, but also not an ordinary file or
                    # directory (e.g. a socket or device) -- never copy it.
                    skipped += 1

    walk(source.resolve(), destination)
    return skipped


def _content_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for current_root, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(current_root) / name
            relative = path.relative_to(root).as_posix()
            manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def _to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
