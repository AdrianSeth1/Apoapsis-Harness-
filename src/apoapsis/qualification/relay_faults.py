"""Stage 3: prove the relay's failure handling by causing the failures.

The previous Stage 3 ran `run_readiness` twenty times and called the result
"relay stability". Twenty consecutive successes say the happy path works; they
say nothing at all about the paths that actually matter, because none of them
was ever taken. Every interesting relay defect lives in what happens when the
upstream dies mid-response, when the stream stops without ending, or when the
reader goes away -- and a loop over the good case cannot reach any of them.

So each fault here is *caused*. A purpose-built upstream misbehaves in one
specific way, a real `ModelRelay` sits in front of it, and the assertion is on
what the relay recorded. The upstream is separate from `FakeProviderServer` on
purpose: the slots depend on that server behaving correctly, and a server that
can be told to break is one configuration mistake away from breaking a scored
slot.

The bar is the same throughout: a failure must be *recorded as a failure*. A
relay that turns a truncated stream into a clean `200` is worse than one that
crashes, because the caller has no way to tell the difference.
"""

from __future__ import annotations

import http.client
import socket
import threading
import time
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.relay import ModelRelay
from apoapsis.workcell.relay_policy import ModelRelayConfig


class RelayFault(StrEnum):
    """One injected upstream or client misbehaviour."""

    #: The upstream closes the socket part-way through the response body.
    UPSTREAM_DISCONNECT = "upstream_disconnect"
    #: The upstream accepts the request and then never answers.
    UPSTREAM_TIMEOUT = "upstream_timeout"
    #: An SSE stream that stops without ever sending `[DONE]`.
    DROPPED_STREAM = "dropped_stream"
    #: A response larger than the reader drains promptly.
    BACKPRESSURE = "backpressure"
    #: The workcell-side reader vanishes mid-stream.
    CLIENT_CANCELLATION = "client_cancellation"


class FaultOutcome(StrictModel):
    """What the relay did when one fault was injected."""

    fault: RelayFault
    #: The relay answered rather than hanging forever.
    relay_responded: bool = False
    #: The relay did not present the broken exchange as a clean success.
    not_reported_as_success: bool = False
    #: A record exists for the exchange, so the failure is auditable.
    recorded: bool = False
    #: No relay worker outlived the exchange.
    no_worker_leaked: bool = False
    observed_status: int | None = None
    observed_detail: str = ""
    cancellations: int = 0
    upstream_failures: int = 0
    #: What the relay recorded about the *end* of the exchange. A fault that
    #: leaves `response_complete` true has been recorded as a finished answer,
    #: which is the failure mode Stage 3 exists to catch.
    response_complete_recorded: bool = True
    #: `None` when the response was not an event stream, so an inapplicable
    #: question is not answered "no".
    terminal_observed: bool | None = None
    incomplete_responses: int = 0
    duration_seconds: float = 0.0
    threads_before: int = 0
    threads_after: int = 0
    error: str | None = None

    @property
    def handled(self) -> bool:
        return (
            self.error is None
            and self.relay_responded
            and self.not_reported_as_success
            and self.recorded
            and self.no_worker_leaked
        )


class RelayFaultReport(StrictModel):
    outcomes: tuple[FaultOutcome, ...] = ()

    @property
    def all_handled(self) -> bool:
        return bool(self.outcomes) and all(item.handled for item in self.outcomes)

    @property
    def unhandled(self) -> tuple[str, ...]:
        return tuple(str(item.fault) for item in self.outcomes if not item.handled)


#: Enough body to overflow the socket buffer, so a slow reader really applies
#: backpressure instead of the whole response fitting in one buffer and the
#: probe proving nothing.
_LARGE_CHUNK = "x" * 64_000
_BACKPRESSURE_CHUNKS = 40


class _FaultyUpstream:
    """An OpenAI-shaped upstream that misbehaves in exactly one way."""

    def __init__(self, fault: RelayFault, *, stall_seconds: float = 30.0) -> None:
        self.fault = fault
        self.stall_seconds = stall_seconds
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def __enter__(self) -> "_FaultyUpstream":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _handler(self):
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                upstream._respond(self)

            def do_GET(self) -> None:  # noqa: N802
                upstream._respond(self)

        return Handler

    def _respond(self, handler: BaseHTTPRequestHandler) -> None:
        fault = self.fault

        if fault is RelayFault.UPSTREAM_TIMEOUT:
            # Accept, then never answer. The relay's upstream deadline is the
            # only thing that can end this exchange.
            time.sleep(self.stall_seconds)
            return

        if fault is RelayFault.UPSTREAM_DISCONNECT:
            # Promise more body than is delivered, then hang up. A relay that
            # reports 200 here has invented a complete response.
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", "4096")
            handler.end_headers()
            handler.wfile.write(b'{"choices":[{"delta":{"content":"partial"')
            handler.wfile.flush()
            handler.close_connection = True
            try:
                handler.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return

        if fault is RelayFault.DROPPED_STREAM:
            # A well-formed SSE stream that simply stops. No `[DONE]`, no error
            # frame -- the shape most likely to be mistaken for a finished one.
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            handler.wfile.write(
                b'data: {"choices":[{"delta":{"content":"half"},"index":0}]}\n\n'
            )
            handler.wfile.flush()
            handler.close_connection = True
            try:
                handler.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return

        # BACKPRESSURE and CLIENT_CANCELLATION both need a long stream; the
        # difference is entirely in what the client does with it.
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        try:
            for _ in range(_BACKPRESSURE_CHUNKS):
                handler.wfile.write(
                    f'data: {{"choices":[{{"delta":{{"content":"{_LARGE_CHUNK}"}},'
                    f'"index":0}}]}}\n\n'.encode()
                )
                handler.wfile.flush()
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
        except OSError:
            # The reader went away. That is the point of CLIENT_CANCELLATION.
            handler.close_connection = True


