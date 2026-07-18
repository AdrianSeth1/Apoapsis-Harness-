from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from apoapsis.verification.failures import FailureNormalizer
from apoapsis.verification.results import VerificationStatus
from apoapsis.verification.runner import (
    VerificationCommand,
    VerificationConfig,
    VerificationRunner,
)


class VerificationRunnerTests(unittest.TestCase):
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

        result = VerificationRunner(config).run("TASK-VERIFY-1", self.root)

        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertEqual(result.commands[0].status, VerificationStatus.PASSED)
        self.assertIn("passed", result.commands[0].stdout)
        self.assertEqual(result.commands[1].exit_code, 7)
        self.assertIn("broken", result.commands[1].stderr)

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

        result = VerificationRunner(config).run("TASK-VERIFY-2", self.root)

        self.assertEqual(result.commands[0].status, VerificationStatus.TIMED_OUT)
        self.assertEqual(result.commands[1].status, VerificationStatus.SKIPPED)
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

        result = VerificationRunner(config).run("TASK-VERIFY-3", self.root)

        self.assertTrue(result.commands[0].output_truncated)
        self.assertLessEqual(len(result.commands[0].stdout), 1_000)

    def test_toml_configuration_loads(self) -> None:
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
        self.assertEqual(config.commands[0].argv[-1], "unittest")

    def test_failure_normalizer_extracts_only_worktree_locations(self) -> None:
        source = self.root / "src" / "broken.py"
        source.parent.mkdir()
        source.write_text(
            "def fail():\n"
            "    value = 1\n"
            "    raise RuntimeError('boom')\n\n"
            "fail()\n",
            encoding="utf-8",
        )
        result = VerificationRunner(
            VerificationConfig(
                commands=[
                    VerificationCommand(
                        name="failure-location",
                        category="tests",
                        argv=[sys.executable, str(source)],
                    )
                ]
            )
        ).run("TASK-VERIFY-LOCATION", self.root)

        _command, failure = FailureNormalizer().extract(result, self.root)

        self.assertIn(("src/broken.py", 3), {(item.path, item.line) for item in failure.locations})
        outside = self.root.parent / "outside.py"
        self.assertEqual(
            FailureNormalizer._locations(
                f'File "{outside}", line 9, in outside', self.root
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
