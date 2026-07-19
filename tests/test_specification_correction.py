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


def _inject_task_id_into_every_json_response(fake: FakeModelProvider) -> None:
    """Both the original and the one bounded correction prompt (ADR 0018)
    embed the real, freshly generated task_id; a raw diff response has no
    such field and is left untouched."""

    original_complete = fake.complete

    def complete(invocation):
        output = original_complete(invocation)
        if 'task_id to "' not in invocation.prompt:
            return output
        task_id = invocation.prompt.split('task_id to "', 1)[1].split('"', 1)[0]
        try:
            raw = json.loads(output.content)
        except json.JSONDecodeError:
            return output
        if isinstance(raw, dict) and "task_id" in raw:
            raw["task_id"] = task_id
            return output.model_copy(update={"content": json.dumps(raw)})
        return output

    fake.complete = complete  # type: ignore[method-assign]


def _invalid_specification_null_hard_constraint_method() -> str:
    payload = json.loads(specification_response())
    payload["hard_constraints"][0]["verification_method"] = None
    return json.dumps(payload)


def _invalid_specification_reworded_verbatim_source() -> str:
    payload = json.loads(specification_response())
    payload["hard_constraints"][0]["verbatim_source"] = "Preserve the public API."
    return json.dumps(payload)


def _invalid_specification_unknown_catalog_mapping() -> str:
    payload = json.loads(specification_response())
    payload["acceptance_criteria"][0]["verification_method"] = "made-up-command"
    return json.dumps(payload)


class SpecificationCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "download-service"
        example = (
            Path(__file__).resolve().parents[1]
            / "examples"
            / "download-service"
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
            ["git", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )

    def _config(self, *, acceptance_command_names: frozenset[str] = frozenset()) -> ApoapsisConfig:
        return ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1",
                    model="fake-coder-v1",
                ),
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
                        acceptance="download-tests" in acceptance_command_names,
                    )
                ]
            ),
        )

    def _audit_dir(self, task_id: str) -> Path:
        return self.root / ".apoapsis" / "tasks" / task_id

    def test_successful_correction_completes_the_task(self) -> None:
        fake = FakeModelProvider(
            [
                _invalid_specification_null_hard_constraint_method(),
                specification_response(),
                COMPLETE_PATCH,
            ]
        )
        _inject_task_id_into_every_json_response(fake)
        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(fake),
            self._config(),
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(report.number_of_calls, 3)
        self.assertEqual(len(fake.invocations), 3)

        audit = self._audit_dir(report.task_id)
        failure = json.loads(
            (audit / "specification-extraction-failure-001.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("verification_method", failure["error"])
        self.assertIn("hard_constraints", failure["raw_response"])

        # both calls got their own complete, immutable audit record.
        for call_number in (1, 2):
            self.assertTrue(
                (audit / f"call-{call_number:03d}-request.json").is_file()
            )
            self.assertTrue(
                (audit / f"call-{call_number:03d}-response.json").is_file()
            )
            self.assertTrue(
                (audit / f"call-{call_number:03d}-telemetry.json").is_file()
            )
            self.assertTrue(
                (audit / f"call-{call_number:03d}-context.json").is_file()
            )

        correction_request = json.loads(
            (audit / "call-002-request.json").read_text(encoding="utf-8")
        )
        correction_prompt = correction_request["prompt"]
        self.assertIn("VALIDATION_ERRORS", correction_prompt)
        self.assertIn("YOUR_PREVIOUS_RESPONSE_START", correction_prompt)
        self.assertIn("verification_method", correction_prompt)

        approved = json.loads(
            (audit / "approved-specification.json").read_text(encoding="utf-8")
        )
        self.assertIsNotNone(approved["hard_constraints"][0]["verification_method"])

        # exactly two provider calls' telemetry, both real, successful calls.
        self.assertEqual(len(report.provider_calls), 3)
        self.assertTrue(all(call.succeeded for call in report.provider_calls))

    def test_repeated_failure_stops_deterministically_at_failed(self) -> None:
        fake = FakeModelProvider(
            [
                _invalid_specification_null_hard_constraint_method(),
                _invalid_specification_null_hard_constraint_method(),
                COMPLETE_PATCH,  # must never be consumed -- proves the retry ceiling
            ]
        )
        _inject_task_id_into_every_json_response(fake)
        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(fake),
            self._config(),
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.FAILED)
        self.assertIn("verification_method", report.error or "")
        # never a third, second-correction attempt.
        self.assertEqual(len(fake.invocations), 2)

    def test_correction_still_enforces_exact_verbatim_constraint_preservation(
        self,
    ) -> None:
        fake = FakeModelProvider(
            [
                _invalid_specification_null_hard_constraint_method(),
                _invalid_specification_reworded_verbatim_source(),
            ]
        )
        _inject_task_id_into_every_json_response(fake)
        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(fake),
            self._config(),
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.FAILED)
        self.assertIn("exact substring", report.error or "")
        self.assertEqual(len(fake.invocations), 2)

    def test_correction_still_enforces_acceptance_catalog_validation(self) -> None:
        fake = FakeModelProvider(
            [
                _invalid_specification_null_hard_constraint_method(),
                _invalid_specification_unknown_catalog_mapping(),
            ]
        )
        _inject_task_id_into_every_json_response(fake)
        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(fake),
            self._config(acceptance_command_names=frozenset({"download-tests"})),
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.FAILED)
        self.assertIn("acceptance-command", report.error or "")
        self.assertEqual(len(fake.invocations), 2)


if __name__ == "__main__":
    unittest.main()
