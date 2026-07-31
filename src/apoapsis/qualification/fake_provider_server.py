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


def script_turns(script: ScriptId) -> list[dict]:
    """Turn a scripted change set into successive assistant messages.

    One turn carrying every `write_file` call, then a turn with no tool calls
    to end the loop. Qwen executes the calls between them, so the files appear
    because the CLI wrote them.
    """

    turn = SCRIPTS[script][0]
    proposal = json.loads(turn.content)
    calls = [
        _tool_call(index, change["path"], change["content"])
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

            def do_GET(self) -> None:  # noqa: N802
                server._record("GET", self.path, None)
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
                server._record("POST", self.path, request)

                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self._send(404, {"error": {"message": "not found"}})
                    return

                turn = server._next_turn()
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

    def _record(self, method: str, path: str, body: dict | None) -> None:
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
                }
            )
