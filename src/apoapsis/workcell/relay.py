"""The controller-owned model relay.

Listens on a Unix domain socket the controller creates and owns, and forwards
allowlisted requests to exactly one configured upstream. This is the workcell's
only egress; `--network none` means there is nothing else to reach.

Everything about *what* may cross lives in `relay_policy`. This module is the
I/O: accepting connections, streaming bodies with a ceiling, cancelling
cleanly, counting what happened, and shutting down without leaving a socket
behind.

Three behaviours are worth naming because they are easy to get subtly wrong:

**Streaming is forwarded, not buffered.** A chat completion with
`stream: true` must reach the CLI token by token, so the response is pumped in
chunks while a running total is checked against the ceiling. Buffering would
break the CLI's incremental parsing and hide a runaway response until it was
already in memory.

**Cancellation propagates upstream.** If the workcell drops the connection
mid-stream, the upstream connection is closed rather than drained. Otherwise a
cancelled generation keeps a slot busy on the model server for as long as it
takes to finish talking to nobody.

**Redirects are never followed.** `http.client` does not follow them, which is
the safe default; a `Location` pointing anywhere but the configured upstream is
refused outright rather than passed back for the client to chase.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import socketserver
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel, utc_now
from apoapsis.workcell.platform_support import (
    PlatformAssessment,
    assess_socket_support,
    prepare_socket_directory,
)
from apoapsis.workcell.relay_policy import (
    ModelRelayConfig,
    RelayRejection,
    classify_redirect,
    classify_request,
    classify_request_body,
    observed_output_budget,
    sanitise_headers,
)

RELAY_VERSION = "1.1"
_CHUNK = 65_536

#: The terminal event of an OpenAI-shaped SSE stream. A stream that ends
#: without it is a fragment, whatever its status line said.
SSE_TERMINAL = b"data: [DONE]"

#: Rejections that mean the *upstream* failed, as opposed to the request being
#: refused at the boundary. All three increment `upstream_failures`, because
#: "the model side broke" is one question and "which way" is another.
_UPSTREAM_FAILURES = frozenset(
    {
        RelayRejection.UPSTREAM_UNAVAILABLE,
        RelayRejection.UPSTREAM_DISCONNECT,
        RelayRejection.DROPPED_STREAM,
    }
)


class _Completion(StrictModel):
    """What the pump observed about the end of a response.

    Separate from the byte count because "how much arrived" and "did it finish"
    are different facts, and the whole defect this closes was reading the first
    as an answer to the second.
    """

    total_bytes: int = 0
    streamed: bool = False
    client_cancelled: bool = False
    complete: bool = True
    terminal_observed: bool | None = None
    rejection: RelayRejection | None = None
    detail: str = ""


class RelayRequestRecord(StrictModel):
    """One request across the boundary, as the controller saw it."""

    at: datetime = Field(default_factory=utc_now)
    method: str = ""
    raw_path: str = ""
    allowed: bool = False
    rejection: RelayRejection | None = None
    status: int = 0
    request_bytes: int = Field(default=0, ge=0)
    response_bytes: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0)
    streamed: bool = False
    client_cancelled: bool = False
    #: Whether the upstream delivered a *complete* response: an SSE stream that
    #: reached its terminal event, or a non-streaming body that reached its
    #: declared length. `False` means the bytes the client received are a
    #: fragment, however successful the status line looked.
    response_complete: bool = True
    #: Whether an SSE terminal (`data: [DONE]`) was observed. `None` for
    #: responses that are not event streams, where the question does not apply
    #: -- deliberately not `False`, which would read as a missing terminal.
    terminal_observed: bool | None = None
    detail: str = ""
    #: The explicit output budget this request carried, or `None` if it named
    #: none. `None` and `0` are different findings; see `observed_output_budget`.
    output_budget_tokens: int | None = Field(default=None, ge=0)


class RelayStats(StrictModel):
    requests_served: int = Field(default=0, ge=0)
    requests_refused: int = Field(default=0, ge=0)
    #: The largest explicit output budget any request carried, and how many
    #: carried one at all. Slice 2C reports the cap as *observed* rather than
    #: as *configured*: "no request exceeded the ceiling" is only meaningful
    #: alongside "and this many requests were actually inspected".
    peak_output_budget_tokens: int | None = Field(default=None, ge=0)
    requests_with_output_budget: int = Field(default=0, ge=0)
    upstream_failures: int = Field(default=0, ge=0)
    #: Responses that began and did not finish. Counted separately from
    #: `cancellations`: a client that hung up and an upstream that stopped
    #: talking are different events with different owners, and merging them
    #: would let an upstream failure be explained away as the reader's doing.
    incomplete_responses: int = Field(default=0, ge=0)
    bytes_to_upstream: int = Field(default=0, ge=0)
    bytes_from_upstream: int = Field(default=0, ge=0)
    cancellations: int = Field(default=0, ge=0)
    peak_concurrent_requests: int = Field(default=0, ge=0)
    records: list[RelayRequestRecord] = Field(default_factory=list)

    @property
    def total_requests(self) -> int:
        return self.requests_served + self.requests_refused

    @property
    def incomplete_records(self) -> tuple[RelayRequestRecord, ...]:
        """Every exchange whose body the client must not treat as an answer."""

        return tuple(item for item in self.records if not item.response_complete)

    @property
    def every_response_complete(self) -> bool:
        """The question a controller must ask before scoring anything.

        A turn whose bytes are a fragment cannot yield a candidate, and the
        fragment is indistinguishable from a short answer by inspection -- which
        is exactly why this is a counter the relay keeps rather than a judgement
        the reader makes.
        """

        return not self.incomplete_records


class RelayStartupError(RuntimeError):
    """The relay could not be started, with a diagnosis the owner can act on."""


class _RelayState:
    """Shared counters. Guarded by one lock; the relay is not hot enough to
    need anything cleverer, and a simpler invariant is worth more here."""

    def __init__(self, config: ModelRelayConfig) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.stats = RelayStats()
        self.active = 0

    def begin(self) -> tuple[int, int]:
        with self.lock:
            return self.stats.total_requests, self.active

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.stats.peak_concurrent_requests = max(
                self.stats.peak_concurrent_requests, self.active
            )

    def leave(self) -> None:
        with self.lock:
            self.active = max(0, self.active - 1)

    def record(self, entry: RelayRequestRecord) -> None:
        with self.lock:
            if entry.allowed and entry.rejection is None:
                self.stats.requests_served += 1
            else:
                self.stats.requests_refused += 1
            if entry.rejection in _UPSTREAM_FAILURES:
                self.stats.upstream_failures += 1
            if not entry.response_complete:
                self.stats.incomplete_responses += 1
            if entry.client_cancelled:
                self.stats.cancellations += 1
            self.stats.bytes_to_upstream += entry.request_bytes
            self.stats.bytes_from_upstream += entry.response_bytes
            if entry.output_budget_tokens is not None:
                # Counted for refused requests too. The peak is a record of
                # what was *asked for* at the boundary, not of what was
                # allowed through, and a refusal is the most interesting thing
                # that can happen to an over-budget request.
                self.stats.requests_with_output_budget += 1
                self.stats.peak_output_budget_tokens = max(
                    self.stats.peak_output_budget_tokens or 0,
                    entry.output_budget_tokens,
                )
            self.stats.records.append(entry)


class _RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"ApoapsisModelRelay/{RELAY_VERSION}"
    # Silence the default stderr access log; every request is recorded
    # structurally in `RelayStats` instead.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    @property
    def state(self) -> _RelayState:
        return self.server.state  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle("OPTIONS")

    def do_CONNECT(self) -> None:  # noqa: N802
        self._handle("CONNECT")

    def _handle(self, method: str) -> None:
        started = time.monotonic()
        state = self.state
        config = state.config
        served, active = state.begin()
        headers = {name: value for name, value in self.headers.items()}
        content_length = _content_length(self.headers.get("Content-Length"))

        decision = classify_request(
            method=method,
            raw_path=self.path,
            headers=headers,
            content_length=content_length,
            config=config,
            requests_served=served,
            active_requests=active,
        )
        if not decision.allowed:
            # The record is written in `finally`: a client that resets while
            # its rejected body is being drained must not make the refusal
            # disappear from the audit trail. A refusal nobody can see is
            # indistinguishable from a request that was allowed.
            try:
                # Drain so the connection closes cleanly rather than resetting
                # mid-upload, but never more than the ceiling: a rejection must
                # not become a memory sink.
                self._drain(min(content_length or 0, config.max_request_bytes))
                self._refuse(decision.status, decision.detail)
            finally:
                state.record(
                    RelayRequestRecord(
                        method=method,
                        raw_path=self.path,
                        allowed=False,
                        rejection=decision.rejection,
                        status=decision.status,
                        duration_seconds=time.monotonic() - started,
                        detail=decision.detail,
                    )
                )
            return

        state.enter()
        try:
            self._forward(
                method=method,
                upstream_path=decision.upstream_path or "/",
                headers=headers,
                content_length=content_length,
                started=started,
            )
        finally:
            state.leave()

    def _forward(
        self,
        *,
        method: str,
        upstream_path: str,
        headers: dict[str, str],
        content_length: int | None,
        started: float,
    ) -> None:
        state = self.state
        config = state.config
        entry = RelayRequestRecord(
            method=method, raw_path=self.path, allowed=True
        )

        body = b""
        if content_length:
            body = self.rfile.read(content_length)
            if len(body) > config.max_request_bytes:
                self._refuse(413, "request body exceeded the configured ceiling")
                entry.rejection = RelayRejection.BODY_TOO_LARGE
                entry.status = 413
                entry.duration_seconds = time.monotonic() - started
                state.record(entry)
                return
        entry.request_bytes = len(body)

        # Body inspection happens here and only here: the body is already in
        # memory for forwarding, so the check costs one JSON parse and adds no
        # buffering the relay was not already doing.
        observed_budget = observed_output_budget(
            upstream_path=upstream_path, body=body
        )
        if observed_budget is not None:
            entry.output_budget_tokens = observed_budget.tokens
        body_decision = classify_request_body(
            upstream_path=upstream_path, body=body, config=config
        )
        if not body_decision.allowed:
            self._refuse(body_decision.status, body_decision.detail)
            # The record was optimistically created as allowed; nothing reached
            # the upstream, so the audit trail must not say otherwise.
            entry.allowed = False
            entry.rejection = body_decision.rejection
            entry.status = body_decision.status
            entry.detail = body_decision.detail
            entry.duration_seconds = time.monotonic() - started
            state.record(entry)
            return

        scheme, host, port = config.upstream_origin
        connection_class = (
            http.client.HTTPSConnection
            if scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(host, port, timeout=config.upstream_timeout_seconds)
        try:
            forwarded = sanitise_headers(headers)
            # The upstream's Host is the controller's business, not the
            # client's; `http.client` sets it from the connection.
            forwarded.pop("Host", None)
            connection.request(method, upstream_path, body=body, headers=forwarded)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            self._refuse(502, f"the configured model upstream did not respond: {exc}")
            entry.rejection = RelayRejection.UPSTREAM_UNAVAILABLE
            entry.status = 502
            entry.detail = str(exc)
            entry.duration_seconds = time.monotonic() - started
            state.record(entry)
            _close(connection)
            return

        location = response.getheader("Location")
        if location and 300 <= response.status < 400:
            redirect = classify_redirect(location=location, config=config)
            if not redirect.allowed:
                self._refuse(redirect.status, redirect.detail)
                entry.rejection = RelayRejection.CROSS_ORIGIN_REDIRECT
                entry.status = redirect.status
                entry.detail = redirect.detail
                entry.duration_seconds = time.monotonic() - started
                state.record(entry)
                _close(connection)
                return

        response_headers = sanitise_headers(
            {name: value for name, value in response.getheaders()}
        )
        # Length is unknown while streaming, so the relay closes the connection
        # to delimit the body rather than lying with a Content-Length.
        response_headers.pop("Content-Length", None)
        self.send_response(response.status)
        for name, value in response_headers.items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        # Sending the header is not enough: the handler would otherwise loop
        # and block reading a second request the workcell will never send.
        self.close_connection = True

        entry.status = response.status
        # Writes to the workcell get their own, shorter deadline for the
        # duration of the stream (see `stream_write_timeout_seconds`).
        try:
            self.connection.settimeout(config.stream_write_timeout_seconds)
        except OSError:
            pass
        try:
            completion = self._pump(response, config.max_response_bytes)
        finally:
            _close(connection)

        entry.response_bytes = completion.total_bytes
        entry.streamed = completion.streamed
        entry.client_cancelled = completion.client_cancelled
        entry.response_complete = completion.complete
        entry.terminal_observed = completion.terminal_observed
        if completion.detail:
            entry.detail = completion.detail
        if completion.rejection is not None:
            entry.rejection = completion.rejection

        if entry.response_bytes >= config.max_response_bytes:
            entry.rejection = RelayRejection.RESPONSE_TOO_LARGE
            entry.detail = (
                f"upstream response reached the {config.max_response_bytes:,}-byte "
                "ceiling and was cut off"
            )

        # The response line went out before any of this was knowable, so the
        # status is left exactly as the upstream sent it. Pretending it could be
        # changed retroactively would put a number in the record that no client
        # ever saw. What the client gets instead is a transfer that ends without
        # a valid terminal, and what the controller gets is this record.
        self.close_connection = True
        entry.duration_seconds = time.monotonic() - started
        state.record(entry)

    def _pump(
        self, response: http.client.HTTPResponse, max_bytes: int
    ) -> _Completion:
        """Stream the upstream response through, bounded and cancellable.

        This used to return only "how many bytes" and "did the reader leave",
        and treated EOF as the end of a response. EOF is not the end of a
        response; it is the end of a *connection*. The two coincide exactly when
        the upstream behaved, which is the case this relay was only ever tested
        against.

        Completion is therefore asked explicitly, and differently by shape:

        * an event stream is complete when it delivers `data: [DONE]`;
        * a non-streaming body is complete when it delivers the `Content-Length`
          it promised;
        * a body with neither -- no terminal and no declared length -- is
          complete at EOF, because connection close is genuinely its framing.

        Anything short of that is a fragment. The status line has already gone
        out by then and cannot be retracted, so this reports the failure and
        lets the caller record it; what it will not do is invent a terminal to
        make the fragment parse.
        """

        is_event_stream = "text/event-stream" in (
            response.getheader("Content-Type") or ""
        ).lower()
        declared_length = _content_length(response.getheader("Content-Length"))

        total = 0
        chunks = 0
        terminal_seen = False
        # The terminal can be split across two reads, so a window of the tail
        # is kept rather than testing each chunk in isolation.
        tail = b""
        truncated_at_ceiling = False

        while True:
            try:
                # `read1`, not `read`: `read` blocks until it has filled the
                # buffer or reached EOF, which would hold a token-by-token SSE
                # stream until 64 KiB had accumulated. `read1` returns whatever
                # has arrived, which is what streaming means.
                chunk = response.read1(_CHUNK)
            except (OSError, http.client.HTTPException) as exc:
                # A read error mid-body is an upstream disconnect, not an
                # ending. Previously this `break` was indistinguishable from a
                # clean finish.
                return _Completion(
                    total_bytes=total,
                    streamed=chunks > 1,
                    complete=False,
                    terminal_observed=terminal_seen if is_event_stream else None,
                    rejection=(
                        RelayRejection.DROPPED_STREAM
                        if is_event_stream
                        else RelayRejection.UPSTREAM_DISCONNECT
                    ),
                    detail=(
                        "the upstream connection failed after "
                        f"{total} byte(s): {type(exc).__name__}: {exc}"
                    ),
                )
            if not chunk:
                break
            remaining = max_bytes - total
            if remaining <= 0:
                truncated_at_ceiling = True
                break
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
                truncated_at_ceiling = True
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except TimeoutError:
                # Not a hang-up: this socket carries `stream_write_timeout_
                # seconds` for the duration of the stream, and the write
                # simply did not drain inside it. `socket.timeout` *is*
                # `TimeoutError`, which subclasses `OSError`, so this used to
                # fall into the clause below and be reported as the workcell
                # closing the connection -- a cause the relay had not
                # established and could not distinguish from its own deadline
                # expiring.
                #
                # That mattered: PLAN-19E795D6DC4B/SLICE-004 (2026-08-02)
                # failed with ten "the workcell closed the connection
                # mid-response" while the model server was demonstrably
                # healthy (62 completed generations, ~38 tok/s, clean exit,
                # no OOM, single copy on the GPU). The message sends a
                # reader to the documented GPU-eviction cause, which was not
                # what was happening.
                #
                # Same completion semantics as a hang-up -- incomplete, not
                # scored, not charged to the upstream -- only the attribution
                # changes, because that is all the relay actually knows.
                deadline = getattr(
                    self.state.config, "stream_write_timeout_seconds", None
                )
                return _Completion(
                    total_bytes=total,
                    streamed=chunks > 1,
                    client_cancelled=True,
                    complete=False,
                    terminal_observed=terminal_seen if is_event_stream else None,
                    detail=(
                        "writing to the workcell exceeded the relay's "
                        f"stream_write_timeout_seconds ({deadline}s) after "
                        f"{total} byte(s); the workcell may have stopped "
                        "reading, or may simply not have drained this chunk "
                        "in time -- the relay cannot tell these apart, and "
                        "this is not evidence that the model server failed"
                    ),
                )
            except (BrokenPipeError, ConnectionResetError, OSError):
                # The workcell hung up. Returning here closes the upstream
                # connection in the caller's `finally`, which frees the model
                # server's slot instead of generating into a closed pipe.
                #
                # This is the reader's doing, so it is a cancellation and not an
                # upstream failure. The response is still incomplete -- nobody
                # may score it -- but it is not counted against the upstream.
                return _Completion(
                    total_bytes=total,
                    streamed=chunks > 1,
                    client_cancelled=True,
                    complete=False,
                    terminal_observed=terminal_seen if is_event_stream else None,
                    detail="the workcell closed the connection mid-response",
                )
            total += len(chunk)
            chunks += 1
            if is_event_stream and not terminal_seen:
                tail = (tail + chunk)[-len(SSE_TERMINAL) * 4 :]
                terminal_seen = SSE_TERMINAL in tail
            if total >= max_bytes:
                truncated_at_ceiling = True
                break

        if truncated_at_ceiling:
            # The ceiling is the relay's own doing and already has its own
            # rejection, applied by the caller. It is still not a complete
            # response, and saying so here keeps one meaning for the word.
            return _Completion(
                total_bytes=total,
                streamed=chunks > 1,
                complete=False,
                terminal_observed=terminal_seen if is_event_stream else None,
                detail="the response was cut off at the configured ceiling",
            )

        if is_event_stream and not terminal_seen:
            return _Completion(
                total_bytes=total,
                streamed=chunks > 1,
                complete=False,
                terminal_observed=False,
                rejection=RelayRejection.DROPPED_STREAM,
                detail=(
                    f"the event stream ended after {total} byte(s) without its "
                    "terminal event; the bytes delivered are a fragment and "
                    "must not be read as an answer"
                ),
            )

        if declared_length is not None and total < declared_length:
            return _Completion(
                total_bytes=total,
                streamed=chunks > 1,
                complete=False,
                terminal_observed=None,
                rejection=RelayRejection.UPSTREAM_DISCONNECT,
                detail=(
                    f"the upstream promised {declared_length} byte(s) and "
                    f"delivered {total} before closing"
                ),
            )

        return _Completion(
            total_bytes=total,
            streamed=chunks > 1,
            complete=True,
            terminal_observed=True if is_event_stream else None,
        )

    def _drain(self, count: int) -> None:
        remaining = count
        while remaining > 0:
            try:
                chunk = self.rfile.read(min(_CHUNK, remaining))
            except OSError:
                return
            if not chunk:
                return
            remaining -= len(chunk)

    def _refuse(self, status: int, detail: str) -> None:
        body = json.dumps(
            {"error": {"type": "apoapsis_relay_refused", "message": detail}}
        ).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
        except OSError:
            pass


class UnixSocketUnsupportedError(RuntimeError):
    """Raised when a Unix-socket relay is *constructed* on a host without one."""


#: `socketserver.ThreadingUnixStreamServer` does not exist on Windows, and
#: subclassing it at import time made this whole module unimportable there.
#: That was not a contained problem: `tests/test_workcell_relay.py` failed
#: during *collection*, which aborts the entire pytest run, so the complete
#: deterministic suite could not be executed on a Windows host at all -- and the
#: release gate reads "the deterministic suite must add no failures".
#:
#: Falling back to `object` keeps import and collection working everywhere. The
#: capability itself is not faked: construction refuses, so a Windows host gets
#: a clear error at the point of use rather than a relay that appears to exist.
_UnixStreamServerBase = getattr(socketserver, "ThreadingUnixStreamServer", object)

_UNIX_SOCKETS_AVAILABLE = hasattr(socketserver, "ThreadingUnixStreamServer")


class _UnixHTTPServer(_UnixStreamServerBase):  # type: ignore[misc,valid-type]
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, socket_path: str, state: _RelayState) -> None:
        if not _UNIX_SOCKETS_AVAILABLE:
            raise UnixSocketUnsupportedError(
                "the controller-owned relay requires Unix domain sockets, "
                "which this host does not provide; run the workcell on Linux. "
                "See workcell/platform_support.assess_socket_support."
            )
        self.state = state
        super().__init__(socket_path, _RelayHandler)

    def get_request(self):  # type: ignore[override]
        request, _client = super().get_request()
        request.settimeout(self.state.config.idle_timeout_seconds)
        # `BaseHTTPRequestHandler` expects a (host, port) client address.
        return request, ("workcell", 0)


class ModelRelay:
    """Owns the socket for the lifetime of one workcell run."""

    version = RELAY_VERSION

    def __init__(self, config: ModelRelayConfig) -> None:
        self.config = config
        self._state = _RelayState(config)
        self._server: _UnixHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.platform: PlatformAssessment | None = None

    @property
    def stats(self) -> RelayStats:
        return self._state.stats

    def wait_for_records(
        self, count: int, *, timeout_seconds: float = 20.0
    ) -> bool:
        """Block until `count` requests are fully recorded, or time out.

        An HTTP call against the relay returns when the response has been
        written; the request is *recorded* afterwards, on the handler thread.
        Any observer reading `stats` straight after a call is therefore racing
        the relay, and will usually win -- which is what makes it a defect
        found once every few dozen runs rather than immediately.

        This is a synchronisation primitive, not a retry: it waits for the
        event the caller is about to assert on, and reports honestly if that
        event never arrived. Callers that need "the relay observed N requests"
        as evidence -- the rehearsal's containment stage among them -- need
        exactly this, because a count read too early understates traffic and
        would make a bypassed turn look like a quiet one.
        """

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if len(self.stats.records) >= count:
                return True
            time.sleep(0.01)
        return len(self.stats.records) >= count

    def start(self) -> None:
        """Create the socket and serve. Refuses early on an unusable platform."""

        self.platform = assess_socket_support(self.config.socket_path)
        if not self.platform.usable:
            raise RelayStartupError(
                self.platform.detail
                + (
                    "\n\nTry:\n- " + "\n- ".join(self.platform.remedies)
                    if self.platform.remedies
                    else ""
                )
            )
        try:
            prepare_socket_directory(self.config.socket_path)
        except (OSError, ValueError) as exc:
            raise RelayStartupError(str(exc)) from exc
        try:
            self._server = _UnixHTTPServer(self.config.socket_path, self._state)
        except OSError as exc:
            raise RelayStartupError(
                f"could not bind the relay socket at {self.config.socket_path}: {exc}"
            ) from exc
        # The container runs as a mapped uid, so the dedicated directory's
        # group is what makes the socket reachable. Docker Desktop bind mounts
        # do not reliably preserve setgid inheritance, so apply that group
        # explicitly and fail before a token-spending probe if it cannot be
        # established.
        try:
            directory_gid = Path(self.config.socket_path).parent.stat().st_gid
            os.chown(self.config.socket_path, -1, directory_gid)
            os.chmod(self.config.socket_path, 0o660)
        except OSError as exc:
            self._server.server_close()
            self._server = None
            Path(self.config.socket_path).unlink(missing_ok=True)
            raise RelayStartupError(
                "could not assign the relay socket to the workcell's "
                f"dedicated group: {exc}"
            ) from exc
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
            name="apoapsis-model-relay",
        )
        self._thread.start()

    def stop(self) -> bool:
        """Shut down and remove the socket. Returns whether nothing was left."""

        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
        path = Path(self.config.socket_path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        return not path.exists()

    def __enter__(self) -> ModelRelay:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def _content_length(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _close(connection: http.client.HTTPConnection) -> None:
    try:
        connection.close()
    except (OSError, http.client.HTTPException):
        pass


def probe_relay_socket(socket_path: str, *, timeout: float = 5.0) -> bool:
    """True when something is actually accepting on the socket.

    A socket *file* left behind by a crashed controller looks identical to a
    live one on disk, so existence is not the test; connecting is.
    """

    path = Path(socket_path)
    if not path.exists():
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        try:
            client.close()
        except OSError:
            pass
