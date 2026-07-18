from __future__ import annotations

from collections import deque

from apoapsis.models.base import TokenUsage
from apoapsis.models.provider import ProviderInvocation, ProviderOutput


class FakeModelProvider:
    def __init__(
        self,
        outputs: list[str | Exception],
        *,
        provider_name: str = "fake_frontier",
        model_name: str = "fake-coder-v1",
    ) -> None:
        self.outputs = deque(outputs)
        self.invocations: list[ProviderInvocation] = []
        self._provider_name = provider_name
        self._model_name = model_name

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_name(self) -> str:
        return self._model_name

    def complete(self, invocation: ProviderInvocation) -> ProviderOutput:
        self.invocations.append(invocation)
        if not self.outputs:
            raise AssertionError("fake provider received an unexpected extra call")
        content = self.outputs.popleft()
        if isinstance(content, Exception):
            raise content
        call_number = len(self.invocations)
        return ProviderOutput(
            response_id=f"fake-response-{call_number}",
            content=content,
            model=self.model_name,
            finish_reason="stop",
            usage=TokenUsage(
                input_tokens=100,
                output_tokens=20,
                cached_input_tokens=10,
            ),
        )