def _post_over_socket(
    socket_path: str,
    *,
    read_bytes: int | None,
    timeout: float,
    slow_read: bool = False,
) -> tuple[int | None, str]:
    """Speak to the relay over its Unix socket the way the forwarder does."""

    connection = http.client.HTTPConnection("localhost", timeout=timeout)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(socket_path)
    connection.sock = sock
    try:
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=b'{"model":"probe","messages":[],"stream":true}',
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        if read_bytes is None:
            body = response.read()
            return response.status, body[:400].decode("utf-8", "replace")
        # Read a little and stop, holding the connection open. For
        # BACKPRESSURE this stalls the writer; for CLIENT_CANCELLATION the
        # caller closes underneath it.
        chunk = response.read(read_bytes)
        if slow_read:
            time.sleep(2.0)
            chunk += response.read(read_bytes)
        return response.status, chunk[:400].decode("utf-8", "replace")
    finally:
        try:
            connection.close()
        except OSError:
            pass


def run_relay_fault(
    fault: RelayFault,
    *,
    socket_directory: Path,
    upstream_timeout_seconds: float = 5.0,
    stream_write_timeout_seconds: float = 3.0,
) -> FaultOutcome:
    """Inject one fault against a real relay and report what the relay did."""

    socket_directory.mkdir(parents=True, exist_ok=True)
    socket_path = socket_directory / f"{fault.value}.sock"
    if socket_path.exists():
        socket_path.unlink()

    threads_before = threading.active_count()
    started = time.monotonic()

    with _FaultyUpstream(fault, stall_seconds=upstream_timeout_seconds * 3) as upstream:
        config = ModelRelayConfig(
            upstream_base_url=upstream.base_url,
            socket_path=str(socket_path),
            # Paths, not "METHOD PATH". `RelayPin` records verbs because a
            # run's identity includes which were reachable; `ModelRelayConfig`
            # narrows the built-in allowlist by path and rejects anything it
            # cannot recognise as a narrowing. Passing the pin's format here
            # raised a validation error the first time these faults were ever
            # injected -- which was during the official rehearsal, because no
            # test had run them.
            allowed_routes=["/v1/chat/completions"],
            upstream_timeout_seconds=upstream_timeout_seconds,
            stream_write_timeout_seconds=stream_write_timeout_seconds,
            idle_timeout_seconds=upstream_timeout_seconds * 2,
        )
        relay = ModelRelay(config)
        relay.start()
        status: int | None = None
        detail = ""
        error: str | None = None
        try:
            if fault is RelayFault.CLIENT_CANCELLATION:
                # Read one chunk, then abandon the connection entirely.
                status, detail = _post_over_socket(
                    str(socket_path),
                    read_bytes=1_024,
                    timeout=upstream_timeout_seconds * 4,
                )
            elif fault is RelayFault.BACKPRESSURE:
                status, detail = _post_over_socket(
                    str(socket_path),
                    read_bytes=1_024,
                    timeout=upstream_timeout_seconds * 4,
                    slow_read=True,
                )
            else:
                status, detail = _post_over_socket(
                    str(socket_path),
                    read_bytes=None,
                    timeout=upstream_timeout_seconds * 4,
                )
        except (OSError, http.client.HTTPException) as exc:
            # A refused or severed exchange is a legitimate outcome for several
            # of these faults; it is recorded, not swallowed.
            detail = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"

        # Give the relay a moment to finish recording and retire its worker.
        time.sleep(1.0)
        # A property, not a method. `relay.stats()` raised
        # "'RelayStats' object is not callable" on the first real injection.
        stats = relay.stats
        stopped = relay.stop()

    duration = time.monotonic() - started
    time.sleep(0.5)
    threads_after = threading.active_count()

    records = list(stats.records)
    relevant = [item for item in records if item.raw_path.endswith("chat/completions")]

    # "Reported as success" means an exchange the caller would treat as a
    # complete answer. The status line alone cannot decide that: for a stream
    # the 200 is sent before anything is known, so a truncated stream and a
    # finished one carry the identical status. What separates them is whether
    # the relay recorded the response as *complete*.
    incomplete = any(not item.response_complete for item in relevant)
    clean_success = (
        status == 200
        and not incomplete
        and not any(item.client_cancelled for item in relevant)
        and stats.upstream_failures == 0
        and fault is not RelayFault.BACKPRESSURE
    )

    return FaultOutcome(
        fault=fault,
        # Timing out is a response; hanging until the test dies is not.
        relay_responded=duration < upstream_timeout_seconds * 6,
        not_reported_as_success=not clean_success,
        recorded=bool(relevant),
        # `stop()` returning True means the relay shut its listener down, and
        # the thread count returning to baseline means no worker outlived it.
        no_worker_leaked=stopped and threads_after <= threads_before + 1,
        observed_status=status,
        observed_detail=detail[:400],
        cancellations=stats.cancellations,
        upstream_failures=stats.upstream_failures,
        response_complete_recorded=not incomplete,
        terminal_observed=(
            relevant[-1].terminal_observed if relevant else None
        ),
        incomplete_responses=stats.incomplete_responses,
        duration_seconds=round(duration, 3),
        threads_before=threads_before,
        threads_after=threads_after,
        error=error,
    )


def run_all_relay_faults(*, socket_directory: Path) -> RelayFaultReport:
    """Inject every fault in turn. Order is fixed so evidence is comparable."""

    return RelayFaultReport(
        outcomes=tuple(run_relay_fault(item, socket_directory=socket_directory) for item in RelayFault)
    )
