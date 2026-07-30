"""Execute the nine conformance checks against the real endpoint.

`conformance.py` declares the checks and classifies their evidence. It has no
way to *obtain* that evidence, which is why the Slice 2 live gate reported nine
`NOT_RUN` results and stopped. This module is the missing half.

Two design commitments follow from what the suite is for:

**Every request goes the way the agent's requests go.** The probes run inside
the workcell container and reach the model through the loopback forwarder, the
controller-owned Unix socket, and the relay. Testing the model server directly
would prove the model works and say nothing about the path the CLI uses, which
is the path that can mangle a tool envelope.

**Stop reasons are provoked, never assembled.** `check_stop_reason_fidelity`
takes a map of six outcomes to provider signals, and it would be trivially easy
to build that map from what we expect each condition to produce. Doing so would
turn the one check that would have caught the Crisis Atlas confusion between
context exhaustion and the output cap into a restatement of our own
assumptions. So each of the six is caused: a real overlong prompt, a real
`max_tokens` cap, a real mid-stream disconnect, a real bad request.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum

from pydantic import Field
from typing import Callable

from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.conformance import (
    CheckResult,
    ConformanceCheck,
    ConformanceStatus,
    ObservedToolCall,
    ObservedStopReason,
    check_declared_limits_match_server,
    check_envelope_integrity,
    check_parallel_tool_calls,
    check_replay_non_idempotence_guard,
    check_role_round_trip,
    check_single_tool_call,
    check_stop_reason_fidelity,
    check_thinking_block_handling,
    check_usage_accounting,
)
from apoapsis.workcell.echo_provider import ECHO_BEGIN, ECHO_END
from apoapsis.workcell.transcription import (
    TranscriptionFidelity,
    measure_transcription_fidelity,
)

#: Output budget for every probe whose point is *not* to hit the output cap.
#:
#: Found the hard way in the first Slice 2B live run. A reasoning model spends
#: its output budget on reasoning before it emits any content or tool call, so
#: probes capped at 64--256 tokens all terminated at the cap and reported
#: `finish_reason: length`. `check_stop_reason_fidelity` then correctly observed
#: that normal completion, the output limit, and a tool call were
#: indistinguishable -- but the collision was manufactured by the probes, not by
#: the provider, and reporting it as an adapter defect would have been a false
#: positive of exactly the kind this suite exists to prevent. The one probe that
#: genuinely wants the cap sets its own small value.
REASONING_HEADROOM_TOKENS = 2048

#: Replaced inside a payload by the probe, so a context-exhaustion request does
#: not have to travel as a quarter-megabyte command-line argument.
PAD_MARKER = "__APOAPSIS_PAD__"

#: One repeated unit of filler. Deliberately ordinary words: a repeated single
#: character can tokenize far more densely than real text and would make the
#: context-limit provocation unreliable.
PAD_UNIT = "context saturation filler token "

#: The multiline Unicode payload. Chosen to contain, in one string, every
#: corruption `check_multiline_unicode_integrity` knows how to name: real
#: newlines, a literal backslash-n that must *not* be unescaped, non-Latin-1
#: characters that double-encoding would mangle, and a trailing line that
#: truncation would drop.
UNICODE_PROBE_CONTENT = (
    "def résumé(x):\n"
    '    """Naïve — ‘quoted’, emoji: 🛰️, CJK: 光年."""\n'
    "    return x.replace('\\n', '<nl>')\n"
    "\n"
    "# tail line that truncation would remove\n"
)


class ProbeMode(StrEnum):
    #: Send the request, read the whole response.
    JSON = "json"
    #: Open a streaming request, read a little, then hang up. Used to provoke a
    #: genuine cancellation rather than to describe one.
    STREAM_CANCEL = "stream_cancel"


_PROBE_SOURCE = r"""
import json, sys, http.client, urllib.parse

