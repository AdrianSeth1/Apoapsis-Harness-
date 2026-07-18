from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from apoapsis.execution.backend import (
    ExecutionBackendConfig,
    ExecutionBackendName,
)
from apoapsis.execution.docker_backend import DockerExecutionBackend
from apoapsis.execution.host_backend import HostExecutionBackend
from apoapsis.verification.results import VerificationStatus
from apoapsis.verification.runner import (
    VerificationCommand,
    VerificationConfig,
    VerificationRunner,
    build_execution_backend,
)


class ExecutionBackendConfigTests(unittest.TestCase):
    def test_default_backend_is_host(self) -> None:
        config = ExecutionBackendConfig()
        self.assertEqual(config.backend, ExecutionBackendName.HOST)
        self.assertIsNone(config.docker)

    def test_docker_backend_requires_docker_config(self) -> None:
        with self.assertRaises(ValueError):
            ExecutionBackendConfig(backend=ExecutionBackendName.DOCKER)

    def test_build_execution_backend_dispatches(self) -> None:
        host = build_execution_backend(ExecutionBackendConfig())
        self.assertIsInstance(host, HostExecutionBackend)

        from apoapsis.execution.backend import DockerBackendConfig

        docker_config = ExecutionBackendConfig(
            backend=ExecutionBackendName.DOCKER,
            docker=DockerBackendConfig(
                image="python:3.12-slim",
                image_digest="sha256:" + "0" * 64,
            ),
        )
        docker = build_execution_backend(docker_config)
        self.assertIsInstance(docker, DockerExecutionBackend)


class HostBackendRegressionTests(unittest.TestCase):
    """Every scenario already covered in test_verification.py, rerun through
    the new ExecutionBackend seam, to prove the refactor changed nothing
    about host-backend behavior."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_pass_and_failure_are_captured_without_shell(self) -> None:
        config = VerificationConfig(
            commands=[
                VerificationCommand(
                    name="pass",
                    category="tests",
                    argv=[sys.executable, "-c", "print('passed')"],
                ),
                VerificationCommand(
                    name="fail",
                    category="tests",
                    argv=[
                        sys.executable,
                        "-c",
                        "import sys; print('broken', file=sys.stderr); sys.exit(7)",
                    ],
                ),
            ]
        )

        result = VerificationRunner(config).run("TASK-VERIFY-EB1", self.root)

        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertEqual(result.commands[0].status, VerificationStatus.PASSED)
        self.assertIn("passed", result.commands[0].stdout)
        self.assertEqual(result.commands[1].exit_code, 7)
        self.assertIn("broken", result.commands[1].stderr)
        self.assertEqual(result.integrity_violations, [])
        for command_result in result.commands:
            self.assertEqual(command_result.backend, "host")
            self.assertEqual(command_result.backend_metadata, {"sandboxed": False})

    def test_timeout_and_stop_on_failure_skip_remaining_checks(self) -> None:
        config = VerificationConfig(
            stop_on_failure=True,
            commands=[
                VerificationCommand(
                    name="slow",
                    category="tests",
                    argv=[sys.executable, "-c", "import time; time.sleep(2)"],
                    timeout_seconds=0.05,
                ),
                VerificationCommand(
                    name="never-run",
                    category="build",
                    argv=[sys.executable, "-c", "raise SystemExit(99)"],
                ),
            ],
        )

        result = VerificationRunner(config).run("TASK-VERIFY-EB2", self.root)

        self.assertEqual(result.commands[0].status, VerificationStatus.TIMED_OUT)
        self.assertEqual(result.commands[1].status, VerificationStatus.SKIPPED)
        self.assertEqual(result.commands[1].backend, "host")
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_output_is_bounded(self) -> None:
        config = VerificationConfig(
            output_limit_chars=1_000,
            commands=[
                VerificationCommand(
                    name="verbose",
                    category="tests",
                    argv=[sys.executable, "-c", "print('x' * 5000)"],
                )
            ],
        )

        result = VerificationRunner(config).run("TASK-VERIFY-EB3", self.root)

        self.assertTrue(result.commands[0].output_truncated)
        self.assertLessEqual(len(result.commands[0].stdout), 1_000)

    def test_toml_configuration_loads_default_host_backend(self) -> None:
        config_path = self.root / "config.toml"
        config_path.write_text(
            """
[verification]
stop_on_failure = true
output_limit_chars = 2000
environment_allowlist = ["PATH"]

[[verification.commands]]
name = "tests"
category = "unit"
argv = ["python", "-m", "unittest"]
timeout_seconds = 30
required = true
""".strip(),
            encoding="utf-8",
        )

        config = VerificationConfig.from_toml(config_path)

        self.assertTrue(config.stop_on_failure)
        self.assertEqual(config.backend.backend, ExecutionBackendName.HOST)

    def test_attempt_parameter_does_not_affect_host_backend(self) -> None:
        config = VerificationConfig(
            commands=[
                VerificationCommand(
                    name="pass",
                    category="tests",
                    argv=[sys.executable, "-c", "print('ok')"],
                )
            ]
        )
        result = VerificationRunner(config).run(
            "TASK-VERIFY-EB4", self.root, attempt=7
        )
        self.assertEqual(result.status, VerificationStatus.PASSED)


if __name__ == "__main__":
    unittest.main()
