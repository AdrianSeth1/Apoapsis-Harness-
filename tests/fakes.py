from __future__ import annotations

from collections import deque

from sol.models.base import TokenUsage
from sol.models.provider import ProviderInvocation, ProviderOutput


class FakeModelProvider:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = deque(outputs)
        self.invocations: list[ProviderInvocation] = []

    @property
    def provider_name(self) -> str:
        return "fake_frontier"

    @property
    def model_name(self) -> str:
        return "fake-coder-v1"

    def complete(self, invocation: ProviderInvocation) -> ProviderOutput:
        self.invocations.append(invocation)
        if not self.outputs:
            raise AssertionError("fake provider received an unexpected extra call")
        content = self.outputs.popleft()
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

