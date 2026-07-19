from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

from apoapsis.config import (
    AgentLoopConfig,
    ContextCompilerConfig,
    ExecutionConfig,
    ExecutionMode,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ProviderPricing,
    ApoapsisConfig,
)
from apoapsis.execution.operation_recovery import recover_stale_execution_operations
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.specification.schema import TaskSpecification
from apoapsis.ui.application import ApoapsisUIService
from apoapsis.ui.server import create_ui_server
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState
from tests.fakes import FakeModelProvider
from tests.test_agent_loop import action
from tests.test_vertical_slice import IMPLEMENTATION_PATCH, specification_response


def _poll_until_terminal(service: ApoapsisUIService, operation_id: str, *, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = service.execution_operation_status(operation_id)
        if record["status"] in {"succeeded", "failed", "ambiguous"}:
            return record
        time.sleep(0.05)
    raise AssertionError(f"operation {operation_id} did not reach a terminal state")


class ExecutionUIServiceTests(unittest.TestCase):
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
        self.config_path = self.root / ".apoapsis" / "config.toml"
        self._write_config()
        self.service = ApoapsisUIService(self.root)
        self.task_id, self.task_version = self._create_approved_task()

    def tearDown(self) -> None:
        if getattr(self, "service", None) is not None:
            self.service._execution_worker = None
            self.service._intake_worker = None
            self.service._review_worker = None

    def _write_config(self) -> None:
        self.config_path.write_text(
            f"""
[models.frontier]
provider = "openai_compatible"
base_url = "https://provider.invalid/v1"
model = "fake-coder-v1"

[execution]
mode = "agent"
route = "auto"

[execution.agent]
max_turns = 8
max_patch_attempts = 2
max_verification_runs = 2
max_search_results = 10
max_read_lines = 120
max_observation_chars = 20000

[execution.frontier_agent]
max_turns = 8
max_patch_attempts = 2
max_verification_runs = 2
max_search_results = 10
max_read_lines = 120
max_observation_chars = 20000

[verification]
[[verification.commands]]
name = "download-tests"
category = "tests"
argv = ["{Path(sys.executable).as_posix()}", "-m", "unittest", "discover", "-s", "tests", "-v"]
timeout_seconds = 30
""",
            encoding="utf-8",
        )

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )

    def _create_approved_task(self, *, risk_level: str = "medium") -> tuple[str, int]:
        task_id = f"TASK-{uuid.uuid4().hex[:12].upper()}"
        payload = json.loads(specification_response())
        payload["task_id"] = task_id
        payload["risk_level"] = risk_level
        specification = TaskSpecification.model_validate(payload)
        self.store.create_task(specification)
        drafted = self.store.transition(
            task_id,
            WorkflowState.SPEC_DRAFTED,
            actor=WorkflowActor.SYSTEM,
            event_type="deterministic_specification_drafted",
        )
        approved = self.store.transition(
            task_id,
            WorkflowState.SPEC_APPROVED,
            actor=WorkflowActor.USER,
            event_type="specification_approved",
            expected_version=drafted.version,
        )
        return task_id, approved.version

    def _execution_operation_store(self) -> ExecutionOperationStore:
        return ExecutionOperationStore(
            self.root / ".apoapsis" / "execution-operations.db"
        )

    def test_task_detail_exposes_execution_preview_and_no_active_operation(self) -> None:
        detail = self.service.task_detail(self.task_id)
        self.assertIn("start_execution", detail["available_actions"])
        self.assertIsNone(detail["active_execution_operation"])
        preview = detail["execution_preview"]
        self.assertEqual(preview["execution_mode"], "agent")
        self.assertIn(preview["predicted_route"], {"local_only", "human_review_required"})
        self.assertEqual(preview["verification_commands"], ["download-tests"])

    def test_submit_execution_operation_completes_via_background_worker(self) -> None:
        local_outputs = [
            action("propose_patch", unified_diff=IMPLEMENTATION_PATCH),
            action("submit_for_verification"),
            action("inspect_diff"),
            action(
                "replace_text",
                path="src/download_service/downloader.py",
                old_text=(
                    '        mode = "ab" if offset else "wb"\n'
                    "        downloaded = offset"
                ),
                new_text=(
                    "        should_append = offset > 0 and "
                    "response.status_code == 206\n"
                    '        mode = "ab" if should_append else "wb"\n'
                    "        downloaded = offset if should_append else 0"
                ),
            ),
            action("run_check", command_name="download-tests"),
        ]
        fake = FakeModelProvider(local_outputs)
        fake_provider = InstrumentedModelProvider(fake, ProviderPricing())
        with patch(
            "apoapsis.execution.operation_service._build_providers",
            return_value=(fake_provider, fake_provider, None),
        ):
            submitted = self.service.submit_execution_operation(
                self.task_id,
                operation_id="EXOP-UI-1",
                expected_version=self.task_version,
            )
            self.assertIn(submitted["status"], {"recorded", "running"})
            final = _poll_until_terminal(self.service, "EXOP-UI-1")

        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(self.store.get_task(self.task_id).state, WorkflowState.COMPLETE)

        detail = self.service.task_detail(self.task_id)
        self.assertIsNone(detail["active_execution_operation"])
        self.assertGreater(len(detail["recent_agent_turns"]), 0)

    def test_duplicate_operation_id_is_rejected_replay_safe(self) -> None:
        with patch(
            "apoapsis.execution.operation_service._build_providers"
        ) as build_providers:
            build_providers.return_value = (
                InstrumentedModelProvider(FakeModelProvider([action("search_repository", query="x")])),
                InstrumentedModelProvider(FakeModelProvider([action("search_repository", query="x")])),
                None,
            )
            self.service.submit_execution_operation(
                self.task_id,
                operation_id="EXOP-UI-REPLAY",
                expected_version=self.task_version,
            )
            with self.assertRaises(Exception):
                self.service.submit_execution_operation(
                    self.task_id,
                    operation_id="EXOP-UI-REPLAY",
                    expected_version=self.task_version,
                )
            _poll_until_terminal(self.service, "EXOP-UI-REPLAY")

    def test_reconnect_reads_persisted_operation_from_a_fresh_service_instance(
        self,
    ) -> None:
        with patch(
            "apoapsis.execution.operation_service._build_providers"
        ) as build_providers:
            build_providers.return_value = (
                InstrumentedModelProvider(FakeModelProvider([action("search_repository", query="x")])),
                InstrumentedModelProvider(FakeModelProvider([action("search_repository", query="x")])),
                None,
            )
            self.service.submit_execution_operation(
                self.task_id,
                operation_id="EXOP-UI-RECONNECT",
                expected_version=self.task_version,
            )
            _poll_until_terminal(self.service, "EXOP-UI-RECONNECT")

        reconnected_service = ApoapsisUIService(self.root)
        record = reconnected_service.execution_operation_status("EXOP-UI-RECONNECT")
        self.assertEqual(record["status"], "succeeded")

    def test_stale_task_version_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.service.submit_execution_operation(
                self.task_id,
                operation_id="EXOP-UI-STALE",
                expected_version=self.task_version + 1,
            )

    def test_ambiguous_operation_is_visible_via_service(self) -> None:
        from apoapsis.execution.operation_service import prepare_execution_operation

        operation_store = self._execution_operation_store()
        prepare_execution_operation(
            self.root,
            self.store,
            operation_store,
            task_id=self.task_id,
            operation_id="EXOP-UI-AMBIGUOUS",
            expected_version=self.task_version,
        )
        operation_store.mark_running("EXOP-UI-AMBIGUOUS")
        recover_stale_execution_operations(
            self.store,
            operation_store,
            running_expiry=datetime.timedelta(seconds=-1),
        )
        status = self.service.execution_operation_status("EXOP-UI-AMBIGUOUS")
        self.assertEqual(status["status"], "ambiguous")
        self.assertEqual(
            self.store.get_task(self.task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )
        detail = self.service.task_detail(self.task_id)
        self.assertIsNone(detail["active_execution_operation"])

    def test_uninitialized_project_rejects_submission(self) -> None:
        service = ApoapsisUIService(Path(self.temporary_directory.name) / "not-a-project")
        with self.assertRaises(Exception):
            service.submit_execution_operation(
                "TASK-DOES-NOT-EXIST", operation_id="EXOP-NONE", expected_version=1
            )


class ExecutionUIServerTests(ExecutionUIServiceTests):
    def setUp(self) -> None:
        super().setUp()
        self.token = "deterministic-execution-session"
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

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, object] | None = None,
        token: str | None = None,
    ):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers: dict[str, str] = {}
        if token is not None:
            headers["X-Apoapsis-Session"] = token
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.origin}{path}", data=data, headers=headers, method=method
        )
        return urllib.request.urlopen(request, timeout=10)

    def test_execute_operations_require_session(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as unauthorized:
            self.request(
                f"/api/tasks/{self.task_id}/execute",
                method="POST",
                payload={"operation_id": "EXOP-NOAUTH", "expected_version": self.task_version},
            )
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()

    def test_http_missing_fields_returns_400(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as bad_request:
            self.request(
                f"/api/tasks/{self.task_id}/execute",
                method="POST",
                payload={"operation_id": "EXOP-BAD"},
                token=self.token,
            )
        self.assertEqual(bad_request.exception.code, 400)
        bad_request.exception.close()

    def test_http_submit_returns_202_and_is_pollable(self) -> None:
        with patch(
            "apoapsis.execution.operation_service._build_providers"
        ) as build_providers:
            build_providers.return_value = (
                InstrumentedModelProvider(FakeModelProvider([action("search_repository", query="x")])),
                InstrumentedModelProvider(FakeModelProvider([action("search_repository", query="x")])),
                None,
            )
            with self.request(
                f"/api/tasks/{self.task_id}/execute",
                method="POST",
                payload={"operation_id": "EXOP-HTTP-1", "expected_version": self.task_version},
                token=self.token,
            ) as response:
                self.assertEqual(response.status, 202)
                submitted = json.load(response)
            self.assertIn(submitted["status"], {"recorded", "running"})

            deadline = time.monotonic() + 15
            record = None
            while time.monotonic() < deadline:
                with self.request(
                    "/api/execution/operations/EXOP-HTTP-1", token=self.token
                ) as response:
                    record = json.load(response)
                if record["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.05)
            assert record is not None
            self.assertEqual(record["status"], "succeeded")

    def test_http_duplicate_operation_id_returns_409(self) -> None:
        with self.request(
            f"/api/tasks/{self.task_id}/execute",
            method="POST",
            payload={"operation_id": "EXOP-HTTP-DUP", "expected_version": self.task_version},
            token=self.token,
        ) as response:
            json.load(response)
        with self.assertRaises(urllib.error.HTTPError) as conflict:
            self.request(
                f"/api/tasks/{self.task_id}/execute",
                method="POST",
                payload={"operation_id": "EXOP-HTTP-DUP", "expected_version": self.task_version},
                token=self.token,
            )
        self.assertEqual(conflict.exception.code, 409)
        conflict.exception.close()

    def test_static_asset_bundles_control_room_actions(self) -> None:
        with self.request("/app.js", token=self.token) as response:
            content = response.read().decode("utf-8")
        self.assertIn("submitExecutionStart", content)
        self.assertIn("/api/execution/operations", content)
        self.assertIn("execution-start-confirm", content)


if __name__ == "__main__":
    unittest.main()
