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
from pathlib import Path
from unittest.mock import patch

from apoapsis.config import (
    ContextCompilerConfig,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ProviderPricing,
    ApoapsisConfig,
)
from apoapsis.intake.recovery import recover_stale_intake_operations
from apoapsis.intake.store import IntakeOperationStore
from apoapsis.operations.lease import new_owner_id
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.ui.application import ApoapsisUIService
from apoapsis.ui.server import create_ui_server
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.states import WorkflowState
from tests.fakes import FakeModelProvider
from tests.test_specification_correction import _inject_task_id_into_every_json_response
from tests.test_vertical_slice import REQUEST, specification_response


def _poll_until_terminal(service: ApoapsisUIService, operation_id: str, *, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = service.intake_operation_status(operation_id)
        if record["status"] in {"pending_specification_approval", "failed", "ambiguous"}:
            return record
        time.sleep(0.05)
    raise AssertionError(f"operation {operation_id} did not reach a terminal state")


class IntakeUIServiceTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        if getattr(self, "service", None) is not None:
            self.service._intake_worker = None
            self.service._review_worker = None

    def _write_config(self) -> None:
        self.config_path.write_text(
            f"""
[models.frontier]
provider = "openai_compatible"
base_url = "https://provider.invalid/v1"
model = "fake-coder-v1"

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

    def _intake_operation_store(self) -> IntakeOperationStore:
        return IntakeOperationStore(self.root / ".apoapsis" / "intake-operations.db")

    def test_submit_intake_operation_completes_via_background_worker(self) -> None:
        fake = FakeModelProvider([specification_response()])
        _inject_task_id_into_every_json_response(fake)
        with patch(
            "apoapsis.intake.execution._build_provider",
            return_value=InstrumentedModelProvider(fake),
        ):
            submitted = self.service.submit_intake_operation(
                request_text=REQUEST, operation_id="INOP-UI-1"
            )
            self.assertIn(submitted["status"], {"recorded", "running"})
            final = _poll_until_terminal(self.service, "INOP-UI-1")

        self.assertEqual(final["status"], "pending_specification_approval")
        task = self.store.get_task(final["task_id"])
        self.assertEqual(task.state, WorkflowState.SPEC_DRAFTED)

    def test_duplicate_operation_id_is_rejected_replay_safe(self) -> None:
        fake = FakeModelProvider([specification_response(), specification_response()])
        _inject_task_id_into_every_json_response(fake)
        with patch(
            "apoapsis.intake.execution._build_provider",
            return_value=InstrumentedModelProvider(fake),
        ):
            self.service.submit_intake_operation(
                request_text=REQUEST, operation_id="INOP-UI-REPLAY"
            )
            with self.assertRaises(Exception):
                self.service.submit_intake_operation(
                    request_text="a different request entirely",
                    operation_id="INOP-UI-REPLAY",
                )
            _poll_until_terminal(self.service, "INOP-UI-REPLAY")

    def test_reconnect_reads_persisted_operation_from_a_fresh_service_instance(
        self,
    ) -> None:
        fake = FakeModelProvider([specification_response()])
        _inject_task_id_into_every_json_response(fake)
        with patch(
            "apoapsis.intake.execution._build_provider",
            return_value=InstrumentedModelProvider(fake),
        ):
            self.service.submit_intake_operation(
                request_text=REQUEST, operation_id="INOP-UI-RECONNECT"
            )
            _poll_until_terminal(self.service, "INOP-UI-RECONNECT")

        reconnected_service = ApoapsisUIService(self.root)
        record = reconnected_service.intake_operation_status("INOP-UI-RECONNECT")
        self.assertEqual(record["status"], "pending_specification_approval")

    def test_ambiguous_operation_is_visible_via_service(self) -> None:
        operation_store = self._intake_operation_store()
        from apoapsis.intake.execution import prepare_intake_operation

        record = prepare_intake_operation(
            self.root,
            self.store,
            operation_store,
            request_text=REQUEST,
            operation_id="INOP-UI-AMBIGUOUS",
        )
        operation_store.mark_running(
            "INOP-UI-AMBIGUOUS",
            owner_id=new_owner_id(),
            lease_duration=datetime.timedelta(seconds=-1),
        )
        recover_stale_intake_operations(self.store, operation_store)
        status = self.service.intake_operation_status("INOP-UI-AMBIGUOUS")
        self.assertEqual(status["status"], "ambiguous")
        self.assertEqual(
            self.store.get_task(record.task_id).state,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
        )

    def test_uninitialized_project_rejects_submission(self) -> None:
        service = ApoapsisUIService(Path(self.temporary_directory.name) / "not-a-project")
        with self.assertRaises(Exception):
            service.submit_intake_operation(request_text=REQUEST, operation_id="INOP-NONE")


class IntakeUIServerTests(IntakeUIServiceTests):
    def setUp(self) -> None:
        super().setUp()
        self.token = "deterministic-intake-session"
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

    def test_intake_operations_require_session(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as unauthorized:
            self.request(
                "/api/intake/operations",
                method="POST",
                payload={"request_text": REQUEST, "operation_id": "INOP-NOAUTH"},
            )
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()

    def test_http_missing_fields_returns_400(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as bad_request:
            self.request(
                "/api/intake/operations",
                method="POST",
                payload={"operation_id": "INOP-BAD"},
                token=self.token,
            )
        self.assertEqual(bad_request.exception.code, 400)
        bad_request.exception.close()

    def test_http_submit_returns_202_and_is_pollable(self) -> None:
        fake = FakeModelProvider([specification_response()])
        _inject_task_id_into_every_json_response(fake)
        with patch(
            "apoapsis.intake.execution._build_provider",
            return_value=InstrumentedModelProvider(fake),
        ):
            with self.request(
                "/api/intake/operations",
                method="POST",
                payload={"request_text": REQUEST, "operation_id": "INOP-HTTP-1"},
                token=self.token,
            ) as response:
                self.assertEqual(response.status, 202)
                submitted = json.load(response)
            self.assertIn(submitted["status"], {"recorded", "running"})

            deadline = time.monotonic() + 10
            record = None
            while time.monotonic() < deadline:
                with self.request(
                    "/api/intake/operations/INOP-HTTP-1", token=self.token
                ) as response:
                    record = json.load(response)
                if record["status"] in {"pending_specification_approval", "failed"}:
                    break
                time.sleep(0.05)
            assert record is not None
            self.assertEqual(record["status"], "pending_specification_approval")

    def test_http_duplicate_operation_id_returns_409(self) -> None:
        with self.request(
            "/api/intake/operations",
            method="POST",
            payload={"request_text": REQUEST, "operation_id": "INOP-HTTP-DUP"},
            token=self.token,
        ) as response:
            json.load(response)
        with self.assertRaises(urllib.error.HTTPError) as conflict:
            self.request(
                "/api/intake/operations",
                method="POST",
                payload={"request_text": REQUEST, "operation_id": "INOP-HTTP-DUP"},
                token=self.token,
            )
        self.assertEqual(conflict.exception.code, 409)
        conflict.exception.close()

    def test_static_asset_bundles_the_new_request_screen(self) -> None:
        with self.request("/app.js", token=self.token) as response:
            content = response.read().decode("utf-8")
        self.assertIn("submitIntakeOperation", content)
        self.assertIn("/api/intake/operations", content)


if __name__ == "__main__":
    unittest.main()
