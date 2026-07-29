"""The looser -- but still mediated -- action protocol for ADR 0059.

The strict Apoapsis loop in `apoapsis.agent.actions` asks a coding model to
hand-author unified diffs. That is the safest possible representation of an
edit, and it stays the default. It is also the single largest source of
observed failure for small local models, which reliably produce correct file
*content* and unreliably produce correct diff *syntax*.

This protocol trades diff authorship for whole-file authorship: the model says
what a file should contain, and the harness computes the diff. It is a
deliberately small, flat vocabulary because the same models that fumble diff
syntax also fumble deeply structured JSON.

One action -- `propose_change_set` (ADR 0071) -- carries a single level of
nesting, because a coherent product increment across `index.html`,
`styles.css`, and `app.js` cannot be expressed one file at a time without
forcing the model to regenerate a partial picture on every turn. Nesting stops
at one level on purpose, and the operations inside a change set are exactly
the whole-file `write`/`delete` this protocol already has. No patch operation
exists: reintroducing diff syntax here would reintroduce the failure mode the
whole mode was built to avoid.

Widening the protocol is not the same as widening authority. Every action
below is still executed by the harness, against one disposable sandbox, under
the containment rules in `apoapsis.agent.sandbox`. Nothing here can touch
Apoapsis state, Git metadata, credentials, or workflow completion.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from apoapsis.specification.schema import StrictModel


class PowerActionError(ValueError):
    """A model response is not one valid, bounded Local Power action."""


class PowerActionKind(StrEnum):
    READ_FILE = "read_file"
    SEARCH = "search"
    WRITE_FILE = "write_file"
    DELETE_FILE = "delete_file"
    PROPOSE_CHANGE_SET = "propose_change_set"
    RUN_SHELL = "run_shell"
    RUN_VERIFICATION = "run_verification"
    FINISH = "finish"


class ChangeSetOperationKind(StrEnum):
    WRITE = "write"
    DELETE = "delete"


class PowerReadFileAction(StrictModel):
    action: Literal[PowerActionKind.READ_FILE]
    path: str = Field(min_length=1, max_length=500)


class PowerSearchAction(StrictModel):
    action: Literal[PowerActionKind.SEARCH]
    query: str = Field(min_length=1, max_length=500)
    path_glob: str | None = Field(default=None, max_length=200)


class PowerWriteFileAction(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    action: Literal[PowerActionKind.WRITE_FILE]
    path: str = Field(min_length=1, max_length=500)
    # Whole-file content, not a diff. An empty string is a legitimate write
    # (truncating a file), so unlike `CreateFileAction` there is no min_length.
    content: str = Field(max_length=400_000)


class PowerDeleteFileAction(StrictModel):
    action: Literal[PowerActionKind.DELETE_FILE]
    path: str = Field(min_length=1, max_length=500)


class PowerChangeSetOperation(StrictModel):
    """One file operation inside an atomic proposal (ADR 0071).

    Deliberately the same two verbs the single-action protocol already has, so
    a model that can emit `write_file` can emit a change set without learning a
    second edit vocabulary. `operation` defaults to `write` because a change
    set that names a path and supplies content means one thing only, and a
    model that omits the discriminator should not lose an otherwise valid
    proposal over it.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    operation: ChangeSetOperationKind = ChangeSetOperationKind.WRITE
    path: str = Field(min_length=1, max_length=500)
    # Whole-file content, not a diff. `None` is only valid for a delete; the
    # validator below refuses the two incoherent combinations rather than
    # letting a missing field silently truncate a file to nothing.
    content: str | None = Field(default=None, max_length=400_000)

    @model_validator(mode="after")
    def content_matches_operation(self) -> "PowerChangeSetOperation":
        if self.operation == ChangeSetOperationKind.WRITE and self.content is None:
            raise ValueError(
                f"change set write for {self.path!r} has no content; send the "
                "file's full intended text"
            )
        if self.operation == ChangeSetOperationKind.DELETE and self.content is not None:
            raise ValueError(
                f"change set delete for {self.path!r} must not carry content"
            )
        return self


class PowerProposeChangeSetAction(StrictModel):
    """An atomic, multi-file product increment (ADR 0071).

    All of it applies or none of it does. The model proposes; the harness
    validates every operation against the same boundary that governs a single
    `write_file`, applies the whole set or nothing, and then runs verification
    itself. `verification_commands` is a *request* against the configured
    catalog, never a new command; `base_worktree_digest`, when supplied, is an
    optimistic-concurrency claim the harness checks rather than trusts.
    """

    action: Literal[PowerActionKind.PROPOSE_CHANGE_SET]
    summary: str = Field(min_length=1, max_length=8_000)
    changes: list[PowerChangeSetOperation] = Field(min_length=1, max_length=100)
    verification_commands: list[str] = Field(default_factory=list, max_length=40)
    base_worktree_digest: str | None = Field(default=None, max_length=200)


class PowerRunShellAction(StrictModel):
    action: Literal[PowerActionKind.RUN_SHELL]
    command: str = Field(min_length=1, max_length=2_000)


