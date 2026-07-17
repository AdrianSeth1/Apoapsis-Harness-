from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from sol.config import FrontierProviderConfig, ProviderPricing
from sol.models.base import ModelOperation
from sol.models.frontier import OpenAICompatibleFrontierProvider
from sol.models.local import OllamaProvider
from sol.models.provider import ModelRole, ProviderError, ProviderInvocation
from sol.models.telemetry import (
    InstrumentedModelProvider,
    InstrumentedProviderError,
)
from sol.specification.extractor import (
    SpecificationExtractionError,
    SpecificationExtractor,
)
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
            api_key_env="SOL_TEST_API_KEY",
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
        with patch.dict(os.environ, {"SOL_TEST_API_KEY": "secret"}):
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


if __name__ == "__main__":
    unittest.main()
