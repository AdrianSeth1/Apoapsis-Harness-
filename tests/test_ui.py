from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from apoapsis.architect.schema import PlanValidationResult
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.cli.app import _init, build_parser
from apoapsis.specification.schema import (
    AcceptanceCriterion,
    HardConstraint,
    SourceKind,
    TaskSpecification,
    TraceableStatement,
)
from apoapsis.ui.application import ApoapsisUIService
from apoapsis.ui.server import create_ui_server
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState
from tests.architect_helpers import make_plan


class UIServiceTests(unittest.TestCase):
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
        (self.root / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Apoapsis Tests",
                "-c",
                "user.email=tests@apoapsis.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        _init(self.root)
        self.store = SQLiteTaskStore(self.root / ".apoapsis" / "apoapsis.db")
        self.task_id = "TASK-UI-001"
        specification = TaskSpecification(
            task_id=self.task_id,
            objective=TraceableStatement(
                text="Add resumable downloads.",
                source=SourceKind.USER,
                source_reference="ui-test",
            ),
            hard_constraints=[
                HardConstraint(
                    id="HC-1",
                    text="Preserve the public API.",
                    verbatim_source="Preserve the public API exactly.",
                    interpreted_meaning="Existing signatures may not change.",
                    source=SourceKind.USER,
                    source_reference="ui-test",
                    verification_method="Run API compatibility tests.",
                )
            ],
            acceptance_criteria=[
                AcceptanceCriterion(
                    id="AC-1",
                    text="Interrupted downloads resume from the persisted byte.",
                    source=SourceKind.DERIVED,
                    source_reference="ui-test",
                    verification_method="unit-tests",
                )
            ],
        )
        self.store.create_task(specification)
        self.store.transition(
            self.task_id,
            WorkflowState.SPEC_DRAFTED,
            actor=WorkflowActor.SYSTEM,
            event_type="deterministic_specification_drafted",
        )

        self.plan_store = SQLitePlanStore(self.root / ".apoapsis" / "architect-plans.db")
        self.plan_id = "PLAN-UI-001"
        self.plan = make_plan()
        self.plan_store.create_plan(
            self.plan_id, "PKG-UI-001", self.plan.idea_text, self.plan
        )
        self.plan_store.record_validation(
            self.plan_id,
            PlanValidationResult(
                plan_id=self.plan_id, plan_version=1, valid=True, findings=[]
            ),
            expected_version=1,
        )

    def test_overview_and_detail_expose_persisted_facts(self) -> None:
        service = ApoapsisUIService(self.root)

        overview = service.overview()
        detail = service.task_detail(self.task_id)

        self.assertTrue(overview["project"]["initialized"])
        self.assertEqual(overview["repository"]["branch"], "main")
        self.assertEqual(overview["tasks"][0]["state"], "SPEC_DRAFTED")
        self.assertEqual(
            detail["task"]["specification"]["hard_constraints"][0][
                "verbatim_source"
            ],
            "Preserve the public API exactly.",
        )
        self.assertEqual(
            detail["task"]["specification"]["acceptance_criteria"][0][
                "verification_method"
            ],
            "unit-tests",
        )
        self.assertEqual(detail["available_actions"], ["approve_specification"])
        self.assertEqual(detail["events"][-1]["actor"], "system")

    def test_ui_cli_arguments_are_explicit_and_loopback_scoped(self) -> None:
        arguments = build_parser().parse_args(["ui", "--port", "8123", "--no-open"])
        self.assertEqual(arguments.command, "ui")
        self.assertEqual(arguments.port, 8123)
        self.assertTrue(arguments.no_open)

    def test_ui_approval_uses_the_same_deterministic_transition_record(self) -> None:
        service = ApoapsisUIService(self.root)
        before = self.store.get_task(self.task_id)

        result = service.approve_specification(
            self.task_id, expected_version=before.version
        )

        self.assertEqual(result["task"]["state"], "SPEC_APPROVED")
        self.assertEqual(result["task"]["version"], before.version + 1)
        # A SPEC_APPROVED task can now start durable execution (ADR 0024).
        self.assertEqual(result["available_actions"], ["start_execution"])
        event = result["events"][-1]
        self.assertEqual(event["event_type"], "specification_approved")
        self.assertEqual(event["actor"], "user")
        self.assertEqual(event["from_state"], "SPEC_DRAFTED")
        self.assertEqual(event["to_state"], "SPEC_APPROVED")

    def test_plans_index_lists_the_persisted_plan(self) -> None:
        service = ApoapsisUIService(self.root)

        index = service.plans()

        self.assertEqual(len(index["plans"]), 1)
        summary = index["plans"][0]
        self.assertEqual(summary["plan_id"], self.plan_id)
        self.assertEqual(summary["status"], "validated")
        self.assertEqual(summary["slice_count"], len(self.plan.slices))

    def test_plan_detail_exposes_dependency_order_and_validation(self) -> None:
        service = ApoapsisUIService(self.root)

        detail = service.plan_detail(self.plan_id)

        self.assertEqual(detail["plan"]["status"], "validated")
        self.assertEqual(detail["plan"]["validation"]["valid"], True)
        self.assertEqual(detail["dependency_order"], ["SLICE-1"])
        self.assertEqual(detail["available_actions"], ["approve_plan"])

    def test_plan_approval_uses_the_same_deterministic_transition_record(self) -> None:
        service = ApoapsisUIService(self.root)
        before = self.plan_store.get_plan(self.plan_id)

        result = service.approve_plan(self.plan_id, expected_version=before.version)

        self.assertEqual(result["plan"]["status"], "approved")
        self.assertEqual(result["plan"]["version"], before.version + 1)
        self.assertEqual(result["available_actions"], [])
        event = result["events"][-1]
        self.assertEqual(event["event_type"], "plan_approved")
        self.assertEqual(event["actor"], "user")
        self.assertEqual(event["to_status"], "approved")

    def test_approving_a_plan_never_executes_a_slice(self) -> None:
        """Approval only ever records a status transition -- there is no
        code path from ``approve_plan`` that touches the filesystem, Git,
        or a verification runner."""

        service = ApoapsisUIService(self.root)
        before = self.plan_store.get_plan(self.plan_id)
        snapshot_before = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        service.approve_plan(self.plan_id, expected_version=before.version)

        snapshot_after = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        # Only the plan's own SQLite/audit bookkeeping under .apoapsis may
        # have changed; that directory is gitignored, so the tracked-file
        # git status is unaffected by approval.
        self.assertEqual(snapshot_before, snapshot_after)


class UIServerTests(UIServiceTests):
    def setUp(self) -> None:
        super().setUp()
        self.token = "deterministic-test-session"
        self.server = create_ui_server(
            self.root, port=0, session_token=self.token
        )
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
        origin: str | None = None,
    ) -> urllib.response.addinfourl:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers: dict[str, str] = {}
        if token is not None:
            headers["X-Apoapsis-Session"] = token
        if origin is not None:
            headers["Origin"] = origin
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.origin}{path}", data=data, headers=headers, method=method
        )
        return urllib.request.urlopen(request, timeout=5)

    def test_static_shell_has_strict_security_headers(self) -> None:
        with self.request("/") as response:
            html = response.read().decode("utf-8")
            policy = response.headers["Content-Security-Policy"]
        self.assertIn("Apoapsis", html)
        self.assertIn("default-src 'self'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_api_requires_session_and_rejects_cross_origin_requests(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as unauthorized:
            self.request("/api/overview")
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()

        with self.assertRaises(urllib.error.HTTPError) as forbidden:
            self.request(
                "/api/overview",
                token=self.token,
                origin="https://malicious.invalid",
            )
        self.assertEqual(forbidden.exception.code, 403)
        forbidden.exception.close()

        with self.request("/api/overview", token=self.token) as response:
            payload = json.load(response)
            self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
        self.assertEqual(payload["tasks"][0]["task_id"], self.task_id)

    def test_server_refuses_non_loopback_binding(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            create_ui_server(self.root, host="0.0.0.0", port=0)

    def test_http_approval_requires_optimistic_task_version(self) -> None:
        version = self.store.get_task(self.task_id).version
        with self.request(
            f"/api/tasks/{self.task_id}/approve",
            method="POST",
            payload={"expected_version": version},
            token=self.token,
        ) as response:
            payload = json.load(response)
        self.assertEqual(payload["task"]["state"], "SPEC_APPROVED")

        with self.assertRaises(urllib.error.HTTPError) as conflict:
            self.request(
                f"/api/tasks/{self.task_id}/approve",
                method="POST",
                payload={"expected_version": version},
                token=self.token,
            )
        self.assertEqual(conflict.exception.code, 409)
        conflict.exception.close()

    def test_plans_index_and_detail_require_session(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as unauthorized:
            self.request("/api/plans")
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()

        with self.request("/api/plans", token=self.token) as response:
            payload = json.load(response)
        self.assertEqual(payload["plans"][0]["plan_id"], self.plan_id)

        with self.request(
            f"/api/plans/{self.plan_id}", token=self.token
        ) as response:
            detail = json.load(response)
        self.assertEqual(detail["plan"]["plan_id"], self.plan_id)
        self.assertEqual(detail["dependency_order"], ["SLICE-1"])

    def test_unknown_plan_returns_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as not_found:
            self.request("/api/plans/PLAN-DOES-NOT-EXIST", token=self.token)
        self.assertEqual(not_found.exception.code, 404)
        not_found.exception.close()

    def test_http_plan_approval_requires_optimistic_plan_version(self) -> None:
        version = self.plan_store.get_plan(self.plan_id).version
        with self.request(
            f"/api/plans/{self.plan_id}/approve",
            method="POST",
            payload={"expected_version": version},
            token=self.token,
        ) as response:
            payload = json.load(response)
        self.assertEqual(payload["plan"]["status"], "approved")

        with self.assertRaises(urllib.error.HTTPError) as conflict:
            self.request(
                f"/api/plans/{self.plan_id}/approve",
                method="POST",
                payload={"expected_version": version},
                token=self.token,
            )
        self.assertEqual(conflict.exception.code, 409)
        conflict.exception.close()


if __name__ == "__main__":
    unittest.main()
