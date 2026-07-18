from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from apoapsis.agent.inspection import RepositoryInspector
from apoapsis.repository.fingerprint import compute_worktree_fingerprint


class _GitRepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Apoapsis Tests")
        (self.root / "tracked.py").write_text("value = 1\n", encoding="utf-8")
        self._git("add", "tracked.py")
        self._git("commit", "-m", "baseline")

    def _git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )


class WorktreeFingerprintTests(_GitRepoTestCase):
    """ADR 0017: the shared fingerprint used to scope verification caching,
    command results, and acceptance proof must be sensitive to tracked
    edits, untracked file creation/editing, and untracked binary/symlink
    presence, while ignoring the harness's own bookkeeping directories."""

    def test_tracked_edit_changes_the_fingerprint(self) -> None:
        before = compute_worktree_fingerprint(self.root).digest
        (self.root / "tracked.py").write_text("value = 2\n", encoding="utf-8")
        after = compute_worktree_fingerprint(self.root).digest
        self.assertNotEqual(before, after)

    def test_creating_an_untracked_file_changes_the_fingerprint(self) -> None:
        before = compute_worktree_fingerprint(self.root).digest
        (self.root / "new_file.py").write_text("value = 1\n", encoding="utf-8")
        after = compute_worktree_fingerprint(self.root).digest
        self.assertNotEqual(before, after)

    def test_editing_only_an_untracked_file_changes_the_fingerprint_again(
        self,
    ) -> None:
        (self.root / "new_file.py").write_text("value = 1\n", encoding="utf-8")
        first = compute_worktree_fingerprint(self.root).digest
        (self.root / "new_file.py").write_text("value = 2\n", encoding="utf-8")
        second = compute_worktree_fingerprint(self.root).digest
        self.assertNotEqual(first, second)

    def test_fingerprint_is_deterministic_across_repeated_calls(self) -> None:
        (self.root / "new_file.py").write_text("value = 1\n", encoding="utf-8")
        first = compute_worktree_fingerprint(self.root).digest
        second = compute_worktree_fingerprint(self.root).digest
        self.assertEqual(first, second)

    def test_ignored_harness_directories_do_not_perturb_the_fingerprint(
        self,
    ) -> None:
        before = compute_worktree_fingerprint(self.root).digest
        (self.root / ".apoapsis").mkdir()
        (self.root / ".apoapsis" / "tasks.json").write_text("{}", encoding="utf-8")
        (self.root / ".sol").mkdir()
        (self.root / ".sol" / "audit.json").write_text("{}", encoding="utf-8")
        after = compute_worktree_fingerprint(self.root).digest
        self.assertEqual(before, after)

    def test_untracked_binary_file_is_hashed_deterministically(self) -> None:
        before = compute_worktree_fingerprint(self.root).digest
        (self.root / "asset.bin").write_bytes(b"\x00\x01\x02binary-payload")
        first = compute_worktree_fingerprint(self.root)
        self.assertNotEqual(before, first.digest)
        entry = next(
            item for item in first.untracked_files if item.path == "asset.bin"
        )
        self.assertEqual(entry.kind.value, "file")
        second = compute_worktree_fingerprint(self.root)
        self.assertEqual(first.digest, second.digest)

    def test_untracked_symlink_changes_the_fingerprint_when_retargeted(
        self,
    ) -> None:
        (self.root / "other.py").write_text("value = 99\n", encoding="utf-8")
        self._git("add", "other.py")
        self._git("commit", "-m", "second target")
        link = self.root / "link.py"
        try:
            os.symlink(self.root / "tracked.py", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not supported on this machine/platform")
        before = compute_worktree_fingerprint(self.root)
        entry = next(
            item for item in before.untracked_files if item.path == "link.py"
        )
        self.assertEqual(entry.kind.value, "symlink")
        link.unlink()
        os.symlink(self.root / "other.py", link)
        after = compute_worktree_fingerprint(self.root)
        self.assertNotEqual(before.digest, after.digest)


class InspectDiffUntrackedExposureTests(_GitRepoTestCase):
    def _inspector(self) -> RepositoryInspector:
        return RepositoryInspector(
            self.root, max_search_results=10, max_read_lines=100, max_chars=20_000
        )

    def test_inspect_diff_exposes_permitted_new_text_files(self) -> None:
        (self.root / "new_module.py").write_text(
            "def helper():\n    return True\n", encoding="utf-8"
        )
        evidence = self._inspector().diff()
        assert evidence is not None
        self.assertIn("new_module.py", evidence.content)
        self.assertIn("+def helper():", evidence.content)
        self.assertIn("+    return True", evidence.content)

    def test_untracked_binary_file_fails_closed_in_inspect_diff(self) -> None:
        (self.root / "asset.bin").write_bytes(b"\x00\x01\x02binary-payload")
        evidence = self._inspector().diff()
        assert evidence is not None
        self.assertIn("Binary files", evidence.content)
        self.assertIn("asset.bin", evidence.content)
        self.assertNotIn("binary-payload", evidence.content)

    def test_untracked_symlink_fails_closed_in_inspect_diff(self) -> None:
        link = self.root / "link.py"
        try:
            os.symlink(self.root / "tracked.py", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not supported on this machine/platform")
        evidence = self._inspector().diff()
        assert evidence is not None
        self.assertIn("link.py", evidence.content)
        self.assertIn("symlink target withheld", evidence.content)
        # the real target path text must never leak into model-visible evidence
        self.assertNotIn(str(self.root / "tracked.py"), evidence.content)

    def test_ignored_harness_directories_are_never_shown_as_untracked_evidence(
        self,
    ) -> None:
        (self.root / ".apoapsis").mkdir()
        (self.root / ".apoapsis" / "tasks.json").write_text("{}", encoding="utf-8")
        evidence = self._inspector().diff()
        self.assertIsNone(evidence)


if __name__ == "__main__":
    unittest.main()
