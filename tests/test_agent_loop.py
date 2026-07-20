from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from apoapsis.agent.actions import (
    AgentActionError,
    agent_action_schema,
    parse_agent_action,
)
from apoapsis.agent.session import compact_observations
from apoapsis.agent.inspection import AgentInspectionError, RepositoryInspector
from apoapsis.config import (
    AgentLoopConfig,
    AgentRoute,
    ContextCompilerConfig,
    ExecutionConfig,
    ExecutionMode,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ApoapsisConfig,
)
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.models.provider import ProviderError
from apoapsis.models.prompts import (
    agent_step_prompt,
    implementation_prompt,
    prompt_static_prefix,
    rejected_patch_repair_prompt,
    repair_prompt,
)
from apoapsis.context.compiler import ContextPackage
from apoapsis.context.provenance import (
    ContextEvidence,
    EvidenceKind,
    TransmissionPolicy,
)
from apoapsis.reporting.report import TaskOutcome
from apoapsis.specification.schema import RiskLevel
from apoapsis.verification.results import VerificationCommandResult, VerificationStatus
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.states import WorkflowState
from apoapsis.workflow.routing import select_agent_route
from apoapsis.workflow.vertical_slice import VerticalSliceRunner
from tests.fakes import FakeModelProvider
from tests.helpers import make_specification
from tests.test_vertical_slice import (
    COMPLETE_PATCH,
    IMPLEMENTATION_PATCH,
    REQUEST,
    specification_response,
)


def action(name: str, **values: object) -> str:
    return json.dumps({"action": name, **values})


def specification_with_risk(risk: str) -> str:
    payload = json.loads(specification_response())
    payload["risk_level"] = risk
    return json.dumps(payload)


class AgentActionTests(unittest.TestCase):
    def test_wire_schema_avoids_provider_specific_union_features(self) -> None:
        schema = agent_action_schema()
        self.assertEqual(schema["type"], "object")
        self.assertNotIn("oneOf", schema)
        self.assertNotIn("$defs", schema)

    def test_parser_accepts_one_typed_action_and_rejects_extra_authority(self) -> None:
        parsed = parse_agent_action(
            action(
                "read_file",
                path="src/service.py",
                start_line=10,
                end_line=30,
            )
        )
        self.assertEqual(parsed.action, "read_file")
        with self.assertRaises(AgentActionError):
            parse_agent_action(
                action("inspect_diff", shell_command="pytest -q")
            )

    def test_prompt_prefix_is_byte_stable_across_agent_turns(self) -> None:
        specification = make_specification()
        context = ContextPackage.specification_only(specification, "deadbeef")
        first = agent_step_prompt(
            context,
            turn=1,
            remaining_budgets={"turns": 2},
            verification_commands=["tests"],
            history=[],
        )
        second = agent_step_prompt(
            context,
            turn=2,
            remaining_budgets={"turns": 1},
            verification_commands=["tests"],
            history=[{"action": "inspect_diff"}],
        )
        prefix = prompt_static_prefix("agent_step")
        self.assertTrue(first.startswith(prefix))
        self.assertTrue(second.startswith(prefix))
        self.assertEqual(
            first[: len(prefix)].encode("utf-8"),
            second[: len(prefix)].encode("utf-8"),
        )
        failing = VerificationCommandResult(
            name="tests",
            category="tests",
            argv=["python", "-m", "unittest"],
            cwd=".",
            status=VerificationStatus.FAILED,
            duration_seconds=0,
        )
        prompts = {
            "implementation": implementation_prompt(context),
            "repair": repair_prompt(context, failing, "boom", "diff --git"),
            "rejected_patch_repair": rejected_patch_repair_prompt(
                context, "bad diff", "rejected"
            ),
        }
        for kind, prompt in prompts.items():
            self.assertTrue(prompt.startswith(prompt_static_prefix(kind)))

    def test_observation_compaction_keeps_latest_slots_and_failure(self) -> None:
        def evidence(
            identifier: str,
            *,
            content: str,
            kind: EvidenceKind = EvidenceKind.FILE_EXCERPT,
            path: str = "src/service.py",
        ) -> ContextEvidence:
            return ContextEvidence(
                evidence_id=identifier,
                kind=kind,
                path=path,
                start_line=None if path.startswith("<") else 1,
                end_line=None if path.startswith("<") else 10,
                commit="deadbeef",
                reason_included="test",
                content=content,
                transmission_policy=TransmissionPolicy.CLOUD_ALLOWED,
            )

        observations = [
            evidence("EV-OLD", content="old source" * 30),
            evidence("EV-NEW", content="new source" * 30),
            evidence(
                "EV-DIFF-OLD",
                path="<working-tree-diff>",
                kind=EvidenceKind.DIFF,
                content="old diff" * 20,
            ),
            evidence(
                "EV-DIFF-NEW",
                path="<working-tree-diff>",
                kind=EvidenceKind.DIFF,
                content="new diff" * 20,
            ),
            evidence(
                "EV-FAIL",
                path="<verification:tests>",
                kind=EvidenceKind.FAILURE,
                content="root failure" * 20,
            ),
        ]

        compacted = compact_observations(observations, max_chars=700)
        identifiers = {item.evidence_id for item in compacted}

        self.assertIn("EV-FAIL", identifiers)
        self.assertIn("EV-DIFF-NEW", identifiers)
        self.assertNotIn("EV-DIFF-OLD", identifiers)
        self.assertNotIn("EV-OLD", identifiers)
        self.assertIn("EV-NEW", identifiers)
        self.assertLessEqual(sum(len(item.content) for item in compacted), 700)


