from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from sol.config import LocalResearchProviderConfig
from sol.models.base import TokenUsage
from sol.models.provider import (
    ModelProvider,
    ProviderError,
    ProviderInvocation,
    ProviderOutput,
)


class OllamaLocalProvider(ModelProvider):
    """Native Ollama adapter for structured, tool-free local research calls."""

    def __init__(self, config: LocalResearchProviderConfig) -> None:
        if config.provider != "ollama":
            raise ValueError(f"unsupported native local provider: {config.provider}")
        self.config = config
        self._model_digest: str | None = None
        self._digest_checked = False

    @property
    def provider_name(self) -> str:
        return "ollama"

    @property
    def model_name(self) -> str:
        return self.config.model

    def complete(self, invocation: ProviderInvocation) -> ProviderOutput:
        timeout_seconds = min(
            self.config.timeout_seconds,
            invocation.timeout_seconds or self.config.timeout_seconds,
        )
        deadline = time.monotonic() + timeout_seconds
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": invocation.prompt}],
            "stream": False,
            "options": {
                "temperature": 0,
                "num_predict": (
                    invocation.max_output_tokens or self.config.max_output_tokens
                ),
            },
        }
        if invocation.response_schema is not None:
            payload["format"] = invocation.response_schema
        if invocation.think is not None:
            payload["think"] = invocation.think
        raw = self._request_json(
            "/api/chat", payload, timeout_seconds=timeout_seconds
        )
        try:
            message = raw["message"]
            content = str(message["content"])
        except (KeyError, TypeError) as exc:
            raise ProviderError("Ollama response is missing message.content") from exc
        structured_valid: bool | None = None
        if invocation.response_schema is not None:
            try:
                json.loads(content)
                structured_valid = True
            except json.JSONDecodeError:
                structured_valid = False
        digest = (
            raw.get("model_digest")
            or raw.get("digest")
            or self._digest(max(0.001, deadline - time.monotonic()))
        )
        thinking_tokens = raw.get("thinking_count") or message.get("thinking_count")
        return ProviderOutput(
            response_id=str(
                raw.get("created_at") or f"ollama-{invocation.request_id}"
            ),
            content=content,
            model=str(raw.get("model") or self.config.model),
            finish_reason=str(raw.get("done_reason") or "stop"),
            usage=TokenUsage(
                input_tokens=int(raw.get("prompt_eval_count") or 0),
                output_tokens=int(raw.get("eval_count") or 0),
            ),
            provider_metadata={
                "model_digest": digest,
                "thinking_tokens": (
                    int(thinking_tokens) if thinking_tokens is not None else None
                ),
                "prompt_evaluation_seconds": self._nanoseconds(
                    raw.get("prompt_eval_duration")
                ),
                "generation_seconds": self._nanoseconds(raw.get("eval_duration")),
                "model_load_seconds": self._nanoseconds(raw.get("load_duration")),
                "structured_output_valid": structured_valid,
                "retry_count": 0,
            },
        )

    def _digest(self, timeout_seconds: float) -> str | None:
        if self._digest_checked:
            return self._model_digest
        self._digest_checked = True
        try:
            raw = self._request_json(
                "/api/tags",
                None,
                method="GET",
                timeout_seconds=timeout_seconds,
            )
            for model in raw.get("models") or []:
                if (
                    model.get("name") == self.config.model
                    or model.get("model") == self.config.model
                ):
                    self._model_digest = model.get("digest")
                    break
        except ProviderError:
            self._model_digest = None
        return self._model_digest

    def _request_json(
        self,
        path: str,
        payload: dict[str, Any] | None,
        *,
        method: str = "POST",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.config.base_url.rstrip('/')}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_seconds or self.config.timeout_seconds,
            ) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Ollama request failed: {exc}") from exc
        if not isinstance(raw, dict):
            raise ProviderError("Ollama returned a non-object response")
        return raw

    @staticmethod
    def _nanoseconds(value: Any) -> float | None:
        return float(value) / 1_000_000_000 if value is not None else None
