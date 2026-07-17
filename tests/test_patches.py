from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path

from sol.config import PatchPolicyConfig
from sol.patches.apply import GitPatchApplier
from sol.patches.parser import UnifiedDiffError, UnifiedDiffParser
from sol.patches.validator import PatchPolicyValidator


def one_file_patch(path: str, *, deleted: bool = False) -> str:
    modes = "deleted file mode 100644\n" if deleted else ""
    new_header = "/dev/null" if deleted else f"b/{path}"
    return (
        f"diff --git a/{path} b/{path}\n"
        f"{modes}"
        f"--- a/{path}\n"
        f"+++ {new_header}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )


class PatchPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.parser = UnifiedDiffParser()

    def violations(self, patch: str, **overrides: object) -> set[str]:
        config = PatchPolicyConfig(**overrides)
        result = PatchPolicyValidator(config).validate(
            self.parser.parse(patch), self.root
        )
        return {item.code for item in result.violations}

    def test_detects_path_escape(self) -> None:
        self.assertIn("path_escape", self.violations(one_file_patch("../escape.py")))

    def test_detects_dependency_verification_and_deleted_test_changes(self) -> None:
        self.assertIn(
            "unexpected_dependency_change",
            self.violations(one_file_patch("pyproject.toml")),
        )
        self.assertIn(
            "verification_config_change",
            self.violations(one_file_patch(".github/workflows/tests.yml")),
        )
        self.assertIn(
            "deleted_test",
            self.violations(one_file_patch("tests/test_api.py", deleted=True)),
        )
        self.assertIn(
            "unexpected_test_change",
            self.violations(one_file_patch("tests/test_api.py")),
        )

    def test_detects_binary_and_excessive_diff(self) -> None:
        binary = (
            "diff --git a/image.bin b/image.bin\n"
            "new file mode 100644\n"
            "GIT binary patch\n"
            "literal 1\n"
            "A0000\n"
        )
        self.assertIn("binary_change", self.violations(binary))
        self.assertIn(
            "excessive_diff_size",
            self.violations(one_file_patch("src/a.py"), max_changed_lines=1),
        )

    def test_rejects_symlink_creation_that_could_escape_repository(self) -> None:
        symlink = (
            "diff --git a/link b/link\n"
            "new file mode 120000\n"
            "--- /dev/null\n"
            "+++ b/link\n"
            "@@ -0,0 +1 @@\n"
            "+../../outside\n"
        )
        self.assertIn("symlink_change", self.violations(symlink))

    def test_accepts_small_source_only_patch(self) -> None:
        result = PatchPolicyValidator().validate(
            self.parser.parse(one_file_patch("src/a.py")), self.root
        )
        self.assertTrue(result.accepted)

    def test_can_explicitly_allow_non_deleted_test_changes(self) -> None:
        result = PatchPolicyValidator(
            PatchPolicyConfig(allow_test_changes=True)
        ).validate(
            self.parser.parse(one_file_patch("tests/test_api.py")), self.root
        )
        self.assertTrue(result.accepted)

    def test_canonicalizes_unmarked_blank_context_inside_hunk(self) -> None:
        patch = (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "@@ -1,3 +1,3 @@\n"
            " first\n"
            "\n"
            "-old\n"
            "+new\n"
        )
        parsed = self.parser.parse(patch)
        self.assertIn("\n \n-old\n", parsed.raw)

    def test_canonicalizes_exact_markdown_diff_wrappers(self) -> None:
        patch = one_file_patch("src/a.py")
        self.assertEqual(self.parser.parse(f"```diff\n{patch}```").raw, patch)
        self.assertEqual(self.parser.parse(patch + "```\n").raw, patch)

    def test_rejects_trailing_prose_inside_hunk(self) -> None:
        with self.assertRaisesRegex(UnifiedDiffError, "non-diff content"):
            self.parser.parse(one_file_patch("src/a.py") + "explanation\n")

    def test_applier_rebases_only_a_uniquely_matching_hunk_header(self) -> None:
        source = self.root / "source.py"
        source.write_text("first\nold\nlast\n", encoding="utf-8")
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "add", "source.py"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        patch = (
            "diff --git a/source.py b/source.py\n"
            "--- a/source.py\n"
            "+++ b/source.py\n"
            "@@ -99,3 +99,3 @@\n"
            " first\n"
            "-old\n"
            "+new\n"
            " last\n"
        )
        applier = GitPatchApplier()
        changed = applier.apply(self.parser.parse(patch), self.root)

        self.assertEqual(changed, ["source.py"])
        self.assertEqual(source.read_text(encoding="utf-8"), "first\nnew\nlast\n")
        self.assertIn("@@ -1,3 +1,3 @@", applier.last_applied_patch or "")

    def test_applier_adds_missing_edge_context_only_after_unique_match(self) -> None:
        source = self.root / "source.py"
        source.write_text("first\nold\n\n", encoding="utf-8")
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "add", "source.py"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        patch = (
            "diff --git a/source.py b/source.py\n"
            "--- a/source.py\n"
            "+++ b/source.py\n"
            "@@ -1,1 +1,1 @@\n"
            " first\n"
            "-old\n"
            "+new\n"
        )
        applier = GitPatchApplier()

        changed = applier.apply(self.parser.parse(patch), self.root)

        self.assertEqual(changed, ["source.py"])
        self.assertEqual(source.read_text(encoding="utf-8"), "first\nnew\n\n")
        self.assertIn("@@ -1,3 +1,3 @@", applier.last_applied_patch or "")


if __name__ == "__main__":
    unittest.main()
