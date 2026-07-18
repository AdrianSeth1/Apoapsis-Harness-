from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from apoapsis.execution.worktree import WorktreeError, WorktreeManager
from apoapsis.repository.git import GitRepository


class GitWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        temporary_root = Path(self.temporary_directory.name)
        self.repository_path = temporary_root / "repository"
        self.worktree_root = temporary_root / "managed-worktrees"
        self.repository_path.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Apoapsis Tests")
        (self.repository_path / "README.md").write_text(
            "fixture\n", encoding="utf-8"
        )
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repository_path,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_repository_snapshot_reports_clean_branch_and_commit(self) -> None:
        snapshot = GitRepository(self.repository_path).snapshot()

        self.assertTrue(snapshot.is_clean)
        self.assertEqual(snapshot.branch, "main")
        self.assertEqual(len(snapshot.head_commit), 40)
        self.assertEqual(snapshot.changed_files, [])

    def test_create_refuse_dirty_cleanup_then_force_cleanup(self) -> None:
        manager = WorktreeManager(
            self.repository_path, worktree_root=self.worktree_root
        )
        managed = manager.create("test-001")
        managed_path = Path(managed.path)

        self.assertTrue(managed_path.is_dir())
        self.assertEqual(managed.branch, "apoapsis/test-001")
        self.assertEqual(
            GitRepository(managed_path).snapshot().branch,
            "apoapsis/test-001",
        )

        (managed_path / "README.md").write_text(
            "changed\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(WorktreeError, "uncommitted changes"):
            manager.cleanup("test-001")

        manager.cleanup("test-001", force=True, delete_branch=True)
        self.assertFalse(managed_path.exists())
        branch = subprocess.run(
            [
                "git",
                "show-ref",
                "--verify",
                "--quiet",
                "refs/heads/apoapsis/test-001",
            ],
            cwd=self.repository_path,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(branch.returncode, 0)

    def test_slug_cannot_escape_managed_root(self) -> None:
        manager = WorktreeManager(
            self.repository_path, worktree_root=self.worktree_root
        )
        with self.assertRaises(WorktreeError):
            manager.create("../escape")


if __name__ == "__main__":
    unittest.main()
