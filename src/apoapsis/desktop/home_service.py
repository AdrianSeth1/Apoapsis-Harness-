from __future__ import annotations

from typing import Any

from apoapsis.desktop.project_service import DesktopProjectService
from apoapsis.desktop.schema import ProjectStatus
from apoapsis.ui.application import ApoapsisUIService

_ACTIONS_BY_STATUS: dict[str, tuple[str, ...]] = {
    ProjectStatus.OK: (
        "import_files",
        "import_folder",
        "attach_reference_project",
        "show_project_folder",
        "close_project",
    ),
    ProjectStatus.NOT_INITIALIZED: ("initialize_project", "close_project"),
    ProjectStatus.NOT_GIT_REPOSITORY: ("close_project",),
    ProjectStatus.MISSING: ("forget_recent_project",),
    ProjectStatus.INACCESSIBLE: ("forget_recent_project",),
}


class DesktopHomeService:
    """Assembles the native shell's Home-screen data (ADR 0050 Phase 5,
    ADR 0052): the active project's identity, Git branch/clean state,
    Apoapsis initialization state, verification readiness, the
    cross-project recent-projects list Phase 2 introduced, and a
    deterministic list of the actions currently available -- so the
    (not-yet-built) native frontend renders state instead of computing it.

    A pure read. Never mutates anything, and never calls a model provider:
    `ApoapsisUIService.doctor()` is invoked with `probe_providers=False`,
    exactly as the existing browser UI already does for its own Models &
    environment page.
    """

    def __init__(self, project_service: DesktopProjectService) -> None:
        self._project_service = project_service

    def home_summary(self, session_id: str) -> dict[str, Any]:
        project_root = self._project_service.resolve_session(session_id)
        validation = self._project_service.validate_project(project_root)

        overview: dict[str, Any] | None = None
        doctor: dict[str, Any] | None = None
        readiness_error: str | None = None

        if validation["exists"] and validation["is_directory"]:
            ui_service = ApoapsisUIService(project_root)
            try:
                overview = ui_service.overview()
            except Exception as exc:  # Home must degrade, never hard-crash
                readiness_error = f"could not read project overview: {exc!r}"
            try:
                doctor = ui_service.doctor()
            except Exception as exc:
                readiness_error = readiness_error or f"could not run doctor: {exc!r}"

        return {
            "project": {
                "canonical_path": validation["canonical_path"],
                "display_name": project_root.name or str(project_root),
                "validation": validation,
            },
            "repository": overview["repository"] if overview is not None else None,
            "verification_readiness": doctor,
            "verification_readiness_error": readiness_error,
            "recent_projects": self._project_service.list_recent_projects()["projects"],
            "available_actions": list(
                _ACTIONS_BY_STATUS.get(validation["status"], ("close_project",))
            ),
        }


__all__ = ["DesktopHomeService"]
