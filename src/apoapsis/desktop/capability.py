from __future__ import annotations

import secrets
import threading
from pathlib import Path

from apoapsis.desktop.errors import CapabilitySessionError


class ProjectCapabilitySessions:
    """In-memory, process-lifetime window/project capability sessions
    (ADR 0050 Phase 6, ADR 0051). A native window (or, in this codebase's
    current state, any caller acting on the desktop layer's behalf) never
    supplies a raw filesystem path to `preview_import`/`approve_import`/
    `execute_import`/`initialize_project` -- it supplies the opaque
    `session_id` returned by `select_project`, and this class alone
    resolves that id back to the one canonical project root it was bound
    to at creation.

    Deliberately **not** persisted to disk: a session is window-scoped and
    must become invalid the moment the process restarts (matching the
    ADR's "invalid after application restart unless the operator selects
    the path again" requirement) -- an in-memory dict achieves that for
    free, with no explicit expiry logic to get wrong.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Path] = {}
        self._lock = threading.Lock()

    def create(self, project_root: Path) -> str:
        session_id = f"desktop-session-{secrets.token_urlsafe(24)}"
        with self._lock:
            self._sessions[session_id] = project_root
        return session_id

    def resolve(self, session_id: str) -> Path:
        with self._lock:
            project_root = self._sessions.get(session_id)
        if project_root is None:
            raise CapabilitySessionError(
                f"unknown or expired desktop session: {session_id!r}"
            )
        return project_root

    def revoke(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()


__all__ = ["ProjectCapabilitySessions"]
