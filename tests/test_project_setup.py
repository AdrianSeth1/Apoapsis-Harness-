from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from apoapsis.project_setup import ProjectSetupError, prepare_selected_project


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


class FriendlyProjectSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "selected project"
        self.root.mkdir()

    def test_empty_folder_becomes_a_ready_clean_project(self) -> None:
        result = prepare_selected_project(self.root)
        self.assertTrue(result["ready"])
        self.assertTrue(result["created_git_repository"])
        self.assertTrue(result["created_initial_commit"])
        self.assertTrue((self.root / ".apoapsis" / "config.toml").is_file())
        self.assertFalse((self.root / ".gitignore").exists())
        self.assertEqual(_git(self.root, "status", "--porcelain").stdout, "")
        self.assertEqual(_git(self.root, "rev-list", "--count", "HEAD").stdout.strip(), "1")
        exclude = _git(self.root, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        exclude_path = Path(exclude)
        if not exclude_path.is_absolute():
            exclude_path = self.root / exclude_path
        self.assertIn(".apoapsis/", exclude_path.read_text(encoding="utf-8"))

    def test_empty_unborn_git_repository_gets_its_first_checkpoint(self) -> None:
        _git(self.root, "init", "-b", "main")
        result = prepare_selected_project(self.root)
        self.assertFalse(result["created_git_repository"])
        self.assertTrue(result["created_initial_commit"])
        self.assertEqual(_git(self.root, "status", "--porcelain").stdout, "")

    def test_existing_committed_project_is_initialized_without_becoming_dirty(self) -> None:
        _git(self.root, "init", "-b", "main")
        (self.root / "README.md").write_text("# Existing project\n", encoding="utf-8")
        _git(self.root, "add", "README.md")
        _git(
            self.root,
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "initial",
        )
        result = prepare_selected_project(self.root)
        self.assertFalse(result["created_git_repository"])
        self.assertFalse(result["created_initial_commit"])
        self.assertTrue(result["config_created"])
        self.assertEqual(_git(self.root, "status", "--porcelain").stdout, "")

    def test_nonempty_non_git_folder_is_refused_without_changes(self) -> None:
        source = self.root / "notes.txt"
        source.write_text("keep me", encoding="utf-8")
        with self.assertRaisesRegex(ProjectSetupError, "already contains files"):
            prepare_selected_project(self.root)
        self.assertEqual(source.read_text(encoding="utf-8"), "keep me")
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / ".apoapsis").exists())

    def test_unborn_repository_with_user_files_is_refused_before_initialization(self) -> None:
        _git(self.root, "init", "-b", "main")
        source = self.root / "draft.txt"
        source.write_text("not committed", encoding="utf-8")
        with self.assertRaisesRegex(ProjectSetupError, "no saved starting point"):
            prepare_selected_project(self.root)
        self.assertTrue(source.is_file())
        self.assertFalse((self.root / ".apoapsis").exists())

    def test_empty_subfolder_of_existing_project_does_not_become_nested_repository(self) -> None:
        parent = self.root
        _git(parent, "init", "-b", "main")
        _git(
            parent,
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        )
        child = parent / "empty child"
        child.mkdir()
        with self.assertRaisesRegex(ProjectSetupError, "inside a Git project"):
            prepare_selected_project(child)
        self.assertFalse((child / ".git").exists())
        self.assertFalse((child / ".apoapsis").exists())

    def test_reopening_a_ready_project_is_idempotent(self) -> None:
        prepare_selected_project(self.root)
        second = prepare_selected_project(self.root)
        self.assertFalse(second["created_git_repository"])
        self.assertFalse(second["created_initial_commit"])
        self.assertFalse(second["config_created"])
        self.assertEqual(_git(self.root, "rev-list", "--count", "HEAD").stdout.strip(), "1")


if __name__ == "__main__":
    unittest.main()
