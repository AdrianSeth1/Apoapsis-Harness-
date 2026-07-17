from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from sol.cli.app import main


class CLITests(unittest.TestCase):
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

    def invoke(self, *arguments: str) -> dict[str, object]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["--project-root", str(self.root), *arguments])
        return json.loads(output.getvalue())

    def test_init_task_inspect_and_approve(self) -> None:
        initialized = self.invoke("init")
        self.assertTrue(initialized["initialized"])
        self.assertTrue((self.root / ".sol" / "sol.db").is_file())

        task = self.invoke(
            "task",
            "Add resumable downloads",
            "--constraint",
            "Preserve the current public API.",
            "--acceptance",
            "Downloads continue after reconnecting.",
        )
        task_id = str(task["task_id"])
        self.assertEqual(task["state"], "SPEC_DRAFTED")
        verbatim = task["specification"]["hard_constraints"][0][
            "verbatim_source"
        ]
        self.assertEqual(verbatim, "Preserve the current public API.")

        inspected = self.invoke("inspect", task_id)
        self.assertEqual(len(inspected["events"]), 2)

        approved = self.invoke("approve", task_id, "--version", "2")
        self.assertEqual(approved["state"], "SPEC_APPROVED")
        self.assertEqual(approved["version"], 3)


if __name__ == "__main__":
    unittest.main()

