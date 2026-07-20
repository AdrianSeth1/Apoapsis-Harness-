from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from apoapsis.discovery.schema import FrontierPlanningRequestPackage
from apoapsis.specification.schema import StrictModel


class AuditArtifact(StrictModel):
    kind: str
    path: str


def _atomic_write(
    root: Path, filename: str, project_root: Path, value: str, kind: str
) -> AuditArtifact:
    """Same atomic-write discipline as ``audit.store.TaskAuditStore``/
    ``architect.audit.PlanAuditStore``: a same-directory tempfile, fsync,
    then ``os.replace``, so an interrupted write never leaves a corrupt or
    half-written artifact."""

    if Path(filename).name != filename:
        raise ValueError("audit filename must not contain a directory")
    root.mkdir(parents=True, exist_ok=True)
    destination = root / filename
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{filename}.", dir=root, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    relative = destination.relative_to(project_root).as_posix()
    return AuditArtifact(kind=kind, path=relative)


class DiscoveryAuditStore:
    """Immutable audit artifacts for one discovery session, rooted at
    ``.apoapsis/discovery/<session_id>/``."""

    def __init__(self, project_root: str | Path, session_id: str) -> None:
        self.project_root = Path(project_root).resolve()
        self.session_id = session_id
        self.root = self.project_root / ".apoapsis" / "discovery" / session_id

    def write_json(
        self, filename: str, value: BaseModel | dict[str, Any] | list[Any], *, kind: str
    ) -> AuditArtifact:
        serializable: Any = (
            value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        )
        text = json.dumps(serializable, indent=2, sort_keys=True) + "\n"
        return _atomic_write(self.root, filename, self.project_root, text, kind)

    def write_text(self, filename: str, value: str, *, kind: str) -> AuditArtifact:
        return _atomic_write(self.root, filename, self.project_root, value, kind)

    def artifacts(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            str(path.relative_to(self.project_root)).replace("\\", "/")
            for path in self.root.rglob("*")
            if path.is_file()
        )


def write_frontier_package_artifact(
    project_root: str | Path, package: FrontierPlanningRequestPackage
) -> str:
    """Writes the exported frontier planning request package before it
    leaves Apoapsis, rooted at
    ``.apoapsis/discovery-planning-packages/<package_id>/`` -- separate
    from the session's own audit directory, mirroring
    ``architect.audit.write_package_artifact``'s convention of keeping an
    exported package addressable by its own id."""

    root = Path(project_root).resolve()
    directory = root / ".apoapsis" / "discovery-planning-packages" / package.package_id
    text = json.dumps(package.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    artifact = _atomic_write(
        directory, "request-package.json", root, text, "frontier_planning_request_package"
    )
    return artifact.path


__all__ = [
    "AuditArtifact",
    "DiscoveryAuditStore",
    "write_frontier_package_artifact",
]
