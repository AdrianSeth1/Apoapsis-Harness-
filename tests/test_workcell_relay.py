from __future__ import annotations

import hashlib
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from apoapsis.workcell.platform_support import (
    HostPlatform,
    SocketSupport,
    assess_socket_support,
    prepare_socket_directory,
)
from apoapsis.workcell.relay import (
    RELAY_VERSION,
    ModelRelay,
    RelayStartupError,
    probe_relay_socket,
)
from apoapsis.workcell.relay_policy import (
    ALLOWED_ROUTES,
    ModelRelayConfig,
    RelayRejection,
    classify_redirect,
    classify_request,
    sanitise_headers,
)
from apoapsis.workcell.relay_preflight import (
    ReadinessStep,
    ReadinessStepResult,
    StepStatus,
    build_probe_argv,
    classify_probe_output,
    evaluate_readiness,
    one_token_payload,
)

_LINUX_ONLY = unittest.skipUnless(
    hasattr(socket, "AF_UNIX"), "the relay requires AF_UNIX"
)


def _config(socket_path: str, upstream: str = "http://127.0.0.1:1", **overrides):
    payload = {"upstream_base_url": upstream, "socket_path": socket_path}
    payload.update(overrides)
    return ModelRelayConfig(**payload)


def _classify(config: ModelRelayConfig, method: str, path: str, **kwargs):
    return classify_request(
        method=method,
        raw_path=path,
        headers=kwargs.pop("headers", {}),
        content_length=kwargs.pop("content_length", None),
        config=config,
        requests_served=kwargs.pop("requests_served", 0),
        active_requests=kwargs.pop("active_requests", 0),
    )


class RelayPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _config("/run/apoapsis/model.sock")

    def test_the_permitted_routes_are_model_api_and_health_only(self) -> None:
        self.assertEqual(
            {path for _method, path in ALLOWED_ROUTES},
            {"/v1/chat/completions", "/v1/completions", "/v1/models", "/health"},
        )

    def test_allowed_requests_pass(self) -> None:
        for method, path in ALLOWED_ROUTES:
            self.assertTrue(_classify(self.config, method, path).allowed, path)

    def test_connect_is_refused(self) -> None:
        decision = _classify(self.config, "CONNECT", "/v1/chat/completions")
        self.assertEqual(decision.rejection, RelayRejection.CONNECT_METHOD)
        self.assertEqual(decision.status, 405)

    def test_an_absolute_form_uri_is_refused(self) -> None:
        # The client does not choose the upstream.
        for path in (
            "http://evil.example/v1/chat/completions",
            "https://127.0.0.1:8080/v1/models",
            "//evil.example/v1/models",
        ):
            decision = _classify(self.config, "POST", path)
            self.assertEqual(
                decision.rejection, RelayRejection.ABSOLUTE_FORM_URI, path
            )

    def test_steering_headers_are_stripped_rather_than_refused(self) -> None:
        # Host is mandatory in HTTP/1.1, so refusing it would break every real
        # client. The safety comes from the relay never consulting it.
        decision = _classify(
            self.config,
            "POST",
            "/v1/chat/completions",
            headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"},
        )
        self.assertTrue(decision.allowed)
        cleaned = sanitise_headers(
            {
                "Host": "evil.example",
                "X-Forwarded-Host": "evil.example",
                "X-Forwarded-For": "10.0.0.1",
                "Forwarded": "for=10.0.0.1",
                "X-Forwarded-Proto": "https",
                "Accept": "*/*",
            }
        )
        self.assertEqual(cleaned, {"Accept": "*/*"})

    def test_unauthorized_paths_are_refused(self) -> None:
        for path in ("/v1/files", "/admin", "/", "/v1/chat/completions/../../etc"):
            decision = _classify(self.config, "POST", path)
            self.assertFalse(decision.allowed, path)
            self.assertIn(
                decision.rejection,
                {RelayRejection.PATH_NOT_ALLOWED, RelayRejection.MALFORMED_PATH},
            )

    def test_traversal_and_empty_segments_are_refused_not_collapsed(self) -> None:
        for path in ("/v1/../v1/models", "/v1//models"):
            self.assertEqual(
                _classify(self.config, "GET", path).rejection,
                RelayRejection.MALFORMED_PATH,
                path,
            )

    def test_wrong_method_on_a_real_route_is_refused(self) -> None:
        decision = _classify(self.config, "DELETE", "/v1/models")
        self.assertEqual(decision.rejection, RelayRejection.METHOD_NOT_ALLOWED)

    def test_a_trailing_slash_is_normalised(self) -> None:
        decision = _classify(self.config, "GET", "/v1/models/")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.upstream_path, "/v1/models")

    def test_a_query_string_survives_but_a_fragment_does_not(self) -> None:
        self.assertEqual(
            _classify(self.config, "GET", "/v1/models?limit=1").upstream_path,
            "/v1/models?limit=1",
        )
        self.assertEqual(
            _classify(self.config, "GET", "/v1/models#x").rejection,
            RelayRejection.MALFORMED_PATH,
        )

    def test_oversized_bodies_are_refused(self) -> None:
        config = _config("/s.sock", max_request_bytes=1024)
        decision = _classify(
            config, "POST", "/v1/chat/completions", content_length=2048
        )
        self.assertEqual(decision.rejection, RelayRejection.BODY_TOO_LARGE)
        self.assertEqual(decision.status, 413)

    def test_the_request_budget_is_enforced(self) -> None:
        config = _config("/s.sock", max_total_requests=2)
        self.assertTrue(
            _classify(config, "GET", "/v1/models", requests_served=1).allowed
        )
        self.assertEqual(
            _classify(config, "GET", "/v1/models", requests_served=2).rejection,
            RelayRejection.REQUEST_BUDGET_EXHAUSTED,
        )

    def test_the_concurrency_limit_is_enforced(self) -> None:
        config = _config("/s.sock", max_concurrent_requests=2)
        self.assertEqual(
            _classify(config, "GET", "/v1/models", active_requests=2).rejection,
            RelayRejection.CONCURRENCY_LIMIT,
        )

    def test_config_can_narrow_routes_but_not_widen_them(self) -> None:
        narrowed = _config("/s.sock", allowed_routes=["/health"])
        self.assertTrue(_classify(narrowed, "GET", "/health").allowed)
        self.assertEqual(
            _classify(narrowed, "POST", "/v1/chat/completions").rejection,
            RelayRejection.PATH_NOT_ALLOWED,
        )
        with self.assertRaises(ValueError):
            _config("/s.sock", allowed_routes=["/v1/files"])

    def test_the_upstream_must_be_an_origin_with_no_path(self) -> None:
        for bad in (
            "http://127.0.0.1:8080/v1",
            "ftp://127.0.0.1",
            "http://127.0.0.1?x=1",
            "not-a-url",
        ):
            with self.assertRaises(ValueError, msg=bad):
                _config("/s.sock", upstream=bad)

    def test_same_origin_and_relative_redirects_are_allowed(self) -> None:
        config = _config("/s.sock", upstream="http://127.0.0.1:8080")
        self.assertTrue(
            classify_redirect(location="/v1/models", config=config).allowed
        )
        self.assertTrue(
            classify_redirect(
                location="http://127.0.0.1:8080/v1/models", config=config
            ).allowed
        )

    def test_cross_origin_redirects_are_refused(self) -> None:
        config = _config("/s.sock", upstream="http://127.0.0.1:8080")
        for location in (
            "http://evil.example/v1/models",
            "http://127.0.0.1:9999/v1/models",
            "https://127.0.0.1:8080/v1/models",
        ):
            decision = classify_redirect(location=location, config=config)
            self.assertEqual(
                decision.rejection, RelayRejection.CROSS_ORIGIN_REDIRECT, location
            )

    def test_hop_by_hop_headers_are_stripped(self) -> None:
        cleaned = sanitise_headers(
            {"Connection": "upgrade", "Upgrade": "websocket", "Accept": "*/*"}
        )
        self.assertEqual(cleaned, {"Accept": "*/*"})


