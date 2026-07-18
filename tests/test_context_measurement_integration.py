from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from apoapsis.config import (
    ContextCompilerConfig,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ApoapsisConfig,
)
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.reporting.report import TaskOutcome
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.vertical_slice import VerticalSliceRunner
from tests.fakes import FakeModelProvider
from tests.test_vertical_slice import COMPLETE_PATCH, REQUEST, specification_response


class ContextMeasurementIntegrationTests(unittest.TestCase):
    """Proves ContextMeasurement is actually wired end to end through
    VerticalSliceRunner, the audit store, and FinalTaskReport -- with no
    live model dependency."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "download-service"
        example = (
            Path(__file__).resolve().parents[1] / "examples" / "download-service"
        )
        shutil.copytree(example, self.root)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Apoapsis Tests")
        self._git("add", ".")
        self._git("commit", "-m", "controlled baseline")
        (self.root / ".apoapsis").mkdir()
        self.store = SQLiteTaskStore(self.root / ".apoapsis" / "apoapsis.db")

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )

    def _config(self) -> ApoapsisConfig:
        return ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1",
                    model="fake-coder-v1",
                    context_window_tokens=65536,
                )
            ),
            context=ContextCompilerConfig(
                max_files=10, max_excerpt_lines=200, max_total_chars=50_000
            ),
            patch=PatchPolicyConfig(max_changed_lines=100),
            verification=VerificationConfig(
                commands=[
                    VerificationCommand(
                        name="download-tests",
                        category="tests",
                        argv=[
                            "python",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "tests",
                            "-v",
                        ],
                        timeout_seconds=30,
                    )
                ]
            ),
        )

    @staticmethod
    def _inject_task_id(fake: FakeModelProvider) -> None:
        original_complete = fake.complete

        def complete(invocation):
            output = original_complete(invocation)
            if 'task_id to "' in invocation.prompt:
                task_id = invocation.prompt.split('task_id to "', 1)[1].split('"', 1)[0]
                raw = json.loads(output.content)
                raw["task_id"] = task_id
                return output.model_copy(update={"content": json.dumps(raw)})
            return output

        fake.complete = complete  # type: ignore[method-assign]

    def test_one_shot_report_carries_one_measurement_per_call(self) -> None:
        fake = FakeModelProvider([specification_response(), COMPLETE_PATCH])
        self._inject_task_id(fake)

        report = VerticalSliceRunner(
            self.root, self.store, InstrumentedModelProvider(fake), self._config()
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(len(report.context_measurements), report.number_of_calls)

        spec_measurement, patch_measurement = report.context_measurements
        # the specification-drafting call has no repository evidence at all.
        self.assertEqual(spec_measurement.files_included, 0)
        self.assertEqual(spec_measurement.new_evidence_count, 0)
        # the implementation call's context is real, non-trivial repository
        # evidence, measured against a real configured model window.
        self.assertGreater(patch_measurement.files_included, 0)
        self.assertEqual(patch_measurement.model_context_window_tokens, 65536)
        self.assertIsNotNone(patch_measurement.model_window_utilization)
        self.assertGreater(patch_measurement.new_evidence_count, 0)

        audit = self.root / ".apoapsis" / "tasks" / report.task_id
        self.assertTrue((audit / "call-001-context-measurement.json").is_file())
        self.assertTrue((audit / "call-002-context-measurement.json").is_file())
        on_disk = json.loads(
            (audit / "call-002-context-measurement.json").read_text(encoding="utf-8")
        )
        self.assertEqual(on_disk["call_number"], 2)

        # report.json itself round-trips the measurements too.
        report_on_disk = json.loads((audit / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report_on_disk["context_measurements"]), 2)
        self.assertIsNotNone(report.context_attribution)
        self.assertTrue(report.context_attribution.accepted_patch)
        self.assertIsNotNone(report.context_attribution.signal_density_ratio)
        self.assertTrue((audit / "context-attribution.json").is_file())


if __name__ == "__main__":
    unittest.main()
