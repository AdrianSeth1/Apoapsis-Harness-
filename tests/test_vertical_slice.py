from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sol.config import (
    ContextCompilerConfig,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ProviderPricing,
    SolConfig,
)
from sol.models.local import OllamaProvider
from sol.models.telemetry import InstrumentedModelProvider
from sol.reporting.report import TaskOutcome
from sol.verification.runner import VerificationCommand, VerificationConfig
from sol.workflow.engine import SQLiteTaskStore
from sol.workflow.states import WorkflowState
from sol.workflow.vertical_slice import VerticalSliceRunner
from tests.fakes import FakeModelProvider


REQUEST = """Add resumable downloads.
Preserve the current public API.
Do not add runtime dependencies.
Existing clients must continue working."""


def specification_response() -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "task_id": "TASK-PLACEHOLDER",
            "objective": {
                "text": "Add resumable downloads.",
                "source": "user",
                "source_reference": "cli-request",
            },
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "text": "Interrupted downloads resume from the persisted byte.",
                    "source": "derived",
                    "source_reference": "cli-request",
                    "status": "active",
                },
                {
                    "id": "AC-2",
                    "text": "A server that ignores Range replaces partial data.",
                    "source": "derived",
                    "source_reference": "cli-request",
                    "status": "active",
                },
            ],
            "hard_constraints": [
                {
                    "id": "HC-1",
                    "text": "Keep the public API unchanged.",
                    "verbatim_source": "Preserve the current public API.",
                    "interpreted_meaning": "Do not change public signatures.",
                    "source": "user",
                    "source_reference": "cli-request",
                    "scope": "task",
                    "status": "active",
                    "verification_method": "Run the existing API tests.",
                },
                {
                    "id": "HC-2",
                    "text": "Do not add runtime packages.",
                    "verbatim_source": "Do not add runtime dependencies.",
                    "interpreted_meaning": "Leave dependency manifests unchanged.",
                    "source": "user",
                    "source_reference": "cli-request",
                    "scope": "task",
                    "status": "active",
                    "verification_method": "Patch policy protects manifests.",
                },
                {
                    "id": "HC-3",
                    "text": "Retain existing-client behavior.",
                    "verbatim_source": "Existing clients must continue working.",
                    "interpreted_meaning": "Existing download behavior remains valid.",
                    "source": "user",
                    "source_reference": "cli-request",
                    "scope": "task",
                    "status": "active",
                    "verification_method": "Run the existing test suite.",
                },
            ],
            "requested_output": "unified_diff",
            "verification_requirements": ["python -m unittest discover -s tests -v"],
            "risk_level": "medium",
        }
    )


IMPLEMENTATION_PATCH = (
    "diff --git a/src/download_service/downloader.py "
    "b/src/download_service/downloader.py\n"
    """--- a/src/download_service/downloader.py
+++ b/src/download_service/downloader.py
@@ -8,13 +8,17 @@ class Downloader:
         self.transport = transport
         self.jobs = jobs
 
     def download(self, url: str, destination: Path) -> int:
-        response = self.transport.get(url, headers={})
+        offset = self.jobs.get_offset(url)
+        headers = {"Range": f"bytes={offset}-"} if offset else {}
+        response = self.transport.get(url, headers=headers)
         destination.parent.mkdir(parents=True, exist_ok=True)
-        downloaded = 0
-        with destination.open("wb") as handle:
+        mode = "ab" if offset else "wb"
+        downloaded = offset
+        with destination.open(mode) as handle:
             for chunk in response.iter_chunks():
                 handle.write(chunk)
                 downloaded += len(chunk)
                 self.jobs.set_offset(url, downloaded)
         return downloaded
"""
)


REPAIR_PATCH = (
    "diff --git a/src/download_service/downloader.py "
    "b/src/download_service/downloader.py\n"
    """--- a/src/download_service/downloader.py
+++ b/src/download_service/downloader.py
@@ -13,8 +13,9 @@ class Downloader:
         headers = {"Range": f"bytes={offset}-"} if offset else {}
         response = self.transport.get(url, headers=headers)
         destination.parent.mkdir(parents=True, exist_ok=True)
-        mode = "ab" if offset else "wb"
-        downloaded = offset
+        should_append = offset > 0 and response.status_code == 206
+        mode = "ab" if should_append else "wb"
+        downloaded = offset if should_append else 0
         with destination.open(mode) as handle:
             for chunk in response.iter_chunks():
                 handle.write(chunk)
"""
)


NON_APPLYING_PATCH = (
    "diff --git a/src/download_service/downloader.py "
    "b/src/download_service/downloader.py\n"
    "--- a/src/download_service/downloader.py\n"
    "+++ b/src/download_service/downloader.py\n"
    "@@ -1 +1 @@\n"
    "-this line is not in the repository\n"
    "+replacement\n"
)


