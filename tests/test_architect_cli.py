from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from apoapsis.architect.errors import PlanImportError
from apoapsis.architect.importer import import_planner_response
from apoapsis.architect.package import build_planner_request_package
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.cli.app import main
from apoapsis.config import ApoapsisConfig
from tests.architect_helpers import make_plan


class ArchitectCLITests(unittest.TestCase):
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
        self.invoke("init")

    def invoke(self, *arguments: str) -> dict[str, object]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["--project-root", str(self.root), *arguments])
        return json.loads(output.getvalue())

    def _envelope(self, package: dict[str, object]) -> dict[str, object]:
        return {
            "package_id": package["package_id"],
            "request_package_sha256": package["package_sha256"],
            "plan": make_plan().model_dump(mode="json"),
        }

    def test_export_writes_immutable_package_before_returning(self) -> None:
        exported = self.invoke("plan", "export", "Add resumable downloads.")
        artifact_path = self.root / exported["artifact_path"]
        self.assertTrue(artifact_path.is_file())
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["package_id"], exported["package"]["package_id"])

    def test_full_lifecycle_export_import_validate_approve_inspect(self) -> None:
        exported = self.invoke("plan", "export", "Add resumable downloads.")
        response_path = self.root / "response.json"
        response_path.write_text(
            json.dumps(self._envelope(exported["package"])), encoding="utf-8"
        )

        imported = self.invoke("plan", "import", str(response_path))
        self.assertEqual(imported["status"], "proposed")
        plan_id = imported["plan_id"]

        validated = self.invoke("plan", "validate", plan_id)
        self.assertTrue(validated["validation"]["valid"])
        self.assertEqual(validated["plan"]["status"], "validated")

        approved = self.invoke(
            "plan",
            "approve",
            plan_id,
            "--expected-version",
            str(validated["plan"]["version"]),
        )
        self.assertEqual(approved["status"], "approved")

        inspected = self.invoke("plan", "inspect", plan_id)
        self.assertEqual(
            [event["event_type"] for event in inspected["events"]],
            ["plan_imported", "plan_validated", "plan_approved"],
        )
        self.assertTrue(
            any("approval-event.json" in path for path in inspected["artifacts"])
        )

    def test_approve_with_stale_version_is_rejected(self) -> None:
        exported = self.invoke("plan", "export", "Add resumable downloads.")
        response_path = self.root / "response.json"
        response_path.write_text(
            json.dumps(self._envelope(exported["package"])), encoding="utf-8"
        )
        imported = self.invoke("plan", "import", str(response_path))
        validated = self.invoke("plan", "validate", imported["plan_id"])
        stale_version = imported["version"]

        from apoapsis.architect.errors import ConcurrentPlanTransitionError

        plan_store = SQLitePlanStore(self.root / ".apoapsis" / "architect-plans.db")
        with self.assertRaises(ConcurrentPlanTransitionError):
            plan_store.approve_plan(
                imported["plan_id"], expected_version=stale_version
            )
        self.assertEqual(validated["plan"]["status"], "validated")


class ImportPlannerResponseTests(unittest.TestCase):
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
        (self.root / ".apoapsis").mkdir()
        from apoapsis.cli.app import DEFAULT_CONFIG

        (self.root / ".apoapsis" / "config.toml").write_text(
            DEFAULT_CONFIG, encoding="utf-8"
        )
        self.config = ApoapsisConfig.from_toml(self.root / ".apoapsis" / "config.toml")
        self.plan_store = SQLitePlanStore(self.root / ".apoapsis" / "architect-plans.db")

    def test_response_package_hash_mismatch_is_rejected(self) -> None:
        package = build_planner_request_package(
            self.root, "Add resumable downloads.", self.config
        )
        from apoapsis.architect.audit import write_package_artifact

        write_package_artifact(self.root, package)
        envelope = {
            "package_id": package.package_id,
            "request_package_sha256": "0" * 64,
            "plan": make_plan().model_dump(mode="json"),
        }
        with self.assertRaises(PlanImportError):
            import_planner_response(self.root, self.plan_store, envelope)

    def test_import_without_a_prior_export_is_rejected(self) -> None:
        envelope = {
            "package_id": "PKG-DOES-NOT-EXIST",
            "request_package_sha256": "0" * 64,
            "plan": make_plan().model_dump(mode="json"),
        }
        with self.assertRaises(PlanImportError):
            import_planner_response(self.root, self.plan_store, envelope)

    def test_successful_import_preserves_verbatim_idea_text(self) -> None:
        idea = "  Add resumable downloads with surrounding whitespace.  "
        package = build_planner_request_package(self.root, idea, self.config)
        from apoapsis.architect.audit import write_package_artifact

        write_package_artifact(self.root, package)
        envelope = {
            "package_id": package.package_id,
            "request_package_sha256": package.package_sha256,
            "plan": make_plan(
            ).model_copy(update={"idea_text": idea}).model_dump(mode="json"),
        }
        record = import_planner_response(self.root, self.plan_store, envelope)
        self.assertEqual(record.idea_text, idea)


if __name__ == "__main__":
    unittest.main()
