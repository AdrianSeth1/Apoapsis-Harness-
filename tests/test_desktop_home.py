from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from apoapsis.cli.app import _init
from apoapsis.desktop.home_service import DesktopHomeService
from apoapsis.desktop.project_service import DesktopProjectService
from apoapsis.desktop.registry_store import ProjectRegistryStore
from apoapsis.desktop.schema import ProjectStatus


def _git_init(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True, text=True
    )
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Apoapsis Tests",
            "-c",
            "user.email=tests@apoapsis.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


class DesktopHomeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.registry = ProjectRegistryStore(self.base / "registry.db")
        self.project_service = DesktopProjectService(self.registry)
        self.home_service = DesktopHomeService(self.project_service)

    def test_home_summary_for_initialized_project(self) -> None:
        project = self.base / "proj"
        project.mkdir()
        _git_init(project)
        _init(project)
        session_id = self.project_service.select_project(project)["session_id"]

        summary = self.home_service.home_summary(session_id)
        self.assertEqual(summary["project"]["validation"]["status"], ProjectStatus.OK)
        self.assertIn("import_files", summary["available_actions"])
        self.assertIn("attach_reference_project", summary["available_actions"])
        self.assertIsNotNone(summary["repository"])

    def test_home_summary_for_uninitialized_project_offers_initialize_only(self) -> None:
        project = self.base / "proj-uninit"
        project.mkdir()
        _git_init(project)
        session_id = self.project_service.select_project(project)["session_id"]

        summary = self.home_service.home_summary(session_id)
        self.assertEqual(
            summary["project"]["validation"]["status"], ProjectStatus.NOT_INITIALIZED
        )
        self.assertEqual(summary["available_actions"], ["initialize_project", "close_project"])
        self.assertNotIn("import_files", summary["available_actions"])

    def test_home_summary_lists_recent_projects_across_sessions(self) -> None:
        project_a = self.base / "proj-a"
        project_a.mkdir()
        _git_init(project_a)
        _init(project_a)
        project_b = self.base / "proj-b"
        project_b.mkdir()
        _git_init(project_b)
        _init(project_b)

        session_a = self.project_service.select_project(project_a)["session_id"]
        self.project_service.select_project(project_b)

        summary = self.home_service.home_summary(session_a)
        canonical_paths = {p["canonical_path"] for p in summary["recent_projects"]}
        self.assertEqual(
            canonical_paths, {str(project_a.resolve()), str(project_b.resolve())}
        )

    def test_home_summary_never_raises_for_missing_project(self) -> None:
        project = self.base / "proj-missing"
        project.mkdir()
        _git_init(project)
        _init(project)
        session_id = self.project_service.select_project(project)["session_id"]

        import shutil

        shutil.rmtree(project)

        summary = self.home_service.home_summary(session_id)
        self.assertEqual(summary["project"]["validation"]["status"], ProjectStatus.MISSING)
        self.assertEqual(summary["available_actions"], ["forget_recent_project"])
        self.assertIsNone(summary["repository"])


if __name__ == "__main__":
    unittest.main()
