from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from apoapsis.config import ContextCompilerConfig
from apoapsis.context.compiler import ContextCompiler
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
        self._git("config", "user.name", "Apoapsis Tests")
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

    def test_changed_symbol_expands_one_hop_callers_and_tests(self) -> None:
        (self.root / "src" / "cursor.py").write_text(
            "def reconcile_cursor(value: int) -> int:\n"
            "    adjusted = value\n"
            "    return adjusted\n",
            encoding="utf-8",
        )
        (self.root / "src" / "consumer.py").write_text(
            "from src.cursor import reconcile_cursor\n\n"
            "def consume(value: int) -> int:\n"
            "    return reconcile_cursor(value)\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_cursor.py").write_text(
            "from src.cursor import reconcile_cursor\n\n"
            "def test_cursor():\n"
            "    assert reconcile_cursor(1) == 1\n",
            encoding="utf-8",
        )
        (self.root / "src" / "decoy.py").write_text(
            'DESCRIPTION = "reconcile_cursor is documented here"\n',
            encoding="utf-8",
        )
        self._git("add", ".")
        self._git("commit", "-m", "add cursor fixture")
        (self.root / "src" / "cursor.py").write_text(
            "def reconcile_cursor(value: int) -> int:\n"
            "    adjusted = max(0, value)\n"
            "    return adjusted\n",
            encoding="utf-8",
        )

        context = ContextCompiler(
            ContextCompilerConfig(max_files=20, max_total_chars=40_000)
        ).compile(make_specification(), self.root)

        by_path = {item.path: item for item in context.evidence}
        self.assertIn("src/cursor.py", by_path)
        self.assertIn("src/consumer.py", by_path)
        self.assertIn("tests/test_cursor.py", by_path)
        self.assertNotIn("src/decoy.py", by_path)
        self.assertIn(
            "Python call reference to reconcile_cursor",
            by_path["src/consumer.py"].reason_included,
        )
        self.assertEqual(
            context.compiler_parameters["changed_symbol_names"],
            ["reconcile_cursor"],
        )
        self.assertGreaterEqual(
            context.compiler_parameters["symbol_reference_file_count"], 2
        )

    def test_failure_line_anchor_overrides_earlier_query_match(self) -> None:
        lines = ["download keyword appears first\n"]
        lines.extend(f"padding_{index} = {index}\n" for index in range(2, 260))
        target = self.root / "src" / "long_module.py"
        target.write_text("".join(lines), encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "add long failure fixture")
        compiler = ContextCompiler(
            ContextCompilerConfig(
                max_files=20,
                max_excerpt_lines=20,
                match_context_lines=5,
                max_total_chars=40_000,
            )
        )

        context = compiler.compile(
            make_specification(),
            self.root,
            preferred_line_anchors={"src/long_module.py": 220},
        )

        evidence = next(
            item for item in context.evidence if item.path == "src/long_module.py"
        )
        self.assertLessEqual(evidence.start_line or 0, 220)
        self.assertGreaterEqual(evidence.end_line or 0, 220)
        self.assertGreater(evidence.start_line or 0, 1)
        self.assertIn("failure location", evidence.reason_included)
        self.assertEqual(
            context.compiler_parameters["failure_line_anchor_count"], 1
        )


if __name__ == "__main__":
    unittest.main()