mode, url, payload, timeout, pad_chars = sys.argv[1:6]
timeout = float(timeout)
pad = int(pad_chars)
if pad > 0:
    unit = "context saturation filler token "
    payload = payload.replace(
        "__APOAPSIS_PAD__", (unit * (pad // len(unit) + 1))[:pad]
    )
parts = urllib.parse.urlsplit(url)
body = payload.encode("utf-8") if payload else None


def emit(obj):
    sys.stdout.write("\n" + json.dumps(obj) + "\n")


connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
target = parts.path + (("?" + parts.query) if parts.query else "")
try:
    headers = {"Content-Type": "application/json"} if body else {}
    connection.request("POST" if body else "GET", target, body=body, headers=headers)
    response = connection.getresponse()
    if mode == "stream_cancel":
        # Read one chunk to prove the stream started, then drop the connection
        # without draining it. The server keeps generating for a reader that no
        # longer exists, which is exactly the condition being provoked.
        first = response.read1(512)
        connection.close()
        emit({"status": response.status, "bytes_read": len(first),
              "cancelled": True, "body": first.decode("utf-8", "replace")})
    else:
        raw = response.read()
        emit({"status": response.status,
              "body": raw.decode("utf-8", "replace")})
except Exception as exc:
    emit({"status": 0, "error": "%s: %s" % (type(exc).__name__, exc)})
finally:
    try:
        connection.close()
    except Exception:
        pass
"""


def apply_pad(payload: str, pad_chars: int) -> str:
    """Expand the padding marker exactly as the in-container probe does.

    Duplicated deliberately: the probe cannot import Apoapsis (the image has no
    such package and mounting one would be a second policy surface), so this
    exists to keep the expansion testable on the controller side.
    """

    if pad_chars <= 0:
        return payload
    filler = (PAD_UNIT * (pad_chars // len(PAD_UNIT) + 1))[:pad_chars]
    return payload.replace(PAD_MARKER, filler)


def build_conformance_probe_argv(
    *,
    url: str,
    payload: str,
    mode: ProbeMode = ProbeMode.JSON,
    timeout_seconds: float = 300.0,
    pad_chars: int = 0,
) -> list[str]:
    """The argv executed inside the workcell for one conformance probe."""

    return [
        "python3",
        "-c",
        _PROBE_SOURCE,
        mode.value,
        url,
        payload,
        str(timeout_seconds),
        str(pad_chars),
    ]


class ProbeOutcome(StrictModel):
    """One probe execution, decoded but not yet judged."""

    status: int = 0
    body: dict | None = None
    raw_body: str = ""
    error: str = ""
    cancelled: bool = False
    bytes_read: int = 0

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.body is not None


def parse_probe_output(stdout: str) -> ProbeOutcome:
    """Decode the probe's last JSON line.

    The last line specifically: the CLI image's Python may print warnings, and
    a parser that took the first line would report a deprecation notice as a
    transport failure.
    """

    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    if not lines:
        return ProbeOutcome(error="the probe produced no output")
    try:
        envelope = json.loads(lines[-1])
    except ValueError:
        return ProbeOutcome(error=f"unparseable probe output: {lines[-1][:200]!r}")
    if not isinstance(envelope, dict):
        return ProbeOutcome(error="the probe did not emit a JSON object")

    raw = envelope.get("body") or ""
    body: dict | None = None
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
            body = parsed if isinstance(parsed, dict) else None
        except ValueError:
            body = None
    return ProbeOutcome(
        status=int(envelope.get("status") or 0),
        body=body,
        raw_body=raw if isinstance(raw, str) else "",
        error=str(envelope.get("error") or ""),
        cancelled=bool(envelope.get("cancelled")),
        bytes_read=int(envelope.get("bytes_read") or 0),
    )


def observed_tool_calls(body: dict) -> list[ObservedToolCall]:
    """Pull the tool calls out of a chat completion, preserving their order."""

    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return []
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return []
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    observed: list[ObservedToolCall] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        function = function if isinstance(function, dict) else {}
        raw = function.get("arguments")
        raw = raw if isinstance(raw, str) else json.dumps(raw or {})
        arguments: dict | None = None
        parse_error: str | None = None
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                arguments = decoded
            else:
                parse_error = "arguments were not a JSON object"
        except ValueError as exc:
            parse_error = str(exc)
        observed.append(
            ObservedToolCall(
                call_id=str(call.get("id") or ""),
                name=str(function.get("name") or "unnamed"),
                raw_arguments=raw,
                arguments=arguments,
                parse_error=parse_error,
            )
        )
    return observed


def first_choice(body: dict) -> dict:
    choices = body.get("choices") or []
    return choices[0] if choices and isinstance(choices[0], dict) else {}


def assistant_text(body: dict) -> str:
    message = first_choice(body).get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_thinking_blocks(text: str) -> str:
    """Remove `<think>...</think>` spans.

    Non-greedy and single-pass by construction, so applying it twice is a no-op
    on its own output. The check exists because a stripper that is *not*
    idempotent silently eats a literal `<think>` a coding agent wrote inside a
    code block -- which is exactly what an agent writes when the task is about
    parsing tags.
    """

    return _THINK_BLOCK.sub("", text)


class ReplayGuardedExecutor:
    """Executes tool calls at most once each, keyed on the provider's call id.

    This is the harness behaviour `REPLAY_NON_IDEMPOTENCE_GUARD` is about. A
    retry after a provider error or a truncated response replays an assistant
    message that may already have run `write_file`; without this ledger the
    second execution is invisible in the transcript and surfaces only as a
    mysterious delta.
    """

    def __init__(self, execute: Callable[[ObservedToolCall], None]) -> None:
        self._execute = execute
        self.executed_call_ids: list[str] = []
        self._seen: set[str] = set()

    def submit(self, call: ObservedToolCall) -> bool:
        """Run the call unless its id already ran. Returns whether it ran."""

        if not call.call_id:
            # An unidentified call cannot be deduplicated, so it must not be
            # executed at all; silently running it would defeat the guard.
            return False
        if call.call_id in self._seen:
            return False
        self._seen.add(call.call_id)
        self._execute(call)
        self.executed_call_ids.append(call.call_id)
        return True


#: Tool schemas used by the suite. Small and fixed: these exist to exercise the
#: envelope, not to resemble the CLI's real portfolio, which is pinned
#: separately from the wire by `pin_capture`.
ECHO_TOOL = {
    "type": "function",
    "function": {
        "name": "echo_value",
        "description": "Echo a value back verbatim.",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    },
}

WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write exact content to a path.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
}

READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}

LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": "List a directory.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


class DeclaredCliLimits(StrictModel):
    """What the CLI believes about the window, and where that was read from.

    `source` is required and free-form on purpose: a limit whose provenance is
    not recorded is the provisional-pin problem again, and the check must be
    able to say `NOT_RUN` when nothing authoritative was found.
    """

    context_limit_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    source: str = Field(min_length=1)


ExecFn = Callable[[list[str], float], tuple[int | None, str, str]]


class LiveConformanceRunner:
    """Drives the nine checks through the relay and collects their evidence."""

    def __init__(
        self,
        *,
        exec_fn: ExecFn,
        base_url: str,
        model_name: str,
        context_limit_tokens: int,
        server_max_output_tokens: int,
        supports_parallel_tool_calls: bool = True,
        declared_cli_limits: DeclaredCliLimits | None = None,
        mutating_tool_runner: Callable[[ObservedToolCall], None] | None = None,
        envelope_base_url: str | None = None,
        captured_envelope_bytes: bytes | None = None,
    ) -> None:
        self.exec_fn = exec_fn
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.context_limit_tokens = context_limit_tokens
        self.server_max_output_tokens = server_max_output_tokens
        self.supports_parallel_tool_calls = supports_parallel_tool_calls
        self.declared_cli_limits = declared_cli_limits
        self.mutating_tool_runner = mutating_tool_runner
        #: Base URL of the second forwarder/relay pair whose upstream is the
        #: deterministic echo provider. `None` makes the envelope check
        #: `NOT_RUN` rather than letting it degrade into a model measurement.
        self.envelope_base_url = (
            envelope_base_url.rstrip("/") if envelope_base_url else None
        )
        #: The echo provider's verbatim record of the request it received. Set
        #: by the session after the probe runs.
        self.captured_envelope_bytes = captured_envelope_bytes
        #: Provider signals actually observed, one per provoked outcome.
        self.stop_signals: dict[ObservedStopReason, str] = {}

    # -- transport ---------------------------------------------------------

    def _post(
        self,
        payload: dict,
        *,
        mode: ProbeMode = ProbeMode.JSON,
        timeout_seconds: float = 300.0,
        pad_chars: int = 0,
        path: str = "/v1/chat/completions",
        base_url: str | None = None,
    ) -> ProbeOutcome:
        argv = build_conformance_probe_argv(
            url=f"{base_url or self.base_url}{path}",
            payload=json.dumps(payload),
            mode=mode,
            timeout_seconds=timeout_seconds,
            pad_chars=pad_chars,
        )
        exit_code, stdout, stderr = self.exec_fn(argv, timeout_seconds + 60.0)
        if exit_code is None:
            return ProbeOutcome(
                error=f"the probe did not complete inside the workcell: {stderr[:200]}"
            )
        return parse_probe_output(stdout)

    def _chat(self, **overrides: object) -> dict:
        payload: dict = {
            "model": self.model_name,
            "temperature": 0,
            "stream": False,
        }
        payload.update(overrides)
        return payload

    # -- the nine checks ---------------------------------------------------

    def run_role_round_trip(self) -> CheckResult:
        roles = ["system", "user", "assistant", "tool"]
        outcome = self._post(
            self._chat(
                max_tokens=REASONING_HEADROOM_TOKENS,
                tools=[READ_TOOL],
                messages=[
                    {"role": "system", "content": "You are a terse coding assistant."},
                    {"role": "user", "content": "Read config.txt and report its contents."},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_role_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "config.txt"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_role_1",
                        "content": "mode=alpha",
                    },
                    {"role": "user", "content": "What value did the file report?"},
                ],
            )
        )
        if not outcome.ok:
            return check_role_round_trip(
                roles_sent=roles,
                status=outcome.status or 0,
                assistant_text="",
            )
        self._note_stop(outcome)
        return check_role_round_trip(
            roles_sent=roles,
            status=outcome.status,
            assistant_text=assistant_text(outcome.body or {}),
        )

    def run_single_tool_call(self) -> CheckResult:
        expected = {"value": "Ωmega-42"}
        outcome = self._post(
            self._chat(
                max_tokens=REASONING_HEADROOM_TOKENS,
                tools=[ECHO_TOOL],
                tool_choice="required",
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Call echo_value once with the value exactly "
                            "Ωmega-42 and nothing else."
                        ),
                    }
                ],
            )
        )
        if not outcome.ok:
            return CheckResult(
                check=ConformanceCheck.SINGLE_TOOL_CALL,
                status=ConformanceStatus.FAILED,
                detail=(
                    f"the tool-call request failed at the transport: HTTP "
                    f"{outcome.status} {outcome.error or outcome.raw_body[:200]}"
                ),
            )
        self._note_stop(outcome)
        return check_single_tool_call(
            expected_name="echo_value",
            expected_arguments=expected,
            observed=observed_tool_calls(outcome.body or {}),
        )

    def run_parallel_tool_calls(self) -> CheckResult:
        outcome = self._post(
            self._chat(
                max_tokens=REASONING_HEADROOM_TOKENS,
                tools=[READ_TOOL, LIST_TOOL],
                tool_choice="required",
                parallel_tool_calls=True,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "In a single turn, make exactly two tool calls, in "
                            "this order: first read_file with path 'alpha.txt', "
                            "then list_directory with path 'src'."
                        ),
                    }
                ],
            )
        )
        if not outcome.ok:
            return CheckResult(
                check=ConformanceCheck.PARALLEL_TOOL_CALLS,
                status=ConformanceStatus.FAILED,
                detail=(
                    f"the parallel tool-call request failed at the transport: "
                    f"HTTP {outcome.status} {outcome.error or outcome.raw_body[:200]}"
                ),
            )
        self._note_stop(outcome)
        return check_parallel_tool_calls(
            expected=[
                ("read_file", {"path": "alpha.txt"}),
                ("list_directory", {"path": "src"}),
            ],
            observed=observed_tool_calls(outcome.body or {}),
            server_declares_support=self.supports_parallel_tool_calls,
        )

    def run_multiline_unicode_integrity(self) -> CheckResult:
        """Round-trip the payload through the real path and a fake model.

        ADR 0078. The request goes through the in-container forwarder, the
        controller-owned Unix socket, and a real `ModelRelay` -- the same three
        hops the agent's requests take -- but the relay's upstream for this one
        check is `DeterministicEchoProvider` rather than `llama-server`. The
        model is the only thing removed, because the model is the only thing
        the check was never trying to measure.

        `envelope_base_url` is required and there is no fallback. An
        unavailable echo path makes the check `NOT_RUN`, and `NOT_RUN` fails
        the gate: the previous design's willingness to fall back on whatever
        evidence was available is how it ended up reporting a model's quotation
        marks as an adapter defect.
        """

        if self.envelope_base_url is None:
            return CheckResult(
                check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
                status=ConformanceStatus.NOT_RUN,
                detail=(
                    "no deterministic echo path was configured, so envelope "
                    "integrity was not exercised; a model-transcription "
                    "substitute would measure the model instead"
                ),
            )
        outcome = self._post(
            {
                "model": self.model_name,
                "temperature": 0,
                "stream": False,
                "max_tokens": 16,
                "tools": [WRITE_TOOL],
                "messages": [
                    {
                        "role": "user",
                        "content": ECHO_BEGIN + UNICODE_PROBE_CONTENT + ECHO_END,
                    }
                ],
            },
            base_url=self.envelope_base_url,
        )
        if not outcome.ok:
            return CheckResult(
                check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
                status=ConformanceStatus.FAILED,
                detail=(
                    f"the envelope round trip failed at the transport: HTTP "
                    f"{outcome.status} {outcome.error or outcome.raw_body[:200]}"
                ),
            )
        calls = observed_tool_calls(outcome.body or {})
        if not calls or calls[0].arguments is None:
            return CheckResult(
                check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
                status=ConformanceStatus.FAILED,
                detail=(
                    "the echoed tool call did not come back parseable, which "
                    "with a deterministic provider can only be the envelope"
                ),
            )
        received = calls[0].arguments.get("content")
        received_bytes = (
            received.encode("utf-8") if isinstance(received, str) else b""
        )
        # `captured_request_bytes` is filled by the session from the echo
        # provider's own record of the request. When it is absent the check
        # falls back to the payload constant, and says so: comparing against a
        # constant proves the response half only.
        sent_bytes = self.captured_envelope_bytes
        if sent_bytes is None:
            result = check_envelope_integrity(
                sent_bytes=UNICODE_PROBE_CONTENT.encode("utf-8"),
                received_bytes=received_bytes,
            )
            return result.model_copy(
                update={
                    "detail": (
                        result.detail
                        + " (compared against the probe constant: the echo "
                        "provider's captured request bytes were not available "
                        "to this runner, so the inbound half is unverified)"
                    )
                }
            )
        return check_envelope_integrity(
            sent_bytes=sent_bytes, received_bytes=received_bytes
        )

    def run_transcription_fidelity(self) -> TranscriptionFidelity:
        """Ask the real model to retype the payload. Reported, never gating.

        This is the signal ADR 0078 removed from the conformance gate, kept
        rather than deleted. It runs against `llama-server` and the real chat
        template, so it also happens to be the only place the multiline payload
        meets the template -- which the ADR records as an accepted narrowing of
        gated coverage, not as an equivalent substitute.
        """

        outcome = self._post(
            self._chat(
                max_tokens=REASONING_HEADROOM_TOKENS * 2,
                tools=[WRITE_TOOL],
                tool_choice="required",
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Call write_file with path 'probe.py' and content set "
                            "to the exact text between the markers, byte for "
                            "byte, preserving every newline and character.\n"
                            "<<<BEGIN>>>\n"
                            + UNICODE_PROBE_CONTENT
                            + "<<<END>>>"
                        ),
                    }
                ],
            )
        )
        if not outcome.ok:
            return measure_transcription_fidelity(
                sent=UNICODE_PROBE_CONTENT, received=None
            )
        self._note_stop(outcome)
        calls = observed_tool_calls(outcome.body or {})
        if not calls or calls[0].arguments is None:
            return measure_transcription_fidelity(
                sent=UNICODE_PROBE_CONTENT, received=None
            )
        received = calls[0].arguments.get("content")
        return measure_transcription_fidelity(
            sent=UNICODE_PROBE_CONTENT,
            received=received if isinstance(received, str) else None,
        )

    def run_thinking_block_handling(self) -> CheckResult:
        outcome = self._post(
            self._chat(
                max_tokens=REASONING_HEADROOM_TOKENS,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Reply with exactly this line and nothing else: "
                            "keep <think> literal inside this sentence."
                        ),
                    }
                ],
            )
        )
        if not outcome.ok:
            return CheckResult(
                check=ConformanceCheck.THINKING_BLOCK_HANDLING,
                status=ConformanceStatus.FAILED,
                detail=(
                    f"the thinking-block request failed at the transport: HTTP "
                    f"{outcome.status} {outcome.error or outcome.raw_body[:200]}"
                ),
            )
        self._note_stop(outcome)
        message = first_choice(outcome.body or {}).get("message")
        message = message if isinstance(message, dict) else {}
        reasoning = message.get("reasoning_content")
        supported = isinstance(reasoning, str) and bool(reasoning.strip())
        text = assistant_text(outcome.body or {})
        once = strip_thinking_blocks(text)
        return check_thinking_block_handling(
            supported=supported,
            stripped_once=once,
            stripped_twice=strip_thinking_blocks(once),
        )

    def run_usage_accounting(self) -> CheckResult:
        requested = 16
        outcome = self._post(
            self._chat(
                max_tokens=requested,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Write a detailed multi-paragraph description of the "
                            "history of timekeeping."
                        ),
                    }
                ],
            )
        )
        if not outcome.ok:
            return CheckResult(
                check=ConformanceCheck.USAGE_ACCOUNTING,
                status=ConformanceStatus.FAILED,
                detail=(
                    f"the usage request failed at the transport: HTTP "
                    f"{outcome.status} {outcome.error or outcome.raw_body[:200]}"
                ),
            )
        body = outcome.body or {}
        finish = first_choice(body).get("finish_reason")
        self._note_stop(outcome)
        return check_usage_accounting(
            requested_max_output=requested,
            usage=body.get("usage") if isinstance(body.get("usage"), dict) else None,
            finish_reason=finish if isinstance(finish, str) else None,
        )

    def run_stop_reason_fidelity(self) -> CheckResult:
        """Cause all six outcomes, then classify the signals they produced."""

        self._provoke_normal_completion()
        self._provoke_tool_call()
        self._provoke_output_limit()
        self._provoke_context_limit()
        self._provoke_cancellation()
        self._provoke_provider_error()
        return check_stop_reason_fidelity(self.stop_signals)

    def run_replay_non_idempotence_guard(self) -> CheckResult:
        """Replay one real mutating tool call and count its executions."""

        outcome = self._post(
            self._chat(
                max_tokens=REASONING_HEADROOM_TOKENS,
                tools=[WRITE_TOOL],
                tool_choice="required",
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Call write_file with path 'replay-probe.txt' and "
                            "content 'one'."
                        ),
                    }
                ],
            )
        )
        if not outcome.ok:
            return CheckResult(
                check=ConformanceCheck.REPLAY_NON_IDEMPOTENCE_GUARD,
                status=ConformanceStatus.FAILED,
                detail=(
                    f"the mutating tool-call request failed at the transport: "
                    f"HTTP {outcome.status} {outcome.error or outcome.raw_body[:200]}"
                ),
            )
        self._note_stop(outcome)
        calls = observed_tool_calls(outcome.body or {})
        if not calls:
            return CheckResult(
                check=ConformanceCheck.REPLAY_NON_IDEMPOTENCE_GUARD,
                status=ConformanceStatus.INCONCLUSIVE,
                detail="the provider returned no mutating call to replay",
            )
        call = calls[0]
        if not call.call_id:
            return CheckResult(
                check=ConformanceCheck.REPLAY_NON_IDEMPOTENCE_GUARD,
                status=ConformanceStatus.FAILED,
                detail=(
                    "the provider returned a mutating tool call with no call id, "
                    "so a replay cannot be distinguished from a new call"
                ),
            )
        runner = self.mutating_tool_runner or (lambda _call: None)
        executor = ReplayGuardedExecutor(runner)
        executor.submit(call)
        # The same provider response, delivered a second time, exactly as a
        # reconnect or a retry after a truncated read would deliver it.
        executor.submit(call)
        return check_replay_non_idempotence_guard(
            mutating_tool_call_id=call.call_id,
            executed_call_ids=executor.executed_call_ids,
        )

    def run_declared_limits_match_server(self) -> CheckResult:
        if self.declared_cli_limits is None:
            return CheckResult(
                check=ConformanceCheck.DECLARED_LIMITS_MATCH_SERVER,
                status=ConformanceStatus.NOT_RUN,
                detail=(
                    "the CLI's declared context and output limits were not read "
                    "from the CLI, and an assumed value would pin nothing"
                ),
            )
        result = check_declared_limits_match_server(
            cli_context_limit=self.declared_cli_limits.context_limit_tokens,
            cli_max_output=self.declared_cli_limits.max_output_tokens,
            server_context_limit=self.context_limit_tokens,
            server_max_output=self.server_max_output_tokens,
        )
        return result.model_copy(
            update={
                "detail": f"{result.detail} (CLI values read from "
                f"{self.declared_cli_limits.source})"
            }
        )

    def run_all(self) -> list[CheckResult]:
        """Run every check. Order matters only for cost, not correctness."""

        return [
            self.run_role_round_trip(),
            self.run_single_tool_call(),
            self.run_parallel_tool_calls(),
            self.run_multiline_unicode_integrity(),
            self.run_thinking_block_handling(),
            self.run_usage_accounting(),
            self.run_replay_non_idempotence_guard(),
            self.run_stop_reason_fidelity(),
            self.run_declared_limits_match_server(),
        ]

    # -- stop-reason provocations -----------------------------------------

    def _note_stop(self, outcome: ProbeOutcome) -> None:
        """Record whichever ordinary outcome this response happened to be.

        Called from the other checks so their responses are not wasted: a turn
        that ended normally is evidence about the normal-completion signal
        whether or not it was provoked for that purpose.
        """

        if not outcome.ok:
            return
        finish = first_choice(outcome.body or {}).get("finish_reason")
        if not isinstance(finish, str) or not finish:
            return
        if finish == "tool_calls":
            self.stop_signals.setdefault(ObservedStopReason.TOOL_CALL, finish)
        elif finish == "stop":
            self.stop_signals.setdefault(ObservedStopReason.NORMAL_COMPLETION, finish)
        elif finish == "length":
            self.stop_signals.setdefault(ObservedStopReason.OUTPUT_LIMIT, finish)

    def _provoke_normal_completion(self) -> None:
        outcome = self._post(
            self._chat(
                max_tokens=REASONING_HEADROOM_TOKENS,
                messages=[{"role": "user", "content": "Reply with the word done."}],
            )
        )
        self._record_finish(ObservedStopReason.NORMAL_COMPLETION, outcome)

    def _provoke_tool_call(self) -> None:
        outcome = self._post(
            self._chat(
                max_tokens=REASONING_HEADROOM_TOKENS,
                tools=[ECHO_TOOL],
                tool_choice="required",
                messages=[{"role": "user", "content": "Echo the value ok."}],
            )
        )
        self._record_finish(ObservedStopReason.TOOL_CALL, outcome)

    def _provoke_output_limit(self) -> None:
        outcome = self._post(
            self._chat(
                max_tokens=8,
                messages=[
                    {
                        "role": "user",
                        "content": "Count slowly from one to five hundred in words.",
                    }
                ],
            )
        )
        self._record_finish(ObservedStopReason.OUTPUT_LIMIT, outcome)

    def _provoke_context_limit(self) -> None:
        """Send a prompt that genuinely cannot fit in the window."""

        # Roughly four characters per token of ordinary English, then a wide
        # margin: the point is to be unambiguously over the window, not to find
        # its exact edge.
        pad_chars = self.context_limit_tokens * 6
        outcome = self._post(
            self._chat(
                max_tokens=16,
                messages=[{"role": "user", "content": PAD_MARKER}],
            ),
            pad_chars=pad_chars,
            timeout_seconds=600.0,
        )
        self.stop_signals[ObservedStopReason.CONTEXT_LIMIT] = _error_signal(
            outcome, fallback="context_limit_unreported"
        )

    def _provoke_cancellation(self) -> None:
        outcome = self._post(
            self._chat(
                max_tokens=512,
                stream=True,
                messages=[
                    {
                        "role": "user",
                        "content": "Write a long essay about orbital mechanics.",
                    }
                ],
            ),
            mode=ProbeMode.STREAM_CANCEL,
            timeout_seconds=120.0,
        )
        if outcome.cancelled and outcome.status == 200:
            self.stop_signals[ObservedStopReason.CANCELLED] = (
                f"client_disconnect_after_{outcome.bytes_read}_bytes"
            )
        else:
            self.stop_signals[ObservedStopReason.CANCELLED] = _error_signal(
                outcome, fallback="cancellation_unreported"
            )

    def _provoke_provider_error(self) -> None:
        outcome = self._post(
            self._chat(
                max_tokens=16,
                model="model-that-does-not-exist",
                messages=[{"role": "user", "content": "hello"}],
            )
        )
        self.stop_signals[ObservedStopReason.PROVIDER_ERROR] = _error_signal(
            outcome, fallback="provider_error_unreported"
        )

    def _record_finish(self, reason: ObservedStopReason, outcome: ProbeOutcome) -> None:
        if not outcome.ok:
            self.stop_signals[reason] = _error_signal(
                outcome, fallback=f"{reason.value}_unreported"
            )
            return
        finish = first_choice(outcome.body or {}).get("finish_reason")
        self.stop_signals[reason] = (
            finish if isinstance(finish, str) and finish else "no_finish_reason"
        )


def _error_signal(outcome: ProbeOutcome, *, fallback: str) -> str:
    """A stable, comparable signal string for a non-200 outcome.

    The signal has to distinguish *kinds* of failure without embedding a
    request-specific message, or every provocation would look distinct and the
    collision check would pass vacuously.
    """

    if outcome.status and outcome.status != 200:
        kind = _error_kind(outcome)
        return f"http_{outcome.status}:{kind}"
    if outcome.error:
        return f"transport:{outcome.error.split(':')[0]}"
    return fallback


def _error_kind(outcome: ProbeOutcome) -> str:
    body = outcome.body or {}
    error = body.get("error")
    if isinstance(error, dict):
        for key in ("type", "code"):
            value = error.get(key)
            if isinstance(value, str) and value:
                return value
    text = (outcome.raw_body or "").lower()
    if "context" in text or "exceed" in text or "too long" in text:
        return "context_exceeded"
    if "not found" in text or "unknown model" in text or "does not exist" in text:
        return "model_not_found"
    return "unclassified"
