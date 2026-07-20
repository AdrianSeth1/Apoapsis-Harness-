from __future__ import annotations

import contextlib
import json
import os
import shutil
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
    real Docker installation.

    `run` invocations are parsed for their `--name`/`apoapsis.run_id` label
    so a later `inspect` call can realistically report what the (fake)
    daemon would have recorded -- this is what lets ownership-verified
    cleanup be tested without a real container ever existing.
    """

    def __init__(
        self,
        *,
        engine_ok: bool = True,
        os_type: str = "linux",
        image_present: bool = True,
        locally_present_digests: list[str] | None = None,
        run_returncode: int = 0,
        run_stdout: str = "",
        run_stderr: str = "",
        raise_timeout_on_run: bool = False,
        inspect_reports_wrong_owner: bool = False,
    ) -> None:
        self.engine_ok = engine_ok
        self.os_type = os_type
        self.image_present = image_present
        self.locally_present_digests = locally_present_digests
        self.run_returncode = run_returncode
        self.run_stdout = run_stdout
        self.run_stderr = run_stderr
        self.raise_timeout_on_run = raise_timeout_on_run
        self.inspect_reports_wrong_owner = inspect_reports_wrong_owner
        self.calls: list[list[str]] = []
        self.created_containers: dict[str, str] = {}

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
            if "--format" in argv:
                # the fallback bare-name/tag digest-listing query used only
                # after the exact digest-pinned inspect above has failed.
                if self.locally_present_digests is None:
                    return subprocess.CompletedProcess(argv, 1, "", "no such image")
                payload = json.dumps(self.locally_present_digests)
                return subprocess.CompletedProcess(argv, 0, f"{payload}\n", "")
            return subprocess.CompletedProcess(
                argv,
                0 if self.image_present else 1,
                "",
                "" if self.image_present else "no such image",
            )
        if sub == "run":
            name = argv[argv.index("--name") + 1]
            run_id = None
            for index, token in enumerate(argv):
                if token == "--label" and argv[index + 1].startswith("apoapsis.run_id="):
                    run_id = argv[index + 1].split("=", 1)[1]
            if run_id is not None:
                self.created_containers[name] = run_id
            if self.raise_timeout_on_run:
                raise subprocess.TimeoutExpired(
                    cmd=argv, timeout=1, output=self.run_stdout, stderr=self.run_stderr
                )
            return subprocess.CompletedProcess(
                argv, self.run_returncode, self.run_stdout, self.run_stderr
            )
        if sub == "inspect":
            name = argv[-1]
            if self.inspect_reports_wrong_owner:
                return subprocess.CompletedProcess(argv, 0, "not-the-real-owner\n", "")
            reported = self.created_containers.get(name)
            if reported is None:
                return subprocess.CompletedProcess(argv, 1, "", "no such container")
            return subprocess.CompletedProcess(argv, 0, f"{reported}\n", "")
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


def _fake_docker_on_path(executable: str = "docker"):
    """Pretend only `executable` resolves on PATH; every other lookup still
    uses the real `shutil.which`. Without this, `DockerBackend.preflight()`'s
    unmocked `shutil.which` check made every test below silently depend on a
    real Docker CLI being installed on the host -- contradicting
    `_FakeDockerProcess`'s own 'without a real Docker installation' contract
    and failing on any machine without Docker. `shutil` is one process-global
    module, so the side effect must delegate rather than replace."""

    real_which = shutil.which

    def side_effect(cmd, *args, **kwargs):
        if cmd == executable:
            return f"/deterministic-fake-path/{executable}"
        return real_which(cmd, *args, **kwargs)

    return mock.patch(
        "apoapsis.execution.docker_backend.shutil.which", side_effect=side_effect
    )


@contextlib.contextmanager
def _patch_subprocess_run(fake: _FakeDockerProcess):
    with mock.patch(
        "apoapsis.execution.docker_backend.subprocess.run", side_effect=fake
    ), _fake_docker_on_path():
        yield fake


def _label_values(argv: list[str], key: str) -> list[str]:
    return [
        argv[index + 1]
        for index, token in enumerate(argv)
        if token == "--label" and argv[index + 1].startswith(f"{key}=")
    ]


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
        name = value_after("--name")
        self.assertRegex(name, r"^apoapsis-verify-argv0001-001-[0-9a-f]{8}$")
        self.assertIn("--pull=never", run_argv)
        managed_labels = _label_values(run_argv, "apoapsis.managed")
        self.assertEqual(managed_labels, ["apoapsis.managed=true"])
        run_id_labels = _label_values(run_argv, "apoapsis.run_id")
        self.assertEqual(len(run_id_labels), 1)
        run_id = run_id_labels[0].split("=", 1)[1]
        self.assertRegex(run_id, r"^[0-9a-f]{32}$")
        self.assertTrue(name.endswith(run_id[:8]))
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

    def test_two_commands_in_the_same_attempt_get_distinct_container_names(self) -> None:
        fake = _FakeDockerProcess()
        backend = DockerExecutionBackend(_config())
        command = VerificationCommand(name="probe", category="sandbox", argv=["true"])
        with _patch_subprocess_run(fake):
            context = backend.prepare(self.project_root, "TASK-ARGV0003", 1)
            backend.run_command(context, command, environment={})
            backend.run_command(context, command, environment={})
            backend.finalize(context)

        run_calls = [call for call in fake.calls if call[1] == "run"]
        names = [call[call.index("--name") + 1] for call in run_calls]
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0], names[1])


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

    def test_missing_image_with_no_local_tag_reports_absent_not_mismatched(
        self,
    ) -> None:
        """When the repository/tag has never been pulled at all (the
        fallback bare-name lookup also finds nothing), the diagnostic must
        say "not present locally", not claim a digest mismatch."""

        fake = _FakeDockerProcess(image_present=False, locally_present_digests=None)
        backend = DockerExecutionBackend(_config())
        with _patch_subprocess_run(fake):
            with self.assertRaisesRegex(SandboxUnavailableError, "is not present locally"):
                backend.prepare(self.project_root, "TASK-FAIL0006", 1)
        self._assert_never_ran_docker_run(fake)
        self.assertFalse(any(call[1] == "pull" for call in fake.calls))

    def test_digest_mismatch_is_distinguished_from_absent_image(self) -> None:
        """When the configured repository/tag *is* present locally but at a
        different digest than pinned, the diagnostic must name the mismatch
        and list the digests actually present -- never claim the image is
        simply absent, and never suggest a re-tag or implicit pull."""

        other_digest = "sha256:" + "b" * 64
        fake = _FakeDockerProcess(
            image_present=False,
            locally_present_digests=[f"python@{other_digest}"],
        )
        backend = DockerExecutionBackend(_config())
        with _patch_subprocess_run(fake):
            with self.assertRaisesRegex(
                SandboxUnavailableError,
                "does not match any locally present digest",
            ) as caught:
                backend.prepare(self.project_root, "TASK-FAIL0007", 1)
        self.assertIn(other_digest, str(caught.exception))
        self._assert_never_ran_docker_run(fake)
        self.assertFalse(any(call[1] == "pull" for call in fake.calls))
        self.assertFalse(any(call[1] == "tag" for call in fake.calls))

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

    def test_timeout_triggers_ownership_verified_kill_and_remove(self) -> None:
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
        self.assertEqual(outcome.backend_metadata["timeout_cleanup"], "removed")
        container_name = outcome.backend_metadata["container_name"]
        self.assertRegex(container_name, r"^apoapsis-verify-timeout01-001-[0-9a-f]{8}$")
        inspect_calls = [call for call in fake.calls if call[1] == "inspect"]
        self.assertTrue(any(container_name in call for call in inspect_calls))
        kill_calls = [call for call in fake.calls if call[1] == "kill"]
        rm_calls = [call for call in fake.calls if call[1] == "rm"]
        self.assertTrue(any(container_name in call for call in kill_calls))
        self.assertTrue(any(container_name in call for call in rm_calls))

    def test_ownership_mismatch_leaves_the_container_untouched(self) -> None:
        """A container reporting a different apoapsis.run_id than the one
        this exact invocation created must never be killed or removed --
        this is the "never touch an unrelated container" guarantee."""

        fake = _FakeDockerProcess(raise_timeout_on_run=True, inspect_reports_wrong_owner=True)
        backend = DockerExecutionBackend(_config())
        command = VerificationCommand(
            name="slow", category="sandbox", argv=["sleep", "999"], timeout_seconds=1
        )
        with _patch_subprocess_run(fake):
            context = backend.prepare(self.project_root, "TASK-TIMEOUT02", 1)
            outcome = backend.run_command(context, command, environment={})
            backend.finalize(context)

        self.assertEqual(outcome.status, VerificationStatus.TIMED_OUT)
        self.assertEqual(
            outcome.backend_metadata["timeout_cleanup"],
            "ownership_unverified_left_running",
        )
        self.assertFalse(any(call[1] == "kill" for call in fake.calls))
        self.assertFalse(any(call[1] == "rm" for call in fake.calls))

    def test_ownership_check_failure_also_leaves_the_container_untouched(self) -> None:
        """If `docker inspect` itself fails (e.g. the container is already
        gone, or the daemon errors), ownership cannot be proven -- cleanup
        must still refuse to kill/remove anything."""

        class _InspectFailsProcess(_FakeDockerProcess):
            def __call__(self, argv, **kwargs):  # type: ignore[override]
                if len(argv) > 1 and argv[1] == "inspect":
                    self.calls.append(list(argv))
                    return subprocess.CompletedProcess(argv, 1, "", "no such container")
                return super().__call__(argv, **kwargs)

        fake = _InspectFailsProcess(raise_timeout_on_run=True)
        backend = DockerExecutionBackend(_config())
        command = VerificationCommand(
            name="slow", category="sandbox", argv=["sleep", "999"], timeout_seconds=1
        )
        with _patch_subprocess_run(fake):
            context = backend.prepare(self.project_root, "TASK-TIMEOUT03", 1)
            outcome = backend.run_command(context, command, environment={})
            backend.finalize(context)

        self.assertEqual(
            outcome.backend_metadata["timeout_cleanup"],
            "ownership_unverified_left_running",
        )
        self.assertFalse(any(call[1] == "kill" for call in fake.calls))
        self.assertFalse(any(call[1] == "rm" for call in fake.calls))


class DockerWorkspaceSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.project_root = root / "project"
        self.project_root.mkdir()
        self.outside_dir = root / "outside"
        self.outside_dir.mkdir()
        (self.outside_dir / "secret.txt").write_text(
            "outside-secret-content\n", encoding="utf-8"
        )
        (self.project_root / "source.py").write_text("x = 1\n", encoding="utf-8")
        (self.project_root / ".git").mkdir()
        (self.project_root / ".git" / "HEAD").write_text(
            "ref: refs/heads/main\n", encoding="utf-8"
        )
        (self.project_root / ".apoapsis").mkdir()
        (self.project_root / ".apoapsis" / "config.toml").write_text(
            "x", encoding="utf-8"
        )
        self.file_symlink_supported = self._try_symlink(
            self.outside_dir / "secret.txt",
            self.project_root / "file_link.py",
            target_is_directory=False,
        )
        self.dir_symlink_supported = self._try_symlink(
            self.outside_dir, self.project_root / "dir_link", target_is_directory=True
        )
        self.junction_supported = self._try_junction(
            self.outside_dir, self.project_root / "junction_link"
        )

    @staticmethod
    def _try_symlink(target: Path, link: Path, *, target_is_directory: bool) -> bool:
        try:
            os.symlink(target, link, target_is_directory=target_is_directory)
            return True
        except (OSError, NotImplementedError):
            return False

    @staticmethod
    def _try_junction(target: Path, link: Path) -> bool:
        if os.name != "nt":
            return False
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and link.exists()

    def _assert_secret_never_copied(self, workspace: Path) -> None:
        copied_names = {path.name for path in workspace.rglob("*")}
        self.assertNotIn("secret.txt", copied_names)
        for path in workspace.rglob("*"):
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="ignore")
                self.assertNotIn("outside-secret-content", content)

    def test_copy_excludes_metadata_and_skips_symlinks(self) -> None:
        fake = _FakeDockerProcess()
        backend = DockerExecutionBackend(_config())
        with _patch_subprocess_run(fake):
            context = backend.prepare(self.project_root, "TASK-COPY0001", 1)
            workspace = context.extra["workspace"]
            self.assertTrue((workspace / "source.py").is_file())
            self.assertFalse((workspace / ".git").exists())
            self.assertFalse((workspace / ".apoapsis").exists())

            expected_skipped = 0
            if self.file_symlink_supported:
                self.assertFalse((workspace / "file_link.py").exists())
                expected_skipped += 1
            if self.dir_symlink_supported:
                self.assertFalse((workspace / "dir_link").exists())
                expected_skipped += 1
            if self.junction_supported:
                self.assertFalse((workspace / "junction_link").exists())
                expected_skipped += 1

            self.assertEqual(context.extra["skipped_reparse_points"], expected_skipped)
            self._assert_secret_never_copied(workspace)
            backend.finalize(context)

        if not (self.file_symlink_supported or self.dir_symlink_supported):
            self.skipTest(
                "neither file nor directory symlinks could be created on this "
                "machine/platform; junction coverage is asserted separately"
            )

    def test_copy_never_traverses_a_windows_junction_pointing_outside_the_repository(
        self,
    ) -> None:
        if not self.junction_supported:
            self.skipTest("could not create a Windows directory junction here")

        fake = _FakeDockerProcess()
        backend = DockerExecutionBackend(_config())
        with _patch_subprocess_run(fake):
            context = backend.prepare(self.project_root, "TASK-COPY0002", 1)
            workspace = context.extra["workspace"]
            # the junction itself must not exist in the copy at all --
            # neither as a traversed directory nor as any kind of entry.
            self.assertFalse((workspace / "junction_link").exists())
            self.assertNotIn("junction_link", {p.name for p in workspace.iterdir()})
            # the defining requirement: the file reachable only through the
            # junction must never have been copied into the workspace.
            self._assert_secret_never_copied(workspace)
            backend.finalize(context)

    def test_finalize_flags_mutation_of_a_pre_existing_file_but_not_new_files(
        self,
    ) -> None:
        fake = _FakeDockerProcess()
        backend = DockerExecutionBackend(_config())
        with _patch_subprocess_run(fake):
            context = backend.prepare(self.project_root, "TASK-COPY0003", 1)
            workspace = context.extra["workspace"]
            # simulate a misbehaving test mutating a pre-existing source file
            (workspace / "source.py").write_text("x = 2\n", encoding="utf-8")
            # simulate a normal, expected new output file
            (workspace / "coverage.out").write_text("ok\n", encoding="utf-8")
            changed = backend.finalize(context)

        self.assertEqual(changed, ["source.py"])


@unittest.skipUnless(
    os.environ.get("APOAPSIS_RUN_LIVE_DOCKER_TESTS") == "1",
    "set APOAPSIS_RUN_LIVE_DOCKER_TESTS=1 to run the real Docker sandbox tests",
)
class DockerLiveIntegrationTest(unittest.TestCase):
    """Live proof against a real Docker engine, gated behind
    `APOAPSIS_RUN_LIVE_DOCKER_TESTS=1` plus a locally-present, digest-pinned
    `APOAPSIS_SANDBOX_TEST_IMAGE`/`APOAPSIS_SANDBOX_TEST_DIGEST`. Never runs
    in the default suite and never pulls an image itself. Each test proves
    exactly one property this milestone claims live, rather than one
    generic smoke test: passing status, network denial, read-only host
    isolation, worktree-copy mutation detection, and timeout-triggered
    container removal (verified independently via `docker ps`, not merely
    by trusting the backend's own report). The network/read-only probes
    use `python3`, matching the `python:3.12-slim` image this project
    documents as the example configuration (HANDOFF.md); if the configured
    test image has no `python3`, those two probes skip themselves rather
    than failing for an unrelated reason."""

    def setUp(self) -> None:
        image = os.environ.get("APOAPSIS_SANDBOX_TEST_IMAGE")
        digest = os.environ.get("APOAPSIS_SANDBOX_TEST_DIGEST")
        if not image or not digest:
            self.skipTest(
                "set APOAPSIS_SANDBOX_TEST_IMAGE and APOAPSIS_SANDBOX_TEST_DIGEST "
                "to a locally-present, digest-pinned image to run this test"
            )
        self.backend = DockerExecutionBackend(
            _config(
                image=image,
                image_digest=digest,
                wall_clock_timeout_seconds=30,
            )
        )
        try:
            self.backend.preflight()
        except SandboxUnavailableError as exc:
            self.skipTest(f"Docker sandbox unavailable: {exc}")
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project_root = Path(self.temporary_directory.name) / "project"
        self.project_root.mkdir()
        (self.project_root / "marker.txt").write_text("hi\n", encoding="utf-8")

    def _run(
        self, name: str, argv: list[str], *, task_id: str, timeout_seconds: int = 30
    ):
        command = VerificationCommand(
            name=name, category="sandbox", argv=argv, timeout_seconds=timeout_seconds
        )
        context = self.backend.prepare(self.project_root, task_id, 1)
        try:
            outcome = self.backend.run_command(context, command, environment={})
        finally:
            changed = self.backend.finalize(context)
        return outcome, changed

    def _skip_if_python_missing(self, outcome) -> None:
        # exit 126/127 conventionally mean "found but not executable" /
        # "command not found" -- the configured test image simply has no
        # python3, not a sandbox-behavior finding either way.
        if outcome.exit_code in (126, 127):
            self.skipTest(
                f"configured test image has no python3 (exit_code={outcome.exit_code})"
            )

    def test_trivial_command_passes_and_reports_sandboxed(self) -> None:
        outcome, changed = self._run(
            "live-probe-trivial", ["true"], task_id="TASK-LIVE0001"
        )
        self.assertEqual(outcome.status, VerificationStatus.PASSED)
        self.assertTrue(outcome.backend_metadata.get("sandboxed"))
        self.assertEqual(changed, [])

    def test_network_access_is_denied(self) -> None:
        probe = (
            "import socket,sys\n"
            "s = socket.socket()\n"
            "s.settimeout(3)\n"
            "try:\n"
            "    s.connect(('1.1.1.1', 80))\n"
            "except OSError:\n"
            "    sys.exit(1)\n"
            "else:\n"
            "    sys.exit(0)\n"
        )
        outcome, _changed = self._run(
            "live-probe-network", ["python3", "-c", probe], task_id="TASK-LIVE0002"
        )
        self._skip_if_python_missing(outcome)
        self.assertNotEqual(
            outcome.exit_code,
            0,
            "network connect() succeeded inside the sandbox -- --network none "
            "is not actually denying network access on this engine",
        )

    def test_root_filesystem_outside_workspace_is_read_only(self) -> None:
        probe = "open('/apoapsis-readonly-probe', 'w').write('x')\n"
        outcome, _changed = self._run(
            "live-probe-readonly", ["python3", "-c", probe], task_id="TASK-LIVE0003"
        )
        self._skip_if_python_missing(outcome)
        self.assertNotEqual(
            outcome.exit_code,
            0,
            "write outside /workspace succeeded -- --read-only is not actually "
            "isolating host state on this engine",
        )

    def test_worktree_copy_mutation_is_detected_by_finalize(self) -> None:
        probe = "open('/workspace/marker.txt', 'w').write('mutated-from-container')\n"
        outcome, changed = self._run(
            "live-probe-mutation", ["python3", "-c", probe], task_id="TASK-LIVE0004"
        )
        self._skip_if_python_missing(outcome)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(
            changed,
            ["marker.txt"],
            "a real in-container mutation of a pre-existing file was not "
            "detected by finalize()'s integrity check",
        )

    def test_timeout_triggers_verified_removal_confirmed_via_docker_ps(self) -> None:
        command = VerificationCommand(
            name="live-probe-timeout",
            category="sandbox",
            argv=["python3", "-c", "import time; time.sleep(999)"],
            timeout_seconds=2,
        )
        context = self.backend.prepare(self.project_root, "TASK-LIVE0005", 1)
        try:
            outcome = self.backend.run_command(context, command, environment={})
        finally:
            self.backend.finalize(context)

        self._skip_if_python_missing(outcome)
        self.assertEqual(outcome.status, VerificationStatus.TIMED_OUT)
        self.assertEqual(outcome.backend_metadata.get("timeout_cleanup"), "removed")
        container_name = outcome.backend_metadata["container_name"]
        # Independent proof, not just trusting the backend's own report:
        # ask the real Docker engine directly whether the container still
        # exists at all (running or stopped).
        listed = subprocess.run(
            [
                self.backend.config.docker_executable,
                "ps",
                "-a",
                "--filter",
                f"name={container_name}",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            listed.stdout.strip(),
            "",
            f"container {container_name!r} still exists on the host after "
            "timeout cleanup claimed removal",
        )


if __name__ == "__main__":
    unittest.main()
