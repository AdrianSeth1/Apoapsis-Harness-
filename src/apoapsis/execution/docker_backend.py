from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
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
        skipped_symlinks = _copy_workspace(project_root, workspace)
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
                "skipped_symlinks": skipped_symlinks,
            },
        )

    def run_command(
        self,
        context: ExecutionContext,
        command: "VerificationCommand",
        *,
        environment: dict[str, str],
    ) -> RawCommandOutcome:
        container_name = (
            f"apoapsis-verify-{context.extra['task_slug']}-"
            f"{context.extra['attempt']:03d}"
        )
        self._remove_if_exists(container_name)
        workspace = context.extra["workspace"]
        argv = self._build_argv(container_name, workspace, command, environment)
        effective_timeout = min(
            command.timeout_seconds, self.config.wall_clock_timeout_seconds
        )
        started_at = datetime.now(timezone.utc)
        started_clock = time.monotonic()
        exit_code: int | None = None
        stdout = ""
        stderr = ""
        status = VerificationStatus.ERROR
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
            self._kill_and_remove(container_name)
        except OSError as exc:
            stderr = str(exc)
            status = VerificationStatus.ERROR
        duration = time.monotonic() - started_clock
        return RawCommandOutcome(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            status=status,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            duration_seconds=duration,
            backend=self.backend_name,
            backend_metadata={
                "sandboxed": True,
                "image": self.config.image,
                "image_digest": self.config.image_digest,
                "container_name": container_name,
                "cpu_limit": self.config.cpu_limit,
                "memory_limit_mb": self.config.memory_limit_mb,
                "pids_limit": self.config.pids_limit,
                "network": self.config.network,
            },
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
            raise SandboxUnavailableError(
                f"pinned image {reference} is not present locally; Apoapsis "
                "never pulls automatically. Run:\n"
                f"    docker pull {reference}\n"
                "(this requires network access), then retry."
            )

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

    def _remove_if_exists(self, name: str) -> None:
        self._run_docker(["rm", "-f", name])

    def _kill_and_remove(self, name: str) -> None:
        self._run_docker(["kill", name])
        self._run_docker(["rm", "-f", name])

    def _build_argv(
        self,
        container_name: str,
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


def _copy_workspace(source: Path, destination: Path) -> int:
    """Copy `source` into `destination` for isolated verification.

    Excludes `.git`/`.apoapsis`/`.sol`. Never follows or copies symlinks --
    this is the entire containment guarantee against a symlink pointing
    outside the workspace. Returns the number of symlinks skipped.
    """

    destination.mkdir(parents=True, exist_ok=True)
    skipped = 0
    resolved_source = source.resolve()
    for current_root, dirs, files in os.walk(resolved_source, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if name not in _EXCLUDED_DIR_NAMES
            and not os.path.islink(os.path.join(current_root, name))
        ]
        relative = Path(current_root).relative_to(resolved_source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            source_file = Path(current_root) / name
            if source_file.is_symlink():
                skipped += 1
                continue
            shutil.copy2(source_file, target_dir / name)
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
