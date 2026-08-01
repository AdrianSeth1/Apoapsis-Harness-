"""Adapt Qwen Code's headless `stream-json` events into Apoapsis evidence.

The single most important property of this module is what it does *not* do: it
does not schedule anything. Qwen Code already runs a model/tool loop with a
persistent shell, background processes, session resume, ripgrep-backed search,
and compaction. Slice 2's requirement is to use that loop, not to rebuild a
slower imitation of it in front of it.

So this is a one-way adapter. Events arrive as line-delimited JSON on the CLI's
stdout; `WorkcellEventAdapter` folds them into a running `WorkcellSessionTrace`
of tool calls, token usage, timings, and ceiling conditions. Nothing here
decides the model's next action, and nothing here decides task state.

Malformed and unrecognised lines are counted, never dropped silently and never
guessed at. A parser that quietly skips what it does not understand is how a
truncated tool call becomes an invisible no-op.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum

from pydantic import Field

from apoapsis.models.ceilings import (
    CeilingEvent,
    CeilingStopReason,
    classify_ceiling_stop_reason,
)
from apoapsis.specification.schema import StrictModel, utc_now


class WorkcellEventKind(StrEnum):
    SESSION_START = "session_start"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    COMPACTION = "compaction"
    #: The agent asks to be inspected. A request, never a completion decision.
    READY_FOR_EVALUATION = "ready_for_evaluation"
    SESSION_END = "session_end"
    ERROR = "error"


#: Event `type` strings the CLI emits, mapped to our kinds. Anything not here
#: is counted as unrecognised rather than assumed harmless.
_EVENT_TYPE_MAP: dict[str, WorkcellEventKind] = {
    "session.start": WorkcellEventKind.SESSION_START,
    "assistant": WorkcellEventKind.ASSISTANT_MESSAGE,
    "assistant.message": WorkcellEventKind.ASSISTANT_MESSAGE,
    "tool.call": WorkcellEventKind.TOOL_CALL,
    "tool_use": WorkcellEventKind.TOOL_CALL,
    "tool.result": WorkcellEventKind.TOOL_RESULT,
    "tool_result": WorkcellEventKind.TOOL_RESULT,
    "usage": WorkcellEventKind.USAGE,
    "compaction": WorkcellEventKind.COMPACTION,
    # The CLI's session banner. It carries `session_id` and the tool list.
    "system": WorkcellEventKind.SESSION_START,
    "session.end": WorkcellEventKind.SESSION_END,
    "result": WorkcellEventKind.SESSION_END,
    "error": WorkcellEventKind.ERROR,
}

#: Event types that carry their real content nested under `message.content`
#: rather than at the top level. Handled before the flat map so the nested
#: blocks reach the ordinary handlers; see `feed_event`.
_ENVELOPE_EVENT_TYPES: frozenset[str] = frozenset({"assistant", "user"})

#: The only signal the agent may send back to the controller. Its name is
#: deliberately not "complete": ADR 0077 makes it a request for inspection.
READY_SIGNAL = "ready_for_evaluation"


class ToolCallRecord(StrictModel):
    call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    #: Argument *shape*, not the argument values. Values can carry whole files.
    argument_keys: list[str] = Field(default_factory=list)
    #: Set when the CLI reports a shell action's exit status.
    exit_code: int | None = None
    #: Background servers get a PID and no exit code.
    background_pid: int | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    output_chars: int = Field(default=0, ge=0)
    output_truncated: bool = False
    #: Where the full, unclipped output was written. Truncation must be
    #: reversible; the model can read this artifact on demand.
    output_artifact: str | None = None
    failed: bool = False


class WorkcellSessionTrace(StrictModel):
    """Everything the adapter learned from one headless CLI session."""

    schema_version: str = "1.0"
    session_id: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    model_requests: int = Field(default=0, ge=0)
    compactions: int = Field(default=0, ge=0)
    ceiling_events: list[CeilingEvent] = Field(default_factory=list)
    #: True once the agent asked for inspection. Carries no authority.
    ready_for_evaluation: bool = False
    ended: bool = False
    end_reason: str | None = None
    errors: list[str] = Field(default_factory=list)
    malformed_lines: int = Field(default=0, ge=0)
    unrecognised_event_types: list[str] = Field(default_factory=list)

    @property
    def distinct_tools_used(self) -> list[str]:
        return sorted({item.tool_name for item in self.tool_calls})

    @property
    def shell_calls(self) -> list[ToolCallRecord]:
        return [
            item
            for item in self.tool_calls
            if item.tool_name in {"run_shell_command", "shell", "bash"}
        ]


class WorkcellEventAdapter:
    """Folds `stream-json` lines into a `WorkcellSessionTrace`.

    Construct one per session, feed it every stdout line, then read `.trace`.
    """

    def __init__(
        self,
        *,
        context_limit_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_tool_output_chars: int = 25_000,
    ) -> None:
        self.trace = WorkcellSessionTrace()
        self.context_limit_tokens = context_limit_tokens
        self.max_output_tokens = max_output_tokens
        self.max_tool_output_chars = max_tool_output_chars
        self._open_calls: dict[str, ToolCallRecord] = {}
        self._context_rolled_over = False

    def feed_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            # A truncated final line is the expected shape of an output-cap
            # hit mid-stream. Counting it keeps that visible instead of
            # letting the session look merely short.
            self.trace.malformed_lines += 1
            return
        if not isinstance(payload, dict):
            self.trace.malformed_lines += 1
            return
        self.feed_event(payload)

    def feed_event(self, payload: dict) -> None:
        raw_type = str(payload.get("type", ""))

        # The CLI does not emit one flat event per action. `assistant` and
        # `user` events are *envelopes* carrying a `message.content` list, and
        # the tool calls and tool results -- the only evidence any capability
        # was exercised -- live inside that list as nested blocks.
        #
        # Slice 2C found this the expensive way. The adapter recognised
        # `assistant`, `user`, and `result` at the top level, so it reported
        # zero malformed lines and zero unrecognised types while silently
        # counting zero tool calls across a 158-event session in which the
        # agent had plainly used tools. The paired spike then read that empty
        # trace as seven lost capabilities and returned CAPABILITY_REGRESSED.
        # A parser that fails by finding nothing, without ever saying it found
        # nothing, is worse than one that raises: unwrapping happens here so
        # the nested blocks reach the same handlers as everything else.
        if raw_type in _ENVELOPE_EVENT_TYPES:
            self._feed_envelope(payload)
            return

        kind = _EVENT_TYPE_MAP.get(raw_type)
        if kind is None:
            if raw_type not in self.trace.unrecognised_event_types:
                self.trace.unrecognised_event_types.append(raw_type)
            return
        handler = {
            WorkcellEventKind.SESSION_START: self._on_session_start,
            WorkcellEventKind.TOOL_CALL: self._on_tool_call,
            WorkcellEventKind.TOOL_RESULT: self._on_tool_result,
            WorkcellEventKind.USAGE: self._on_usage,
            WorkcellEventKind.COMPACTION: self._on_compaction,
            WorkcellEventKind.SESSION_END: self._on_session_end,
            WorkcellEventKind.ERROR: self._on_error,
        }.get(kind)
        if handler is not None:
            handler(payload)

    def _feed_envelope(self, payload: dict) -> None:
        """Dispatch the blocks nested inside an `assistant`/`user` envelope.

        Usage is read from the envelope rather than from a separate event
        because that is where the CLI puts it. It is fed through `_on_usage`
        unchanged so that ceiling classification stays in one place.
        """

        message = payload.get("message")
        if not isinstance(message, dict):
            # An envelope with no message is a malformed line even though it
            # parsed as JSON: the type promised content that is not there.
            self.trace.malformed_lines += 1
            return

        usage = message.get("usage")
        if isinstance(usage, dict):
            self._on_usage(
                _flatten_usage(usage, finish_reason=message.get("stop_reason"))
            )

        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", ""))
            if block_type == "tool_use":
                self._on_tool_call(
                    {
                        "id": block.get("id"),
                        "name": block.get("name"),
                        # The CLI names the argument object `input`; the flat
                        # schema called it `arguments`. Both are accepted so
                        # the handler stays the single definition of a call.
                        "arguments": block.get("input"),
                    }
                )
            elif block_type == "tool_result":
                content_text = block.get("content")
                self._on_tool_result(
                    {
                        "id": block.get("tool_use_id"),
                        "output": content_text
                        if isinstance(content_text, str)
                        else json.dumps(content_text, default=str),
                        # `is_error` is the only failure signal in this schema;
                        # there is no exit code on the block. Mapping it to a
                        # synthetic exit code would invent a number the CLI
                        # never reported, so `failed` is set directly instead.
                        "is_error": bool(block.get("is_error")),
                    }
                )

    # -- handlers ---------------------------------------------------------

    def _on_session_start(self, payload: dict) -> None:
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.trace.session_id = session_id

    def _on_tool_call(self, payload: dict) -> None:
        call_id = str(payload.get("call_id") or payload.get("id") or "")
        name = str(payload.get("name") or payload.get("tool") or "")
        if not call_id or not name:
            self.trace.malformed_lines += 1
            return
        arguments = payload.get("arguments")
        record = ToolCallRecord(
            call_id=call_id,
            tool_name=name,
            argument_keys=sorted(arguments) if isinstance(arguments, dict) else [],
        )
        self._open_calls[call_id] = record
        self.trace.tool_calls.append(record)
        if name == READY_SIGNAL:
            # A request for inspection. The controller decides what happens
            # next; the agent has not completed anything.
            self.trace.ready_for_evaluation = True

    def _on_tool_result(self, payload: dict) -> None:
        call_id = str(payload.get("call_id") or payload.get("id") or "")
        record = self._open_calls.pop(call_id, None)
        if record is None:
            # A result with no matching call means the transcript is
            # incomplete. Silently accepting it would hide a dropped call.
            self.trace.malformed_lines += 1
            return
        exit_code = payload.get("exit_code")
        if isinstance(exit_code, int):
            record.exit_code = exit_code
            record.failed = exit_code != 0
        elif payload.get("is_error"):
            # No exit code exists in the nested schema. `failed` is set without
            # inventing an `exit_code`, because a fabricated 1 would be
            # indistinguishable from one the tool actually reported.
            record.failed = True
        pid = payload.get("background_pid") or payload.get("pid")
        if isinstance(pid, int):
            record.background_pid = pid
        duration = payload.get("duration_seconds")
        if isinstance(duration, (int, float)) and duration >= 0:
            record.duration_seconds = float(duration)
        output = payload.get("output")
        text = output if isinstance(output, str) else ""
        record.output_chars = len(text)
        artifact = payload.get("output_artifact")
        if isinstance(artifact, str) and artifact:
            record.output_artifact = artifact
        truncated = payload.get("truncated")
        record.output_truncated = bool(truncated) or (
            record.output_chars >= self.max_tool_output_chars
        )
        if record.output_truncated:
            self.trace.ceiling_events.append(
                CeilingEvent(
                    reason=CeilingStopReason.TOOL_OUTPUT_TRUNCATION,
                    detail=(
                        f"tool {record.tool_name!r} output was clipped at "
                        f"{record.output_chars:,} characters"
                        + (
                            f"; full output at {record.output_artifact}"
                            if record.output_artifact
                            else "; no spill artifact was recorded, so the "
                            "truncation is not reversible"
                        )
                    ),
                )
            )
        if payload.get("error"):
            record.failed = True

    def _on_usage(self, payload: dict) -> None:
        self.trace.model_requests += 1
        for field, key in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cached_input_tokens", "cached_input_tokens"),
        ):
            value = payload.get(key)
            if isinstance(value, int) and value >= 0:
                setattr(self.trace, field, getattr(self.trace, field) + value)

        finish_reason = payload.get("finish_reason")
        reason = classify_ceiling_stop_reason(
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            input_tokens=_as_int(payload.get("input_tokens")),
            output_tokens=_as_int(payload.get("output_tokens")),
            context_limit=self.context_limit_tokens,
            max_output_tokens=self.max_output_tokens,
        )
        if reason is None:
            return
        if reason == CeilingStopReason.INPUT_CONTEXT_EXHAUSTED:
            # Remembered so the *next* provider error is attributed to the
            # window rather than to the provider's health -- the exact
            # sequence the unrestricted control hit.
            self._context_rolled_over = True
        self.trace.ceiling_events.append(
            CeilingEvent(
                reason=reason,
                input_tokens=_as_int(payload.get("input_tokens")),
                output_tokens=_as_int(payload.get("output_tokens")),
                context_limit=self.context_limit_tokens,
                max_output_tokens=self.max_output_tokens,
                finish_reason=finish_reason
                if isinstance(finish_reason, str)
                else None,
                detail="classified from provider-reported usage",
            )
        )

    def _on_compaction(self, payload: dict) -> None:
        self.trace.compactions += 1

    def _on_session_end(self, payload: dict) -> None:
        self.trace.ended = True

        # The CLI reports per-message usage as zeros and puts the session's
        # real totals on this final event. They are adopted only when nothing
        # else reported any, so a CLI that does populate per-message usage is
        # never double-counted. `model_requests` is deliberately left alone:
        # one summary event is not evidence of how many requests there were,
        # and inferring a count here would be inventing one.
        usage = payload.get("usage")
        if isinstance(usage, dict) and not (
            self.trace.input_tokens
            or self.trace.output_tokens
            or self.trace.cached_input_tokens
        ):
            flat = _flatten_usage(usage)
            for field in ("input_tokens", "output_tokens", "cached_input_tokens"):
                value = flat.get(field)
                if isinstance(value, int) and value >= 0:
                    setattr(self.trace, field, value)

        reason = payload.get("reason") or payload.get("stop_reason")
        if isinstance(reason, str) and reason:
            self.trace.end_reason = reason

    def _on_error(self, payload: dict) -> None:
        message = str(payload.get("message") or payload.get("error") or "unknown")
        self.trace.errors.append(message)
        reason = classify_ceiling_stop_reason(
            provider_error=True,
            context_rolled_over=self._context_rolled_over,
            context_limit=self.context_limit_tokens,
        )
        if reason is not None:
            self.trace.ceiling_events.append(
                CeilingEvent(
                    reason=reason,
                    context_limit=self.context_limit_tokens,
                    detail=(
                        "the provider failed on the request following a context "
                        f"rollover: {message}"
                    ),
                )
            )

    def finish(self) -> WorkcellSessionTrace:
        """Close the trace, recording any tool call that never got a result."""

        for call_id, record in self._open_calls.items():
            record.failed = True
            self.trace.errors.append(
                f"tool call {call_id!r} ({record.tool_name}) never reported a result"
            )
        self._open_calls.clear()
        return self.trace


def _flatten_usage(usage: dict, *, finish_reason: object = None) -> dict:
    """Lift a nested `usage` object into the flat shape `_on_usage` reads.

    `cache_read_input_tokens` is the CLI's name for what the trace calls
    `cached_input_tokens`. Both spellings are accepted rather than one being
    canonicalised at the source, because the source is a vendored CLI whose
    field names are not ours to normalise.
    """

    flat = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cached_input_tokens": usage.get("cached_input_tokens")
        if usage.get("cached_input_tokens") is not None
        else usage.get("cache_read_input_tokens"),
    }
    if finish_reason is not None:
        flat["finish_reason"] = finish_reason
    return flat


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None
