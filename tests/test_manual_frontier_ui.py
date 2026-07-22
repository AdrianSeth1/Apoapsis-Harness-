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
from apoapsis.manual_frontier.errors import (
    PreviewNotApprovedError,
    ResponseHashMismatchError,
    TaskVersionMismatchError,
)
from apoapsis.manual_frontier.package import build_manual_frontier_handoff_package
from apoapsis.manual_frontier.store import ManualFrontierPreviewStore
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.reporting.report import TaskOutcome
from apoapsis.review.case import build_review_case
from apoapsis.review.errors import ActiveOperationExistsError, DuplicateOperationError
from apoapsis.review.schema import ReviewActionKind
from apoapsis.review.store import ReviewOperationStore
from apoapsis.ui.application import ApoapsisUIService
from apoapsis.ui.server import create_ui_server
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.states import WorkflowState
from apoapsis.workflow.vertical_slice import VerticalSliceRunner
from tests.fakes import FakeModelProvider
from tests.test_agent_loop import action
from tests.test_vertical_slice import COMPLETE_PATCH, REQUEST, specification_response


def _poll_until_terminal(service: ApoapsisUIService, operation_id: str, *, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = service.review_operation_status(operation_id)
        if record["status"] in {"succeeded", "failed"}:
            return record
        time.sleep(0.05)
    raise AssertionError(f"operation {operation_id} did not reach a terminal state")


def _envelope(package, *, patch: str, **overrides) -> str:
    payload = {
        "package_id": package.package_id,
        "package_sha256": package.package_sha256,
        "task_id": package.task_id,
        "task_version": package.task_version,
        "patch": patch,
        "summary": "fixed it",
    }
    payload.update(overrides)
    return json.dumps(payload)


class ManualFrontierUIServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "download-service"
        example = Path(__file__).resolve().parents[1] / "examples" / "download-service"
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
                    max_turns=3, max_patch_attempts=2, max_verification_runs=2,
                    max_search_results=10, max_read_lines=120, max_observation_chars=20_000,
                ),
                frontier_agent=AgentLoopConfig(
                    max_turns=3, max_patch_attempts=2, max_verification_runs=2,
                    max_search_results=10, max_read_lines=120, max_observation_chars=20_000,
                ),
            ),
            context=ContextCompilerConfig(max_files=10, max_excerpt_lines=200, max_total_chars=50_000),
            patch=PatchPolicyConfig(max_changed_lines=100),
            verification=VerificationConfig(
                commands=[
                    VerificationCommand(
                        name="download-tests", category="tests",
                        argv=[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                        timeout_seconds=30,
                    )
                ]
            ),
        )
        self._write_config()
        self.task_id = self._escalate_locally()
        self.service = ApoapsisUIService(self.root)

    def tearDown(self) -> None:
        if getattr(self, "service", None) is not None:
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
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)

    @staticmethod
    def _inject_task_id(fake: FakeModelProvider) -> None:
        original_complete = fake.complete

        def complete(invocation):
            output = original_complete(invocation)
            if len(fake.invocations) == 1:
                task_id = invocation.prompt.split('task_id to "', 1)[1].split('"', 1)[0]
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

    def _export(self):
        return self.service.export_manual_frontier_handoff(self.task_id)

    def test_export_writes_absolute_paths_and_eligible_only(self) -> None:
        result = self._export()
        self.assertTrue(Path(result["package_artifact_absolute_path"]).is_file())
        self.assertTrue(Path(result["markdown_artifact_absolute_path"]).is_file())
        self.assertTrue(result["markdown_artifact_absolute_path"].endswith(".md"))
        self.assertIn(result["package"]["package_id"], result["package_artifact_absolute_path"])
        self.assertEqual(result["package"]["schema_version"], "1.1")
        self.assertIsNotNone(result["package"]["repository_context"])
        self.assertTrue(result["package"]["prior_agent_sessions"])
        self.assertIn("verification_results", result["package"])

    def test_import_then_approve_then_apply_two_step_confirmation(self) -> None:
        exported = self._export()
        package_id = exported["package"]["package_id"]
        preview = self.service.import_manual_frontier_response(
            self.task_id,
            package_id=package_id,
            response_text=_envelope(
                _PackageStub(exported["package"]), patch=COMPLETE_PATCH
            ),
            declared_model_name="claude-opus-4.6-web",
            preview_id="MFPV-UI-1",
        )
        self.assertEqual(preview["status"], "previewed")

        task = self.store.get_task(self.task_id)
        review_case = build_review_case(self.root, self.store, self.config, self.task_id)

        # Applying before approval must fail -- the second step cannot skip the first.
        with self.assertRaises(PreviewNotApprovedError):
            operation_store = ReviewOperationStore(
                self.root / ".apoapsis" / "review-operations.db"
            )
            from apoapsis.review.execution import execute_review_action

            execute_review_action(
                self.root, self.store, operation_store, self.config,
                task_id=self.task_id, action=ReviewActionKind.MANUAL_FRONTIER_HANDOFF,
                operation_id="RVOP-TOO-EARLY", expected_version=task.version,
                expected_worktree_fingerprint=review_case.worktree_fingerprint,
                manual_frontier_preview_id="MFPV-UI-1",
            )

        approved = self.service.approve_manual_frontier_preview(
            self.task_id, "MFPV-UI-1", expected_task_version=task.version
        )
        self.assertEqual(approved["status"], "approved")

        operation = self.service.submit_review_operation(
            self.task_id,
            action="manual_frontier_handoff",
            operation_id="RVOP-UI-APPLY-1",
            expected_version=task.version,
            expected_worktree_fingerprint=review_case.worktree_fingerprint,
            manual_frontier_preview_id="MFPV-UI-1",
        )
        self.assertIn(operation["status"], {"recorded", "running"})
        record = _poll_until_terminal(self.service, "RVOP-UI-APPLY-1")
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(self.store.get_task(self.task_id).state, WorkflowState.COMPLETE)

    def test_stale_task_version_response_rejected(self) -> None:
        exported = self._export()
        package_id = exported["package"]["package_id"]
        stale_envelope = json.loads(
            _envelope(_PackageStub(exported["package"]), patch=COMPLETE_PATCH)
        )
        stale_envelope["task_version"] = exported["package"]["task_version"] + 1
        with self.assertRaises(TaskVersionMismatchError):
            self.service.import_manual_frontier_response(
                self.task_id,
                package_id=package_id,
                response_text=json.dumps(stale_envelope),
                declared_model_name="claude-opus-4.6-web",
                preview_id="MFPV-STALE",
            )

    def test_response_hash_mismatch_rejected(self) -> None:
        exported = self._export()
        package_id = exported["package"]["package_id"]
        tampered = json.loads(
            _envelope(_PackageStub(exported["package"]), patch=COMPLETE_PATCH)
        )
        tampered["package_sha256"] = "0" * 64
        with self.assertRaises(ResponseHashMismatchError):
            self.service.import_manual_frontier_response(
                self.task_id,
                package_id=package_id,
                response_text=json.dumps(tampered),
                declared_model_name="claude-opus-4.6-web",
                preview_id="MFPV-HASH",
            )

    def test_replayed_operation_id_rejected(self) -> None:
        exported = self._export()
        package_id = exported["package"]["package_id"]
        self.service.import_manual_frontier_response(
            self.task_id, package_id=package_id,
            response_text=_envelope(_PackageStub(exported["package"]), patch=COMPLETE_PATCH),
            declared_model_name="claude-opus-4.6-web", preview_id="MFPV-REPLAY",
        )
        task = self.store.get_task(self.task_id)
        self.service.approve_manual_frontier_preview(
            self.task_id, "MFPV-REPLAY", expected_task_version=task.version
        )
        review_case = build_review_case(self.root, self.store, self.config, self.task_id)
        self.service.submit_review_operation(
            self.task_id, action="manual_frontier_handoff", operation_id="RVOP-REPLAY-1",
            expected_version=task.version,
            expected_worktree_fingerprint=review_case.worktree_fingerprint,
            manual_frontier_preview_id="MFPV-REPLAY",
        )
        record = _poll_until_terminal(self.service, "RVOP-REPLAY-1")
        self.assertEqual(record["status"], "succeeded")
        # A completed operation's id can never be resubmitted -- a replayed
        # request is rejected outright at the ledger, never silently
        # re-applied a second time.
        operation_store = ReviewOperationStore(self.root / ".apoapsis" / "review-operations.db")
        with self.assertRaises(DuplicateOperationError):
            operation_store.create(
                "RVOP-REPLAY-1", self.task_id, ReviewActionKind.MANUAL_FRONTIER_HANDOFF,
                expected_task_version=1,
            )

    def test_concurrent_active_operation_conflict(self) -> None:
        exported = self._export()
        package_id = exported["package"]["package_id"]
        self.service.import_manual_frontier_response(
            self.task_id, package_id=package_id,
            response_text=_envelope(_PackageStub(exported["package"]), patch=COMPLETE_PATCH),
            declared_model_name="claude-opus-4.6-web", preview_id="MFPV-CONFLICT",
        )
        task = self.store.get_task(self.task_id)
        self.service.approve_manual_frontier_preview(
            self.task_id, "MFPV-CONFLICT", expected_task_version=task.version
        )
        review_case = build_review_case(self.root, self.store, self.config, self.task_id)
        operation_store = ReviewOperationStore(self.root / ".apoapsis" / "review-operations.db")
        operation_store.create(
            "RVOP-BLOCKER", self.task_id, ReviewActionKind.VERIFICATION_ONLY_RETRY,
            expected_task_version=task.version,
            expected_worktree_fingerprint=review_case.worktree_fingerprint,
        )
        with self.assertRaises(ActiveOperationExistsError):
            self.service.submit_review_operation(
                self.task_id, action="manual_frontier_handoff", operation_id="RVOP-BLOCKED",
                expected_version=task.version,
                expected_worktree_fingerprint=review_case.worktree_fingerprint,
                manual_frontier_preview_id="MFPV-CONFLICT",
            )