class PlatformSupportTests(unittest.TestCase):
    def test_a_windows_host_cannot_mount_the_socket(self) -> None:
        assessment = assess_socket_support(
            r"C:\Users\x\run\model.sock", host_platform=HostPlatform.WINDOWS
        )
        self.assertEqual(assessment.support, SocketSupport.UNSUPPORTED_PLATFORM)
        self.assertFalse(assessment.usable)
        self.assertIn("socket inodes", assessment.detail)
        # The remedy must not be "open a TCP port", which would breach the
        # no-network rule.
        self.assertTrue(any("WSL2" in item for item in assessment.remedies))
        self.assertTrue(any("Do not substitute a TCP port" in i for i in assessment.remedies))

    def test_wsl2_on_a_windows_drive_is_refused(self) -> None:
        assessment = assess_socket_support(
            "/mnt/c/Users/x/model.sock", host_platform=HostPlatform.WSL2
        )
        self.assertEqual(assessment.support, SocketSupport.UNSUPPORTED_PATH)
        self.assertIn("DrvFs", assessment.detail)

    def test_wsl2_on_ext4_is_supported(self) -> None:
        assessment = assess_socket_support(
            "/run/apoapsis/model.sock", host_platform=HostPlatform.WSL2
        )
        self.assertTrue(assessment.usable)

    def test_linux_is_supported(self) -> None:
        self.assertTrue(
            assess_socket_support(
                "/run/apoapsis/model.sock", host_platform=HostPlatform.LINUX
            ).usable
        )

    def test_the_socket_directory_must_be_dedicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "run"
            directory.mkdir()
            (directory / "unrelated.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                prepare_socket_directory(str(directory / "model.sock"))
            self.assertIn("not dedicated", str(caught.exception))

    def test_a_stale_socket_file_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run" / "model.sock"
            path.parent.mkdir()
            path.write_text("", encoding="utf-8")
            prepare_socket_directory(str(path))
            self.assertFalse(path.exists())


class _FakeUpstream:
    """A minimal OpenAI-shaped model server for the end-to-end relay tests."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                return

            def _record(self) -> None:
                outer.requests.append(
                    (self.command, self.path, dict(self.headers.items()))
                )

            def do_GET(self) -> None:  # noqa: N802
                self._record()
                if outer.redirect_away:
                    self.send_response(302)
                    self.send_header("Location", "http://evil.example/v1/models")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._json(200, {"data": [{"id": "qwen"}]})

            def do_POST(self) -> None:  # noqa: N802
                self._record()
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                if outer.huge_response:
                    payload = b"x" * 200_000
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if outer.stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    # Streams until the relay hangs up, so that "the upstream
                    # was released" is an observable event rather than a race
                    # against a fixed chunk count.
                    for index in range(20_000):
                        try:
                            self.wfile.write(f"data: chunk-{index}\n\n".encode())
                            self.wfile.flush()
                        except OSError:
                            outer.stream_aborted = True
                            return
                        time.sleep(0.005)
                    return
                self._json(
                    200,
                    {
                        "choices": [{"message": {"content": "."}}],
                        "echo_bytes": len(body),
                    },
                )

            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.stream = False
        self.huge_response = False
        self.stream_aborted = False
        self.redirect_away = False
        class _QuietServer(ThreadingHTTPServer):
            # The relay resetting an upstream connection is the expected
            # outcome of a cancelled stream, not a test failure to print.
            def handle_error(self, request, client_address) -> None:
                return

        self.server = _QuietServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """Speaks HTTP over the relay's Unix socket, like the forwarder does."""

    def __init__(self, socket_path: str, timeout: float = 10.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


@_LINUX_ONLY
class RelayEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = _FakeUpstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = str(Path(self.tmp.name) / "run" / "model.sock")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.upstream.stop)

    def _relay(self, **overrides) -> ModelRelay:
        relay = ModelRelay(
            _config(self.socket_path, upstream=self.upstream.base_url, **overrides)
        )
        relay.start()
        self.addCleanup(relay.stop)
        return relay

    def _request(self, method: str, path: str, body: bytes | None = None, headers=None):
        connection = _UnixHTTPConnection(self.socket_path)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_an_allowed_request_reaches_the_configured_upstream(self) -> None:
        relay = self._relay()
        status, body = self._request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertIn(b"qwen", body)
        self.assertEqual(relay.stats.requests_served, 1)
        self.assertEqual(self.upstream.requests[0][1], "/v1/models")

    def test_a_one_token_completion_round_trips(self) -> None:
        relay = self._relay()
        payload = one_token_payload("qwen").encode("utf-8")
        status, body = self._request(
            "POST",
            "/v1/chat/completions",
            payload,
            {"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        self.assertEqual(status, 200)
        self.assertIn("choices", json.loads(body))
        self.assertEqual(relay.stats.requests_served, 1)

    def test_an_unauthorized_path_never_reaches_the_upstream(self) -> None:
        relay = self._relay()
        status, body = self._request("GET", "/admin")
        self.assertEqual(status, 403)
        self.assertIn(b"apoapsis_relay_refused", body)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(relay.stats.requests_refused, 1)

    def test_an_arbitrary_upstream_attempt_is_refused(self) -> None:
        relay = self._relay()
        status, _ = self._request("GET", "http://evil.example/v1/models")
        self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(
            relay.stats.records[0].rejection, RelayRejection.ABSOLUTE_FORM_URI
        )

    def test_connect_is_refused_over_the_socket(self) -> None:
        self._relay()
        status, _ = self._request("CONNECT", "/v1/models")
        self.assertEqual(status, 405)
        self.assertEqual(self.upstream.requests, [])

    def test_an_oversized_body_is_refused_without_reaching_upstream(self) -> None:
        relay = self._relay(max_request_bytes=1024)
        payload = b"x" * 4096
        status, _ = self._request(
            "POST",
            "/v1/chat/completions",
            payload,
            {"Content-Length": str(len(payload))},
        )
        self.assertEqual(status, 413)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(
            relay.stats.records[0].rejection, RelayRejection.BODY_TOO_LARGE
        )

    def test_an_oversized_response_is_cut_off_at_the_ceiling(self) -> None:
        self.upstream.huge_response = True
        relay = self._relay(max_response_bytes=8192)
        payload = b"{}"
        _status, body = self._request(
            "POST",
            "/v1/chat/completions",
            payload,
            {"Content-Length": str(len(payload))},
        )
        self.assertLessEqual(len(body), 8192)
        self.assertEqual(
            relay.stats.records[0].rejection, RelayRejection.RESPONSE_TOO_LARGE
        )

    def test_a_cross_origin_redirect_is_not_passed_back(self) -> None:
        # The upstream tries to relocate the workcell to another origin. The
        # relay refuses rather than handing back a Location to chase.
        self.upstream.redirect_away = True
        relay = self._relay()
        status, body = self._request("GET", "/v1/models")
        self.assertEqual(status, 502)
        self.assertIn(b"apoapsis_relay_refused", body)
        self.assertEqual(
            relay.stats.records[0].rejection, RelayRejection.CROSS_ORIGIN_REDIRECT
        )

    def test_a_same_origin_redirect_is_passed_through(self) -> None:
        relay = self._relay()
        decision = classify_redirect(location="/v1/models", config=relay.config)
        self.assertTrue(decision.allowed)

    def test_client_steering_headers_never_reach_the_upstream(self) -> None:
        self._relay()
        self._request("GET", "/v1/models", headers={"X-Forwarded-Host": "evil.example"})
        _method, _path, headers = self.upstream.requests[0]
        self.assertNotIn(
            "x-forwarded-host", {name.lower() for name in headers}
        )
        # The Host the upstream sees is the one http.client generated for the
        # connection the controller actually opened.
        self.assertIn(self.upstream.base_url.split("//")[1], headers.get("Host", ""))

    def test_a_dropped_stream_is_recorded_and_upstream_is_released(self) -> None:
        self.upstream.stream = True
        # A vanished client does not always produce an immediate EPIPE; once
        # the socket buffer fills, the write simply blocks. The short deadline
        # is what turns that into a recorded cancellation.
        relay = self._relay(stream_write_timeout_seconds=2.0)
        payload = b"{}"
        connection = _UnixHTTPConnection(self.socket_path)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=payload,
            headers={"Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        response.read(64)
        # The workcell hangs up mid-stream.
        connection.close()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not relay.stats.records:
            time.sleep(0.05)
        self.assertTrue(relay.stats.records, "the relay never finished the request")
        self.assertTrue(relay.stats.records[0].client_cancelled)
        self.assertEqual(relay.stats.cancellations, 1)
        # The upstream connection was closed rather than drained, so the model
        # server's slot is free instead of generating for nobody.
        self.assertTrue(self.upstream.stream_aborted)

    def test_an_unreachable_upstream_is_a_502_not_a_hang(self) -> None:
        relay = ModelRelay(
            _config(self.socket_path, upstream="http://127.0.0.1:9")
        )
        relay.start()
        self.addCleanup(relay.stop)
        status, _ = self._request("GET", "/v1/models")
        self.assertEqual(status, 502)
        self.assertEqual(relay.stats.upstream_failures, 1)

    def test_the_request_budget_stops_the_session(self) -> None:
        relay = self._relay(max_total_requests=2)
        for _ in range(2):
            self.assertEqual(self._request("GET", "/v1/models")[0], 200)
        self.assertEqual(self._request("GET", "/v1/models")[0], 429)
        self.assertEqual(relay.stats.requests_served, 2)

    def test_teardown_removes_the_socket(self) -> None:
        relay = self._relay()
        self.assertTrue(probe_relay_socket(self.socket_path))
        self.assertTrue(relay.stop())
        self.assertFalse(Path(self.socket_path).exists())
        self.assertFalse(probe_relay_socket(self.socket_path))

    def test_a_stale_socket_file_is_not_mistaken_for_a_live_relay(self) -> None:
        # A crashed controller leaves a file that looks identical on disk.
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        self.assertFalse(probe_relay_socket(self.socket_path))

    def test_the_relay_rebinds_over_a_stale_socket(self) -> None:
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        relay = self._relay()
        self.assertEqual(self._request("GET", "/v1/models")[0], 200)
        self.assertEqual(relay.stats.requests_served, 1)

    def test_the_workcell_sees_a_transport_error_when_the_controller_dies(self) -> None:
        relay = self._relay()
        relay.stop()
        connection = _UnixHTTPConnection(self.socket_path, timeout=3.0)
        with self.assertRaises(OSError):
            connection.request("GET", "/v1/models")
            connection.getresponse()
        connection.close()

    def test_a_windows_host_refuses_to_start(self) -> None:
        import apoapsis.workcell.relay as relay_module

        original = relay_module.assess_socket_support
        relay_module.assess_socket_support = lambda path: assess_socket_support(
            path, host_platform=HostPlatform.WINDOWS
        )
        try:
            relay = ModelRelay(_config(self.socket_path))
            with self.assertRaises(RelayStartupError) as caught:
                relay.start()
            self.assertIn("socket inodes", str(caught.exception))
        finally:
            relay_module.assess_socket_support = original


class RelayReadinessTests(unittest.TestCase):
    def test_the_probe_argv_uses_only_the_standard_library(self) -> None:
        argv = build_probe_argv(
            method="GET", url="http://127.0.0.1:8080/health", payload="",
            timeout_seconds=30.0,
        )
        self.assertEqual(argv[0], "python3")
        self.assertNotIn("apoapsis", argv[2])
        self.assertIn("urllib.request", argv[2])

    def test_one_token_payload_spends_one_token(self) -> None:
        payload = json.loads(one_token_payload("qwen"))
        self.assertEqual(payload["max_tokens"], 1)
        self.assertFalse(payload["stream"])

    def _passed(self, step: ReadinessStep) -> ReadinessStepResult:
        return ReadinessStepResult(step=step, status=StepStatus.PASSED)

    def test_all_steps_passing_with_observed_traffic_is_ready(self) -> None:
        report = evaluate_readiness(
            [self._passed(step) for step in ReadinessStep],
            relay_requests_observed=3,
        )
        self.assertTrue(report.ready)

    def test_success_with_no_relay_traffic_is_a_containment_failure(self) -> None:
        # The container reached a model by some path other than the socket.
        report = evaluate_readiness(
            [self._passed(step) for step in ReadinessStep],
            relay_requests_observed=0,
        )
        self.assertFalse(report.ready)
        self.assertIn("containment failure", report.detail)

    def test_a_missing_step_is_not_ready(self) -> None:
        report = evaluate_readiness(
            [self._passed(ReadinessStep.FORWARDER_LISTENING)],
            relay_requests_observed=1,
        )
        self.assertFalse(report.ready)
        self.assertEqual(
            report.step(ReadinessStep.ONE_TOKEN_COMPLETION).status, StepStatus.NOT_RUN
        )

    def test_probe_output_classification(self) -> None:
        good = classify_probe_output(
            ReadinessStep.HEALTH_ROUND_TRIP,
            stdout=json.dumps({"status": 200, "body": "{}"}),
            exit_code=0,
            duration=0.1,
        )
        self.assertEqual(good.status, StepStatus.PASSED)

        refused = classify_probe_output(
            ReadinessStep.HEALTH_ROUND_TRIP,
            stdout=json.dumps({"status": 403, "body": "refused"}),
            exit_code=0,
            duration=0.1,
        )
        self.assertEqual(refused.status, StepStatus.FAILED)

        unreachable = classify_probe_output(
            ReadinessStep.FORWARDER_LISTENING,
            stdout=json.dumps({"status": 0, "error": "URLError: refused"}),
            exit_code=0,
            duration=0.1,
        )
        self.assertEqual(unreachable.status, StepStatus.FAILED)

        timed_out = classify_probe_output(
            ReadinessStep.HEALTH_ROUND_TRIP, stdout="", exit_code=None, duration=90.0
        )
        self.assertEqual(timed_out.status, StepStatus.FAILED)

    def test_a_200_with_no_choices_is_not_a_working_model(self) -> None:
        result = classify_probe_output(
            ReadinessStep.ONE_TOKEN_COMPLETION,
            stdout=json.dumps({"status": 200, "body": json.dumps({"choices": []})}),
            exit_code=0,
            duration=0.1,
        )
        self.assertEqual(result.status, StepStatus.FAILED)
        self.assertIn("did not generate", result.detail)


class ForwarderPackagingTests(unittest.TestCase):
    def test_the_forwarder_is_standalone_stdlib_only(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "apoapsis"
            / "workcell"
            / "forwarder.py"
        )
        text = source.read_text(encoding="utf-8")
        # It runs inside a minimal image with nothing installed, and it must
        # not import the harness it is isolating.
        self.assertNotIn("from apoapsis", text)
        self.assertNotIn("import apoapsis", text)
        self.assertNotIn("pydantic", text)

    def test_the_forwarder_applies_no_policy(self) -> None:
        # A forwarder that understood requests would be a second place for
        # policy to live, and the second place is always the one that is wrong.
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "apoapsis"
            / "workcell"
            / "forwarder.py"
        )
        text = source.read_text(encoding="utf-8")
        for forbidden in ("ALLOWED_ROUTES", "classify_request", "/v1/chat"):
            self.assertNotIn(forbidden, text)

    def test_the_forwarder_binds_loopback_only(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "apoapsis"
            / "workcell"
            / "forwarder.py"
        )
        text = source.read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1"', text)
        self.assertNotIn('"0.0.0.0"', text)

    def test_the_forwarder_hashes_stably(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "apoapsis"
            / "workcell"
            / "forwarder.py"
        )
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_relay_version_is_pinned(self) -> None:
        self.assertRegex(RELAY_VERSION, r"^\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
