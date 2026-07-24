from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from apoapsis.cli.app import _init
from apoapsis.desktop.errors import ImportApprovalError, ImportSafetyError
from apoapsis.desktop.import_service import DesktopImportService
from apoapsis.desktop.project_service import DesktopProjectService
from apoapsis.desktop.registry_store import ProjectRegistryStore
from apoapsis.desktop.schema import ImportFileDisposition


def _git_init(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True, text=True
    )
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Apoapsis Tests",
            "-c",
            "user.email=tests@apoapsis.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


class DesktopImportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

        self.project_root = self.base / "project"
        self.project_root.mkdir()
        _git_init(self.project_root)
        _init(self.project_root)

        self.registry = ProjectRegistryStore(self.base / "registry.db")
        self.project_service = DesktopProjectService(self.registry)
        self.session_id = self.project_service.select_project(self.project_root)[
            "session_id"
        ]

        self.import_service = DesktopImportService(self.project_service)

        self.other_project = self.base / "other-project"
        self.other_project.mkdir()
        _git_init(self.other_project)

    def _write_source_file(self, relative_path: str, content: bytes) -> Path:
        path = self.other_project / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    # -- basic preview/approve/execute happy path --------------------------

    def test_preview_then_approve_then_execute_copies_a_new_file(self) -> None:
        source = self._write_source_file("notes.txt", b"hello world")
        preview = self.import_service.preview_import(
            self.session_id, sources=[str(source)]
        )
        self.assertEqual(preview["new_file_count"], 1)
        self.assertEqual(preview["replacement_count"], 0)
        entry = preview["entries"][0]
        self.assertEqual(entry["disposition"], ImportFileDisposition.NEW)
        self.assertEqual(
            entry["sha256"], hashlib.sha256(b"hello world").hexdigest()
        )

        self.import_service.approve_import(self.session_id, preview["preview_id"])
        manifest = self.import_service.execute_import(
            self.session_id, preview["preview_id"]
        )

        destination = self.project_root / "notes.txt"
        self.assertTrue(destination.is_file())
        self.assertEqual(destination.read_bytes(), b"hello world")
        self.assertEqual(manifest["copied_relative_paths"], ["notes.txt"])
        self.assertTrue(Path(manifest["manifest_path"]).is_file())

    def test_execute_without_approval_raises(self) -> None:
        source = self._write_source_file("a.txt", b"a")
        preview = self.import_service.preview_import(
            self.session_id, sources=[str(source)]
        )
        with self.assertRaises(ImportApprovalError):
            self.import_service.execute_import(self.session_id, preview["preview_id"])

    def test_directory_import_preserves_relative_structure(self) -> None:
        self._write_source_file("pkg/mod.py", b"print('hi')\n")
        self._write_source_file("pkg/sub/deep.py", b"x = 1\n")
        source_dir = self.other_project / "pkg"
        preview = self.import_service.preview_import(
            self.session_id, sources=[str(source_dir)]
        )
        relative_paths = {e["relative_destination_path"] for e in preview["entries"]}
        self.assertEqual(relative_paths, {"pkg/mod.py", "pkg/sub/deep.py"})

        self.import_service.approve_import(self.session_id, preview["preview_id"])
        self.import_service.execute_import(self.session_id, preview["preview_id"])
        self.assertTrue((self.project_root / "pkg" / "mod.py").is_file())
        self.assertTrue((self.project_root / "pkg" / "sub" / "deep.py").is_file())

    # -- replacement + confirmation + backup/recovery ----------------------

    def test_replacement_requires_confirmation(self) -> None:
        (self.project_root / "existing.txt").write_text("old", encoding="utf-8")
        source = self._write_source_file("existing.txt", b"new")
        preview = self.import_service.preview_import(
            self.session_id, sources=[str(source)]
        )
        self.assertEqual(preview["replacement_count"], 1)
        self.assertTrue(preview["requires_replacement_confirmation"])
        with self.assertRaises(ImportApprovalError):
            self.import_service.approve_import(
                self.session_id, preview["preview_id"], replacements_confirmed=False
            )

    def test_replacement_backs_up_original_and_overwrites(self) -> None:
        original_bytes = b"old-content"
        (self.project_root / "existing.txt").write_bytes(original_bytes)
        source = self._write_source_file("existing.txt", b"new-content")
        preview = self.import_service.preview_import(
            self.session_id, sources=[str(source)]
        )
        self.import_service.approve_import(
            self.session_id, preview["preview_id"], replacements_confirmed=True
        )
        manifest = self.import_service.execute_import(
            self.session_id, preview["preview_id"]
        )
        self.assertEqual(
            (self.project_root / "existing.txt").read_bytes(), b"new-content"
        )
        backup_path = Path(manifest["backup_paths"]["existing.txt"])
        self.assertEqual(backup_path.read_bytes(), original_bytes)

    def test_conflict_when_destination_is_a_directory(self) -> None:
        (self.project_root / "blocked").mkdir()
        source = self._write_source_file("blocked", b"data")
        preview = self.import_service.preview_import(
            self.session_id, sources=[str(source)]
        )
        self.assertEqual(preview["conflict_count"], 1)
        with self.assertRaises(ImportApprovalError):
            self.import_service.approve_import(self.session_id, preview["preview_id"])

    # -- safety exclusions --------------------------------------------------

    def test_git_directory_is_excluded(self) -> None:
        self._write_source_file(".git/HEAD", b"ref: refs/heads/main\n")
        preview = self.import_service.preview_import(
            self.session_id, sources=[str(self.other_project / ".git" / "HEAD")]
        )
        self.assertEqual(preview["entries"][0]["disposition"], ImportFileDisposition.SKIPPED_EXCLUDED)
        self.assertEqual(preview["total_files_to_copy"], 0)

    def test_apoapsis_directory_is_excluded(self) -> None:
        self._write_source_file(".apoapsis/apoapsis.db", b"fake-db")
        preview = self.import_service.preview_import(
            self.session_id,
            sources=[str(self.other_project / ".apoapsis" / "apoapsis.db")],
        )
        self.assertEqual(
            preview["entries"][0]["disposition"], ImportFileDisposition.SKIPPED_EXCLUDED
        )

    def test_secret_like_files_are_excluded(self) -> None:
        for name in (".env", "id_rsa", "credentials.json", "server.pem"):
            self._write_source_file(name, b"secret")
        source_dir = self.other_project
        preview = self.import_service.preview_import(
            self.session_id,
            sources=[str(source_dir / name) for name in (".env", "id_rsa", "credentials.json", "server.pem")],
        )
        dispositions = {e["disposition"] for e in preview["entries"]}
        self.assertEqual(dispositions, {ImportFileDisposition.SKIPPED_EXCLUDED})

    def test_dependency_and_build_directories_are_excluded(self) -> None:
        self._write_source_file("node_modules/pkg/index.js", b"module.exports = {};")
        self._write_source_file("__pycache__/mod.cpython-312.pyc", b"\x00\x01")
        source_dir = self.other_project
        preview = self.import_service.preview_import(
            self.session_id, sources=[str(source_dir)]
        )
        excluded_paths = {
            e["relative_destination_path"]
            for e in preview["entries"]
            if e["disposition"] == ImportFileDisposition.SKIPPED_EXCLUDED
        }
        self.assertTrue(
            any("node_modules" in p for p in excluded_paths)
            or not any("node_modules" in e["relative_destination_path"] for e in preview["entries"])
        )
        # Either excluded explicitly, or never enumerated at all (both are
        # acceptable: the walker is allowed to prune the directory outright).
        copied_paths = {e["relative_destination_path"] for e in preview["entries"] if e["disposition"] in (ImportFileDisposition.NEW, ImportFileDisposition.REPLACEMENT)}
        self.assertNotIn("node_modules/pkg/index.js", copied_paths)
        self.assertNotIn("__pycache__/mod.cpython-312.pyc", copied_paths)

    def test_symlink_source_is_skipped_not_followed(self) -> None:
        target = self._write_source_file("real.txt", b"real content")
        link = self.other_project / "link.txt"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks not supported in this environment")
        preview = self.import_service.preview_import(
            self.session_id, sources=[str(link)]
        )
        self.assertEqual(len(preview["entries"]), 1)
        self.assertEqual(
            preview["entries"][0]["disposition"], ImportFileDisposition.SKIPPED_SYMLINK
        )

    # -- destination containment / traversal / reserved names --------------

    def test_absolute_destination_directory_is_rejected(self) -> None:
        source = self._write_source_file("a.txt", b"a")
        with self.assertRaises(ImportSafetyError):
            self.import_service.preview_import(
                self.session_id,
                sources=[str(source)],
                destination_relative_dir="/etc",
            )

    def test_traversal_destination_directory_is_rejected(self) -> None:
        source = self._write_source_file("a.txt", b"a")
        with self.assertRaises(ImportSafetyError):
            self.import_service.preview_import(
                self.session_id,
                sources=[str(source)],
                destination_relative_dir="../escape",
            )

    def test_reserved_windows_name_destination_is_rejected(self) -> None:
        source = self._write_source_file("a.txt", b"a")
        with self.assertRaises(ImportSafetyError):
            self.import_service.preview_import(
                self.session_id, sources=[str(source)], destination_relative_dir="CON"
            )

    def test_missing_source_raises_immediately(self) -> None:
        with self.assertRaises(ImportSafetyError):
            self.import_service.preview_import(
                self.session_id, sources=[str(self.other_project / "nope.txt")]
            )

    # -- preview determinism -------------------------------------------------

    def test_preview_is_deterministic_for_identical_input(self) -> None:
        source = self._write_source_file("stable.txt", b"stable content")
        preview_one = self.import_service.preview_import(
            self.session_id, sources=[str(source)]
        )
        preview_two = self.import_service.preview_import(
            self.session_id, sources=[str(source)]
        )
        # Different preview_id (freshly generated), but identical
        # file-level facts.
        self.assertNotEqual(preview_one["preview_id"], preview_two["preview_id"])
        strip = lambda p: {k: v for k, v in p.items() if k not in ("preview_id", "created_at")}
        self.assertEqual(
            [
                {k: v for k, v in e.items() if k != "destination_path"}
                for e in preview_one["entries"]
            ],
            [
                {k: v for k, v in e.items() if k != "destination_path"}
                for e in preview_two["entries"]
            ],
        )

    # -- audit manifest -------------------------------------------------------

    def test_manifest_is_valid_json_and_records_full_decision(self) -> None:
        source = self._write_source_file("audited.txt", b"content")
        preview = self.import_service.preview_import(
            self.session_id, sources=[str(source)]
        )
        self.import_service.approve_import(self.session_id, preview["preview_id"])
        manifest = self.import_service.execute_import(
            self.session_id, preview["preview_id"]
        )
        on_disk = json.loads(Path(manifest["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["import_id"], manifest["import_id"])
        self.assertTrue(on_disk["decision"]["approved"])
        self.assertIn("audited.txt", on_disk["copied_relative_paths"])

    def test_execute_is_all_or_nothing_when_a_source_changes_after_preview(self) -> None:
        # Phase 7 coverage: "atomic import behavior." Two files are
        # previewed together; the second one's content changes before
        # execute runs. Execution must abort entirely -- the first file
        # must NOT have been promoted into the project just because it was
        # staged/validated before the second one's mismatch was detected.
        first = self._write_source_file("first.txt", b"first content")
        second = self._write_source_file("second.txt", b"second content")
        preview = self.import_service.preview_import(
            self.session_id, sources=[str(first), str(second)]
        )
        self.assertEqual(preview["new_file_count"], 2)
        self.import_service.approve_import(self.session_id, preview["preview_id"])

        # Mutate the second source file after the preview hashed it.
        second.write_bytes(b"tampered content")

        with self.assertRaises(ImportSafetyError):
            self.import_service.execute_import(self.session_id, preview["preview_id"])

        self.assertFalse(
            (self.project_root / "first.txt").exists(),
            "no file may be promoted if any file in the same import fails "
            "its re-validation -- partial imports are not acceptable",
        )
        self.assertFalse((self.project_root / "second.txt").exists())

    def test_execute_never_touches_source_files(self) -> None:
        source = self._write_source_file("source-untouched.txt", b"original")
        preview = self.import_service.preview_import(
            self.session_id, sources=[str(source)]
        )
        self.import_service.approve_import(self.session_id, preview["preview_id"])
        self.import_service.execute_import(self.session_id, preview["preview_id"])
        self.assertTrue(source.is_file(), "source file must never be deleted")
        self.assertEqual(source.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
