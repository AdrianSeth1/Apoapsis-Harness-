"""Slice 5A minimal profile: diagnostics are advisory, structurally.

The tests that carry weight here are the ones asserting what diagnostics
*cannot* do. A diagnostics subsystem that works correctly and is also allowed to
end a session would be a faithful reimplementation of the Crisis Atlas Slice 2
defect with better tooling, so the separation from readiness is tested as a
property of the types and the call signature rather than as a convention.
"""

from __future__ import annotations

import inspect
import unittest

from apoapsis.workcell.acceptance import evaluate_checkpoint
from apoapsis.workcell.diagnostics import (
    Diagnostic,
    DiagnosticReport,
    DiagnosticSeverity,
    DiagnosticStatus,
    not_checked,
    run_syntax_diagnostics,
)
from apoapsis.workcell.runtime_profile import (
    QUALIFIED_PROFILE,
    OptimisationVerdict,
    candidates,
)
from apoapsis.workcell.witness import StructuredWitness

_TRACEBACK = (
    'Traceback (most recent call last):\n'
    '  File "/workspace/app/routes.py", line 12\n'
    '    def handler(\n'
    '               ^\n'
    'SyntaxError: unexpected EOF while parsing\n'
)


def _runner(code, stdout="", stderr=""):
    def run(argv, timeout):
        return code, stdout, stderr

    return run


class AdvisoryByConstructionTests(unittest.TestCase):
    def test_a_report_is_not_a_witness(self) -> None:
        """Different types, so one cannot be passed where the other is expected.

        A witness discharges a contract obligation. If a diagnostic could be
        substituted for one, a clean parse would satisfy an acceptance
        criterion, which is the whole defect.
        """

        report = DiagnosticReport(
            status=DiagnosticStatus.CLEAN, tool_name="python-compile"
        )
        self.assertNotIsInstance(report, StructuredWitness)

    def test_evaluate_checkpoint_cannot_see_diagnostics(self) -> None:
        """The structural guarantee, asserted on the signature.

        Mirrors the existing Slice 4 test that pins `evaluate_checkpoint` to
        admission and readiness only. Adding a diagnostics parameter would make
        this fail, which is the intent.
        """

        parameters = set(inspect.signature(evaluate_checkpoint).parameters)
        self.assertNotIn("diagnostics", parameters)
        self.assertNotIn("diagnostic_report", parameters)

    def test_advisory_cannot_be_switched_off(self) -> None:
        with self.assertRaises(Exception):
            DiagnosticReport(
                status=DiagnosticStatus.CLEAN,
                tool_name="python-compile",
                advisory=False,
            )


class NotCheckedIsNotCleanTests(unittest.TestCase):
    def test_absent_and_failed_are_not_readings(self) -> None:
        for report in (
            not_checked("pyright", "not installed"),
            not_checked("pyright", "crashed", failed=True),
        ):
            with self.subTest(status=report.status):
                self.assertFalse(report.status.is_a_reading)
                self.assertEqual(report.diagnostics, [])

    def test_an_empty_findings_list_does_not_mean_clean(self) -> None:
        """The distinction a boolean would destroy.

        All four statuses have an empty or short findings list. Only CLEAN
        means the tool looked and found nothing.
        """

        absent = not_checked("pyright", "not installed")
        clean = DiagnosticReport(
            status=DiagnosticStatus.CLEAN, tool_name="python-compile"
        )
        self.assertEqual(absent.diagnostics, clean.diagnostics)
        self.assertIsNot(absent.status, clean.status)
        self.assertTrue(clean.status.is_a_reading)

    def test_the_agent_is_told_plainly_that_nothing_was_checked(self) -> None:
        summary = not_checked("pyright", "binary missing").agent_summary()
        self.assertIn("NOT CHECKED", summary)
        self.assertIn("not an all-clear", summary)

    def test_a_clean_summary_still_disclaims_completion(self) -> None:
        summary = DiagnosticReport(
            status=DiagnosticStatus.CLEAN, tool_name="python-compile"
        ).agent_summary()
        self.assertIn("Advisory only", summary)
        self.assertIn("not that the slice is implemented", summary)


