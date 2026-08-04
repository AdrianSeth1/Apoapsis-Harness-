"""An OpenAI-compatible server that scripts tool calls and reaches no model.

The rehearsal has to exercise the path a live run uses, and that path is: Qwen
CLI -> in-container forwarder -> Unix socket -> controller relay -> upstream.
A provider object with a `complete()` method cannot sit at the end of it, so
this is a real HTTP server the relay forwards to.

Its responses are scripted, and they are scripted *as native tool calls*.
That distinction is the whole point of this module. The earlier runner applied
candidate files itself with `_apply_script`, which meant the rehearsal proved
that Python can write files -- not that Qwen, driven through the relay, would
produce the candidate. Emitting `tool_calls` for `write_file` puts the real CLI
in the loop: if the tool surface is not what the manifest expects, or the CLI
refuses the call, or the envelope does not survive the relay, the candidate
does not appear and the slot fails. Those are exactly the failures a rehearsal
is for.

It holds no model client of any kind. Every response comes from a table, and a
request beyond the script gets a deterministic terminal reply rather than an
invention, because a provider that improvised would be supplying behaviour the
manifest never bound.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from apoapsis.qualification.fake_pilot_provider import SCRIPTS, ScriptId

#: Bound into the manifest alongside the script digest, so a change to the wire
#: shape invalidates authorization even when the candidate bytes are identical.
SERVER_VERSION = "1.0"


def _tool_call(index: int, path: str, content: str) -> dict:
    """One `write_file` call in the shape the Qwen CLI consumes."""

    return {
        "id": f"call_{index:03d}",
        "type": "function",
        "function": {
            "name": "write_file",
            # Arguments are a JSON *string*, as the OpenAI wire format
            # requires. Sending an object here is the single most common way
            # to make a tool call that looks right and never executes.
            "arguments": json.dumps(
                {"file_path": path, "content": content}, ensure_ascii=False
            ),
        },
    }


#: The CLI's `write_file` schema says: "The absolute path to the file to write
#: to. Relative paths are not supported." Sending relative paths produced tool
#: calls the CLI accepted and silently did not execute -- the run completed,
#: the summary turn arrived, and no candidate file existed. Rooting the paths
#: is what makes the call actually run.
WORKSPACE_ROOT = "/workspace"

#: Placed in `probe_source.txt` by the controller and written back out by the
#: agent. It can only reach the written file by being read first, which is what
#: makes read capability observable rather than asserted.
PROBE_MARKER = "APOAPSIS-CAPABILITY-MARKER-7P5"


def _classify_authorization(header: str | None) -> str:
    """What kind of thing the arm sent, without treating it as a secret.

    Three outcomes, and the middle one is the point. `declared_placeholder`
    means the arm sent exactly the public, non-secret value the manifest binds;
    `unrecognised` means it sent something else, which is the finding -- a value
    this evidence has no account of is the only shape that could be a real
    credential. Classifying rather than redacting keeps that distinction
    readable, which redaction would destroy.
    """

    from apoapsis.qualification.slot_driver import LOCAL_PLACEHOLDER_API_KEY

    if not header:
        return "absent"
    value = header.split(" ", 1)[-1].strip() if " " in header else header.strip()
    if value == LOCAL_PLACEHOLDER_API_KEY:
        return "declared_placeholder"
    return "unrecognised"


def tool_schema_digest(tools: object) -> str:
    """One digest over the whole declared tool schema, not just the names.

    Names alone would miss a changed parameter contract -- and a changed
    contract is precisely what makes a tool call look right and never execute,
    as the relative-vs-absolute `file_path` requirement already demonstrated.
    """

    return hashlib.sha256(
        json.dumps(tools, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def script_turns(script: ScriptId, *, root: str = WORKSPACE_ROOT) -> list[dict]:
    """Turn a scripted change set into successive assistant messages.

    One turn carrying every `write_file` call, then a turn with no tool calls
    to end the loop. Qwen executes the calls between them, so the files appear
    because the CLI wrote them.
    """

    turn = SCRIPTS[script][0]
    proposal = json.loads(turn.content)

    if script is ScriptId.WEB_FETCH_EGRESS_PROBE:
        # Calls the tool rather than reasoning about it. `web_fetch` being in
        # the surface is acceptable; a successful fetch under `--network none`
        # would not be, and only invoking it distinguishes the two.
        return [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_egress",
                        "type": "function",
                        "function": {
                            "name": "web_fetch",
                            # Both `url` and `prompt` are required; sending
                            # either alone yields "params must have required
                            # property ...", so the tool rejects the call and
                            # NO fetch is attempted. A classifier that treated
                            # any error as a refusal would score that schema
                            # complaint as proof of containment.
                            #
                            # The caller passes a unique URL per run because
                            # the tool serves repeat fetches of the same URL
                            # from a 15-minute local cache -- a cache hit would
                            # not exercise the network boundary at all.
                            "arguments": json.dumps(
                                {
                                    "url": proposal["url"],
                                    "prompt": proposal["prompt"],
                                }
                            ),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {
                "role": "assistant",
                "content": proposal["summary"],
                "tool_calls": None,
                "finish_reason": turn.finish_reason,
            },
        ]

    if script is ScriptId.CAPABILITY_PROBE:
        # Three tools, one per capability, so a partial surface shows up as a
        # partial result instead of a single opaque failure.
        return [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps(
                                {"absolute_path": f"{root}/probe_source.txt"}
                            ),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_write",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {
                                    "file_path": f"{root}/probe_written.txt",
                                    "content": PROBE_MARKER + "\n",
                                }
                            ),
                        },
                    },
                    {
                        "id": "call_shell",
                        "type": "function",
                        "function": {
                            "name": "run_shell_command",
                            "arguments": json.dumps(
                                {
                                    "command": (
                                        f"echo SHELL_RAN > {root}/probe_shell.txt"
                                    ),
                                    "description": "prove shell capability",
                                }
                            ),
                        },
                    },
                ],
                "finish_reason": "tool_calls",
            },
            {
                "role": "assistant",
                "content": proposal["summary"],
                "tool_calls": None,
                "finish_reason": turn.finish_reason,
            },
        ]

    calls = [
        _tool_call(
            index,
            f"{root.rstrip('/')}/{change['path'].lstrip('/')}",
            change["content"],
        )
        for index, change in enumerate(proposal["changes"])
    ]
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": calls,
            "finish_reason": "tool_calls",
        },
        {
            "role": "assistant",
            "content": proposal["summary"],
            "tool_calls": None,
            "finish_reason": turn.finish_reason,
        },
    ]


class FakeProviderServer:
    """Serves one script over HTTP. Counts every request it answers."""

    def __init__(
        self,
        script: ScriptId,
        *,
        model_name: str,
        host: str = "127.0.0.1",
        port: int = 0,
        transcript_path: Path | None = None,
    ) -> None:
        self.script = script
        self.model_name = model_name
        self.transcript_path = transcript_path
        self._turns = script_turns(script)
        self._served = 0
        self._requests: list[dict] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer((host, port), self._handler())
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self._requests)

    @property
    def observed_tool_names(self) -> tuple[str, ...]:
        """The tool surface the CLI actually declared, taken off the wire.

        This is the authoritative answer, and it is not the one a static probe
        gives: searching the installed bundle matches vendored files, and the
        manifest's expectation came from a wire capture of a *different* image.
        What the CLI sends in `tools` is what the CLI has.
        """

        with self._lock:
            for entry in self._requests:
                body = entry.get("body")
                if not isinstance(body, dict):
                    continue
                tools = body.get("tools")
                if not tools:
                    continue
                names = {
                    (tool.get("function") or tool).get("name")
                    for tool in tools
                    if isinstance(tool, dict)
                }
                return tuple(sorted(name for name in names if name))
        return ()

    @property
    def observed_tool_schema(self) -> list | None:
        """The complete `tools` array the CLI declared, for digesting."""

        with self._lock:
            for entry in self._requests:
                body = entry.get("body")
                if isinstance(body, dict) and body.get("tools"):
                    return body["tools"]
        return None

    @property
    def requests(self) -> list[dict]:
        with self._lock:
            return list(self._requests)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self.transcript_path is not None:
            self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
            self.transcript_path.write_text(
                json.dumps(self.requests, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    def __enter__(self) -> "FakeProviderServer":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


    def _next_turn(self) -> dict:
        with self._lock:
            index = self._served
            self._served += 1
        if index < len(self._turns):
            return self._turns[index]
        # Past the script. A terminal reply rather than a repeat or an
        # invention: repeating would loop the CLI forever, and inventing would
        # supply behaviour the manifest never bound.
        return {
            "role": "assistant",
            "content": "script exhausted",
            "tool_calls": None,
            "finish_reason": "stop",
        }

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:  # noqa: N802
                # The relay and the rehearsal evidence record the traffic; a
                # second uncorrelated log on stderr is noise during a run.
                return

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_stream(self, turn: dict, include_usage: bool = False) -> None:
                """Emit the turn as Server-Sent Events.

                Tool calls in a streamed delta carry an `index`, which a
                non-streamed message does not. Omitting it makes the CLI
                discard the call, so the shapes are not interchangeable.

                `include_usage` mirrors the real streaming contract rather than
                being generous with it: counts are emitted in one final frame
                before the terminal, and only when the request asked for them.
                A fake that always reported usage would let a telemetry path
                pass here and report nothing against a real server.
                """

                identifier = f"chatcmpl-rehearsal-{server._served:03d}"

                def event(delta: dict, finish: str | None) -> bytes:
                    payload = {
                        "id": identifier,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": server.model_name,
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": finish}
                        ],
                    }
                    return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                self.wfile.write(event({"role": "assistant"}, None))
                if turn["tool_calls"]:
                    for index, call in enumerate(turn["tool_calls"]):
                        self.wfile.write(
                            event({"tool_calls": [{"index": index, **call}]}, None)
                        )
                    self.wfile.write(event({}, "tool_calls"))
                else:
                    if turn["content"]:
                        self.wfile.write(event({"content": turn["content"]}, None))
                    self.wfile.write(event({}, turn["finish_reason"]))
                if include_usage:
                    self.wfile.write(
                        b"data: "
                        + json.dumps(
                            {
                                "id": identifier,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": server.model_name,
                                "choices": [],
                                "usage": {
                                    "prompt_tokens": 13_562,
                                    "completion_tokens": 1_127,
                                    "total_tokens": 14_689,
                                },
                            }
                        ).encode("utf-8")
                        + b"\n\n"
                    )
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def do_GET(self) -> None:  # noqa: N802
                server._record(
                    "GET", self.path, None, self.headers.get("Authorization")
                )
                # Relay readiness probes `/health` before any token is spent.
                # A provider that 404s here makes readiness fail in a way that
                # reads like a containment or relay defect rather than a
                # missing route on the stub.
                if self.path.rstrip("/").endswith("/health"):
                    self._send(200, {"status": "ok"})
                    return
                if self.path.rstrip("/").endswith("/models"):
                    self._send(
                        200,
                        {
                            "object": "list",
                            "data": [
                                {
                                    "id": server.model_name,
                                    "object": "model",
                                    "owned_by": "apoapsis-rehearsal",
                                }
                            ],
                        },
                    )
                    return
                self._send(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    request = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    request = {"unparsable": True}
                server._record(
                    "POST",
                    self.path,
                    request,
                    self.headers.get("Authorization"),
                )

                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self._send(404, {"error": {"message": "not found"}})
                    return

                turn = server._next_turn()
                # The CLI asks for SSE. Answering a streaming request with
                # application/json produces "Streaming request received a
                # non-SSE response", the tool calls are never executed, and the
                # slot silently produces no candidate -- which would look like
                # a capability failure rather than a provider defect.
                if request.get("stream"):
                    options = request.get("stream_options")
                    self._send_stream(
                        turn,
                        include_usage=bool(
                            isinstance(options, dict)
                            and options.get("include_usage")
                        ),
                    )
                    return
                message = {"role": "assistant", "content": turn["content"]}
                if turn["tool_calls"]:
                    message["tool_calls"] = turn["tool_calls"]
                self._send(
                    200,
                    {
                        "id": f"chatcmpl-rehearsal-{server._served:03d}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": server.model_name,
                        "choices": [
                            {
                                "index": 0,
                                "message": message,
                                "finish_reason": turn["finish_reason"],
                            }
                        ],
                        # Real telemetry shape, so Stage 7 has something with
                        # the same structure a live run would produce.
                        "usage": {
                            "prompt_tokens": 13_562,
                            "completion_tokens": 1_127,
                            "total_tokens": 14_689,
                        },
                    },
                )

        return Handler

    def _record(
        self,
        method: str,
        path: str,
        body: dict | None,
        authorization: str | None = None,
    ) -> None:
        with self._lock:
            self._requests.append(
                {
                    "index": len(self._requests),
                    "method": method,
                    "path": path,
                    # The prompt is kept whole: comparing what the arm was told
                    # between the two arms of a pair is how "byte-identical
                    # task information" stops being an assertion.
                    "body": body,
                    #: Recorded verbatim and classified, because the value is a
                    #: declared public placeholder rather than secret material.
                    #: Redacting it would make the one thing worth checking --
                    #: that the arm sent the declared placeholder and not
                    #: something from the host -- unreadable in the evidence.
                    "authorization": authorization,
                    "authorization_kind": _classify_authorization(authorization),
                }
            )

    @property
    def observed_authorizations(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                entry["authorization"]
                for entry in self._requests
                if entry.get("authorization")
            )

    @property
    def authorization_kinds(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                entry.get("authorization_kind", "absent") for entry in self._requests
            )
