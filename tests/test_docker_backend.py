from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apoapsis.execution.backend import DockerBackendConfig, SandboxUnavailableError
from apoapsis.execution.docker_backend import DockerExecutionBackend
from apoapsis.verification.results import VerificationStatus
from apoapsis.verification.runner import VerificationCommand

_DIGEST = "sha256:" + "a" * 64


class _FakeDockerProcess:
    """Stand-in for `subprocess.run` keyed on the docker subcommand, so
    Docker CLI/engine behavior can be injected deterministically without a
    real Docker installation."""

    def __init__(
        self,
        *,
        engine_ok: bool = True,
        os_type: str = "linux",
        image_present: bool = True,
        run_returncode: int = 0,
        run_stdout: str = "",
        run_stderr: str = "",
        raise_timeout_on_run: bool = False,
    ) -> None:
        self.engine_ok = engine_ok
        self.os_type = os_type
        self.image_present = image_present
        self.run_returncode = run_returncode
        self.run_stdout = run_stdout
        self.run_stderr = run_stderr
        self.raise_timeout_on_run = raise_timeout_on_run
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        sub = argv[1] if len(argv) > 1 else None
        if sub == "info":
            if not self.engine_ok:
                return subprocess.CompletedProcess(argv, 1, "", "engine down")
            fmt = argv[argv.index("--format") + 1]
            if fmt == "{{.ServerVersion}}":
                return subprocess.CompletedProcess(argv, 0, "29.5.2\n", "")
            if fmt == "{{.OSType}}":
                return subprocess.CompletedProcess(argv, 0, f"{self.os_type}\n", "")
            raise AssertionError(f"unexpected info format: {fmt}")
        if sub == "image":
            return subprocess.CompletedProcess(
                argv,
                0 if self.image_present else 1,
                "",
                "" if self.image_present else "no such image",
            )
        if sub == "run":
            if self.raise_timeout_on_run:
                raise subprocess.TimeoutExpired(
                    cmd=argv, timeout=1, output=self.run_stdout, stderr=self.run_stderr
                )
            return subprocess.CompletedProcess(
                argv, self.run_returncode, self.run_stdout, self.run_stderr
            )
        if sub in ("rm", "kill"):
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected docker invocation: {argv}")


def _config(**overrides: object) -> DockerBackendConfig:
    values: dict[str, object] = dict(
        image="python",
        image_digest=_DIGEST,
        cpu_limit=1.0,
        memory_limit_mb=256,
        pids_limit=32,
        tmpfs_size_mb=64,
        wall_clock_timeout_seconds=30,
        environment_allowlist=[],
        user="65532:65532",
    )
    values.update(overrides)
    return DockerBackendConfig(**values)


def _patch_subprocess_run(fake: _FakeDockerProcess):
    return mock.patch("apoapsis.execution.docker_backend.subprocess.run", side_effect=fake)


class DockerCommandConstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project_root = Path(self.temporary_directory.name) / "project"
        self.project_root.mkdir()
        (self.project_root / "file.txt").write_text("hello\n", encoding="utf-8")

    def test_argv_contains_every_required_flag_and_no_forbidden_ones(self) -> None:
        fake = _FakeDockerProcess(run_returncode=0, run_stdout="ok\n")
        backend = DockerExecutionBackend(_config())
        command = VerificationCommand(
            name="probe", category="sandbox", argv=["echo", "ok"]
        )
        with _patch_subprocess_run(fake):
            context = backend.prepare(self.project_root, "TASK-ARGV0001", 1)
            outcome = backend.run_command(context, command, environment={})
            backend.finalize(context)

        self.assertEqual(outcome.status, VerificationStatus.PASSED)
        run_argv = next(call for call in fake.calls if call[1] == "run")

        def value_after(flag: str) -> str:
            return run_argv[run_argv.index(flag) + 1]

        self.assertIn("--rm", run_argv)
        self.assertEqual(value_after("--name"), "apoapsis-verify-argv0001-001")
        self.assertEqual(value_after("--network"), "none")
        self.assertIn("--read-only", run_argv)
        self.assertEqual(value_after("--cap-drop"), "ALL")
        self.assertEqual(value_after("--security-opt"), "no-new-privileges")
        self.assertEqual(value_after("--pids-limit"), "32")
        self.assertEqual(value_after("--memory"), "256m")
        self.assertEqual(value_after("--cpus"), "1.0")
        self.assertEqual(value_after("--user"), "65532:65532")
        self.assertEqual(value_after("--tmpfs"), "/tmp:size=64m")
        self.assertTrue(value_after("-v").endswith(":/workspace:rw"))
        self.assertEqual(value_after("-w"), "/workspace")
        self.assertIn(f"python@{_DIGEST}", run_argv)
        self.assertEqual(run_argv[-2:], ["echo", "ok"])

        forbidden_tokens = {
            "--privileged",
            "host",
            "--pid",
            "--ipc",
            "--userns",
            "--network=host",
        }
        self.assertFalse(forbidden_tokens & set(run_argv))
        self.assertNotIn("-e", run_argv)
        joined = " ".join(run_argv)
        self.assertNotIn("docker.sock", joined)

    def test_environment_allowlist_is_the_only_source_of_dash_e_flags(self) -> None:
        fake = _FakeDockerProcess()
        backend = DockerExecutionBackend(_config(environment_allowlist=["ALLOWED_VAR"]))
        command = VerificationCommand(name="probe", category="sandbox", argv=["true"])
        with _patch_subprocess_run(fake):
            context = backend.prepare(self.project_root, "TASK-ARGV0002", 1)
            backend.run_command(
                context,
                command,
                environment={"ALLOWED_VAR": "x", "SECRET_VAR": "y"},
            )
            backend.finalize(context)

        run_argv = next(call for call in fake.calls if call[1] == "run")
        self.assertIn("-e", run_argv)
        self.assertIn("ALLOWED_VAR=x", run_argv)
        self.assertNotIn("SECRET_VAR=y", run_argv)
        self.assertEqual(run_argv.count("-e"), 1)


class DockerFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project_root = Path(self.temporary_directory.name) / "project"
        self.project_root.mkdir()

    def _assert_never_ran_docker_run(self, fake: _FakeDockerProcess) -> None:
        self.assertFalse(any(call[1] == "run" for call in fake.calls))

    def test_missing_cli_fails_closed(self) -> None:
        backend = DockerExecutionBackend(_config(docker_executable="apoapsis-no-such-docker"))
        with self.assertRaisesRegex(SandboxUnavailableError, "was not found on PATH"):
            backend.prepare(self.project_root, "TASK-FAIL0001", 1)

    def test_engine_unreachable_fails_closed(self) -> None:
        fake = _FakeDockerProcess(engine_ok=False)
        backend = DockerExecutionBackend(_config())
        with _patch_subprocess_run(fake):
            with self.assertRaisesRegex(SandboxUnavailableError, "did not respond"):
                backend.prepare(self.project_root, "TASK-FAIL0002", 1)
        self._assert_never_ran_docker_run(fake)

    def test_windows_containers_fail_closed(self) -> None:
        fake = _FakeDockerProcess(os_type="windows")
        backend = DockerExecutionBackend(_config())
        with _patch_subprocess_run(fake):
            with self.assertRaisesRegex(SandboxUnavailableError, "Linux containers"):
                backend.prepare(self.project_root, "TASK-FAIL0003", 1)
        self._assert_never_ran_docker_run(fake)

    def test_missing_image_fails_closed_with_pull_command(self) -> None:
        fake = _FakeDockerProcess(image_present=False)
        backend = DockerExecutionBackend(_config())
        with _patch_subprocess_run(fake):
            with self.assertRaisesRegex(
                SandboxUnavailableError, f"docker pull python@{_DIGEST}"
            ):
                backend.prepare(self.project_root, "TASK-FAIL0004", 1)
        self._assert_never_ran_docker_run(fake)
        # never pulls automatically
        self.assertFalse(any(call[1] == "pull" for call in fake.calls))

    def test_preflight_failure_never_yields_a_result_shaped_like_success(self) -> None:
        fake = _FakeDockerProcess(engine_ok=False)
        backend = DockerExecutionBackend(_config())
        with _patch_subprocess_run(fake):
            try:
                backend.prepare(self.project_root, "TASK-FAIL0005", 1)
                self.fail("expected SandboxUnavailableError")
            except SandboxUnavailableError:
                pass


class DockerTimeoutCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project_root = Path(self.temporary_directory.name) / "project"
        self.project_root.mkdir()

    def test_timeout_triggers_explicit_kill_and_remove(self) -> None:
        fake = _FakeDockerProcess(raise_timeout_on_run=True)
        backend = DockerExecutionBackend(_config())
        command = VerificationCommand(
            name="slow", category="sandbox", argv=["sleep", "999"], timeout_seconds=1
        )
        with _patch_subprocess_run(fake):
            context = backend.prepare(self.project_root, "TASK-TIMEOUT01", 1)
            outcome = backend.run_command(context, command, environment={})
            backend.finalize(context)

        self.assertEqual(outcome.status, VerificationStatus.TIMED_OUT)
        container_name = "apoapsis-verify-timeout01-001"
        kill_calls = [call for call in fake.calls if call[1] == "kill"]
        rm_calls = [call for call in fake.calls if call[1] == "rm"]
        self.assertTrue(any(container_name in call for call in kill_calls))
        self.assertTrue(any(container_name in call for call in rm_calls))


class DockerWorkspaceSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project_root = Path(self.temporary_directory.name) / "project"
        self.project_root.mkdir()
        (self.project_root / "source.py").write_text("x = 1\n", encoding="utf-8")
        (self.project_root / ".git").mkdir()
        (self.project_root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (self.project_root / ".apoapsis").mkdir()
        (self.project_root / ".apoapsis" / "config.toml").write_text("x", encoding="utf-8")
        if not (self.project_root / "escape_target").exists():
            (self.project_root / "escape_target").write_text("outside\n", encoding="utf-8")
        try:
            os.symlink(
                self.project_root / "escape_target", self.project_root / "link.py"
            )
            self.symlinks_supported = True
        except (OSError, NotImplementedError):
            self.symlinks_supported = False

    def test_copy_excludes_metadata_and_skips_symlinks(self) -> None:
        fake = _FakeDockerProcess()
        backend = DockerExecutionBackend(_config())
        with _patch_subprocess_run(fake):
            context = backend.prepare(self.project_root, "TASK-COPY0001", 1)
            workspace = context.extra["workspace"]
            self.assertTrue((workspace / "source.py").is_file())
            self.assertFalse((workspace / ".git").exists())
            self.assertFalse((workspace / ".apoapsis").exists())
            if self.symlinks_supported:
                self.assertFalse((workspace / "link.py").exists())
                self.assertEqual(context.extra["skipped_symlinks"], 1)
            backend.finalize(context)

    def test_finalize_flags_mutation_of_a_pre_existing_file_but_not_new_files(
        self,
    ) -> None:
        fake = _FakeDockerProcess()
        backend = DockerExecutionBackend(_config())
        with _patch_subprocess_run(fake):
            context = backend.prepare(self.project_root, "TASK-COPY0002", 1)
            workspace = context.extra["workspace"]
            # simulate a misbehaving test mutating a pre-existing source file
            (workspace / "source.py").write_text("x = 2\n", encoding="utf-8")
            # simulate a normal, expected new output file
            (workspace / "coverage.out").write_text("ok\n", encoding="utf-8")
            changed = backend.finalize(context)

        self.assertEqual(changed, ["source.py"])


class DockerLiveIntegrationTest(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("APOAPSIS_RUN_LIVE_DOCKER_TESTS") == "1",
        "set APOAPSIS_RUN_LIVE_DOCKER_TESTS=1 to run the real Docker sandbox smoke test",
    )
    def test_real_docker_engine_runs_a_trivial_command(self) -> None:
        image = os.environ.get("APOAPSIS_SANDBOX_TEST_IMAGE")
        digest = os.environ.get("APOAPSIS_SANDBOX_TEST_DIGEST")
        if not image or not digest:
            self.skipTest(
                "set APOAPSIS_SANDBOX_TEST_IMAGE and APOAPSIS_SANDBOX_TEST_DIGEST "
                "to a locally-present, digest-pinned image to run this test"
            )
        backend = DockerExecutionBackend(_config(image=image, image_digest=digest))
        try:
            backend.preflight()
        except SandboxUnavailableError as exc:
            self.skipTest(f"Docker sandbox unavailable: {exc}")

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            project_root.mkdir()
            (project_root / "marker.txt").write_text("hi\n", encoding="utf-8")
            command = VerificationCommand(
                name="live-probe", category="sandbox", argv=["true"]
            )
            context = backend.prepare(project_root, "TASK-LIVE0001", 1)
            try:
                outcome = backend.run_command(context, command, environment={})
            finally:
                changed = backend.finalize(context)

        self.assertEqual(outcome.status, VerificationStatus.PASSED)
        self.assertTrue(outcome.backend_metadata.get("sandboxed"))
        self.assertEqual(changed, [])


if __name__ == "__main__":
    unittest.main()
