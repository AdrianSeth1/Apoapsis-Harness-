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
from apoapsis.operations.lease import new_owner_id
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

    def _config(self) -> ApoapsisConfig:
        return ApoapsisConfig.from_toml(self.config_path)

    def _authorization_sha256(self, task_id: str | None = None) -> str:
        detail = self.service.task_detail(task_id or self.task_id)
        return detail["execution_preview"]["authorization_sha256"]

    def test_task_detail_exposes_execution_preview_and_no_active_operation(self) -> None:
        detail = self.service.task_detail(self.task_id)
        self.assertIn("start_execution", detail["available_actions"])
        self.assertIsNone(detail["active_execution_operation"])
        preview = detail["execution_preview"]
        self.assertEqual(preview["execution_mode"], "agent")
        self.assertIn(preview["predicted_route"], {"local_only", "human_review_required"})
        self.assertEqual(preview["verification_commands"], ["download-tests"])

    def test_recent_agent_turns_are_ordered_local_then_frontier(self) -> None:
        """ADR 0026: a session always exhausts every local turn (if any)
        before ever escalating to a frontier turn -- never interleaved --
        so ``recent_agent_turns`` must show local turns first, in actual
        execution order, not alphabetically ("frontier" < "local")."""

        task_directory = self.root / ".apoapsis" / "tasks" / self.task_id
        task_directory.mkdir(parents=True, exist_ok=True)
        turn = lambda turn_number: json.dumps(
            {
                "turn": turn_number,
                "action": "search_repository",
                "accepted": True,
                "summary": "ok",
            }
        )
        (task_directory / "agent-turn-001.json").write_text(turn(1), encoding="utf-8")
        (task_directory / "agent-turn-002.json").write_text(turn(2), encoding="utf-8")
        (task_directory / "frontier-agent-turn-001.json").write_text(
            turn(1), encoding="utf-8"
        )
        detail = self.service.task_detail(self.task_id)
        stages = [item["stage"] for item in detail["recent_agent_turns"]]
        self.assertEqual(stages, ["local", "local", "frontier"])

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
                expected_authorization_sha256=self._authorization_sha256(),
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
            authorization_sha256 = self._authorization_sha256()
            self.service.submit_execution_operation(
                self.task_id,
                operation_id="EXOP-UI-REPLAY",
                expected_version=self.task_version,
                expected_authorization_sha256=authorization_sha256,
            )
            with self.assertRaises(Exception):
                self.service.submit_execution_operation(
                    self.task_id,
                    operation_id="EXOP-UI-REPLAY",
                    expected_version=self.task_version,
                    expected_authorization_sha256=authorization_sha256,
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
                expected_authorization_sha256=self._authorization_sha256(),
            )
            _poll_until_terminal(self.service, "EXOP-UI-RECONNECT")

        reconnected_service = ApoapsisUIService(self.root)
        record = reconnected_service.execution_operation_status("EXOP-UI-RECONNECT")
        self.assertEqual(record["status"], "succeeded")

    def test_stale_preview_hash_is_rejected_before_prepare(self) -> None:
        """ADR 0026: the confirmation must authorize exactly what the
        preview showed. A stale ``expected_authorization_sha256`` (as if
        the browser rendered its preview, then the task/config/repository
        changed before the user clicked "Start coding") is rejected
        before ``prepare_execution_operation`` ever runs -- no audit
        write, no operation record, nothing durably created."""

        with self.assertRaises(Exception):
            self.service.submit_execution_operation(
                self.task_id,
                operation_id="EXOP-UI-STALE-PREVIEW",
                expected_version=self.task_version,
                expected_authorization_sha256="0" * 64,
            )
        with self.assertRaises(Exception):
            self._execution_operation_store().get("EXOP-UI-STALE-PREVIEW")

    def test_stale_task_version_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.service.submit_execution_operation(
                self.task_id,
                operation_id="EXOP-UI-STALE",
                expected_version=self.task_version + 1,
                expected_authorization_sha256=self._authorization_sha256(),
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
            config=self._config(),
        )
        operation_store.mark_running(
            "EXOP-UI-AMBIGUOUS",
            owner_id=new_owner_id(),
            lease_duration=datetime.timedelta(seconds=-1),
        )
        recover_stale_execution_operations(self.store, operation_store)
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
                "TASK-DOES-NOT-EXIST",
                operation_id="EXOP-NONE",
                expected_version=1,
                expected_authorization_sha256="0" * 64,
            )

    def test_start_background_workers_reclaims_stranded_recorded_operation(
        self,
    ) -> None:
        """ADR 0025: a process can durably record an operation and then
        crash before ever enqueueing it. A freshly constructed service
        must reclaim and run it to completion the moment
        ``start_background_workers`` is called -- exactly what
        ``create_ui_server`` now does at startup -- without any browser
        ever calling ``submit_execution_operation`` for it."""

        from apoapsis.execution.operation_service import prepare_execution_operation

        operation_store = self._execution_operation_store()
        prepare_execution_operation(
            self.root,
            self.store,
            operation_store,
            task_id=self.task_id,
            operation_id="EXOP-UI-STRANDED",
            expected_version=self.task_version,
            config=self._config(),
        )
        self.assertEqual(operation_store.get("EXOP-UI-STRANDED").status.value, "recorded")

        fresh_service = ApoapsisUIService(self.root)
        self.addCleanup(setattr, fresh_service, "_execution_worker", None)
        fake = FakeModelProvider([action("search_repository", query="x")])
        fake_provider = InstrumentedModelProvider(fake, ProviderPricing())
        with patch(
            "apoapsis.execution.operation_service._build_providers",
            return_value=(fake_provider, fake_provider, None),
        ):
            fresh_service.start_background_workers()
            final = _poll_until_terminal(fresh_service, "EXOP-UI-STRANDED")
        self.assertIn(final["status"], {"succeeded", "failed"})
        self.assertGreaterEqual(len(fake.invocations), 1)


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

    def _http_authorization_sha256(self, task_id: str | None = None) -> str:
        with self.request(
            f"/api/tasks/{task_id or self.task_id}", token=self.token
        ) as response:
            detail = json.load(response)
        return detail["execution_preview"]["authorization_sha256"]

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
                payload={
                    "operation_id": "EXOP-NOAUTH",
                    "expected_version": self.task_version,
                    "expected_authorization_sha256": "0" * 64,
                },
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
                payload={
                    "operation_id": "EXOP-HTTP-1",
                    "expected_version": self.task_version,
                    "expected_authorization_sha256": self._http_authorization_sha256(),
                },
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
        authorization_sha256 = self._http_authorization_sha256()
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
                payload={
                    "operation_id": "EXOP-HTTP-DUP",
                    "expected_version": self.task_version,
                    "expected_authorization_sha256": authorization_sha256,
                },
                token=self.token,
            ) as response:
                json.load(response)
            with self.assertRaises(urllib.error.HTTPError) as conflict:
                self.request(
                    f"/api/tasks/{self.task_id}/execute",
                    method="POST",
                    payload={
                        "operation_id": "EXOP-HTTP-DUP",
                        "expected_version": self.task_version,
                        "expected_authorization_sha256": authorization_sha256,
                    },
                    token=self.token,
                )
            self.assertEqual(conflict.exception.code, 409)
            conflict.exception.close()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                with self.request(
                    "/api/execution/operations/EXOP-HTTP-DUP", token=self.token
                ) as response:
                    record = json.load(response)
                if record["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.05)

    def test_static_asset_bundles_control_room_actions(self) -> None:
        with self.request("/app.js", token=self.token) as response:
            content = response.read().decode("utf-8")
        self.assertIn("submitExecutionStart", content)
        self.assertIn("/api/execution/operations", content)
        self.assertIn("execution-start-confirm", content)
        # ADR 0026: the confirmation must send back the exact hash the
        # preview showed -- a live-browser pass caught this once already
        # (the JS never sent it, so every real "Start coding" click failed
        # with a 400 the deterministic suite could not see).
        self.assertIn("expected_authorization_sha256", content)
        self.assertIn("authorizationSha256", content)


if __name__ == "__main__":
    unittest.main()
