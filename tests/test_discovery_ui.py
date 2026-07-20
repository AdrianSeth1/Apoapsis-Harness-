from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from apoapsis.discovery.errors import (
    BriefNotApprovedError,
    ClarificationRoundCeilingExceededError,
    StaleSessionError,
)
from apoapsis.discovery.operation_recovery import recover_stale_discovery_operations
from apoapsis.discovery.operation_schema import DiscoveryOperationAction
from apoapsis.discovery.operation_store import (
    ActiveDiscoveryOperationExistsError,
    DiscoveryOperationStore,
    DuplicateDiscoveryOperationError,
)
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.config import ProviderPricing
from apoapsis.ui.application import ApoapsisUIService
from apoapsis.ui.server import create_ui_server
from tests.fakes import FakeModelProvider


def _questions_json(count: int = 2) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "questions": [
                {"question_id": f"Q-{index}", "text": f"Question {index}?"}
                for index in range(1, count + 1)
            ],
        }
    )


def _brief_json() -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "summary": "Add resumable downloads with a pluggable storage backend.",
            "goals": ["Support resuming interrupted downloads."],
            "non_goals": [],
            "key_constraints": [
                {
                    "id": "HC-1",
                    "text": "Preserve the current public API.",
                    "verbatim_source": "Preserve the current public API.",
                    "interpreted_meaning": "Do not change public signatures.",
                    "source": "user",
                    "source_reference": "idea",
                    "verification_method": "unit-tests",
                }
            ],
            "open_questions": [],
        }
    )


IDEA_TEXT = "Add resumable downloads with a pluggable storage backend. Preserve the current public API."


