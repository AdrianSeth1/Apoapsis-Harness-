from __future__ import annotations

from pathlib import Path
from typing import Any

from apoapsis.cli.app import _init as _apoapsis_init
from apoapsis.desktop.capability import ProjectCapabilitySessions
from apoapsis.desktop.errors import (
    ProjectAlreadyInitializedError,
    ProjectNotFoundError,
    ProjectNotGitRepositoryError,
)
from apoapsis.desktop.registry_store import ProjectRegistryStore
from apoapsis.desktop.schema import ProjectRecord, ProjectStatus, ProjectValidation
from apoapsis.specification.schema import utc_now


class DesktopProjectService:
    """Trusted desktop-controller service (ADR 0050 Phase 2, ADR 0051):
    native project selection, switching, and explicit initialization,
    outside browser JavaScript and outside any model's reach.

    Every public method here either takes a canonical filesystem path that
    *this process* just resolved (from wherever a native folder picker's
    result eventually arrives) or an opaque `session_id` from a prior
    `select_project` call -- never a path a browser or a model invented.
    Mirrors `apoapsis.ui.application.ApoapsisUIService`'s convention of
    returning plain `dict[str, Any]` payloads and raising typed exceptions.
    """

    def __init__(
        self,
        registry: ProjectRegistryStore,
        *,
        capabilities: ProjectCapabilitySessions | None = None,
    ) -> None:
        self._registry = registry
        self._capabilities = capabilities or ProjectCapabilitySessions()

    def validate_project(self, path: str | Path) -> dict[str, Any]:
        """Deterministically inspects one path. Never initializes,
        never indexes, never mutates anything -- a pure read."""

        return self._validate(path).model_dump(mode="json")

    def _validate(self, path: str | Path) -> ProjectValidation:
        candidate = Path(path)
        try:
            canonical = candidate.resolve(strict=False)
        except OSError as exc:
            raise ProjectNotFoundError(f"cannot resolve path: {path}") from exc

        exists = canonical.exists()
        if not exists:
            return ProjectValidation(
                canonical_path=str(canonical),
                exists=False,
                is_directory=False,
                is_git_repository=False,
                is_initialized=False,
                status=ProjectStatus.MISSING,
                detail="path does not exist (moved or deleted)",
            )

        is_directory = canonical.is_dir()
        if not is_directory:
            return ProjectValidation(
                canonical_path=str(canonical),
                exists=True,
                is_directory=False,
                is_git_repository=False,
                is_initialized=False,
                status=ProjectStatus.INACCESSIBLE,
                detail="path exists but is not a directory",
            )

        is_git_repository = (canonical / ".git").exists()
        if not is_git_repository:
            return ProjectValidation(
                canonical_path=str(canonical),
                exists=True,
                is_directory=True,
                is_git_repository=False,
                is_initialized=False,
                status=ProjectStatus.NOT_GIT_REPOSITORY,
                detail="directory is not a Git repository",
            )

        is_initialized = (canonical / ".apoapsis" / "config.toml").is_file()
        return ProjectValidation(
            canonical_path=str(canonical),
            exists=True,
            is_directory=True,
            is_git_repository=True,
            is_initialized=is_initialized,
            status=ProjectStatus.OK if is_initialized else ProjectStatus.NOT_INITIALIZED,
            detail=(
                "ready"
                if is_initialized
                else "Git repository is not yet an Apoapsis project "
                "(run initialize_project explicitly)"
            ),
        )

    def select_project(self, path: str | Path) -> dict[str, Any]:
        """Binds one window/session to one canonical project root
        (ADR 0050: "each window must be bound to exactly one project
        root"). Adds/updates the recent-projects registry. Never
        initializes automatically -- a `NOT_GIT_REPOSITORY` or
        `NOT_INITIALIZED` status is returned, not raised, so the caller can
        offer explicit initialization; only a missing/inaccessible path
        raises, since there is nothing usable to open."""

        validation = self._validate(path)
        if validation.status in (ProjectStatus.MISSING, ProjectStatus.INACCESSIBLE):
            raise ProjectNotFoundError(
                f"project path is not usable ({validation.status}): {validation.canonical_path}"
            )

        canonical = Path(validation.canonical_path)
        now = utc_now()
        existing = self._registry.get(str(canonical))
        record = ProjectRecord(
            canonical_path=str(canonical),
            display_name=canonical.name or str(canonical),
            added_at=existing.added_at if existing is not None else now,
            last_opened_at=now,
            initialized=validation.is_initialized,
        )
        stored = self._registry.upsert(record)
        session_id = self._capabilities.create(canonical)
        payload = stored.model_dump(mode="json")
        payload["session_id"] = session_id
        payload["validation"] = validation.model_dump(mode="json")
        return payload

    def initialize_project(self, session_id: str) -> dict[str, Any]:
        """Explicit initialization only -- never automatic. Requires an
        active session (i.e. the operator already selected this exact
        project through `select_project`); there is no path parameter, so
        a caller cannot initialize a project it never opened."""

        canonical = self._capabilities.resolve(session_id)
        validation = self._validate(canonical)
        if validation.status == ProjectStatus.OK:
            raise ProjectAlreadyInitializedError(
                f"already initialized: {validation.canonical_path}"
            )
        if not validation.is_git_repository:
            raise ProjectNotGitRepositoryError(
                f"not a Git repository: {validation.canonical_path}"
            )

        init_result = _apoapsis_init(canonical)

        now = utc_now()
        existing = self._registry.get(str(canonical))
        record = ProjectRecord(
            canonical_path=str(canonical),
            display_name=canonical.name or str(canonical),
            added_at=existing.added_at if existing is not None else now,
            last_opened_at=now,
            initialized=True,
        )
        stored = self._registry.upsert(record)
        payload = stored.model_dump(mode="json")
        payload["initialize_result"] = init_result
        return payload

    def list_recent_projects(self) -> dict[str, Any]:
        """Recent projects with canonical paths, last-opened time, and a
        freshly re-validated status -- so a moved/missing project is
        surfaced honestly instead of from stale registry metadata."""

        items = []
        for record in self._registry.list_all():
            validation = self._validate(record.canonical_path)
            item = record.model_dump(mode="json")
            item["validation"] = validation.model_dump(mode="json")
            items.append(item)
        return {"projects": items}

    def forget_recent_project(self, canonical_path: str) -> dict[str, Any]:
        """Removes one registry entry. Never deletes, moves, or otherwise
        touches the actual project directory on disk."""

        removed = self._registry.delete(canonical_path)
        return {"canonical_path": canonical_path, "removed": removed}

    def resolve_session(self, session_id: str) -> Path:
        """The one function the import service (Phase 3) is allowed to use
        to turn an opaque session id back into a real path -- never
        exposed as a way to look up an arbitrary path by itself."""

        return self._capabilities.resolve(session_id)

    def close_session(self, session_id: str) -> None:
        self._capabilities.revoke(session_id)


__all__ = ["DesktopProjectService"]
