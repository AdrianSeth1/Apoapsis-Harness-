from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sol.config import PatchPolicyConfig
from sol.patches.parser import UnifiedDiffParser
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


if __name__ == "__main__":
    unittest.main()
