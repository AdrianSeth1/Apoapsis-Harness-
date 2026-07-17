from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from sol.cli.app import main
from sol.config import SolConfig


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
        config = SolConfig.from_toml(self.root / ".sol" / "config.toml")
        self.assertEqual(config.models.frontier.provider, "ollama")
        self.assertEqual(config.models.frontier.model, "qwen3-coder:30b")
        self.assertEqual(config.models.frontier.context_window_tokens, 16384)
        self.assertEqual(config.models.local_research.provider, "ollama")
        self.assertEqual(config.models.local_research.model, "qwen3.6:27b")
        self.assertFalse(config.research.sources.reddit.enabled)
        self.assertNotIn("-t", config.verification.commands[0].argv)

        task = self.invoke(
            "task",
            "Add resumable downloads",
            "--constraint",
            "Preserve the current public API.",
            "--acceptance",
            "Downloads continue after reconnecting.",
            "--research",
            "full",
        )
        task_id = str(task["task_id"])
        self.assertEqual(task["state"], "SPEC_DRAFTED")
        verbatim = task["specification"]["hard_constraints"][0][
            "verbatim_source"
        ]
        self.assertEqual(verbatim, "Preserve the current public API.")

        inspected = self.invoke("inspect", task_id)
        self.assertEqual(len(inspected["events"]), 2)
        self.assertEqual(
            inspected["events"][-1]["payload"]["requested_research_mode"],
            "FULL",
        )

        cache = self.invoke("research", "cache", "inspect")
        self.assertEqual(cache["entries"], [])

        approved = self.invoke("approve", task_id, "--version", "2")
        self.assertEqual(approved["state"], "SPEC_APPROVED")
        self.assertEqual(approved["version"], 3)


if __name__ == "__main__":
    unittest.main()