class _PackageStub:
    """Adapter so ``_envelope`` can accept either the real
    ``ManualFrontierHandoffPackage`` or a plain dict from a service
    response's ``package`` field."""

    def __init__(self, payload: dict) -> None:
        self.package_id = payload["package_id"]
        self.package_sha256 = payload["package_sha256"]
        self.task_id = payload["task_id"]
        self.task_version = payload["task_version"]


class ManualFrontierUIServerTests(ManualFrontierUIServiceTests):
    def setUp(self) -> None:
        super().setUp()
        self.token = "deterministic-manual-frontier-session"
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

    def request(self, path: str, *, method: str = "GET", payload=None, token: str | None = None, origin: str | None = None):
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

    def test_export_requires_session(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as unauthorized:
            self.request(
                f"/api/reviews/{self.task_id}/manual-frontier/export", method="POST", payload={}
            )
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()

    def test_export_rejects_foreign_origin(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as forbidden:
            self.request(
                f"/api/reviews/{self.task_id}/manual-frontier/export",
                method="POST", payload={}, token=self.token, origin="https://evil.example",
            )
        self.assertEqual(forbidden.exception.code, 403)
        forbidden.exception.close()

    def test_http_export_import_approve_apply_reconnect(self) -> None:
        with self.request(
            f"/api/reviews/{self.task_id}/manual-frontier/export", method="POST",
            payload={}, token=self.token,
        ) as response:
            exported = json.load(response)
        package_id = exported["package"]["package_id"]
        with self.request(
            f"/api/reviews/{self.task_id}/manual-frontier/import", method="POST",
            payload={
                "package_id": package_id,
                "response_text": _envelope(_PackageStub(exported["package"]), patch=COMPLETE_PATCH),
                "declared_model_name": "claude-opus-4.6-web",
                "preview_id": "MFPV-HTTP-1",
            },
            token=self.token,
        ) as response:
            self.assertEqual(response.status, 200)

        task_version = self.store.get_task(self.task_id).version
        with self.request(
            f"/api/reviews/{self.task_id}/manual-frontier/previews/MFPV-HTTP-1/approve",
            method="POST", payload={"expected_version": task_version}, token=self.token,
        ) as response:
            self.assertEqual(response.status, 200)

        review_case = build_review_case(self.root, self.store, self.config, self.task_id)
        with self.request(
            f"/api/reviews/{self.task_id}/operations", method="POST",
            payload={
                "action": "manual_frontier_handoff",
                "operation_id": "RVOP-HTTP-APPLY-1",
                "expected_version": task_version,
                "expected_worktree_fingerprint": review_case.worktree_fingerprint,
                "manual_frontier_preview_id": "MFPV-HTTP-1",
            },
            token=self.token,
        ) as response:
            self.assertEqual(response.status, 202)

        deadline = time.monotonic() + 15
        record = None
        while time.monotonic() < deadline:
            with self.request(
                f"/api/reviews/{self.task_id}/operations/RVOP-HTTP-APPLY-1", token=self.token
            ) as response:
                record = json.load(response)
            if record["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.05)
        assert record is not None
        self.assertEqual(record["status"], "succeeded")

        # Reconnect: a fresh service instance reads the same persisted state.
        fresh_service = ApoapsisUIService(self.root)
        reloaded = fresh_service.manual_frontier_previews(self.task_id)
        self.assertEqual(reloaded["previews"][0]["status"], "applied")

    def test_static_asset_bundles_manual_frontier_actions(self) -> None:
        with self.request("/app.js", token=self.token) as response:
            content = response.read().decode("utf-8")
        self.assertIn("manual-frontier-export", content)
        self.assertIn("manual-frontier-import", content)
        self.assertIn("manual-frontier-approve-confirm", content)
        self.assertIn("manual-frontier-apply-confirm", content)
        self.assertIn("/manual-frontier/export", content)
        self.assertIn("Unmeasured", content.replace("UNMEASURED", "Unmeasured"))


if __name__ == "__main__":
    unittest.main()