class SyntaxDiagnosticsTests(unittest.TestCase):
    def test_a_syntax_error_becomes_a_located_finding(self) -> None:
        report = run_syntax_diagnostics(
            paths=["app/routes.py"], runner=_runner(1, stderr=_TRACEBACK)
        )
        self.assertIs(report.status, DiagnosticStatus.FINDINGS)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0].path, "/workspace/app/routes.py")
        self.assertEqual(report.errors[0].line, 12)
        self.assertIn("SyntaxError", report.errors[0].message)

    def test_a_clean_compile_is_clean(self) -> None:
        report = run_syntax_diagnostics(
            paths=["app/routes.py"], runner=_runner(0), worktree_fingerprint="a" * 64
        )
        self.assertIs(report.status, DiagnosticStatus.CLEAN)
        self.assertEqual(report.worktree_fingerprint, "a" * 64)

    def test_unparseable_failure_output_is_not_clean(self) -> None:
        """The subtle one, and the reason `_parse_py_compile` is tolerant.

        A non-zero exit whose output cannot be parsed yields no findings. If
        that were reported as CLEAN, a broken toolchain would read as a passing
        parse -- the same shape as a green suite over an unexercised file.
        """

        report = run_syntax_diagnostics(
            paths=["app/routes.py"], runner=_runner(2, stderr="segfault")
        )
        self.assertIs(report.status, DiagnosticStatus.TOOL_FAILED)
        self.assertFalse(report.status.is_a_reading)

    def test_a_raising_runner_becomes_tool_failed(self) -> None:
        def boom(argv, timeout):
            raise TimeoutError("no response in 60s")

        report = run_syntax_diagnostics(paths=["a.py"], runner=boom)
        self.assertIs(report.status, DiagnosticStatus.TOOL_FAILED)
        self.assertIn("TimeoutError", report.reason)

    def test_no_python_files_is_clean_with_a_stated_reason(self) -> None:
        report = run_syntax_diagnostics(paths=["README.md"], runner=_runner(99))
        self.assertIs(report.status, DiagnosticStatus.CLEAN)
        self.assertIn("no Python files", report.reason)

    def test_findings_are_bounded_and_the_remainder_counted(self) -> None:
        many = "".join(
            f'  File "/workspace/f{i}.py", line 1\nSyntaxError: bad\n'
            for i in range(70)
        )
        report = run_syntax_diagnostics(paths=["f.py"], runner=_runner(1, stderr=many))
        self.assertEqual(len(report.diagnostics), 50)
        self.assertEqual(report.truncated_count, 20)
        self.assertIn("and 20 more", report.agent_summary())


class RuntimeProfileTests(unittest.TestCase):
    def test_the_profile_matches_the_qualified_run(self) -> None:
        self.assertEqual(QUALIFIED_PROFILE.context_limit_tokens, 65_536)
        self.assertEqual(QUALIFIED_PROFILE.max_output_tokens, 16_384)
        self.assertEqual(QUALIFIED_PROFILE.cli_version, "0.21.1")
        self.assertFalse(QUALIFIED_PROFILE.reasoning_enabled)

    def test_the_trigger_is_the_measured_ladder_not_a_percentage(self) -> None:
        self.assertEqual(QUALIFIED_PROFILE.auto_compact_trigger_tokens, 32_536)
        self.assertNotEqual(
            QUALIFIED_PROFILE.auto_compact_trigger_tokens,
            int(0.85 * QUALIFIED_PROFILE.context_limit_tokens),
        )

    def test_rejections_are_recorded_with_reasons(self) -> None:
        rejected = [
            item
            for item in QUALIFIED_PROFILE.optimisation_decisions
            if item.verdict is OptimisationVerdict.REJECTED_WITHOUT_BENCHMARK
        ]
        self.assertGreaterEqual(len(rejected), 4)
        for item in rejected:
            with self.subTest(name=item.name):
                self.assertTrue(item.rationale.strip())

    def test_only_quality_moving_knobs_are_candidates(self) -> None:
        """Throughput knobs are not candidates; the two quality ones are."""

        self.assertEqual(
            sorted(candidates()),
            ["LSP diagnostics beyond syntax", "reasoning-effort routing"],
        )


if __name__ == "__main__":
    unittest.main()
