from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from sol.config import ContextCompilerConfig
from sol.context.compiler import ContextCompiler
from tests.helpers import make_specification


class ContextCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "downloader.py").write_text(
            "from jobs import JobStore\n\n"
            "def download(url: str) -> bytes:\n"
            "    return b'download'\n",
            encoding="utf-8",
        )
        (self.root / "jobs.py").write_text(
            "class JobStore:\n    pass\n", encoding="utf-8"
        )
        (self.root / "tests" / "test_downloader.py").write_text(
            "from src.downloader import download\n\n"
            "def test_resume_download():\n    assert download('x')\n",
            encoding="utf-8",
        )
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "SOL Tests")
        self._git("add", ".")
        self._git("commit", "-m", "fixture")

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_context_is_reproducible_and_every_excerpt_has_provenance(self) -> None:
        compiler = ContextCompiler(
            ContextCompilerConfig(max_files=10, max_total_chars=20_000)
        )
        specification = make_specification()

        first = compiler.compile(specification, self.root)
        second = compiler.compile(specification, self.root)

        self.assertEqual(first.context_sha256, second.context_sha256)
        paths = {item.path for item in first.evidence}
        self.assertIn("src/downloader.py", paths)
        self.assertIn("tests/test_downloader.py", paths)
        self.assertIn("git", first.retrieval_tools)
        for evidence in first.evidence:
            self.assertTrue(evidence.commit)
            self.assertTrue(evidence.reason_included)
            self.assertEqual(len(evidence.content_sha256 or ""), 64)
            if not evidence.path.startswith("<"):
                self.assertIsNotNone(evidence.start_line)
                self.assertIsNotNone(evidence.end_line)


if __name__ == "__main__":
    unittest.main()

