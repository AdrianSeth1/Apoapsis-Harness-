#!/usr/bin/env python3
"""In-container forwarder: loopback TCP in, controller Unix socket out.

This file is mounted **read-only** into the workcell at a path outside
`/workspace`, so it never appears in the project tree, never enters the
computed delta, and cannot be edited by the agent. It is hashed into the run
manifest as `forwarder_sha256`, so a run is bound to the exact bytes that ran.

It is deliberately dumb. It does no HTTP parsing, applies no policy, and knows
nothing about model APIs. Every decision about what may cross the boundary is
made by the controller-side relay on the other end of the socket. A forwarder
that understood requests would be a second place for policy to live, and the
second place is always the one that is wrong.

Standard library only, single file, no imports from Apoapsis: it has to run
inside a minimal image with nothing installed.

Usage (as the container's forwarder process):

    python3 /opt/apoapsis/forwarder.py --port 8080 --socket /run/apoapsis/model.sock
"""

from __future__ import annotations

import argparse
import socket
import socketserver
import sys
import threading

FORWARDER_VERSION = "1.0"

#: Bounded so a runaway agent cannot exhaust the container's file descriptors
#: by opening connections the relay will refuse anyway.
DEFAULT_MAX_CONNECTIONS = 16
DEFAULT_IDLE_TIMEOUT_SECONDS = 120.0
_CHUNK = 65_536


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.settimeout(self.server.idle_timeout)  # type: ignore[attr-defined]
        try:
            upstream.connect(self.server.socket_path)  # type: ignore[attr-defined]
        except OSError as exc:
            # The controller is gone, or the socket is stale. Answer with a
            # real HTTP error so the CLI reports a transport failure instead of
            # hanging until its own timeout.
            self._refuse(
                "502 Bad Gateway",
                f"the Apoapsis model relay is not accepting connections: {exc}",
            )
            return
        self.request.settimeout(self.server.idle_timeout)  # type: ignore[attr-defined]
        try:
            self._pump_both_ways(upstream)
        finally:
            _close(upstream)

    def _pump_both_ways(self, upstream: socket.socket) -> None:
        # One thread per direction. When either side closes -- including the
        # agent cancelling a stream mid-response -- both are shut down, so a
        # cancelled request does not leave the relay streaming into nothing.
        done = threading.Event()
        outbound = threading.Thread(
            target=self._pump,
            args=(self.request, upstream, done),
            daemon=True,
        )
        outbound.start()
        self._pump(upstream, self.request, done)
        done.set()
        _shutdown(upstream)
        _shutdown(self.request)
        outbound.join(timeout=5.0)

    @staticmethod
    def _pump(
        source: socket.socket, destination: socket.socket, done: threading.Event
    ) -> None:
        try:
            while not done.is_set():
                chunk = source.recv(_CHUNK)
                if not chunk:
                    break
                destination.sendall(chunk)
        except OSError:
            pass
        finally:
            done.set()
            _shutdown(destination)

    def _refuse(self, status: str, message: str) -> None:
        body = message.encode("utf-8")
        response = (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii") + body
        try:
            self.request.sendall(response)
        except OSError:
            pass


class _ForwarderServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    #: Refuses rather than queues once the bound is reached.
    request_queue_size = 8

    def __init__(
        self,
        address: tuple[str, int],
        socket_path: str,
        idle_timeout: float,
        max_connections: int,
    ) -> None:
        self.socket_path = socket_path
        self.idle_timeout = idle_timeout
        self._semaphore = threading.BoundedSemaphore(max_connections)
        super().__init__(address, _Handler)

    def process_request(self, request, client_address) -> None:  # type: ignore[override]
        if not self._semaphore.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def shutdown_request(self, request) -> None:  # type: ignore[override]
        try:
            super().shutdown_request(request)
        finally:
            try:
                self._semaphore.release()
            except ValueError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument(
        "--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--max-connections", type=int, default=DEFAULT_MAX_CONNECTIONS
    )
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        sys.stdout.write(FORWARDER_VERSION + "\n")
        return 0

    # Bound to loopback only. Inside a `--network none` namespace this reaches
    # nothing but this process; binding 0.0.0.0 would still be unreachable, but
    # it would also make the `model-socket-is-only-egress` containment probe
    # fail, which is the correct outcome for a forwarder that got this wrong.
    server = _ForwarderServer(
        ("127.0.0.1", args.port),
        args.socket,
        args.idle_timeout,
        args.max_connections,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _close(sock: socket.socket) -> None:
    _shutdown(sock)
    try:
        sock.close()
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
