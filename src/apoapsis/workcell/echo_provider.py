"""A deterministic OpenAI-compatible provider that returns its input verbatim.

This exists for one check. `multiline_unicode_integrity` claims to prove that
file content survives the adapter without being escaped, truncated, or
double-encoded. Until Slice 2C it obtained that evidence by asking a language
model to retype a string and then attributing every difference to the adapter.
Slice 2B's live run failed the check because Qwen replaced two typographic
quotes with ASCII apostrophes, while the emoji, the variation selector, the
em-dash, and both CJK characters all survived byte-exact. The check reported an
adapter defect and had measured a model preference. ADR 0078 records that
reasoning and this module is its instrument.

The provider is deliberately dull. It parses the request, finds the payload the
probe marked, and emits a tool call whose `content` argument is that exact
string. There is no sampling, no template, no model, and therefore no second
variable: any difference between what went in and what came out is the
envelope, which is the thing the check is named after.

Two properties are load-bearing and easy to lose:

* **The request bytes are kept verbatim.** The comparison the check performs is
  between bytes captured off the wire and bytes parsed out of the response, not
  between two Python strings that happen to be in scope. Comparing in-process
  objects would prove that Python can hold a string.
* **It is never an upstream for a measured run.** Like `WireCaptureUpstream` in
  `pin_capture`, it applies no policy and exists for seconds. It is wired
  behind a real `ModelRelay` precisely so the relay, the forwarder, and the
  container hop are all in the path -- but the model is not.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pydantic import ConfigDict, Field

from apoapsis.specification.schema import StrictModel

#: The probe marks its payload rather than relying on "the last user message".
#: A marker survives a CLI that prepends a system turn, appends a reminder, or
#: reorders content blocks; positional extraction would silently start echoing
#: the wrong string and the check would pass on the wrong evidence.
ECHO_BEGIN = "<<<APOAPSIS_ECHO_BEGIN>>>"
ECHO_END = "<<<APOAPSIS_ECHO_END>>>"

#: The tool the echoed content comes back inside. `write_file` on purpose: the
#: failure this check protects against is a corrupted file write, so the
#: envelope under test is the one a file write would really use.
ECHO_TOOL_NAME = "write_file"


class EchoExchange(StrictModel):
    """One request as the provider received it, before anything interpreted it.

    `model_config` is overridden for a reason worth stating. `StrictModel` sets
    `str_strip_whitespace=True`, which is right for specification text and
    catastrophic here: the payload under test ends in a newline, and a record
    that silently trimmed it would make the envelope check compare a
    normalised copy against the original and report a corruption that the model
    layer had introduced. The evidence object for a byte-fidelity check must
    not normalise bytes. This was found by the round-trip test in
    `tests/test_workcell_slice2c.py`, not by inspection.
    """

    model_config = ConfigDict(str_strip_whitespace=False)

    path: str = Field(min_length=1)
    request_bytes: bytes
    #: The marked payload, decoded from the captured bytes. `None` when the
    #: request carried no marker, which the check reports rather than hides.
    payload: str | None = None
    response_bytes: bytes = b""


def extract_marked_payload(request_bytes: bytes) -> str | None:
    """Pull the marked payload out of raw request bytes.

    Works on the bytes rather than on a pre-parsed object so that the string
    the provider echoes is provably the string that arrived, not one
    reconstructed from a dict that some other layer built.
    """

    try:
        payload = json.loads(request_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        chunks: list[str] = []
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            chunks.extend(
                block["text"]
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
        for chunk in chunks:
            start = chunk.find(ECHO_BEGIN)
            end = chunk.find(ECHO_END)
            if start != -1 and end > start:
                return chunk[start + len(ECHO_BEGIN) : end]
    return None


def build_echo_response(payload: str, *, model: str) -> dict:
    """The chat completion carrying the payload back, unchanged.

    The content is placed in a tool call's JSON arguments rather than in
    message text because that is the encoding layer that can actually mangle
    it: arguments are a JSON string *inside* a JSON document, so a
    double-encoding or an over-eager escape has somewhere to happen.
    """

    return {
        "id": "apoapsis-echo",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_echo_1",
                            "type": "function",
                            "function": {
                                "name": ECHO_TOOL_NAME,
                                "arguments": json.dumps(
                                    {"path": "probe.py", "content": payload}
                                ),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        # Present so `usage_accounting` style readers do not treat the echo as a
        # provider that lost its accounting. The counts are honestly zero: no
        # tokens were spent, because nothing was generated.
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class DeterministicEchoProvider:
    """An OpenAI-compatible endpoint whose only behaviour is to return input."""

    def __init__(
        self, *, model: str = "apoapsis-echo", host: str = "127.0.0.1", port: int = 0
    ) -> None:
        self.model = model
        self.exchanges: list[EchoExchange] = []
        self._lock = threading.Lock()
        provider = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/").endswith("/health"):
                    self._send(200, {"status": "ok"})
                    return
                if self.path.rstrip("/").endswith("/models"):
                    self._send(
                        200,
                        {
                            "object": "list",
                            "data": [{"id": provider.model, "object": "model"}],
                        },
                    )
                    return
                self._send(404, {"error": {"type": "not_found"}})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                payload = extract_marked_payload(raw)
                if payload is None:
                    body = {
                        "error": {
                            "type": "no_echo_marker",
                            "message": (
                                "the request carried no marked payload, so "
                                "there was nothing to echo"
                            ),
                        }
                    }
                    status = 400
                else:
                    body = build_echo_response(payload, model=provider.model)
                    status = 200
                encoded = self._send(status, body)
                provider._record(
                    EchoExchange(
                        path=self.path,
                        request_bytes=raw,
                        payload=payload,
                        response_bytes=encoded,
                    )
                )

            def _send(self, status: int, body: dict) -> bytes:
                # `ensure_ascii=False` is the point of the exercise: the
                # provider must be capable of putting real UTF-8 on the wire,
                # or the check would only ever prove that `\uXXXX` escapes
                # survive and would never exercise multi-byte transport.
                encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return encoded

        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._thread: threading.Thread | None = None

    def _record(self, exchange: EchoExchange) -> None:
        with self._lock:
            self.exchanges.append(exchange)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def last_exchange(self) -> EchoExchange | None:
        with self._lock:
            return self.exchanges[-1] if self.exchanges else None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def __enter__(self) -> DeterministicEchoProvider:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
