from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apoapsis.cli.app import main
from apoapsis.intake.execution import prepare_intake_operation
from apoapsis.intake.store import IntakeOperationStore
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.workflow.engine import SQLiteTaskStore
from tests.fakes import FakeModelProvider
from tests.test_specification_correction import (
    _inject_task_id_into_every_json_response,
)
from tests.test_vertical_slice import REQUEST, specification_response


class IntakeCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "a@a.com"], cwd=self.root, check=True
        )
        subprocess.run(["git", "config", "user.name", "a"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("hi\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "."], cwd=self.root, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )

    def invoke(self, *arguments: str) -> dict[str, object]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["--project-root", str(self.root), *arguments])
        return json.loads(output.getvalue())

    def test_intake_recover_is_empty_on_a_fresh_project(self) -> None:
        self.invoke("init")
        result = self.invoke("intake", "recover")
        self.assertEqual(
            result,
            {
                "reclaimed_operation_ids": [],
                "ambiguous_operation_ids": [],
                "tasks_returned_to_review": [],
            },
        )

    def test_intake_inspect_unknown_operation_errors(self) -> None:
        self.invoke("init")
        with self.assertRaises(SystemExit):
            self.invoke("intake", "inspect", "INOP-DOES-NOT-EXIST")

    def test_intake_submit_requires_operation_id_flag(self) -> None:
        from apoapsis.cli.app import build_parser

        parsed = build_parser().parse_args(
            ["intake", "submit", "add a feature", "--operation-id", "INOP-1"]
        )
        self.assertEqual(parsed.intake_command, "submit")
        self.assertEqual(parsed.request_text, "add a feature")
        self.assertEqual(parsed.operation_id, "INOP-1")

    def test_intake_recover_without_resume_flag_only_reports(self) -> None:
        self.invoke("init")
        store = SQLiteTaskStore(self.root / ".apoapsis" / "apoapsis.db")
        operation_store = IntakeOperationStore(
            self.root / ".apoapsis" / "intake-operations.db"
        )
        prepare_intake_operation(
            self.root,
            store,
            operation_store,
            request_text=REQUEST,
            operation_id="INOP-STRANDED",
        )
        result = self.invoke("intake", "recover")
        self.assertEqual(result["reclaimed_operation_ids"], ["INOP-STRANDED"])
        self.assertNotIn("resumed", result)
        # Report-only: the operation must still be sitting exactly where it
        # was, untouched, since no model call was authorized.
        self.assertEqual(
            operation_store.get("INOP-STRANDED").status.value, "recorded"
        )

    def test_intake_recover_with_resume_flag_actually_runs_the_operation(self) -> None:
        self.invoke("init")
        store = SQLiteTaskStore(self.root / ".apoapsis" / "apoapsis.db")
        operation_store = IntakeOperationStore(
            self.root / ".apoapsis" / "intake-operations.db"
        )
        prepare_intake_operation(
            self.root,
            store,
            operation_store,
            request_text=REQUEST,
            operation_id="INOP-STRANDED-2",
        )
        fake = FakeModelProvider([specification_response()])
        _inject_task_id_into_every_json_response(fake)
        fake_provider = InstrumentedModelProvider(fake)
        with patch(
            "apoapsis.intake.execution._build_provider", return_value=fake_provider
        ):
            result = self.invoke("intake", "recover", "--resume-recorded")
        self.assertEqual(result["reclaimed_operation_ids"], ["INOP-STRANDED-2"])
        self.assertEqual(len(result["resumed"]), 1)
        self.assertEqual(
            result["resumed"][0]["status"], "pending_specification_approval"
        )
        self.assertEqual(
            operation_store.get("INOP-STRANDED-2").status.value,
            "pending_specification_approval",
        )

    def test_existing_run_and_task_commands_are_unaffected(self) -> None:
        self.invoke("init")
        result = self.invoke(
            "task",
            "Add a small feature.",
            "--constraint",
            "Keep it small.",
        )
        self.assertEqual(result["state"], "SPEC_DRAFTED")


if __name__ == "__main__":
    unittest.main()