def _poll_until_terminal(service: ApoapsisUIService, operation_id: str, *, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = service.discovery_operation_status(operation_id)
        if record["status"] in {"succeeded", "failed", "ambiguous"}:
            return record
        time.sleep(0.05)
    raise AssertionError(f"operation {operation_id} did not reach a terminal state")


class DiscoveryUIServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Apoapsis Tests")
        (self.root / "README.md").write_text("hi\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "init")
        (self.root / ".apoapsis").mkdir()
        (self.root / ".apoapsis" / "config.toml").write_text(
            """
[models.frontier]
provider = "openai_compatible"
base_url = "https://provider.invalid/v1"
model = "fake-local-v1"

[verification]
[[verification.commands]]
name = "unit-tests"
category = "tests"
argv = ["python", "-m", "unittest"]
timeout_seconds = 30
""",
            encoding="utf-8",
        )
        from apoapsis.workflow.engine import SQLiteTaskStore

        SQLiteTaskStore(self.root / ".apoapsis" / "apoapsis.db")
        self.service = ApoapsisUIService(self.root)

    def tearDown(self) -> None:
        if getattr(self, "service", None) is not None:
            self.service._discovery_worker = None

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)

    def _start_session(self) -> dict:
        return self.service.start_discovery_session(IDEA_TEXT)

    def _run_local_questions(self, session_id: str, version: int, *, outputs=None):
        outputs = outputs or [_questions_json(2)]
        fake = InstrumentedModelProvider(FakeModelProvider(outputs), ProviderPricing())
        with patch("apoapsis.discovery.service.build_local_provider", return_value=fake):
            op = self.service.submit_discovery_operation(
                session_id, action="local_questions", operation_id=f"DISCOP-Q-{session_id}",
                expected_version=version,
            )
            return _poll_until_terminal(self.service, op["operation_id"])

    def _run_idea_brief(self, session_id: str, version: int, *, outputs=None):
        outputs = outputs or [_brief_json()]
        fake = InstrumentedModelProvider(FakeModelProvider(outputs), ProviderPricing())
        with patch("apoapsis.discovery.service.build_local_provider", return_value=fake):
            op = self.service.submit_discovery_operation(
                session_id, action="idea_brief", operation_id=f"DISCOP-B-{session_id}",
                expected_version=version,
            )
            return _poll_until_terminal(self.service, op["operation_id"])

    def _to_approved_brief(self):
        session = self._start_session()
        session_id = session["session_id"]
        self._run_local_questions(session_id, session["version"])
        detail = self.service.discovery_session_detail(session_id)
        session = detail["session"]
        questions = session["local_questions"]
        answers = [{"question_id": q["question_id"], "text": "Local disk for now."} for q in questions]
        session = self.service.record_discovery_local_answers(
            session_id, answers, expected_version=session["version"]
        )
        self._run_idea_brief(session_id, session["version"])
        session = self.service.discovery_session_detail(session_id)["session"]
        session = self.service.approve_discovery_idea_brief(
            session_id, expected_version=session["version"]
        )
        return session

    def test_start_and_inspect_session(self) -> None:
        session = self._start_session()
        self.assertEqual(session["status"], "idea_entered")
        detail = self.service.discovery_session_detail(session["session_id"])
        self.assertEqual(detail["session"]["idea_text"], IDEA_TEXT)
        self.assertEqual(detail["max_clarification_questions"], 5)

    def test_local_questions_operation_completes_via_worker(self) -> None:
        session = self._start_session()
        record = self._run_local_questions(session["session_id"], session["version"])
        self.assertEqual(record["status"], "succeeded")
        detail = self.service.discovery_session_detail(session["session_id"])
        self.assertEqual(len(detail["session"]["local_questions"]), 2)

    def test_question_count_capped_regardless_of_model_output(self) -> None:
        session = self._start_session()
        self._run_local_questions(session["session_id"], session["version"], outputs=[_questions_json(9)])
        detail = self.service.discovery_session_detail(session["session_id"])
        self.assertEqual(len(detail["session"]["local_questions"]), 5)

    def test_export_before_brief_approval_rejected(self) -> None:
        session = self._start_session()
        with self.assertRaises(BriefNotApprovedError):
            self.service.export_discovery_frontier_package(
                session["session_id"], transport="manual", expected_version=session["version"]
            )

    def test_full_manual_flow_reaches_plan_imported(self) -> None:
        session = self._to_approved_brief()
        exported = self.service.export_discovery_frontier_package(
            session["session_id"], transport="manual", expected_version=session["version"]
        )
        self.assertTrue(Path(exported["markdown_artifact_absolute_path"]).is_file())
        self.assertIn("FRONTIER-PLANNING-HANDOFF", exported["markdown_artifact_absolute_path"])
        package = exported["package"]

        from tests.architect_helpers import make_plan

        plan = make_plan()
        envelope = json.dumps(
            {
                "schema_version": "1.0",
                "package_id": package["package_id"],
                "package_sha256": package["package_sha256"],
                "session_id": session["session_id"],
                "kind": "plan",
                "plan": json.loads(plan.model_dump_json()),
            }
        )
        record = self.service.import_discovery_manual_response(
            session["session_id"], package_id=package["package_id"],
            response_text=envelope, declared_model_name="claude-opus-4.6-web",
        )
        self.assertEqual(record["status"], "plan_imported")
        self.assertIsNotNone(record["plan_id"])
        detail = self.service.discovery_session_detail(session["session_id"])
        self.assertIsNotNone(detail["plan_summary"])

    def test_stale_package_response_rejected(self) -> None:
        session = self._to_approved_brief()
        exported = self.service.export_discovery_frontier_package(
            session["session_id"], transport="manual", expected_version=session["version"]
        )
        with self.assertRaises(Exception):
            self.service.import_discovery_manual_response(
                session["session_id"], package_id="FPKG-DOES-NOT-EXIST",
                response_text=json.dumps({
                    "schema_version": "1.0", "package_id": "FPKG-DOES-NOT-EXIST",
                    "package_sha256": "0" * 64, "session_id": session["session_id"],
                    "kind": "clarification_questions",
                    "clarification_questions": [{"question_id": "Q-1", "text": "?"}],
                }),
                declared_model_name="claude-opus-4.6-web",
            )

    def test_clarification_round_ceiling_enforced(self) -> None:
        session = self._to_approved_brief()
        session_id = session["session_id"]
        version = session["version"]
        for round_number in range(1, 3):
            exported = self.service.export_discovery_frontier_package(
                session_id, transport="manual", expected_version=version
            )
            package = exported["package"]
            envelope = json.dumps({
                "schema_version": "1.0", "package_id": package["package_id"],
                "package_sha256": package["package_sha256"], "session_id": session_id,
                "kind": "clarification_questions",
                "clarification_questions": [{"question_id": f"Q-{round_number}", "text": "More?"}],
            })
            if round_number <= 2:
                record = self.service.import_discovery_manual_response(
                    session_id, package_id=package["package_id"], response_text=envelope,
                    declared_model_name="claude-opus-4.6-web",
                )
                self.assertEqual(record["status"], "frontier_clarification_proposed")
                answers = [{"question_id": q["question_id"], "text": "answer"} for q in record["frontier_questions"]]
                record = self.service.record_discovery_frontier_answers(
                    session_id, answers, expected_version=record["version"]
                )
                version = record["version"]
            else:
                with self.assertRaises(ClarificationRoundCeilingExceededError):
                    self.service.import_discovery_manual_response(
                        session_id, package_id=package["package_id"], response_text=envelope,
                        declared_model_name="claude-opus-4.6-web",
                    )

    def test_replayed_operation_id_rejected(self) -> None:
        session = self._start_session()
        self._run_local_questions(session["session_id"], session["version"])
        operation_store = DiscoveryOperationStore(
            self.root / ".apoapsis" / "discovery-operations.db"
        )
        with self.assertRaises(DuplicateDiscoveryOperationError):
            operation_store.create(
                f"DISCOP-Q-{session['session_id']}", session["session_id"],
                DiscoveryOperationAction.LOCAL_QUESTIONS, expected_session_version=1,
            )

    def test_concurrent_active_operation_conflict(self) -> None:
        session = self._start_session()
        operation_store = DiscoveryOperationStore(
            self.root / ".apoapsis" / "discovery-operations.db"
        )
        operation_store.create(
            "DISCOP-BLOCKER", session["session_id"], DiscoveryOperationAction.LOCAL_QUESTIONS,
            expected_session_version=session["version"],
        )
        with self.assertRaises(ActiveDiscoveryOperationExistsError):
            self.service.submit_discovery_operation(
                session["session_id"], action="idea_brief", operation_id="DISCOP-BLOCKED",
                expected_version=session["version"],
            )

    def test_stale_session_version_rejected(self) -> None:
        session = self._start_session()
        with self.assertRaises(StaleSessionError):
            self.service.submit_discovery_operation(
                session["session_id"], action="local_questions", operation_id="DISCOP-STALE",
                expected_version=session["version"] + 1,
            )

    def test_recovery_reclaims_recorded_and_marks_stale_running_ambiguous(self) -> None:
        session = self._start_session()
        operation_store = DiscoveryOperationStore(
            self.root / ".apoapsis" / "discovery-operations.db"
        )
        operation_store.create(
            "DISCOP-STRANDED", session["session_id"], DiscoveryOperationAction.LOCAL_QUESTIONS,
            expected_session_version=session["version"],
        )
        report = recover_stale_discovery_operations(operation_store)
        self.assertIn("DISCOP-STRANDED", report.reclaimed_operation_ids)

        from datetime import timedelta
        from apoapsis.operations.lease import new_owner_id

        operation_store.mark_running(
            "DISCOP-STRANDED", owner_id=new_owner_id(), lease_duration=timedelta(seconds=-1)
        )
        report2 = recover_stale_discovery_operations(operation_store)
        self.assertIn("DISCOP-STRANDED", report2.ambiguous_operation_ids)
        self.assertEqual(operation_store.get("DISCOP-STRANDED").status.value, "ambiguous")


class DiscoveryUIServerTests(DiscoveryUIServiceTests):
    def setUp(self) -> None:
        super().setUp()
        self.token = "deterministic-discovery-session"
        self.server = create_ui_server(self.root, port=0, session_token=self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def origin(self) -> str:
        return self.server.origin

    def request(self, path: str, *, method: str = "GET", payload=None, token=None, origin=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers: dict[str, str] = {}
        if token is not None:
            headers["X-Apoapsis-Session"] = token
        if data is not None:
            headers["Content-Type"] = "application/json"
        if origin is not None:
            headers["Origin"] = origin
        request = urllib.request.Request(f"{self.origin}{path}", data=data, headers=headers, method=method)
        return urllib.request.urlopen(request, timeout=10)

    def test_start_session_requires_session_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as unauthorized:
            self.request("/api/discovery/sessions", method="POST", payload={"idea_text": "x"})
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()

    def test_start_session_rejects_foreign_origin(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as forbidden:
            self.request(
                "/api/discovery/sessions", method="POST", payload={"idea_text": "x"},
                token=self.token, origin="https://evil.example",
            )
        self.assertEqual(forbidden.exception.code, 403)
        forbidden.exception.close()

    def test_http_start_and_reconnect(self) -> None:
        with self.request(
            "/api/discovery/sessions", method="POST", payload={"idea_text": IDEA_TEXT}, token=self.token
        ) as response:
            session = json.load(response)
        with self.request(
            f"/api/discovery/sessions/{session['session_id']}", token=self.token
        ) as response:
            detail = json.load(response)
        self.assertEqual(detail["session"]["idea_text"], IDEA_TEXT)

        fresh_service = ApoapsisUIService(self.root)
        reloaded = fresh_service.discovery_session_detail(session["session_id"])
        self.assertEqual(reloaded["session"]["session_id"], session["session_id"])

    def test_static_asset_bundles_discovery_actions(self) -> None:
        with self.request("/app.js", token=self.token) as response:
            content = response.read().decode("utf-8")
        self.assertIn("discover-start", content)
        self.assertIn("discover-op-submit", content)
        self.assertIn("discover-brief-approve-confirm", content)
        self.assertIn("discover-export-package", content)
        self.assertIn("discover-call-api", content)
        self.assertIn("/api/discovery/sessions", content)


if __name__ == "__main__":
    unittest.main()
