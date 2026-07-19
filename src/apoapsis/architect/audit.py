from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from apoapsis.architect.schema import PlannerRequestPackage
from apoapsis.specification.schema import StrictModel


class AuditArtifact(StrictModel):
    kind: str
    path: str


def _atomic_write(root: Path, filename: str, project_root: Path, value: str, kind: str) -> AuditArtifact:
    """Atomic, fsync'd write identical in discipline to ``audit.store
    .TaskAuditStore._write``: a same-directory tempfile, fsync, then
    ``os.replace``, so an interrupted write never leaves a corrupt or
    half-written artifact at its final path."""

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


class PlanAuditStore:
    """Immutable audit artifacts for one Architect Mode plan, rooted at
    ``.apoapsis/plans/<plan_id>/`` -- imported responses, per-version plan
    snapshots, validation results, and approval events."""

    def __init__(self, project_root: str | Path, plan_id: str) -> None:
        self.project_root = Path(project_root).resolve()
        self.plan_id = plan_id
        self.root = self.project_root / ".apoapsis" / "plans" / plan_id

    def write_json(
        self, filename: str, value: BaseModel | dict[str, Any] | list[Any], *, kind: str
    ) -> AuditArtifact:
        serializable: Any = (
            value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        )
        text = json.dumps(serializable, indent=2, sort_keys=True) + "\n"
        return _atomic_write(self.root, filename, self.project_root, text, kind)

    def artifacts(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            str(path.relative_to(self.project_root)).replace("\\", "/")
            for path in self.root.rglob("*")
            if path.is_file()
        )


def write_package_artifact(project_root: str | Path, package: PlannerRequestPackage) -> str:
    """Write the exported planner request package before it leaves
    Apoapsis, rooted at ``.apoapsis/plan-packages/<package_id>/`` --
    separate from any plan, since no plan exists yet at export time."""

    root = Path(project_root).resolve()
    directory = root / ".apoapsis" / "plan-packages" / package.package_id
    text = json.dumps(package.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    artifact = _atomic_write(directory, "request-package.json", root, text, "planner_request_package")
    return artifact.path


__all__ = ["AuditArtifact", "PlanAuditStore", "write_package_artifact"]
