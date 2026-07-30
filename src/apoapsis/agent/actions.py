from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, ValidationError

from apoapsis.specification.schema import StrictModel


class AgentActionError(ValueError):
    """A model response is not one valid, bounded Apoapsis action."""


class AgentActionKind(StrEnum):
    SEARCH_REPOSITORY = "search_repository"
    READ_FILE = "read_file"
    INSPECT_DIFF = "inspect_diff"
    PROPOSE_PATCH = "propose_patch"
    REPLACE_TEXT = "replace_text"
    CREATE_FILE = "create_file"
    RUN_CHECK = "run_check"
    SUBMIT_FOR_VERIFICATION = "submit_for_verification"
    REQUEST_ESCALATION = "request_escalation"


class SearchRepositoryAction(StrictModel):
    action: Literal[AgentActionKind.SEARCH_REPOSITORY]
    query: str = Field(min_length=1, max_length=500)
    path_glob: str | None = Field(default=None, max_length=200)


class ReadFileAction(StrictModel):
    action: Literal[AgentActionKind.READ_FILE]
    path: str = Field(min_length=1, max_length=500)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class InspectDiffAction(StrictModel):
    action: Literal[AgentActionKind.INSPECT_DIFF]


class ProposePatchAction(StrictModel):
    action: Literal[AgentActionKind.PROPOSE_PATCH]
    unified_diff: str = Field(min_length=1)


class ReplaceTextAction(StrictModel):
    action: Literal[AgentActionKind.REPLACE_TEXT]
    path: str = Field(min_length=1, max_length=500)
    old_text: str = Field(min_length=1, max_length=40_000)
    new_text: str = Field(max_length=40_000)


class CreateFileAction(StrictModel):
    action: Literal[AgentActionKind.CREATE_FILE]
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=40_000)


class RunCheckAction(StrictModel):
    action: Literal[AgentActionKind.RUN_CHECK]
    command_name: str = Field(min_length=1, max_length=200)


class SubmitForVerificationAction(StrictModel):
    action: Literal[AgentActionKind.SUBMIT_FOR_VERIFICATION]


class RequestEscalationAction(StrictModel):
    action: Literal[AgentActionKind.REQUEST_ESCALATION]
    reason: str = Field(min_length=1, max_length=4_000)


AgentAction = Annotated[
    SearchRepositoryAction
    | ReadFileAction
    | InspectDiffAction
    | ProposePatchAction
    | ReplaceTextAction
    | CreateFileAction
    | RunCheckAction
    | SubmitForVerificationAction
    | RequestEscalationAction,
    Field(discriminator="action"),
]

_ACTION_ADAPTER = TypeAdapter(AgentAction)
_LLAMA_CPP_TOOL_MARKERS = (
    "</arg_value></tool_call>",
    "</tool_call>",
)


def _normalize_known_local_tool_noise(raw: object) -> object:
    """Remove known llama.cpp chat-template residue from a typed action.

    Some local GGUF chat templates can echo tool-call closing tags into a JSON
    string and attach a field from a different action. The discriminator still
    has to select a valid Apoapsis action, and unknown authority fields remain
    forbidden.
    """

    if not isinstance(raw, dict):
        return raw
    action = raw.get("action")
    if action != AgentActionKind.CREATE_FILE:
        return raw
    if not isinstance(raw.get("content"), str):
        return raw
    allowed = {"action", "path", "content", "command_name", "reason"}
    if not set(raw).issubset(allowed):
        return raw
    normalized: dict[str, Any] = dict(raw)
    content = normalized["content"]
    for marker in _LLAMA_CPP_TOOL_MARKERS:
        if marker in content:
            content = content.split(marker, 1)[0]
            break
    normalized["content"] = content
    normalized.pop("command_name", None)
    normalized.pop("reason", None)
    return normalized


def agent_action_schema() -> dict[str, object]:
    # Ollama's structured-output grammar accepts a conservative JSON Schema
    # subset and rejects Pydantic's discriminated-union `oneOf` representation.
    # The wire shape is intentionally flat; parse_agent_action still applies the
    # strict per-action discriminated union after the provider returns JSON.
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [item.value for item in AgentActionKind],
            },
            "query": {"type": "string"},
            "path_glob": {"type": "string"},
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "unified_diff": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "content": {"type": "string"},
            "command_name": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }


def parse_agent_action(content: str) -> AgentAction:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AgentActionError(f"response is not valid JSON: {exc.msg}") from exc
    raw = _normalize_known_local_tool_noise(raw)
    try:
        return _ACTION_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        location = ".".join(str(item) for item in first["loc"])
        detail = first["msg"]
        raise AgentActionError(
            f"response is not a valid Apoapsis action at {location}: {detail}"
        ) from exc
