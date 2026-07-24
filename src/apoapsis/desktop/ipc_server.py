from __future__ import annotations

import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from apoapsis.desktop.errors import (
    CapabilitySessionError,
    DesktopError,
    ImportApprovalError,
    ImportPreviewNotFoundError,
    ImportSafetyError,
    ProjectAlreadyInitializedError,
    ProjectNotFoundError,
    ProjectNotGitRepositoryError,
    ReferenceEvidenceSafetyError,
    ReferenceProjectInvalidError,
    RegistryStoreError,
)
from apoapsis.desktop.services import DesktopServices

_MAX_REQUEST_BYTES = 256 * 1024

# Every route is a typed operation name, matching ADR 0050 Phase 6's list
# exactly. There is deliberately no route that accepts an arbitrary path
# and hands it straight to a filesystem call without going through one of
# these named, validated service methods.
_ROUTES: tuple[str, ...] = (
    "validate_project",
    "select_project",
    "initialize_project",
    "list_recent_projects",
    "forget_recent_project",
    "close_session",
    "preview_import",
    "approve_import",
    "execute_import",
    "attach_reference_project",
    "select_reference_evidence",
    "list_reference_evidence",
    "detach_reference_project",
    "home_summary",
)


class DesktopIPCHTTPServer(ThreadingHTTPServer):
    """The privileged, non-browser-facing local IPC channel ADR 0052
    identified as missing and ADR 0053 implements: a second loopback HTTP
    listener, on its own OS-assigned port, guarded by its own capability
    token -- generated and held only by the native host process, never
    the browser-facing token/URL the webview receives, and never reachable
    through the browser-facing server's own CSP (`connect-src 'self'`
    resolves to that server's own origin/port, not this one).

    Runs in the same Python process as the existing browser-facing
    `apoapsis.ui.server.ApoapsisUIHTTPServer` -- not a second process --
    so `DesktopServices`' in-memory capability sessions (ADR 0051/0052)
    are the same objects both the browser-facing overview data and this
    channel's project/import/reference operations see."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        services: DesktopServices,
        privileged_token: str,
    ) -> None:
        self.services = services
        self.privileged_token = privileged_token
        super().__init__(server_address, DesktopIPCRequestHandler)

    @property
    def origin(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


class DesktopIPCRequestHandler(BaseHTTPRequestHandler):
    server: DesktopIPCHTTPServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._send_error(HTTPStatus.NOT_FOUND, "route not found")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorized():
            return
        path = urlsplit(self.path).path
        route = path[len("/desktop/") :] if path.startswith("/desktop/") else None
        if route not in _ROUTES:
            self._send_error(HTTPStatus.NOT_FOUND, "route not found")
            return
        try:
            body = self._read_json_body()
            handler: Callable[[dict[str, Any]], dict[str, Any]] = getattr(
                self, f"_handle_{route}"
            )
            payload = handler(body)
        except (
            ProjectNotFoundError,
            CapabilitySessionError,
            ImportPreviewNotFoundError,
        ) as exc:
            self._send_error(HTTPStatus.NOT_FOUND, str(exc))
        except (
            ProjectNotGitRepositoryError,
            ProjectAlreadyInitializedError,
            ImportApprovalError,
            ReferenceProjectInvalidError,
        ) as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except (
            ImportSafetyError,
            ReferenceEvidenceSafetyError,
            RegistryStoreError,
            DesktopError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            self._send_json(HTTPStatus.OK, payload)

    # -- one handler per route, each a thin, typed wrapper -----------------

    def _handle_validate_project(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.server.services.project.validate_project(
            self._require_str(body, "path")
        )

    def _handle_select_project(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.server.services.project.select_project(
            self._require_str(body, "path")
        )

    def _handle_initialize_project(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.server.services.project.initialize_project(
            self._require_str(body, "session_id")
        )

    def _handle_list_recent_projects(self, _body: dict[str, Any]) -> dict[str, Any]:
        return self.server.services.project.list_recent_projects()

    def _handle_forget_recent_project(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.server.services.project.forget_recent_project(
            self._require_str(body, "canonical_path")
        )

    def _handle_close_session(self, body: dict[str, Any]) -> dict[str, Any]:
        session_id = self._require_str(body, "session_id")
        self.server.services.project.close_session(session_id)
        return {"session_id": session_id, "closed": True}

    def _handle_preview_import(self, body: dict[str, Any]) -> dict[str, Any]:
        session_id = self._require_str(body, "session_id")
        sources = body.get("sources")
        if not isinstance(sources, list) or not sources or not all(
            isinstance(item, str) for item in sources
        ):
            raise ValueError("sources must be a non-empty list of strings")
        destination_relative_dir = body.get("destination_relative_dir", "")
        if not isinstance(destination_relative_dir, str):
            raise ValueError("destination_relative_dir must be a string")
        return self.server.services.imports.preview_import(
            session_id,
            sources=sources,
            destination_relative_dir=destination_relative_dir,
        )

    def _handle_approve_import(self, body: dict[str, Any]) -> dict[str, Any]:
        replacements_confirmed = body.get("replacements_confirmed", False)
        if not isinstance(replacements_confirmed, bool):
            raise ValueError("replacements_confirmed must be a boolean")
        return self.server.services.imports.approve_import(
            self._require_str(body, "session_id"),
            self._require_str(body, "preview_id"),
            replacements_confirmed=replacements_confirmed,
        )

    def _handle_execute_import(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.server.services.imports.execute_import(
            self._require_str(body, "session_id"),
            self._require_str(body, "preview_id"),
        )

    def _handle_attach_reference_project(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.server.services.reference.attach_reference_project(
            self._require_str(body, "session_id"),
            self._require_str(body, "reference_path"),
        )

    def _handle_select_reference_evidence(self, body: dict[str, Any]) -> dict[str, Any]:
        relative_paths = body.get("relative_paths")
        if not isinstance(relative_paths, list) or not relative_paths or not all(
            isinstance(item, str) for item in relative_paths
        ):
            raise ValueError("relative_paths must be a non-empty list of strings")
        return self.server.services.reference.select_reference_evidence(
            self._require_str(body, "reference_session_id"), relative_paths
        )

    def _handle_list_reference_evidence(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.server.services.reference.list_reference_evidence(
            self._require_str(body, "session_id")
        )

    def _handle_detach_reference_project(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.server.services.reference.detach_reference_project(
            self._require_str(body, "reference_session_id")
        )

    def _handle_home_summary(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.server.services.home.home_summary(
            self._require_str(body, "session_id")
        )

    # -- shared plumbing ---------------------------------------------------

    @staticmethod
    def _require_str(body: dict[str, Any], key: str) -> str:
        value = body.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} is required and must be a non-empty string")
        return value

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Apoapsis-Desktop-Token", "")
        if not secrets.compare_digest(supplied, self.server.privileged_token):
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid desktop IPC token")
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
        if content_length < 0 or content_length > _MAX_REQUEST_BYTES:
            raise ValueError("request body size is invalid")
        raw = self.rfile.read(content_length) if content_length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        content = (
            json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def create_desktop_ipc_server(
    registry_database_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    privileged_token: str | None = None,
) -> DesktopIPCHTTPServer:
    """Mirrors `apoapsis.ui.server.create_ui_server`'s shape, but for the
    privileged desktop channel: loopback-only, a fresh high-entropy token
    per call unless one is supplied (tests only), and `port=0` by default
    so the OS assigns an ephemeral port -- exactly as `backend_entry.py`
    already does for the browser-facing server, and for the same reason
    (a native host process reads the real bound port back, rather than
    the two servers racing over a fixed port number)."""

    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("the Apoapsis desktop IPC channel may bind only to loopback")
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    services = DesktopServices(registry_database_path)
    return DesktopIPCHTTPServer(
        (host, port),
        services,
        privileged_token or secrets.token_urlsafe(32),
    )


__all__ = [
    "DesktopIPCHTTPServer",
    "DesktopIPCRequestHandler",
    "create_desktop_ipc_server",
]
