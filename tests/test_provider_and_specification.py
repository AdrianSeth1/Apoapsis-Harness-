from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from apoapsis.config import FrontierProviderConfig, ProviderPricing
from apoapsis.models.base import ModelOperation
from apoapsis.models.frontier import OpenAICompatibleFrontierProvider
from apoapsis.models.local import OllamaProvider
from apoapsis.models.provider import ModelRole, ProviderError, ProviderInvocation
from apoapsis.models.telemetry import (
    InstrumentedModelProvider,
    InstrumentedProviderError,
)
from apoapsis.specification.extractor import (
    SpecificationExtractionError,
    SpecificationExtractor,
)
from apoapsis.verification.runner import VerificationCommand
from tests.fakes import FakeModelProvider


class _FakeHTTPResponse:
    def __init__(self, body: dict[str, object]) -> None:
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class ProviderTests(unittest.TestCase):
    def test_instrumentation_records_cache_latency_tokens_and_cost(self) -> None:
        provider = InstrumentedModelProvider(
            FakeModelProvider(["proposal"]),
            ProviderPricing(
                input_per_million_usd=2,
                output_per_million_usd=4,
                cached_input_per_million_usd=1,
            ),
        )

        call = provider.complete(
            ProviderInvocation(
                request_id="MRQ-1",
                operation=ModelOperation.IMPLEMENT_PATCH,
                prompt="Return a patch.",
            )
        )

        self.assertEqual(call.telemetry.input_tokens, 100)
        self.assertEqual(call.telemetry.cached_input_tokens, 10)
        self.assertTrue(call.telemetry.cache_hit)
        self.assertAlmostEqual(call.telemetry.estimated_cost_usd, 0.00027)
        self.assertGreaterEqual(call.telemetry.latency_seconds, 0)

    def test_failed_provider_call_still_has_telemetry(self) -> None:
        class FailingProvider:
            provider_name = "failing"
            model_name = "failing-model"

            def complete(self, invocation):
                raise ProviderError("simulated outage")

        provider = InstrumentedModelProvider(FailingProvider())
        with self.assertRaises(InstrumentedProviderError) as caught:
            provider.complete(
                ProviderInvocation(
                    request_id="MRQ-FAILED",
                    operation=ModelOperation.DRAFT_SPECIFICATION,
                    prompt="extract",
                )
            )

        telemetry = caught.exception.telemetry
        self.assertFalse(telemetry.succeeded)
        self.assertIn("simulated outage", telemetry.error or "")
        self.assertEqual(telemetry.input_tokens, 0)

    def test_openai_compatible_adapter_maps_chat_completion_usage(self) -> None:
        config = FrontierProviderConfig(
            base_url="https://provider.invalid/v1",
            model="frontier-test",
            api_key_env="APOAPSIS_TEST_API_KEY",
        )
        adapter = OpenAICompatibleFrontierProvider(config)
        response = _FakeHTTPResponse(
            {
                "id": "chat-1",
                "model": "frontier-test-2026",
                "choices": [
                    {
                        "message": {"content": "diff --git a/a b/a"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 7},
                },
            }
        )
        with patch.dict(os.environ, {"APOAPSIS_TEST_API_KEY": "secret"}):
            with patch(
                "urllib.request.urlopen", return_value=response
            ) as urlopen:
                output = adapter.complete(
                    ProviderInvocation(
                        request_id="MRQ-1",
                        operation=ModelOperation.IMPLEMENT_PATCH,
                        prompt="patch",
                    )
                )

        self.assertEqual(output.response_id, "chat-1")
        self.assertEqual(output.model, "frontier-test-2026")
        self.assertEqual(output.usage.cached_input_tokens, 7)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["max_tokens"], 8192)

    def test_native_ollama_frontier_uses_local_generation_controls(self) -> None:
        class StubOllama(OllamaProvider):
            def __init__(self, config: FrontierProviderConfig) -> None:
                super().__init__(config)
                self.payload: dict[str, object] | None = None

            def _request_json(
                self,
                path: str,
                payload: dict[str, object] | None,
                *,
                method: str = "POST",
                timeout_seconds: float | None = None,
            ) -> dict[str, object]:
                if path == "/api/chat":
                    self.payload = payload
                    return {
                        "model": self.config.model,
                        "created_at": "now",
                        "message": {"content": "diff --git a/a.py b/a.py\n"},
                        "done_reason": "stop",
                        "prompt_eval_count": 40,
                        "eval_count": 8,
                        "model_digest": "sha256:frontier-model",
                    }
                return {"models": []}

        adapter = StubOllama(
            FrontierProviderConfig(
                provider="ollama",
                base_url="http://127.0.0.1:11434",
                model="qwen3-coder:30b",
                max_output_tokens=8192,
                temperature=1.0,
                context_window_tokens=16384,
                think=False,
            )
        )
        call = InstrumentedModelProvider(adapter).complete(
            ProviderInvocation(
                request_id="MRQ-LOCAL-FRONTIER",
                operation=ModelOperation.IMPLEMENT_PATCH,
                prompt="patch",
                role=ModelRole.FRONTIER_IMPLEMENTATION,
            )
        )

        assert adapter.payload is not None
        options = adapter.payload["options"]
        assert isinstance(options, dict)
        self.assertEqual(options["num_predict"], 8192)
        self.assertEqual(options["temperature"], 1.0)
        self.assertEqual(options["num_ctx"], 16384)
        self.assertFalse(adapter.payload["think"])
        self.assertEqual(call.telemetry.provider, "ollama")
        self.assertEqual(call.telemetry.model_digest, "sha256:frontier-model")
        self.assertEqual(call.telemetry.input_tokens, 40)
        self.assertEqual(call.telemetry.output_tokens, 8)

    def test_native_ollama_can_disable_thinking_only_for_specification(self) -> None:
        class StubOllama(OllamaProvider):
            def __init__(self, config: FrontierProviderConfig) -> None:
                super().__init__(config)
                self.payload: dict[str, object] | None = None

            def _request_json(
                self,
                path: str,
                payload: dict[str, object] | None,
                *,
                method: str = "POST",
                timeout_seconds: float | None = None,
            ) -> dict[str, object]:
                self.payload = payload
                return {
                    "message": {"content": "{}"},
                    "done_reason": "stop",
                    "model_digest": "sha256:model",
                }

        adapter = StubOllama(
            FrontierProviderConfig(
                provider="ollama",
                base_url="http://127.0.0.1:11434",
                model="qwen3.6:27b",
                think=True,
                specification_think=False,
            )
        )
        adapter.complete(
            ProviderInvocation(
                request_id="MRQ-SPEC-THINK",
                operation=ModelOperation.DRAFT_SPECIFICATION,
                prompt="extract",
            )
        )

        assert adapter.payload is not None
        self.assertFalse(adapter.payload["think"])


class SpecificationExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = SpecificationExtractor()
        self.request = (
            "Add resumable downloads.\n"
            "Preserve the current public API.\n"
            "Do not add runtime dependencies."
        )

    def proposal(self, verbatim: str) -> str:
        return json.dumps(
            {
                "schema_version": "1.0",
                "task_id": "TASK-SPEC-1",
                "objective": {
                    "text": "Add resumable downloads.",
                    "source": "user",
                    "source_reference": "user-request",
                },
                "acceptance_criteria": [],
                "hard_constraints": [
                    {
                        "id": "HC-1",
                        "text": "Keep the API stable.",
                        "verbatim_source": verbatim,
                        "interpreted_meaning": "Public call signatures stay stable.",
                        "source": "user",
                        "source_reference": "user-request",
                        "scope": "task",
                        "status": "active",
                        "verification_method": "Run compatibility tests.",
                    }
                ],
                "requested_output": "unified_diff",
            }
        )

    def test_exact_verbatim_constraint_is_preserved(self) -> None:
        exact = "Preserve the current public API."
        specification = self.extractor.parse(
            self.proposal(exact), self.request, "TASK-SPEC-1"
        )
        self.assertEqual(
            specification.hard_constraints[0].verbatim_source, exact
        )

    def test_reworded_constraint_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            SpecificationExtractionError, "exact substring"
        ):
            self.extractor.parse(
                self.proposal("Preserve the public API."),
                self.request,
                "TASK-SPEC-1",
            )

    def proposal_with_mapping(self, verification_method: str | None) -> str:
        payload = json.loads(self.proposal("Preserve the current public API."))
        payload["acceptance_criteria"] = [
            {
                "id": "AC-1",
                "text": "Interrupted downloads resume from the persisted byte.",
                "source": "derived",
                "source_reference": "user-request",
                "status": "active",
                "verification_method": verification_method,
            }
        ]
        return json.dumps(payload)

    def test_build_prompt_includes_the_exact_configured_acceptance_catalog(
        self,
    ) -> None:
        commands = [
            VerificationCommand(
                name="unit-tests",
                category="tests",
                description="Runs the full test suite.",
                argv=["python", "-m", "unittest"],
                acceptance=True,
            ),
            VerificationCommand(
                name="lint",
                category="lint",
                description="",
                argv=["ruff", "check", "."],
                required=False,
            ),
        ]
        prompt = self.extractor.build_prompt(
            self.request, "TASK-SPEC-1", commands
        )
        self.assertIn("ACCEPTANCE_COMMAND_CATALOG:", prompt)
        catalog_start = prompt.index("ACCEPTANCE_COMMAND_CATALOG:") + len(
            "ACCEPTANCE_COMMAND_CATALOG:\n"
        )
        catalog_json = prompt[catalog_start:].split("\n", 1)[0]
        catalog = json.loads(catalog_json)
        self.assertEqual(
            catalog,
            [
                {
                    "name": "lint",
                    "category": "lint",
                    "description": "",
                    "acceptance_designated": False,
                },
                {
                    "name": "unit-tests",
                    "category": "tests",
                    "description": "Runs the full test suite.",
                    "acceptance_designated": True,
                },
            ],
        )

    def test_mapping_to_an_unconfigured_command_is_rejected(self) -> None:
        commands = [
            VerificationCommand(
                name="unit-tests", category="tests", argv=["python", "-m", "unittest"]
            )
        ]
        with self.assertRaisesRegex(
            SpecificationExtractionError, "not in the configured"
        ):
            self.extractor.parse(
                self.proposal_with_mapping("a-made-up-shell-command"),
                self.request,
                "TASK-SPEC-1",
                commands,
            )

    def test_mapping_to_a_catalog_command_is_accepted(self) -> None:
        commands = [
            VerificationCommand(
                name="unit-tests",
                category="tests",
                argv=["python", "-m", "unittest"],
                acceptance=True,
            )
        ]
        specification = self.extractor.parse(
            self.proposal_with_mapping("unit-tests"),
            self.request,
            "TASK-SPEC-1",
            commands,
        )
        self.assertEqual(
            specification.acceptance_criteria[0].verification_method,
            "unit-tests",
        )

    def test_null_mapping_is_accepted_regardless_of_catalog(self) -> None:
        specification = self.extractor.parse(
            self.proposal_with_mapping(None),
            self.request,
            "TASK-SPEC-1",
            [],
        )
        self.assertIsNone(
            specification.acceptance_criteria[0].verification_method
        )

    def test_correction_prompt_includes_errors_schema_catalog_and_prior_response(
        self,
    ) -> None:
        commands = [
            VerificationCommand(
                name="unit-tests",
                category="tests",
                description="Runs the full test suite.",
                argv=["python", "-m", "unittest"],
                acceptance=True,
            )
        ]
        prompt = self.extractor.build_correction_prompt(
            self.request,
            "TASK-SPEC-1",
            commands,
            previous_response='{"bad": true}',
            validation_errors="hard_constraints.0.verification_method: null",
        )

        self.assertIn("VALIDATION_ERRORS", prompt)
        self.assertIn(
            "hard_constraints.0.verification_method: null", prompt
        )
        self.assertIn("YOUR_PREVIOUS_RESPONSE_START", prompt)
        self.assertIn('{"bad": true}', prompt)
        self.assertIn("SCHEMA:", prompt)
        self.assertIn("ACCEPTANCE_COMMAND_CATALOG:", prompt)
        self.assertIn("unit-tests", prompt)
        self.assertIn("Every hard_constraints item's verification_method", prompt)
        self.assertIn("never null", prompt)
        self.assertIn('task_id to "TASK-SPEC-1"', prompt)
        self.assertIn(self.request, prompt)


if __name__ == "__main__":
    unittest.main()
