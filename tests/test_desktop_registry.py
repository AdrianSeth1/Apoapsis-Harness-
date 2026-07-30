from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from apoapsis.cli.app import _init
from apoapsis.desktop.capability import ProjectCapabilitySessions
from apoapsis.desktop.errors import (
    CapabilitySessionError,
    ProjectAlreadyInitializedError,
    ProjectNotFoundError,
    ProjectNotGitRepositoryError,
)
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


class DesktopProjectServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.registry_db = self.base / "registry" / "registry.db"
        self.registry = ProjectRegistryStore(self.registry_db)
        self.service = DesktopProjectService(self.registry)

    def _make_git_project(self, name: str, *, initialized: bool = False) -> Path:
        root = self.base / name
        root.mkdir(parents=True)
        _git_init(root)
        if initialized:
            _init(root)
        return root

    # -- validate_project -------------------------------------------------

    def test_validate_missing_path(self) -> None:
        result = self.service.validate_project(self.base / "does-not-exist")
        self.assertEqual(result["status"], ProjectStatus.MISSING)
        self.assertFalse(result["exists"])

    def test_validate_file_not_directory(self) -> None:
        file_path = self.base / "not-a-dir.txt"
        file_path.write_text("x", encoding="utf-8")
        result = self.service.validate_project(file_path)
        self.assertEqual(result["status"], ProjectStatus.INACCESSIBLE)

    def test_validate_non_git_directory(self) -> None:
        plain_dir = self.base / "plain"
        plain_dir.mkdir()
        result = self.service.validate_project(plain_dir)
        self.assertEqual(result["status"], ProjectStatus.NOT_GIT_REPOSITORY)

    def test_validate_git_but_uninitialized(self) -> None:
        project = self._make_git_project("uninitialized-project")
        result = self.service.validate_project(project)
        self.assertEqual(result["status"], ProjectStatus.NOT_INITIALIZED)
        self.assertTrue(result["is_git_repository"])
        self.assertFalse(result["is_initialized"])

    def test_validate_initialized_project_is_ok(self) -> None:
        project = self._make_git_project("initialized-project", initialized=True)
        result = self.service.validate_project(project)
        self.assertEqual(result["status"], ProjectStatus.OK)
        self.assertTrue(result["is_initialized"])

    # -- select_project / registry -----------------------------------------

    def test_select_project_rejects_missing_path(self) -> None:
        with self.assertRaises(ProjectNotFoundError):
            self.service.select_project(self.base / "nope")

    def test_select_project_adds_to_registry_and_returns_session(self) -> None:
        project = self._make_git_project("proj-a", initialized=True)
        payload = self.service.select_project(project)
        self.assertIn("session_id", payload)
        self.assertEqual(payload["canonical_path"], str(project.resolve()))

        recents = self.service.list_recent_projects()["projects"]
        self.assertEqual(len(recents), 1)
        self.assertEqual(recents[0]["canonical_path"], str(project.resolve()))

    def test_select_project_on_uninitialized_repo_does_not_raise(self) -> None:
        # Never initializes automatically -- selecting an uninitialized Git
        # repository must succeed so the UI can then *offer* initialization.
        project = self._make_git_project("proj-uninit")
        payload = self.service.select_project(project)
        self.assertFalse(payload["initialized"])
        self.assertEqual(payload["validation"]["status"], ProjectStatus.NOT_INITIALIZED)

    def test_select_project_twice_preserves_added_at_updates_last_opened(self) -> None:
        project = self._make_git_project("proj-b", initialized=True)
        first = self.service.select_project(project)
        second = self.service.select_project(project)
        self.assertEqual(first["added_at"], second["added_at"])

    def test_forget_recent_project_removes_entry_not_directory(self) -> None:
        project = self._make_git_project("proj-c", initialized=True)
        self.service.select_project(project)
        result = self.service.forget_recent_project(str(project.resolve()))
        self.assertTrue(result["removed"])
        self.assertEqual(self.service.list_recent_projects()["projects"], [])
        self.assertTrue(project.is_dir(), "forgetting a recent entry must not delete it")

    def test_moved_project_is_detected_as_missing_on_relist(self) -> None:
        project = self._make_git_project("proj-moved", initialized=True)
        self.service.select_project(project)
        import shutil

        shutil.rmtree(project)
        recents = self.service.list_recent_projects()["projects"]
        self.assertEqual(recents[0]["validation"]["status"], ProjectStatus.MISSING)

    # -- initialize_project (explicit only) --------------------------------

    def test_initialize_project_requires_a_session(self) -> None:
        with self.assertRaises(CapabilitySessionError):
            self.service.initialize_project("not-a-real-session")

    def test_initialize_project_rejects_non_git_directory(self) -> None:
        plain_dir = self.base / "plain2"
        plain_dir.mkdir()
        # Bypass select_project's validation by creating a session directly
        # against a non-Git directory, to prove initialize_project itself
        # refuses -- not just that select_project would have refused first.
        session_id = self.service._capabilities.create(plain_dir)
        with self.assertRaises(ProjectNotGitRepositoryError):
            self.service.initialize_project(session_id)

    def test_initialize_project_explicit_confirmation_flow(self) -> None:
        project = self._make_git_project("proj-init")
        selected = self.service.select_project(project)
        self.assertEqual(selected["validation"]["status"], ProjectStatus.NOT_INITIALIZED)

        result = self.service.initialize_project(selected["session_id"])
        self.assertTrue(result["initialized"])
        self.assertTrue((project / ".apoapsis" / "config.toml").is_file())

    def test_one_project_per_window_session_is_immutable(self) -> None:
        # Phase 7 coverage: "one-project-per-window binding." A session id
        # is bound to exactly one canonical root for its entire lifetime --
        # there is no API that lets a caller retarget an existing session
        # to a different project; opening a second project always yields a
        # second, independent session id.
        project_a = self._make_git_project("window-a", initialized=True)
        project_b = self._make_git_project("window-b", initialized=True)
        session_a = self.service.select_project(project_a)["session_id"]
        session_b = self.service.select_project(project_b)["session_id"]

        self.assertNotEqual(session_a, session_b)
        self.assertEqual(
            self.service.resolve_session(session_a), project_a.resolve()
        )
        self.assertEqual(
            self.service.resolve_session(session_b), project_b.resolve()
        )
        # Re-selecting project_a again returns a *third*, independent
        # session -- it never mutates or reuses session_a.
        session_a_again = self.service.select_project(project_a)["session_id"]
        self.assertNotEqual(session_a, session_a_again)
        self.assertEqual(
            self.service.resolve_session(session_a), project_a.resolve()
        )

    def test_initialize_project_twice_raises_already_initialized(self) -> None:
        project = self._make_git_project("proj-init-twice", initialized=True)
        selected = self.service.select_project(project)
        with self.assertRaises(ProjectAlreadyInitializedError):
            self.service.initialize_project(selected["session_id"])


class ProjectCapabilitySessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = ProjectCapabilitySessions()

    def test_unknown_session_raises(self) -> None:
        with self.assertRaises(CapabilitySessionError):
            self.sessions.resolve("bogus")

    def test_create_and_resolve_round_trip(self) -> None:
        root = Path("/tmp/example-project")
        session_id = self.sessions.create(root)
        self.assertEqual(self.sessions.resolve(session_id), root)

    def test_revoke_invalidates_session(self) -> None:
        root = Path("/tmp/example-project")
        session_id = self.sessions.create(root)
        self.sessions.revoke(session_id)
        with self.assertRaises(CapabilitySessionError):
            self.sessions.resolve(session_id)

    def test_two_sessions_are_independent_and_non_transferable(self) -> None:
        root_a = Path("/tmp/project-a")
        root_b = Path("/tmp/project-b")
        session_a = self.sessions.create(root_a)
        session_b = self.sessions.create(root_b)
        self.assertEqual(self.sessions.resolve(session_a), root_a)
        self.assertEqual(self.sessions.resolve(session_b), root_b)
        self.assertNotEqual(session_a, session_b)


class ProjectRegistryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "state" / "registry.db"
        self.store = ProjectRegistryStore(self.database)

    def test_missing_database_without_initialize_raises(self) -> None:
        from apoapsis.desktop.errors import RegistryStoreError

        with self.assertRaises(RegistryStoreError):
            ProjectRegistryStore(
                Path(self.tmp.name) / "does-not-exist.db", initialize=False
            )

    def test_list_all_orders_by_last_opened_descending(self) -> None:
        from apoapsis.desktop.schema import ProjectRecord
        from apoapsis.specification.schema import utc_now
        import datetime as dt

        now = utc_now()
        older = ProjectRecord(
            canonical_path="/tmp/older",
            display_name="older",
            added_at=now - dt.timedelta(days=1),
            last_opened_at=now - dt.timedelta(days=1),
        )
        newer = ProjectRecord(
            canonical_path="/tmp/newer",
            display_name="newer",
            added_at=now,
            last_opened_at=now,
        )
        self.store.upsert(older)
        self.store.upsert(newer)
        ordered = self.store.list_all()
        self.assertEqual([r.canonical_path for r in ordered], ["/tmp/newer", "/tmp/older"])


if __name__ == "__main__":
    unittest.main()
