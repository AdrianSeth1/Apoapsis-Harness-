from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from sol.config import FrontierProviderConfig
from sol.models.base import TokenUsage
from sol.models.provider import (
    ModelProvider,
    ProviderError,
    ProviderInvocation,
    ProviderOutput,
)


class OpenAICompatibleFrontierProvider(ModelProvider):
    """Configurable chat-completions adapter with no workflow authority."""

    def __init__(self, config: FrontierProviderConfig) -> None:
        if config.provider != "openai_compatible":
            raise ValueError(f"unsupported frontier provider: {config.provider}")
        self.config = config

    @property
    def provider_name(self) -> str:
        return self.config.provider

    @property
    def model_name(self) -> str:
        return self.config.model

    def complete(self, invocation: ProviderInvocation) -> ProviderOutput:
        api_key = os.environ.get(self.config.api_key_env)
        if not api_key:
            raise ProviderError(
                f"missing frontier credential environment variable: "
                f"{self.config.api_key_env}"
            )
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        payload_object: dict[str, object] = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": invocation.prompt}],
            "temperature": self.config.temperature,
            "max_tokens": (
                invocation.max_output_tokens or self.config.max_output_tokens
            ),
        }
        if invocation.response_schema is not None:
            payload_object["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "sol_model_response",
                    "strict": True,
                    "schema": invocation.response_schema,
                },
            }
        payload = json.dumps(payload_object).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=(
                    invocation.timeout_seconds or self.config.timeout_seconds
                ),
            ) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2_000]
            raise ProviderError(
                f"frontier request failed with HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"frontier request failed: {exc}") from exc
        try:
            choice = raw["choices"][0]
            content = choice["message"]["content"]
            usage = raw.get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}
            structured_valid: bool | None = None
            if invocation.response_schema is not None:
                try:
                    json.loads(content)
                    structured_valid = True
                except json.JSONDecodeError:
                    structured_valid = False
            return ProviderOutput(
                response_id=str(raw.get("id") or invocation.request_id),
                content=content,
                model=str(raw.get("model") or self.config.model),
                finish_reason=str(choice.get("finish_reason") or "unknown"),
                usage=TokenUsage(
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                    cached_input_tokens=int(details.get("cached_tokens") or 0),
                ),
                provider_metadata={
                    "structured_output_valid": structured_valid,
                },
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("frontier response has an invalid shape") from exc
