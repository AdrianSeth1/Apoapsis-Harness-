from __future__ import annotations

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
    AgentLoopConfig,
    AgentRoute,
    ContextCompilerConfig,
    ExecutionConfig,
    ExecutionMode,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ProviderPricing,
    ApoapsisConfig,
)
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.reporting.report import TaskOutcome
from apoapsis.review.store import ReviewOperationStore
from apoapsis.ui.application import ApoapsisUIService
from apoapsis.ui.server import create_ui_server
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.states import WorkflowState
from apoapsis.workflow.vertical_slice import VerticalSliceRunner
from tests.fakes import FakeModelProvider
from tests.test_agent_loop import action
from tests.test_vertical_slice import IMPLEMENTATION_PATCH, REQUEST, specification_response


def _poll_until_terminal(service: ApoapsisUIService, operation_id: str, *, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = service.review_operation_status(operation_id)
        if record["status"] in {"succeeded", "failed"}:
            return record
        time.sleep(0.05)
    raise AssertionError(f"operation {operation_id} did not reach a terminal state")


class ReviewUIServiceTests(unittest.TestCase):
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

        self.config = ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1", model="fake-coder-v1"
                )
            ),
            execution=ExecutionConfig(
                mode=ExecutionMode.AGENT,
                route=AgentRoute.AUTO,
                agent=AgentLoopConfig(
                    max_turns=3,
                    max_patch_attempts=2,
                    max_verification_runs=2,
                    max_search_results=10,
                    max_read_lines=120,
                    max_observation_chars=20_000,
                ),
                frontier_agent=AgentLoopConfig(
                    max_turns=3,
                    max_patch_attempts=2,
                    max_verification_runs=2,
                    max_search_results=10,
                    max_read_lines=120,
                    max_observation_chars=20_000,
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
                            sys.executable,
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
        self._write_config()
        self.task_id = self._escalate_locally()
        self.service = ApoapsisUIService(self.root)

    def tearDown(self) -> None:
        # Background worker threads are daemons and stop with the process,
        # but drop our reference so tests don't accumulate live threads.
        if getattr(self, "service", None) is not None:
            self.service._review_worker = None

    def _write_config(self) -> None:
        # `ApoapsisUIService` and `ReviewWorker` both only ever read
        # `.apoapsis/config.toml` from disk (never an in-memory config
        # object), so the fixture's `self.config` must also exist as a real
        # TOML file matching it exactly.
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
max_turns = 3
max_patch_attempts = 2
max_verification_runs = 2
max_search_results = 10
max_read_lines = 120
max_observation_chars = 20000

[execution.frontier_agent]
max_turns = 3
max_patch_attempts = 2
max_verification_runs = 2
max_search_results = 10
max_read_lines = 120
max_observation_chars = 20000

[context]
max_files = 10
max_excerpt_lines = 200
max_total_chars = 50000

[patch]
max_changed_lines = 100

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

    @staticmethod
    def _inject_task_id(fake: FakeModelProvider) -> None:
        original_complete = fake.complete

        def complete(invocation):
            output = original_complete(invocation)
            if len(fake.invocations) == 1:
                task_id = invocation.prompt.split('task_id to "', 1)[1].split('"', 1)[
                    0
                ]
                raw = json.loads(output.content)
                raw["task_id"] = task_id
                return output.model_copy(update={"content": json.dumps(raw)})
            return output

        fake.complete = complete  # type: ignore[method-assign]

    def _escalate_locally(self) -> str:
        fake = FakeModelProvider(
            [
                specification_response(),
                action("search_repository", query="get_offset"),
                action("search_repository", query="downloader"),
                action("search_repository", query="jobs"),
            ]
        )
        self._inject_task_id(fake)
        provider = InstrumentedModelProvider(fake, ProviderPricing())
        report = VerticalSliceRunner(self.root, self.store, provider, self.config).run(
            REQUEST, approve=lambda specification: True
        )
        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)
        return report.task_id

    def test_review_cases_lists_the_stopped_task(self) -> None:
        index = self.service.review_cases()
        self.assertEqual(len(index["cases"]), 1)
        self.assertEqual(index["cases"][0]["task_id"], self.task_id)

    def test_review_case_detail_matches_projection(self) -> None:
        detail = self.service.review_case_detail(self.task_id)
        self.assertEqual(
            detail["stop_reason_kind"], "local_agent_escalation_unavailable"
        )
        self.assertIn("local_continuation", detail["eligible_actions"])
        self.assertTrue(detail["worktree_exists"])

    def test_verification_only_retry_runs_on_a_background_worker_and_persists(
        self,
    ) -> None:
        detail = self.service.review_case_detail(self.task_id)
        submitted = self.service.submit_review_operation(
            self.task_id,
            action="verification_only_retry",
            operation_id="RVOP-UI-1",
            expected_version=detail["task_version"],
            expected_worktree_fingerprint=detail["worktree_fingerprint"],
        )
        # Submission itself must never block on the actual work -- it
        # returns before or without ever reaching a terminal state.
        self.assertIn(submitted["status"], {"recorded", "running", "succeeded"})

        final = _poll_until_terminal(self.service, "RVOP-UI-1")
        self.assertEqual(final["status"], "succeeded")
        # The download-service fixture's tests fail without a real patch,
        # so a verification-only retry (no code change) stays incomplete --
        # the task must still be at HUMAN_REVIEW_REQUIRED, not silently
        # marked complete.
        self.assertEqual(
            self.store.get_task(self.task_id).state, WorkflowState.HUMAN_REVIEW_REQUIRED
        )

    def test_duplicate_operation_id_is_rejected_replay_safe(self) -> None:
        detail = self.service.review_case_detail(self.task_id)
        self.service.submit_review_operation(
            self.task_id,
            action="verification_only_retry",
            operation_id="RVOP-UI-REPLAY",
            expected_version=detail["task_version"],
            expected_worktree_fingerprint=detail["worktree_fingerprint"],
        )
        with self.assertRaises(Exception):
            self.service.submit_review_operation(
                self.task_id,
                action="verification_only_retry",
                operation_id="RVOP-UI-REPLAY",
                expected_version=detail["task_version"],
                expected_worktree_fingerprint=detail["worktree_fingerprint"],
            )
        _poll_until_terminal(self.service, "RVOP-UI-REPLAY")

    def test_reconnect_reads_persisted_operation_from_a_fresh_service_instance(
        self,
    ) -> None:
        detail = self.service.review_case_detail(self.task_id)
        self.service.submit_review_operation(
            self.task_id,
            action="verification_only_retry",
            operation_id="RVOP-UI-RECONNECT",
            expected_version=detail["task_version"],
            expected_worktree_fingerprint=detail["worktree_fingerprint"],
        )
        _poll_until_terminal(self.service, "RVOP-UI-RECONNECT")

        # Simulate a browser reconnect: a brand-new service instance (no
        # shared in-memory state with the one that submitted the operation)
        # must see the exact same, durably persisted operation record.
        reconnected_service = ApoapsisUIService(self.root)
        record = reconnected_service.review_operation_status("RVOP-UI-RECONNECT")
        self.assertEqual(record["status"], "succeeded")

    def test_stale_task_version_is_rejected(self) -> None:
        detail = self.service.review_case_detail(self.task_id)
        with self.assertRaises(Exception):
            self.service.submit_review_operation(
                self.task_id,
                action="abandon",
                operation_id="RVOP-UI-STALE",
                expected_version=detail["task_version"] + 1,
            )

    def test_local_continuation_completes_via_background_worker(self) -> None:
        detail = self.service.review_case_detail(self.task_id)
        continuation_outputs = [
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
        fake = FakeModelProvider(continuation_outputs)
        fake_provider = InstrumentedModelProvider(fake, ProviderPricing())

        with patch(
            "apoapsis.review.execution._build_provider", return_value=fake_provider
        ):
            self.service.submit_review_operation(
                self.task_id,
                action="local_continuation",
                operation_id="RVOP-UI-CONTINUE",
                expected_version=detail["task_version"],
                expected_worktree_fingerprint=detail["worktree_fingerprint"],
                additional_turns=5,
            )
            final = _poll_until_terminal(self.service, "RVOP-UI-CONTINUE", timeout=20)

        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(
            self.store.get_task(self.task_id).state, WorkflowState.COMPLETE
        )

    def test_authorize_frontier_stage_completes_via_background_worker(self) -> None:
        # Simulate the user adding frontier credentials *after* the local
        # stop -- frontier availability is always checked fresh against
        # the current config, never the stale original routing decision.
        # A fresh stage always uses the full configured frontier_agent
        # budget (no additional_turns delta like continuations get), so
        # this rewrite also raises max_turns enough for the fake session
        # below (TOML forbids re-declaring [execution.frontier_agent], so
        # the whole file is rewritten rather than appended).
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                "[execution.frontier_agent]\nmax_turns = 3",
                "[execution.frontier_agent]\nmax_turns = 8",
            )
            + """
[models.frontier_coder]
provider = "openai_compatible"
base_url = "https://frontier.invalid/v1"
model = "fake-frontier-stage-v1"
""",
            encoding="utf-8",
        )
        detail = self.service.review_case_detail(self.task_id)
        self.assertIn("authorize_frontier_stage", detail["eligible_actions"])
        self.assertEqual(detail["frontier_model"], "fake-frontier-stage-v1")

        stage_outputs = [
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
        fake = FakeModelProvider(stage_outputs, model_name="fake-frontier-stage-v1")
        fake_provider = InstrumentedModelProvider(fake, ProviderPricing())

        with patch(
            "apoapsis.review.execution._build_provider", return_value=fake_provider
        ):
            self.service.submit_review_operation(
                self.task_id,
                action="authorize_frontier_stage",
                operation_id="RVOP-UI-STAGE-1",
                expected_version=detail["task_version"],
                expected_worktree_fingerprint=detail["worktree_fingerprint"],
            )
            final = _poll_until_terminal(self.service, "RVOP-UI-STAGE-1", timeout=20)

        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(
            self.store.get_task(self.task_id).state, WorkflowState.COMPLETE
        )
        package_path = (
            self.root
            / ".apoapsis"
            / "tasks"
            / self.task_id
            / "review-frontier-stage-RVOP-UI-STAGE-1.json"
        )
        self.assertTrue(package_path.is_file())


class ReviewUIServerTests(ReviewUIServiceTests):
    def setUp(self) -> None:
        super().setUp()
        self.token = "deterministic-review-session"
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
    ) -> urllib.response.addinfourl:
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

    def _poll_http_until_terminal(self, task_id: str, operation_id: str, *, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.request(
                f"/api/reviews/{task_id}/operations/{operation_id}", token=self.token
            ) as response:
                record = json.load(response)
            if record["status"] in {"succeeded", "failed"}:
                return record
            time.sleep(0.05)
        raise AssertionError("operation did not reach a terminal state over HTTP")

    def test_reviews_index_and_detail_require_session(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as unauthorized:
            self.request("/api/reviews")
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()

        with self.request("/api/reviews", token=self.token) as response:
            payload = json.load(response)
        self.assertEqual(payload["cases"][0]["task_id"], self.task_id)

        with self.request(
            f"/api/reviews/{self.task_id}", token=self.token
        ) as response:
            detail = json.load(response)
        self.assertEqual(detail["task_id"], self.task_id)

    def test_http_submit_returns_202_and_is_pollable(self) -> None:
        detail_response = self.request(f"/api/reviews/{self.task_id}", token=self.token)
        with detail_response as response:
            detail = json.load(response)

        with self.request(
            f"/api/reviews/{self.task_id}/operations",
            method="POST",
            payload={
                "action": "verification_only_retry",
                "operation_id": "RVOP-HTTP-1",
                "expected_version": detail["task_version"],
                "expected_worktree_fingerprint": detail["worktree_fingerprint"],
            },
            token=self.token,
        ) as response:
            self.assertEqual(response.status, 202)
            submitted = json.load(response)
        self.assertIn(submitted["status"], {"recorded", "running"})

        final = self._poll_http_until_terminal(self.task_id, "RVOP-HTTP-1")
        self.assertEqual(final["status"], "succeeded")

    def test_http_duplicate_operation_id_returns_409(self) -> None:
        with self.request(f"/api/reviews/{self.task_id}", token=self.token) as response:
            detail = json.load(response)
        payload = {
            "action": "verification_only_retry",
            "operation_id": "RVOP-HTTP-DUP",
            "expected_version": detail["task_version"],
            "expected_worktree_fingerprint": detail["worktree_fingerprint"],
        }
        with self.request(
            f"/api/reviews/{self.task_id}/operations",
            method="POST",
            payload=payload,
            token=self.token,
        ):
            pass
        with self.assertRaises(urllib.error.HTTPError) as conflict:
            self.request(
                f"/api/reviews/{self.task_id}/operations",
                method="POST",
                payload=payload,
                token=self.token,
            )
        self.assertEqual(conflict.exception.code, 409)
        conflict.exception.close()
        self._poll_http_until_terminal(self.task_id, "RVOP-HTTP-DUP")

    def test_http_stale_version_returns_409(self) -> None:
        with self.request(f"/api/reviews/{self.task_id}", token=self.token) as response:
            detail = json.load(response)
        with self.assertRaises(urllib.error.HTTPError) as conflict:
            self.request(
                f"/api/reviews/{self.task_id}/operations",
                method="POST",
                payload={
                    "action": "abandon",
                    "operation_id": "RVOP-HTTP-STALE",
                    "expected_version": detail["task_version"] + 1,
                },
                token=self.token,
            )
        self.assertEqual(conflict.exception.code, 409)
        conflict.exception.close()


if __name__ == "__main__":
    unittest.main()