COMPLETE_PATCH = (
    "diff --git a/src/download_service/downloader.py "
    "b/src/download_service/downloader.py\n"
    "--- a/src/download_service/downloader.py\n"
    "+++ b/src/download_service/downloader.py\n"
    "@@ -9,13 +9,18 @@ class Downloader:\n"
    "         self.jobs = jobs\n"
    " \n"
    "     def download(self, url: str, destination: Path) -> int:\n"
    "-        response = self.transport.get(url, headers={})\n"
    "+        offset = self.jobs.get_offset(url)\n"
    '+        headers = {"Range": f"bytes={offset}-"} if offset else {}\n'
    "+        response = self.transport.get(url, headers=headers)\n"
    "         destination.parent.mkdir(parents=True, exist_ok=True)\n"
    "-        downloaded = 0\n"
    '-        with destination.open("wb") as handle:\n'
    "+        should_append = offset > 0 and response.status_code == 206\n"
    '+        mode = "ab" if should_append else "wb"\n'
    "+        downloaded = offset if should_append else 0\n"
    "+        with destination.open(mode) as handle:\n"
    "             for chunk in response.iter_chunks():\n"
    "                 handle.write(chunk)\n"
    "                 downloaded += len(chunk)\n"
    "                 self.jobs.set_offset(url, downloaded)\n"
    "         return downloaded\n"
)


