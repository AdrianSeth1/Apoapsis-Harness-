# ADR 0068: Normalize bounded local reasoning wrappers before JSON parsing

## Status

Accepted — 2026-07-27

## Decision

Discovery local-model responses may contain a leading `<think>...</think>`
reasoning wrapper in the assistant content, including when the requested
payload is otherwise valid JSON. The discovery parser removes only that
leading transport wrapper, then still requires one JSON object and performs
the existing strict Pydantic validation and source-faithfulness checks.

Markdown fences remain the only other supported transport normalization. The
harness does not scan for the first brace, accept prose, or treat model output
as workflow authority.

## Evidence

The live Qwen endpoint returned `<think>...</think>{...}` for a JSON request,
which previously failed at character 1. Deterministic fake-provider coverage
was added in `tests/test_discovery.py`.
