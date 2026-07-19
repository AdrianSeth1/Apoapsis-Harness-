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

    def test_acceptance_flag_is_carried_into_the_command_result(self) -> None:
        config = VerificationConfig(
            commands=[
                VerificationCommand(
                    name="dev",
                    category="tests",
                    argv=[sys.executable, "-c", "pass"],
                ),
                VerificationCommand(
                    name="acc",
                    category="acceptance",
                    argv=[sys.executable, "-c", "pass"],
                    required=False,
                    acceptance=True,
                ),
            ]
        )

        result = VerificationRunner(config).run("TASK-VERIFY-ACC", self.root)

        by_name = {item.name: item for item in result.commands}
        self.assertFalse(by_name["dev"].acceptance)
        self.assertTrue(by_name["acc"].acceptance)

    def test_failing_optional_acceptance_command_does_not_flip_aggregate_status(
        self,
    ) -> None:
        # ADR 0018: an acceptance-designated command that is not required
        # must never become a required development gate -- the aggregate
        # status is computed exactly as before.
        config = VerificationConfig(
            commands=[
                VerificationCommand(
                    name="acc-only",
                    category="acceptance",
                    argv=[sys.executable, "-c", "import sys; sys.exit(1)"],
                    required=False,
                    acceptance=True,
                ),
            ]
        )

        result = VerificationRunner(config).run("TASK-VERIFY-ACC-FAIL", self.root)

        self.assertEqual(result.commands[0].status, VerificationStatus.FAILED)
        self.assertTrue(result.commands[0].acceptance)
        self.assertEqual(result.status, VerificationStatus.PASSED)

    def test_failure_normalizer_selects_a_failing_acceptance_only_command(
        self,
    ) -> None:
        config = VerificationConfig(
            commands=[
                VerificationCommand(
                    name="acc-only",
                    category="acceptance",
                    argv=[
                        sys.executable,
                        "-c",
                        "import sys; print('AssertionError: boom', "
                        "file=sys.stderr); sys.exit(1)",
                    ],
                    required=False,
                    acceptance=True,
                ),
            ]
        )
        result = VerificationRunner(config).run("TASK-VERIFY-ACC-EVIDENCE", self.root)
        self.assertEqual(result.status, VerificationStatus.PASSED)

        failed, failure = FailureNormalizer().extract(result, self.root)

        self.assertEqual(failed.name, "acc-only")
        self.assertIn("AssertionError", failure.relevant_error)

    def test_failure_normalizer_selects_a_timed_out_acceptance_command(self) -> None:
        config = VerificationConfig(
            commands=[
                VerificationCommand(
                    name="slow-acceptance",
                    category="acceptance",
                    argv=[sys.executable, "-c", "import time; time.sleep(2)"],
                    timeout_seconds=0.05,
                    required=False,
                    acceptance=True,
                ),
            ]
        )
        result = VerificationRunner(config).run("TASK-VERIFY-ACC-TIMEOUT", self.root)
        self.assertEqual(result.commands[0].status, VerificationStatus.TIMED_OUT)
        self.assertEqual(result.status, VerificationStatus.PASSED)

        failed, _failure = FailureNormalizer().extract(result, self.root)

        self.assertEqual(failed.name, "slow-acceptance")

    def test_failure_normalizer_still_raises_when_only_a_dev_only_optional_command_fails(
        self,
    ) -> None:
        # A plain optional command (neither required nor acceptance) stays
        # exactly as before: its failure produces no normalized-failure
        # evidence -- only `required` or `acceptance` commands ever do.
        config = VerificationConfig(
            commands=[
                VerificationCommand(
                    name="dev-only",
                    category="tests",
                    argv=[sys.executable, "-c", "import sys; sys.exit(1)"],
                    required=False,
                    acceptance=False,
                ),
            ]
        )
        result = VerificationRunner(config).run("TASK-VERIFY-NOOP-FAIL", self.root)
        self.assertEqual(result.status, VerificationStatus.PASSED)

        with self.assertRaisesRegex(
            ValueError, "no failed required or"
        ):
            FailureNormalizer().extract(result, self.root)

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