class FrontierVerticalSliceTests(unittest.TestCase):
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
        self._git("config", "user.name", "SOL Tests")
        self._git("add", ".")
        self._git("commit", "-m", "controlled baseline")
        (self.root / ".sol").mkdir()
        self.store = SQLiteTaskStore(self.root / ".sol" / "sol.db")

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_full_flow_repairs_once_and_writes_complete_audit(self) -> None:
        fake = FakeModelProvider(
            [specification_response(), IMPLEMENTATION_PATCH, REPAIR_PATCH]
        )
        original_complete = fake.complete

        def complete_with_task_id(invocation):
            next_call = len(fake.invocations) + 1
            task_directories = list((self.root / ".sol" / "tasks").glob("TASK-*"))
            self.assertEqual(len(task_directories), 1)
            self.assertTrue(
                (task_directories[0] / f"call-{next_call:03d}-context.json").is_file()
            )
            self.assertTrue(
                (task_directories[0] / f"call-{next_call:03d}-request.json").is_file()
            )
            output = original_complete(invocation)
            if len(fake.invocations) == 1:
                task_id = invocation.prompt.split('task_id to "', 1)[1].split('"', 1)[0]
                raw = json.loads(output.content)
                raw["task_id"] = task_id
                output = output.model_copy(update={"content": json.dumps(raw)})
            return output

        fake.complete = complete_with_task_id  # type: ignore[method-assign]
        pricing = ProviderPricing(
            input_per_million_usd=2,
            output_per_million_usd=4,
            cached_input_per_million_usd=1,
        )
        config = SolConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    provider="openai_compatible",
                    base_url="https://provider.invalid/v1",
                    model=fake.model_name,
                    pricing=pricing,
                )
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
        runner = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(fake, pricing),
            config,
        )

        report = runner.run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(report.number_of_calls, 3)
        self.assertEqual(report.input_tokens, 300)
        self.assertEqual(report.output_tokens, 60)
        self.assertEqual(report.cached_input_tokens, 30)
        self.assertAlmostEqual(report.estimated_cost_usd, 0.00081)
        self.assertEqual(len(report.constraint_coverage), 3)
        self.assertEqual(len(report.verification_results), 2)
        self.assertEqual(
            report.verification_results[0].status.value, "failed"
        )
        self.assertEqual(
            report.verification_results[1].status.value, "passed"
        )
        self.assertIn("src/download_service/downloader.py", report.files_changed)
        self.assertGreater(report.transmitted_files, 0)
        self.assertGreater(report.transmitted_lines, 0)
        self.assertTrue(
            any(
                item.path == "src/download_service/jobs.py"
                for item in report.transmitted_excerpts
            )
        )

        task = self.store.get_task(report.task_id)
        self.assertEqual(task.state, WorkflowState.COMPLETE)
        worktree = Path(report.worktree_path or "")
        final_source = (
            worktree / "src" / "download_service" / "downloader.py"
        ).read_text(encoding="utf-8")
        self.assertIn("response.status_code == 206", final_source)
        baseline_source = (
            self.root / "src" / "download_service" / "downloader.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("response.status_code == 206", baseline_source)

        audit_root = self.root / ".sol" / "tasks" / report.task_id
        expected = {
            "call-001-context.json",
            "call-001-request.json",
            "call-002-context.json",
            "call-002-request.json",
            "call-003-context.json",
            "call-003-request.json",
            "patch-001.diff",
            "patch-002.diff",
            "verification-001.json",
            "verification-002.json",
            "verification-failure-001.json",
            "report.json",
        }
        self.assertTrue(expected.issubset({path.name for path in audit_root.iterdir()}))
        repair_request = json.loads(
            (audit_root / "call-003-request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(repair_request["request_package_sha256"]), 64)
        self.assertIn("CURRENT_DIFF", repair_request["prompt"])
        self.assertIn("EXACT_FAILING_COMMAND", repair_request["prompt"])
        self.assertIn("test_server_ignores_range", repair_request["prompt"])
        for constraint in task.specification.hard_constraints:
            self.assertIn(constraint.verbatim_source, repair_request["prompt"])

    def test_native_ollama_provider_runs_the_verified_repair_flow(self) -> None:
        class StubOllama(OllamaProvider):
            def __init__(self, config: FrontierProviderConfig) -> None:
                super().__init__(config)
                self.responses = [
                    specification_response(),
                    IMPLEMENTATION_PATCH,
                    REPAIR_PATCH,
                ]
                self.payloads: list[dict[str, object]] = []

            def _request_json(
                self,
                path: str,
                payload: dict[str, object] | None,
                *,
                method: str = "POST",
                timeout_seconds: float | None = None,
            ) -> dict[str, object]:
                self.assert_chat_request(path, payload)
                assert payload is not None
                self.payloads.append(payload)
                content = self.responses.pop(0)
                if len(self.payloads) == 1:
                    messages = payload["messages"]
                    assert isinstance(messages, list)
                    prompt = messages[0]["content"]
                    assert isinstance(prompt, str)
                    task_id = prompt.split('task_id to "', 1)[1].split('"', 1)[0]
                    specification = json.loads(content)
                    specification["task_id"] = task_id
                    content = json.dumps(specification)
                return {
                    "model": self.config.model,
                    "created_at": f"response-{len(self.payloads)}",
                    "message": {"content": content},
                    "done_reason": "stop",
                    "prompt_eval_count": 100,
                    "eval_count": 20,
                    "model_digest": "sha256:local-coder",
                }

            @staticmethod
            def assert_chat_request(
                path: str, payload: dict[str, object] | None
            ) -> None:
                if path != "/api/chat" or payload is None:
                    raise AssertionError("expected one native Ollama chat request")

        frontier = FrontierProviderConfig(
            provider="ollama",
            base_url="http://127.0.0.1:11434",
            model="qwen3-coder:30b",
            context_window_tokens=16384,
            think=False,
        )
        adapter = StubOllama(frontier)
        config = SolConfig(
            models=ModelsConfig(frontier=frontier),
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

        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(adapter),
            config,
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(report.number_of_calls, 3)
        self.assertEqual(len(report.models_used), 1)
        self.assertEqual(report.models_used[0].provider, "ollama")
        self.assertEqual(report.models_used[0].model, "qwen3-coder:30b")
        self.assertEqual(len(adapter.payloads), 3)
        self.assertTrue(all(payload["think"] is False for payload in adapter.payloads))
        self.assertTrue(
            all(
                payload["options"]["num_ctx"] == 16384
                for payload in adapter.payloads
            )
        )

    def test_non_applying_patch_uses_the_single_repair_budget(self) -> None:
        fake = FakeModelProvider(
            [specification_response(), NON_APPLYING_PATCH, COMPLETE_PATCH]
        )
        original_complete = fake.complete

        def complete_with_task_id(invocation):
            output = original_complete(invocation)
            if len(fake.invocations) == 1:
                task_id = invocation.prompt.split('task_id to "', 1)[1].split('"', 1)[0]
                raw = json.loads(output.content)
                raw["task_id"] = task_id
                return output.model_copy(update={"content": json.dumps(raw)})
            return output

        fake.complete = complete_with_task_id  # type: ignore[method-assign]
        config = SolConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1",
                    model=fake.model_name,
                )
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

        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(fake),
            config,
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(report.number_of_calls, 3)
        self.assertEqual(len(report.verification_results), 1)
        self.assertEqual(report.verification_results[0].status.value, "passed")
        audit_root = self.root / ".sol" / "tasks" / report.task_id
        self.assertTrue((audit_root / "patch-failure-001.json").is_file())
        replacement_request = json.loads(
            (audit_root / "call-003-request.json").read_text(encoding="utf-8")
        )
        self.assertIn("REJECTED_PATCH", replacement_request["prompt"])
        self.assertIn("EXACT_PATCH_REJECTION", replacement_request["prompt"])
        events = self.store.events(report.task_id)
        self.assertIn(
            "targeted_patch_repair_required",
            [event.event_type for event in events],
        )

    def test_unapproved_specification_stops_before_worktree_or_patch(self) -> None:
        fake = FakeModelProvider([specification_response()])
        original_complete = fake.complete

        def complete_with_task_id(invocation):
            output = original_complete(invocation)
            task_id = invocation.prompt.split('task_id to "', 1)[1].split('"', 1)[0]
            raw = json.loads(output.content)
            raw["task_id"] = task_id
            return output.model_copy(update={"content": json.dumps(raw)})

        fake.complete = complete_with_task_id  # type: ignore[method-assign]
        frontier = FrontierProviderConfig(
            base_url="https://provider.invalid/v1", model=fake.model_name
        )
        config = SolConfig(
            models=ModelsConfig(frontier=frontier),
            verification=VerificationConfig(commands=[]),
        )
        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(fake),
            config,
        ).run(REQUEST, approve=lambda specification: False)

        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)
        self.assertEqual(report.number_of_calls, 1)
        self.assertIsNone(report.worktree_path)
        self.assertEqual(
            self.store.get_task(report.task_id).state,
            WorkflowState.HUMAN_REVIEW_REQUIRED,
        )


if __name__ == "__main__":
    unittest.main()
