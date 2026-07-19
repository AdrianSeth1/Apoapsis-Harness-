from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from apoapsis.architect.errors import (
    ConcurrentPlanTransitionError,
    InvalidPlanTransitionError,
    PlanActionError,
    PlanNotFoundError,
    PlanStoreError,
)
from apoapsis.execution.operation_errors import (
    ExecutionOperationError,
    ExecutionOperationNotFoundError,
)
from apoapsis.intake.errors import IntakeError, IntakeOperationNotFoundError
from apoapsis.review.errors import FrontierUnavailableError, OperationNotFoundError, ReviewError
from apoapsis.ui.application import ApoapsisUIService, UIActionError
from apoapsis.workflow.engine import (
    ConcurrentTransitionError,
    InvalidTransitionError,
    TaskNotFoundError,
    TaskStoreError,
)

_ASSET_CONTENT_TYPES = {
    "/": "text/html; charset=utf-8",
    "/index.html": "text/html; charset=utf-8",
    "/app.js": "text/javascript; charset=utf-8",
    "/styles.css": "text/css; charset=utf-8",
}
_ASSET_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
}
_MAX_REQUEST_BYTES = 64 * 1024


class ApoapsisUIHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        service: ApoapsisUIService,
        session_token: str,
    ) -> None:
        self.service = service
        self.session_token = session_token
        self.static_root = Path(__file__).with_name("static")
        super().__init__(server_address, ApoapsisUIRequestHandler)

    @property
    def origin(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


class ApoapsisUIRequestHandler(BaseHTTPRequestHandler):
    server: ApoapsisUIHTTPServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"}, authorize=False)
            return
        if path.startswith("/api/"):
            if not self._authorized():
                return
            self._handle_api_get(path)
            return
        self._serve_asset(path)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if not path.startswith("/api/") or not self._authorized():
            if not path.startswith("/api/"):
                self._send_error(HTTPStatus.NOT_FOUND, "route not found")
            return
        if path.startswith("/api/tasks/") and path.endswith("/approve"):
            self._handle_task_approve(path)
            return
        if path.startswith("/api/tasks/") and path.endswith("/execute"):
            self._handle_task_execute(path)
            return
        if path.startswith("/api/plans/") and path.endswith("/approve"):
            self._handle_plan_approve(path)
            return
        if path.startswith("/api/reviews/") and path.endswith("/operations"):
            self._handle_review_operation_submit(path)
            return
        if path == "/api/intake/operations":
            self._handle_intake_operation_submit()
            return
        self._send_error(HTTPStatus.NOT_FOUND, "route not found")

    def _handle_task_approve(self, path: str) -> None:
        task_id = unquote(path[len("/api/tasks/") : -len("/approve")]).strip("/")
        try:
            expected_version = self._read_expected_version()
            payload = self.server.service.approve_specification(
                task_id, expected_version=expected_version
            )
        except TaskNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "task not found")
        except (ConcurrentTransitionError, InvalidTransitionError, UIActionError) as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except (TaskStoreError, ValueError, json.JSONDecodeError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            self._send_json(HTTPStatus.OK, payload)

    def _handle_task_execute(self, path: str) -> None:
        task_id = unquote(path[len("/api/tasks/") : -len("/execute")]).strip("/")
        try:
            body = self._read_json_body()
            operation_id = body.get("operation_id")
            expected_version = body.get("expected_version")
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError("operation_id is required")
            if not isinstance(expected_version, int) or isinstance(
                expected_version, bool
            ):
                raise ValueError("expected_version must be an integer")
            payload = self.server.service.submit_execution_operation(
                task_id,
                operation_id=operation_id,
                expected_version=expected_version,
            )
        except TaskNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "task not found")
        except ExecutionOperationError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except (TaskStoreError, ValueError, json.JSONDecodeError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            self._send_json(HTTPStatus.ACCEPTED, payload)

    def _handle_plan_approve(self, path: str) -> None:
        plan_id = unquote(path[len("/api/plans/") : -len("/approve")]).strip("/")
        try:
            expected_version = self._read_expected_version()
            payload = self.server.service.approve_plan(
                plan_id, expected_version=expected_version
            )
        except PlanNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "plan not found")
        except (
            ConcurrentPlanTransitionError,
            InvalidPlanTransitionError,
            PlanActionError,
        ) as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except (PlanStoreError, ValueError, json.JSONDecodeError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            self._send_json(HTTPStatus.OK, payload)

    def _handle_review_operation_submit(self, path: str) -> None:
        task_id = unquote(
            path[len("/api/reviews/") : -len("/operations")]
        ).strip("/")
        try:
            body = self._read_json_body()
            action = body.get("action")
            operation_id = body.get("operation_id")
            expected_version = body.get("expected_version")
            expected_fingerprint = body.get("expected_worktree_fingerprint")
            additional_turns = body.get("additional_turns")
            if not isinstance(action, str) or not action:
                raise ValueError("action is required")
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError("operation_id is required")
            if not isinstance(expected_version, int) or isinstance(
                expected_version, bool
            ):
                raise ValueError("expected_version must be an integer")
            if expected_fingerprint is not None and not isinstance(
                expected_fingerprint, str
            ):
                raise ValueError("expected_worktree_fingerprint must be a string")
            if additional_turns is not None and (
                not isinstance(additional_turns, int)
                or isinstance(additional_turns, bool)
            ):
                raise ValueError("additional_turns must be an integer")
            payload = self.server.service.submit_review_operation(
                task_id,
                action=action,
                operation_id=operation_id,
                expected_version=expected_version,
                expected_worktree_fingerprint=expected_fingerprint,
                additional_turns=additional_turns,
            )
        except TaskNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "task not found")
        except FrontierUnavailableError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except ReviewError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except (TaskStoreError, ValueError, json.JSONDecodeError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            self._send_json(HTTPStatus.ACCEPTED, payload)

    def _handle_intake_operation_submit(self) -> None:
        try:
            body = self._read_json_body()
            request_text = body.get("request_text")
            operation_id = body.get("operation_id")
            if not isinstance(request_text, str) or not request_text.strip():
                raise ValueError("request_text is required")
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError("operation_id is required")
            payload = self.server.service.submit_intake_operation(
                request_text=request_text, operation_id=operation_id
            )
        except IntakeError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except (TaskStoreError, ValueError, json.JSONDecodeError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            self._send_json(HTTPStatus.ACCEPTED, payload)

    def _read_expected_version(self) -> int:
        body = self._read_json_body()
        expected_version = body.get("expected_version")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValueError("expected_version must be an integer")
        return expected_version

    def _handle_api_get(self, path: str) -> None:
        try:
            if path == "/api/overview":
                payload = self.server.service.overview()
            elif path == "/api/doctor":
                payload = self.server.service.doctor()
            elif path == "/api/evaluations":
                payload = self.server.service.evaluations()
            elif path == "/api/plans":
                payload = self.server.service.plans()
            elif path.startswith("/api/tasks/"):
                task_id = unquote(path[len("/api/tasks/") :]).strip("/")
                payload = self.server.service.task_detail(task_id)
            elif path.startswith("/api/plans/"):
                plan_id = unquote(path[len("/api/plans/") :]).strip("/")
                payload = self.server.service.plan_detail(plan_id)
            elif path == "/api/reviews":
                payload = self.server.service.review_cases()
            elif "/operations/" in path and path.startswith("/api/reviews/"):
                remainder = path[len("/api/reviews/") :]
                _task_id_part, _, operation_id_part = remainder.partition(
                    "/operations/"
                )
                operation_id = unquote(operation_id_part).strip("/")
                payload = self.server.service.review_operation_status(operation_id)
            elif path.startswith("/api/reviews/"):
                task_id = unquote(path[len("/api/reviews/") :]).strip("/")
                payload = self.server.service.review_case_detail(task_id)
            elif path.startswith("/api/intake/operations/"):
                operation_id = unquote(
                    path[len("/api/intake/operations/") :]
                ).strip("/")
                payload = self.server.service.intake_operation_status(operation_id)
            elif path.startswith("/api/execution/operations/"):
                operation_id = unquote(
                    path[len("/api/execution/operations/") :]
                ).strip("/")
                payload = self.server.service.execution_operation_status(operation_id)
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "route not found")
                return
        except (
            TaskNotFoundError,
            PlanNotFoundError,
            OperationNotFoundError,
            IntakeOperationNotFoundError,
            ExecutionOperationNotFoundError,
        ):
            self._send_error(HTTPStatus.NOT_FOUND, "not found")
        except (
            TaskStoreError,
            PlanStoreError,
            ReviewError,
            IntakeError,
            ExecutionOperationError,
            ValueError,
        ) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            self._send_json(HTTPStatus.OK, payload)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Apoapsis-Session", "")
        if not secrets.compare_digest(supplied, self.server.session_token):
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid UI session")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            self._send_error(HTTPStatus.FORBIDDEN, "cross-origin request rejected")
            return False
        return True

    def _read_json_body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if content_length < 1 or content_length > _MAX_REQUEST_BYTES:
            raise ValueError("request body size is invalid")
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _serve_asset(self, path: str) -> None:
        filename = _ASSET_FILES.get(path)
        if filename is None:
            self._send_error(HTTPStatus.NOT_FOUND, "asset not found")
            return
        target = self.server.static_root / filename
        try:
            content = target.read_bytes()
        except OSError:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "asset unavailable")
            return
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header("Content-Type", _ASSET_CONTENT_TYPES[path])
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        authorize: bool = True,
    ) -> None:
        del authorize  # documents the intentionally public health response
        content = (
            json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def _security_headers(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def create_ui_server(
    project_root: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 7331,
    session_token: str | None = None,
) -> ApoapsisUIHTTPServer:
    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("the Apoapsis UI may bind only to a loopback address")
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    service = ApoapsisUIService(project_root)
    # Start every operation worker (and its startup recovery pass, ADR
    # 0025) immediately, before this server ever accepts a request --
    # not lazily, on whichever operation type happens to be submitted
    # first.
    service.start_background_workers()
    return ApoapsisUIHTTPServer(
        (host, port),
        service,
        session_token or secrets.token_urlsafe(32),
    )


def serve_ui(
    project_root: str | Path,
    *,
    port: int = 7331,
    open_browser: bool = True,
) -> None:
    server = create_ui_server(project_root, port=port)
    url = f"{server.origin}/?session={server.session_token}"
    print(f"Apoapsis UI: {url}")
    print("Press Ctrl+C to stop the local UI server.")
    if open_browser:
        threading.Timer(0.15, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = ["ApoapsisUIHTTPServer", "create_ui_server", "serve_ui"]