class DeterministicRoutingTests(unittest.TestCase):
    def test_auto_route_uses_risk_and_frontier_availability(self) -> None:
        execution = ExecutionConfig(
            mode=ExecutionMode.AGENT,
            route=AgentRoute.AUTO,
        )
        medium = make_specification().model_copy(
            update={"risk_level": RiskLevel.MEDIUM}
        )
        high = medium.model_copy(update={"risk_level": RiskLevel.HIGH})
        critical = medium.model_copy(update={"risk_level": RiskLevel.CRITICAL})

        self.assertEqual(
            select_agent_route(
                medium, execution, frontier_available=True
            ).route,
            AgentRoute.LOCAL_THEN_FRONTIER,
        )
        self.assertEqual(
            select_agent_route(
                medium, execution, frontier_available=False
            ).route,
            AgentRoute.LOCAL_ONLY,
        )
        self.assertEqual(
            select_agent_route(high, execution, frontier_available=True).route,
            AgentRoute.FRONTIER_ONLY,
        )
        self.assertEqual(
            select_agent_route(
                critical, execution, frontier_available=True
            ).route,
            AgentRoute.HUMAN_REVIEW_REQUIRED,
        )


class BoundedAgentIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "download-service"
        example = (
            Path(__file__).resolve().parents[1]
            / "examples"
            / "download-service"
        )
        shutil.copytree(example, self.root)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Apoapsis Tests")
        self._git("add", ".")
        self._git("commit", "-m", "controlled baseline")
        (self.root / ".apoapsis").mkdir()
        self.store = SQLiteTaskStore(self.root / ".apoapsis" / "apoapsis.db")

    def _git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )

    def _config(
        self,
        *,
        route: AgentRoute = AgentRoute.AUTO,
        frontier_coder: FrontierProviderConfig | None = None,
        frontier_turns: int = 8,
    ) -> ApoapsisConfig:
        return ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1",
                    model="fake-coder-v1",
                ),
                frontier_coder=frontier_coder,
            ),
            execution=ExecutionConfig(
                mode=ExecutionMode.AGENT,
                route=route,
                agent=AgentLoopConfig(
                    max_turns=8,
                    max_patch_attempts=3,
                    max_verification_runs=3,
                    max_search_results=10,
                    max_read_lines=120,
                    max_observation_chars=20_000,
                ),
                frontier_agent=AgentLoopConfig(
                    max_turns=frontier_turns,
                    max_patch_attempts=3,
                    max_verification_runs=3,
                    max_search_results=10,
                    max_read_lines=120,
                    max_observation_chars=20_000,
                ),
            ),
            context=ContextCompilerConfig(
                max_files=10,
                max_excerpt_lines=200,
                max_total_chars=50_000,
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

    @staticmethod
    def _inject_task_id(fake: FakeModelProvider) -> None:
        original_complete = fake.complete

        def complete(invocation):
            output = original_complete(invocation)
            if len(fake.invocations) == 1:
                task_id = invocation.prompt.split(
                    'task_id to "', 1
                )[1].split('"', 1)[0]
                raw = json.loads(output.content)
                raw["task_id"] = task_id
                return output.model_copy(update={"content": json.dumps(raw)})
            return output

        fake.complete = complete  # type: ignore[method-assign]

    def test_agent_inspects_patches_tests_repairs_and_completes(self) -> None:
        fake = FakeModelProvider(
            [
                specification_response(),
                action("search_repository", query="get_offset"),
                action(
                    "read_file",
                    path="src/download_service/downloader.py",
                    start_line=1,
                    end_line=120,
                ),
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
        )
        self._inject_task_id(fake)
        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(fake),
            self._config(),
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(report.execution_mode, ExecutionMode.AGENT)
        self.assertEqual(report.number_of_calls, 8)
        self.assertEqual(report.agent_turns, 7)
        self.assertEqual(report.agent_patch_attempts, 2)
        self.assertEqual(report.agent_verification_runs, 2)
        self.assertEqual(len(report.verification_results), 2)
        self.assertEqual(report.verification_results[0].status, "failed")
        self.assertEqual(report.verification_results[1].status, "passed")
        self.assertTrue(
            all(
                invocation.response_schema is not None
                for invocation in fake.invocations[1:]
            )
        )
        task = self.store.get_task(report.task_id)
        self.assertEqual(task.state, WorkflowState.COMPLETE)
        events = [item.event_type for item in self.store.events(report.task_id)]
        self.assertIn("local_agent_patch_ready", events)
        self.assertIn("local_agent_verification_passed", events)

        audit = self.root / ".apoapsis" / "tasks" / report.task_id
        self.assertTrue((audit / "agent-session.json").is_file())
        self.assertTrue((audit / "agent-turn-007.json").is_file())
        self.assertTrue((audit / "verification-failure-001.json").is_file())
        final_turn = json.loads(
            (audit / "agent-turn-007.json").read_text(encoding="utf-8")
        )
        self.assertGreater(len(final_turn["observation_ledger"]), 0)
        self.assertGreater(final_turn["observation_ledger_chars"], 0)
        turn_after_failure = json.loads(
            (audit / "call-007-request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            turn_after_failure["provider_invocation"]["role"],
            "LOCAL_CODING_AGENT",
        )
        self.assertEqual(
            turn_after_failure["provider_invocation"]["response_schema"]["type"],
            "object",
        )
        self.assertIn("test_server_ignores_range", turn_after_failure["prompt"])
        for constraint in task.specification.hard_constraints:
            self.assertIn(
                constraint.verbatim_source, turn_after_failure["prompt"]
            )
        # After the session patched downloader.py, its compile-time excerpt
        # must be transmitted labeled as a pre-edit copy, never as an
        # unlabeled stale duplicate of the fresh post-edit content (the
        # D4c-deferred stale-evidence defect).
        self.assertIn(
            "[STALE: compiled before this session patched",
            turn_after_failure["prompt"],
        )
        first_request = json.loads(
            (audit / "call-002-request.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("[STALE:", first_request["prompt"])

    def test_explicit_escalation_stops_without_frontier_fallback(self) -> None:
        fake = FakeModelProvider(
            [
                specification_response(),
                action(
                    "request_escalation",
                    reason="The change requires an unapproved dependency.",
                ),
            ]
        )
        self._inject_task_id(fake)
        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(fake),
            self._config(),
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)
        self.assertEqual(report.agent_turns, 1)
        self.assertIn("requires escalation", report.error or "")
        self.assertEqual(
            self.store.get_task(report.task_id).state,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
        )

    def test_local_failure_escalates_to_frontier_repair_on_same_worktree(self) -> None:
        frontier_config = FrontierProviderConfig(
            base_url="https://frontier.invalid/v1",
            model="big-coder-v1",
        )
        local = FakeModelProvider(
            [
                specification_response(),
                action("propose_patch", unified_diff=IMPLEMENTATION_PATCH),
                action("run_check", command_name="download-tests"),
                action(
                    "request_escalation",
                    reason="The remaining server-ignore failure needs stronger review.",
                ),
            ],
            provider_name="fake_local",
            model_name="small-coder-v1",
        )
        frontier = FakeModelProvider(
            [
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
            ],
            provider_name="fake_hosted",
            model_name="big-coder-v1",
        )
        self._inject_task_id(local)
        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(local),
            self._config(
                route=AgentRoute.LOCAL_THEN_FRONTIER,
                frontier_coder=frontier_config,
            ),
            frontier_coder_provider=InstrumentedModelProvider(frontier),
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(report.agent_route, AgentRoute.LOCAL_THEN_FRONTIER)
        self.assertTrue(report.escalation_triggered)
        self.assertEqual(report.local_agent_turns, 3)
        self.assertEqual(report.frontier_agent_turns, 2)
        self.assertEqual(report.agent_turns, 5)
        self.assertEqual(report.agent_patch_attempts, 2)
        self.assertEqual(report.agent_verification_runs, 2)
        self.assertEqual(report.number_of_calls, 6)
        self.assertEqual(
            {(item.provider, item.model) for item in report.models_used},
            {
                ("fake_local", "small-coder-v1"),
                ("fake_hosted", "big-coder-v1"),
            },
        )
        self.assertEqual(report.verification_results[-1].status, "passed")

        audit = self.root / ".apoapsis" / "tasks" / report.task_id
        package = json.loads(
            (audit / "frontier-escalation-package.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(package["frontier_model"], "big-coder-v1")
        self.assertEqual(len(package["normalized_failures"]), 1)
        self.assertIn("mode =", package["current_diff"])
        self.assertEqual(
            {
                item["verbatim_source"]
                for item in package["active_constraints"]
            },
            {
                item["verbatim_source"]
                for item in package["specification"]["hard_constraints"]
                if item["status"] == "active"
            },
        )
        frontier_request = json.loads(
            (audit / "call-005-request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            frontier_request["provider_invocation"]["role"],
            "FRONTIER_CODING_AGENT",
        )
        self.assertIn("test_server_ignores_range", frontier_request["prompt"])
        self.assertTrue((audit / "patch-002.diff").is_file())
        self.assertTrue((audit / "frontier-verification-001.json").is_file())
        events = [item.event_type for item in self.store.events(report.task_id)]
        self.assertIn("bounded_frontier_escalation_started", events)
        self.assertIn("frontier_agent_verification_passed", events)

    def test_frontier_budget_exhaustion_requires_human_review(self) -> None:
        frontier_config = FrontierProviderConfig(
            base_url="https://frontier.invalid/v1",
            model="big-coder-v1",
        )
        local = FakeModelProvider(
            [
                specification_response(),
                action("request_escalation", reason="Need frontier reasoning."),
            ],
            provider_name="fake_local",
            model_name="small-coder-v1",
        )
        frontier = FakeModelProvider(
            [json.dumps({}), json.dumps({})],
            provider_name="fake_hosted",
            model_name="big-coder-v1",
        )
        self._inject_task_id(local)
        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(local),
            self._config(
                route=AgentRoute.LOCAL_THEN_FRONTIER,
                frontier_coder=frontier_config,
                frontier_turns=2,
            ),
            frontier_coder_provider=InstrumentedModelProvider(frontier),
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)
        self.assertEqual(report.local_agent_turns, 1)
        self.assertEqual(report.frontier_agent_turns, 2)
        self.assertEqual(
            self.store.get_task(report.task_id).state,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
        )

    def test_local_provider_failure_is_audited_and_escalated(self) -> None:
        frontier_config = FrontierProviderConfig(
            base_url="https://frontier.invalid/v1",
            model="big-coder-v1",
        )
        local = FakeModelProvider(
            [
                specification_response(),
                ProviderError("local model became unavailable"),
            ],
            provider_name="fake_local",
            model_name="small-coder-v1",
        )
        frontier = FakeModelProvider(
            [
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
            ],
            provider_name="fake_hosted",
            model_name="big-coder-v1",
        )
        self._inject_task_id(local)
        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(local),
            self._config(
                route=AgentRoute.LOCAL_THEN_FRONTIER,
                frontier_coder=frontier_config,
            ),
            frontier_coder_provider=InstrumentedModelProvider(frontier),
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertTrue(report.escalation_triggered)
        self.assertEqual(report.local_agent_turns, 0)
        self.assertEqual(report.frontier_agent_turns, 2)
        self.assertFalse(report.provider_calls[1].succeeded)
        self.assertIn("provider call failed", report.escalation_reason or "")
        audit = self.root / ".apoapsis" / "tasks" / report.task_id
        package = json.loads(
            (audit / "frontier-escalation-package.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("provider call failed", package["trigger"])

    def test_high_risk_auto_route_skips_local_coding(self) -> None:
        frontier_config = FrontierProviderConfig(
            base_url="https://frontier.invalid/v1",
            model="big-coder-v1",
        )
        specification_provider = FakeModelProvider(
            [specification_with_risk("high")],
            provider_name="fake_local",
            model_name="small-coder-v1",
        )
        frontier = FakeModelProvider(
            [
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
            ],
            provider_name="fake_hosted",
            model_name="big-coder-v1",
        )
        self._inject_task_id(specification_provider)
        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(specification_provider),
            self._config(
                route=AgentRoute.AUTO,
                frontier_coder=frontier_config,
            ),
            frontier_coder_provider=InstrumentedModelProvider(frontier),
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(report.agent_route, AgentRoute.FRONTIER_ONLY)
        self.assertEqual(report.local_agent_turns, 0)
        self.assertEqual(report.frontier_agent_turns, 2)
        self.assertFalse(report.escalation_triggered)
        self.assertIsNone(report.escalation_package_path)

    def test_repository_inspector_rejects_path_escape(self) -> None:
        inspector = RepositoryInspector(
            self.root,
            max_search_results=5,
            max_read_lines=50,
            max_chars=5_000,
        )
        with self.assertRaises(AgentInspectionError):
            inspector.read("../outside.py")
        with self.assertRaises(AgentInspectionError):
            inspector.read(".apoapsis/config.toml")
        with self.assertRaises(AgentInspectionError):
            inspector.read(".sol/config.toml")

    def test_search_falls_back_to_lexical_scan_when_ripgrep_is_missing(self) -> None:
        """A missing ripgrep binary (the D4c `[WinError 2]` defect) must
        degrade to the deterministic pure-Python scan, not surface a raw
        OSError to the model as an opaque failed action."""

        inspector = RepositoryInspector(
            self.root,
            max_search_results=5,
            max_read_lines=50,
            max_chars=5_000,
            ripgrep_executable="apoapsis-no-such-ripgrep",
        )
        results = inspector.search("downloaded")
        self.assertTrue(results)
        self.assertTrue(
            all("lexical fallback" in item.reason_included for item in results)
        )
        self.assertTrue(
            any("downloader.py" in item.path for item in results)
        )
        self.assertLessEqual(len(results), 5)
        # The glob filter must still apply in the fallback path.
        globbed = inspector.search("downloaded", path_glob="src/*/*.py")
        self.assertTrue(globbed)
        self.assertTrue(all(item.path.startswith("src/") for item in globbed))

    def test_replace_text_requires_one_exact_match_and_generates_diff(self) -> None:
        inspector = RepositoryInspector(
            self.root,
            max_search_results=5,
            max_read_lines=50,
            max_chars=5_000,
        )
        patch = inspector.replacement_patch(
            "src/download_service/downloader.py",
            "        downloaded = 0",
            "        downloaded = 1\n        \n        downloaded += 1",
        )
        self.assertTrue(patch.startswith("diff --git"))
        self.assertIn("-        downloaded = 0", patch)
        self.assertIn("+        downloaded = 1", patch)
        self.assertNotIn("+        \n", patch)
        with self.assertRaisesRegex(AgentInspectionError, "found 0 matches"):
            inspector.replacement_patch(
                "src/download_service/downloader.py",
                "not present",
                "replacement",
            )


if __name__ == "__main__":
    unittest.main()
