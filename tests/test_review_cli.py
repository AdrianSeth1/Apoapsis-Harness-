from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from apoapsis.cli.app import main


class ReviewCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "a@a.com"], cwd=self.root, check=True
        )
        subprocess.run(["git", "config", "user.name", "a"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("hi\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "."], cwd=self.root, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )

    def invoke(self, *arguments: str) -> dict[str, object]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["--project-root", str(self.root), *arguments])
        return json.loads(output.getvalue())

    def test_review_list_is_empty_on_a_fresh_project(self) -> None:
        self.invoke("init")
        result = self.invoke("review", "list")
        self.assertEqual(result, {"cases": []})

    def test_review_inspect_unknown_task_errors(self) -> None:
        self.invoke("init")
        with self.assertRaises(SystemExit):
            self.invoke("review", "inspect", "TASK-DOES-NOT-EXIST")

    def test_review_abandon_requires_operation_id_and_version_flags(self) -> None:
        self.invoke("init")
        from apoapsis.cli.app import build_parser

        parsed = build_parser().parse_args(
            [
                "review",
                "abandon",
                "TASK-X",
                "--expected-version",
                "1",
                "--operation-id",
                "RVOP-1",
            ]
        )
        self.assertEqual(parsed.review_command, "abandon")
        self.assertEqual(parsed.expected_version, 1)
        self.assertEqual(parsed.operation_id, "RVOP-1")


if __name__ == "__main__":
    unittest.main()
