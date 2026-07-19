from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from apoapsis.architect.slice_service import package_slice
from apoapsis.ui.application import ApoapsisUIService
from apoapsis.ui.server import create_ui_server
from apoapsis.workflow.states import WorkflowState
from tests.test_architect_slice import PlanSliceExecutionTestsBase
from tests.architect_helpers import make_slice


class PlanSliceUITestsBase(PlanSliceExecutionTestsBase):
    def setUp(self) -> None:
        super().setUp()
        self.config_path = self.root / ".apoapsis" / "config.toml"
        self.config_path.write_text(
            f"""
[models.frontier]
provider = "openai_compatible"
base_url = "https://provider.invalid/v1"
model = "fake-coder-v1"

[context]
max_files = 10
max_excerpt_lines = 200
max_total_chars = 50000

[patch]
max_changed_lines = 100

[[verification.commands]]
name = "unit-tests"
category = "tests"
argv = ["{sys.executable.replace(chr(92), "/")}", "-m", "unittest", "discover", "-s", "tests", "-v"]
timeout_seconds = 30
""",
            encoding="utf-8",
        )
        self.service = ApoapsisUIService(self.root)


class PlanSliceUIServiceTests(PlanSliceUITestsBase):
    def test_plan_detail_exposes_live_slice_statuses(self) -> None:
        record, _config = self._approved_plan()
        detail = self.service.plan_detail(record.plan_id)
        self.assertEqual(len(detail["slices"]), 1)
        self.assertEqual(detail["slices"][0]["slice_id"], "SLICE-1")
        self.assertEqual(detail["slices"][0]["status"], "ready_or_blocked")
        self.assertIsNone(detail["slices"][0]["record"])

    def test_plan_slice_detail_before_packaging(self) -> None:
        record, _config = self._approved_plan()
        detail = self.service.plan_slice_detail(record.plan_id, "SLICE-1")
        self.assertEqual(detail["status"]["status"], "ready_or_blocked")
        self.assertIsNone(detail["package"])
        self.assertIsNone(detail["task"])
        self.assertEqual(detail["slice"]["slice_id"], "SLICE-1")

    def test_package_plan_slice_through_the_service(self) -> None:
        record, _config = self._approved_plan()
        package = self.service.package_plan_slice(
            record.plan_id, "SLICE-1", expected_plan_version=record.version
        )
        self.assertTrue(package["package_sha256"])
        detail = self.service.plan_slice_detail(record.plan_id, "SLICE-1")
        self.assertEqual(detail["status"]["status"], "packaged")
        self.assertEqual(detail["package"]["package_sha256"], package["package_sha256"])
        plan_detail = self.service.plan_detail(record.plan_id)
        self.assertEqual(plan_detail["slices"][0]["status"], "packaged")

    def test_approve_plan_slice_creates_the_derived_task(self) -> None:
        record, config = self._approved_plan()
        package = package_slice(
            self.root,
            self.plan_store,
            self.slice_store,
            self.task_store,
            self.operation_store,
            record.plan_id,
            "SLICE-1",
            expected_plan_version=record.version,
            config=config,
        )
        approved = self.service.approve_plan_slice(
            record.plan_id, "SLICE-1", expected_package_sha256=package.package_sha256
        )
        self.assertEqual(approved["status"], "approved")
        self.assertIsNotNone(approved["task_id"])
        task = self.task_store.get_task(approved["task_id"])
        self.assertEqual(task.state, WorkflowState.SPEC_APPROVED)

        detail = self.service.plan_slice_detail(record.plan_id, "SLICE-1")
        self.assertEqual(detail["status"]["status"], "approved")
        self.assertIsNotNone(detail["task"])
        self.assertIn("start_execution", detail["task"]["available_actions"])

    def test_package_hash_mismatch_at_approval_is_rejected(self) -> None:
        record, config = self._approved_plan()
        self.service.package_plan_slice(
            record.plan_id, "SLICE-1", expected_plan_version=record.version
        )
        with self.assertRaises(Exception):
            self.service.approve_plan_slice(
                record.plan_id, "SLICE-1", expected_package_sha256="0" * 64
            )

    def test_dependency_reasons_are_surfaced_before_packaging(self) -> None:
        base = make_slice(slice_id="SLICE-1")
        dependent = make_slice(slice_id="SLICE-2", dependencies=["SLICE-1"])
        record, _config = self._approved_plan(slices=[base, dependent])
        detail = self.service.plan_slice_detail(record.plan_id, "SLICE-2")
        self.assertEqual(detail["slice"]["dependencies"], ["SLICE-1"])
        self.assertIsNone(detail["package"])


