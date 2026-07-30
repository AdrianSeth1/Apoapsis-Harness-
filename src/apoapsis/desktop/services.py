from __future__ import annotations

from pathlib import Path

from apoapsis.desktop.home_service import DesktopHomeService
from apoapsis.desktop.import_service import DesktopImportService
from apoapsis.desktop.project_service import DesktopProjectService
from apoapsis.desktop.reference_service import DesktopReferenceService
from apoapsis.desktop.registry_store import ProjectRegistryStore


class DesktopServices:
    """Bundles the Phase 2-5 desktop services behind one project registry
    (ADR 0051/0052), so a host process -- currently only
    `DesktopIPCHTTPServer` (ADR 0053) -- constructs exactly one of these
    rather than wiring four services and their shared registry by hand."""

    def __init__(self, registry_database_path: str | Path) -> None:
        self.registry = ProjectRegistryStore(registry_database_path)
        self.project = DesktopProjectService(self.registry)
        self.imports = DesktopImportService(self.project)
        self.reference = DesktopReferenceService(self.project)
        self.home = DesktopHomeService(self.project)


__all__ = ["DesktopServices"]
