from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from apoapsis.cli.app import _init
from apoapsis.desktop.errors import (
    CapabilitySessionError,
    ReferenceEvidenceSafetyError,
    ReferenceProjectInvalidError,
)
from apoapsis.desktop.project_service import DesktopProjectService
from apoapsis.desktop.reference_service import DesktopReferenceService
from apoapsis.desktop.registry_store import ProjectRegistryStore


def _git_init(root: Path, *, filename: str = "README.md") -> None:
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True, text=True
    )
    (root / filename).write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=root, check=True, capture_output=True, text=True)
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


class DesktopReferenceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

        self.primary_root = self.base / "primary"
        self.primary_root.mkdir()
        _git_init(self.primary_root)
        _init(self.primary_root)

        self.reference_root = self.base / "reference"
        self.reference_root.mkdir()
        _git_init(self.reference_root, filename="OTHER.md")
        # Written as bytes, not text: `write_text` goes through the platform
        # newline translation, so on Windows this file would land as
        # `reference content\r\n` while the test below asserts the sha256 of
        # `reference content\n`. The service hashes whatever bytes are on
        # disk and is correct either way -- it was the fixture that was
        # platform-dependent, and only on the one file whose exact bytes are
        # asserted.
        (self.reference_root / "notes.txt").write_bytes(b"reference content\n")
        subprocess.run(
            ["git", "add", "notes.txt"], cwd=self.reference_root, check=True, capture_output=True
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Apoapsis Tests",
                "-c",
                "user.email=tests@apoapsis.invalid",
                "commit",
                "-m",
                "add notes",
            ],
            cwd=self.reference_root,
            check=True,
            capture_output=True,
        )

        self.registry = ProjectRegistryStore(self.base / "registry.db")
        self.project_service = DesktopProjectService(self.registry)
        self.session_id = self.project_service.select_project(self.primary_root)[
            "session_id"
        ]
        self.reference_service = DesktopReferenceService(self.project_service)

    def test_attach_reference_project_returns_git_state(self) -> None:
        record = self.reference_service.attach_reference_project(
            self.session_id, self.reference_root
        )
        self.assertEqual(record["reference_canonical_path"], str(self.reference_root.resolve()))
        self.assertTrue(record["is_clean"])
        self.assertIsNotNone(record["head_commit"])

    def test_attach_self_as_reference_is_rejected(self) -> None:
        with self.assertRaises(ReferenceProjectInvalidError):
            self.reference_service.attach_reference_project(
                self.session_id, self.primary_root
            )

    def test_attach_non_git_directory_is_rejected(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        with self.assertRaises(ReferenceProjectInvalidError):
            self.reference_service.attach_reference_project(self.session_id, plain)

    def test_attach_nested_reference_is_rejected(self) -> None:
        nested = self.primary_root / "nested-repo"
        nested.mkdir()
        _git_init(nested, filename="NESTED.md")
        with self.assertRaises(ReferenceProjectInvalidError):
            self.reference_service.attach_reference_project(self.session_id, nested)

    def test_select_reference_evidence_records_provenance_and_hash(self) -> None:
        record = self.reference_service.attach_reference_project(
            self.session_id, self.reference_root
        )
        result = self.reference_service.select_reference_evidence(
            record["reference_session_id"], ["notes.txt"]
        )
        entry = result["evidence"][0]
        self.assertEqual(entry["relative_path"], "notes.txt")
        self.assertEqual(entry["source_canonical_path"], str(self.reference_root.resolve()))
        self.assertEqual(entry["source_commit"], record["head_commit"])
        import hashlib

        expected_hash = hashlib.sha256(b"reference content\n").hexdigest()
        self.assertEqual(entry["sha256"], expected_hash)

        cached = Path(entry["cached_path"])
        self.assertTrue(cached.is_file())
        self.assertEqual(cached.read_bytes(), b"reference content\n")
        # Cached read-only copy lives under the primary project's own
        # .apoapsis/, never inside the reference project.
        self.assertTrue(str(cached).startswith(str(self.primary_root.resolve())))

    def test_select_reference_evidence_never_writes_to_reference_root(self) -> None:
        record = self.reference_service.attach_reference_project(
            self.session_id, self.reference_root
        )
        before = sorted(p.name for p in self.reference_root.iterdir())
        self.reference_service.select_reference_evidence(
            record["reference_session_id"], ["notes.txt"]
        )
        after = sorted(p.name for p in self.reference_root.iterdir())
        self.assertEqual(before, after)

    def test_select_reference_evidence_excludes_secrets_and_git(self) -> None:
        (self.reference_root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        record = self.reference_service.attach_reference_project(
            self.session_id, self.reference_root
        )
        with self.assertRaises(ReferenceEvidenceSafetyError):
            self.reference_service.select_reference_evidence(
                record["reference_session_id"], [".env"]
            )
        with self.assertRaises(ReferenceEvidenceSafetyError):
            self.reference_service.select_reference_evidence(
                record["reference_session_id"], [".git/HEAD"]
            )

    def test_select_reference_evidence_rejects_traversal(self) -> None:
        record = self.reference_service.attach_reference_project(
            self.session_id, self.reference_root
        )
        with self.assertRaises(ReferenceEvidenceSafetyError):
            self.reference_service.select_reference_evidence(
                record["reference_session_id"], ["../primary/README.md"]
            )

    def test_select_reference_evidence_rejects_directory(self) -> None:
        (self.reference_root / "subdir").mkdir()
        record = self.reference_service.attach_reference_project(
            self.session_id, self.reference_root
        )
        with self.assertRaises(ReferenceEvidenceSafetyError):
            self.reference_service.select_reference_evidence(
                record["reference_session_id"], ["subdir"]
            )

    def test_detach_revokes_session_but_keeps_captured_evidence(self) -> None:
        record = self.reference_service.attach_reference_project(
            self.session_id, self.reference_root
        )
        self.reference_service.select_reference_evidence(
            record["reference_session_id"], ["notes.txt"]
        )
        detach_result = self.reference_service.detach_reference_project(
            record["reference_session_id"]
        )
        self.assertTrue(detach_result["detached"])

        with self.assertRaises(CapabilitySessionError):
            self.reference_service.select_reference_evidence(
                record["reference_session_id"], ["notes.txt"]
            )

        listed = self.reference_service.list_reference_evidence(self.session_id)
        self.assertEqual(len(listed["evidence"]), 1)
        self.assertEqual(listed["evidence"][0]["relative_path"], "notes.txt")

    def test_list_reference_evidence_ledger_is_valid_jsonl_on_disk(self) -> None:
        record = self.reference_service.attach_reference_project(
            self.session_id, self.reference_root
        )
        self.reference_service.select_reference_evidence(
            record["reference_session_id"], ["notes.txt"]
        )
        ledger_path = (
            self.primary_root
            / ".apoapsis"
            / "reference-evidence"
            / record["reference_session_id"]
            / "evidence.jsonl"
        )
        self.assertTrue(ledger_path.is_file())
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["relative_path"], "notes.txt")

    def test_unknown_reference_session_raises(self) -> None:
        with self.assertRaises(CapabilitySessionError):
            self.reference_service.select_reference_evidence("bogus", ["notes.txt"])


if __name__ == "__main__":
    unittest.main()