class PlanSliceUIServerTests(PlanSliceUITestsBase):
    def setUp(self) -> None:
        super().setUp()
        self.token = "deterministic-plan-slice-session"
        self.server = create_ui_server(self.root, port=0, session_token=self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

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
            f"{self.server.origin}{path}", data=data, headers=headers, method=method
        )
        return urllib.request.urlopen(request, timeout=10)

    def test_slice_routes_require_session(self) -> None:
        record, _config = self._approved_plan()
        with self.assertRaises(urllib.error.HTTPError) as unauthorized:
            self.request(f"/api/plans/{record.plan_id}/slices/SLICE-1")
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()

    def test_full_http_lifecycle_package_then_approve(self) -> None:
        record, _config = self._approved_plan()
        with self.request(
            f"/api/plans/{record.plan_id}/slices/SLICE-1",
            token=self.token,
        ) as response:
            detail = json.load(response)
        self.assertEqual(detail["status"]["status"], "ready_or_blocked")

        with self.request(
            f"/api/plans/{record.plan_id}/slices/SLICE-1/package",
            method="POST",
            payload={"expected_plan_version": record.version},
            token=self.token,
        ) as response:
            package = json.load(response)
        self.assertEqual(response.status, 200)

        with self.request(
            f"/api/plans/{record.plan_id}/slices/SLICE-1/approve",
            method="POST",
            payload={"expected_package_sha256": package["package_sha256"]},
            token=self.token,
        ) as response:
            approved = json.load(response)
        self.assertEqual(approved["status"], "approved")

        with self.request(
            f"/api/plans/{record.plan_id}", token=self.token
        ) as response:
            plan_detail = json.load(response)
        self.assertEqual(plan_detail["slices"][0]["status"], "approved")

    def test_http_package_hash_mismatch_returns_409(self) -> None:
        record, _config = self._approved_plan()
        with self.request(
            f"/api/plans/{record.plan_id}/slices/SLICE-1/package",
            method="POST",
            payload={"expected_plan_version": record.version},
            token=self.token,
        ) as response:
            json.load(response)
        with self.assertRaises(urllib.error.HTTPError) as conflict:
            self.request(
                f"/api/plans/{record.plan_id}/slices/SLICE-1/approve",
                method="POST",
                payload={"expected_package_sha256": "0" * 64},
                token=self.token,
            )
        self.assertEqual(conflict.exception.code, 409)
        conflict.exception.close()

    def test_http_unknown_slice_returns_400(self) -> None:
        record, _config = self._approved_plan()
        with self.assertRaises(urllib.error.HTTPError) as not_found:
            self.request(
                f"/api/plans/{record.plan_id}/slices/SLICE-DOES-NOT-EXIST",
                token=self.token,
            )
        self.assertEqual(not_found.exception.code, 400)
        not_found.exception.close()

    def test_http_missing_fields_returns_400(self) -> None:
        record, _config = self._approved_plan()
        with self.assertRaises(urllib.error.HTTPError) as bad_request:
            self.request(
                f"/api/plans/{record.plan_id}/slices/SLICE-1/package",
                method="POST",
                payload={},
                token=self.token,
            )
        self.assertEqual(bad_request.exception.code, 400)
        bad_request.exception.close()

    def test_static_asset_bundles_slice_actions(self) -> None:
        with self.request("/app.js", token=self.token) as response:
            content = response.read().decode("utf-8")
        self.assertIn("slice-package", content)
        self.assertIn("slice-approve-confirm", content)
        self.assertIn("/slices/", content)


if __name__ == "__main__":
    unittest.main()
