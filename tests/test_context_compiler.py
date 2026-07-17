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

    def test_frequent_plural_keeps_its_searchable_stem_with_tight_budget(self) -> None:
        compiler = ContextCompiler(ContextCompilerConfig(max_search_terms=2))
        terms = compiler._query_terms(
            "downloads configuration downloads compatibility"
        )
        self.assertEqual(terms, ["downloads", "download"])

    def test_plural_stem_is_not_evicted_at_the_search_budget_boundary(self) -> None:
        compiler = ContextCompiler(ContextCompilerConfig(max_search_terms=12))
        terms = compiler._query_terms(
            "dependencies runtime range compatibility configuration "
            "functionality maintaining downloads implement resumable avoiding"
        )
        self.assertIn("downloads", terms)
        self.assertIn("download", terms)
        self.assertLessEqual(len(terms), 12)

    def test_import_neighbors_follow_package_reexports(self) -> None:
        package = self.root / "src" / "download_service"
        package.mkdir()
        (package / "__init__.py").write_text(
            "from .worker import Worker\n", encoding="utf-8"
        )
        (package / "worker.py").write_text(
            "class Worker:\n    pass\n", encoding="utf-8"
        )
        (self.root / "tests" / "test_download_service.py").write_text(
            "from download_service import Worker\n\n"
            "def test_download_behavior():\n    assert Worker()\n",
            encoding="utf-8",
        )
        compiler = ContextCompiler(
            ContextCompilerConfig(max_files=20, max_import_depth=2)
        )

        context = compiler.compile(make_specification(), self.root)

        paths = {item.path for item in context.evidence}
        self.assertIn("src/download_service/__init__.py", paths)
        self.assertIn("src/download_service/worker.py", paths)


if __name__ == "__main__":
    unittest.main()