class PowerRunVerificationAction(StrictModel):
    action: Literal[PowerActionKind.RUN_VERIFICATION]
    command_name: str | None = Field(default=None, max_length=200)


class PowerFinishAction(StrictModel):
    action: Literal[PowerActionKind.FINISH]
    summary: str = Field(min_length=1, max_length=8_000)


PowerAction = Annotated[
    PowerReadFileAction
    | PowerSearchAction
    | PowerWriteFileAction
    | PowerDeleteFileAction
    | PowerProposeChangeSetAction
    | PowerRunShellAction
    | PowerRunVerificationAction
    | PowerFinishAction,
    Field(discriminator="action"),
]

_POWER_ACTION_ADAPTER = TypeAdapter(PowerAction)

# The same llama.cpp chat-template residue `apoapsis.agent.actions` normalizes
# (ADR 0058). Local GGUF templates echo tool-call closing tags into whichever
# long string field the action carries; for this protocol that is `content`
# on a write_file, which would otherwise silently land in a real source file.
_LLAMA_CPP_TOOL_MARKERS = (
    "</arg_value></tool_call>",
    "</tool_call>",
)


def _strip_content_markers(content: str) -> str:
    for marker in _LLAMA_CPP_TOOL_MARKERS:
        if marker in content:
            return content.split(marker, 1)[0]
    return content


def _strip_local_tool_noise(raw: object) -> object:
    if not isinstance(raw, dict):
        return raw
    action = raw.get("action")
    if action == PowerActionKind.PROPOSE_CHANGE_SET:
        return _strip_change_set_noise(raw)
    if action != PowerActionKind.WRITE_FILE:
        return raw
    content = raw.get("content")
    if not isinstance(content, str):
        return raw
    normalized: dict[str, Any] = dict(raw)
    normalized["content"] = _strip_content_markers(content)
    # Cross-action fields a local template sometimes attaches to whichever
    # action it emitted. They carry no authority and are dropped rather than
    # failing an otherwise valid, already-contained write.
    for stray in ("command", "command_name", "reason", "query", "summary"):
        normalized.pop(stray, None)
    return normalized


def _strip_change_set_noise(raw: dict[str, object]) -> dict[str, object]:
    """Apply the same transport-residue normalization inside a change set.

    A change set is where the residue matters most: the closing tag lands in
    the *last* file's content, which under the single-action protocol was one
    corrupted file and here would be one corrupted file inside an otherwise
    correct three-file increment. Only transport artifacts are removed --
    nothing about which paths or operations were requested is touched.
    """

    normalized: dict[str, Any] = dict(raw)
    for stray in ("path", "content", "command", "command_name", "reason", "query"):
        normalized.pop(stray, None)
    changes = normalized.get("changes")
    if not isinstance(changes, list):
        return normalized
    cleaned: list[object] = []
    for item in changes:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        entry: dict[str, Any] = dict(item)
        content = entry.get("content")
        if isinstance(content, str):
            entry["content"] = _strip_content_markers(content)
        cleaned.append(entry)
    normalized["changes"] = cleaned
    return normalized


def power_action_schema(*, include_change_sets: bool = True) -> dict[str, object]:
    """A flat JSON Schema for structured-output grammars.

    Same rationale as `agent_action_schema`: Ollama's grammar compiler accepts
    a conservative subset and rejects Pydantic's discriminated-union `oneOf`.
    The strict per-action union is still applied by `parse_power_action` after
    the provider returns, so the flat wire shape never widens what is accepted.

    `include_change_sets=False` produces the pre-ADR-0071 shape exactly, so a
    session with `atomic_change_sets` turned off does not advertise -- in the
    grammar or in the enum -- an action the harness would then refuse. That
    also makes the one-action mode a real comparison arm rather than the same
    mode with a discouraging sentence in the prompt.
    """

    kinds = [
        item.value
        for item in PowerActionKind
        if include_change_sets or item != PowerActionKind.PROPOSE_CHANGE_SET
    ]
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": kinds},
            "path": {"type": "string"},
            "content": {"type": "string"},
            "query": {"type": "string"},
            "path_glob": {"type": "string"},
            "command": {"type": "string"},
            "command_name": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    if include_change_sets:
        properties = schema["properties"]
        assert isinstance(properties, dict)
        properties["changes"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [item.value for item in ChangeSetOperationKind],
                    },
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        }
        properties["verification_commands"] = {
            "type": "array",
            "items": {"type": "string"},
        }
        properties["base_worktree_digest"] = {"type": "string"}
    return schema


def parse_power_action(content: str) -> PowerAction:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PowerActionError(f"response is not valid JSON: {exc.msg}") from exc
    raw = _strip_local_tool_noise(raw)
    try:
        return _POWER_ACTION_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        location = ".".join(str(item) for item in first["loc"])
        raise PowerActionError(
            f"response is not a valid Local Power action at {location}: "
            f"{first['msg']}"
        ) from exc
