"""Slice 7P.1a: a digest is evidence only when bytes on disk produce it.

Almost every test here asserts a *refusal*. That is the point: the defect in
draft manifest `cfe7df7` was not a wrong value, it was that a name and a
measurement are both 64 hex characters and nothing could tell them apart.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from apoapsis.qualification.artifacts import (
    ArtifactKind,
    ArtifactRejection,
    ArtifactResolutionError,
    DeclaredArtifact,
    assert_no_label_derived_digests,
    is_label_derived,
    label_derived_digests,
    resolve_artifact,
    sha256_file,
)


class ArtifactResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "package"
        self.root.mkdir()

    def _write(self, name: str, content: bytes = b"task text\n") -> str:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    def _declared(self, name: str, digest: str, **overrides) -> DeclaredArtifact:
        values = {
            "relative_path": name,
            "kind": ArtifactKind.TASK_TEXT,
            "sha256": digest,
            "purpose": "the immutable task text shown to both arms",
        }
        values.update(overrides)
        return DeclaredArtifact(**values)

    def _resolve(self, declared, **kwargs):
        return resolve_artifact(declared, package_root=self.root, **kwargs)

    def _expect(self, declared, rejection, **kwargs):
        with self.assertRaises(ArtifactResolutionError) as caught:
            self._resolve(declared, **kwargs)
        self.assertIs(caught.exception.rejection, rejection)
        return caught.exception

    # -- the happy path, which must actually read bytes -------------------

    def test_a_real_artifact_resolves_with_its_observed_size(self) -> None:
        digest = self._write("task.md", b"build the incident dashboard\n")
        resolved = self._resolve(self._declared("task.md", digest))
        self.assertEqual(resolved.sha256, digest)
        self.assertEqual(resolved.size_bytes, 29)
        self.assertTrue(Path(resolved.absolute_path).is_file())

    def test_large_files_hash_in_chunks_without_error(self) -> None:
        content = os.urandom(3 * (1 << 20))
        digest = self._write("seed.tar", content)
        resolved = self._resolve(
            self._declared("seed.tar", digest, kind=ArtifactKind.SEED_TREE)
        )
        self.assertEqual(resolved.size_bytes, len(content))
        self.assertEqual(resolved.sha256, sha256_file(self.root / "seed.tar"))

    # -- the refusals -----------------------------------------------------

    def test_a_missing_artifact_is_refused_however_valid_the_digest(self) -> None:
        """The defect, stated directly.

        The digest below is a genuine SHA-256. It refers to nothing, and a
        well-formed string is not evidence that an artifact exists.
        """

        digest = hashlib.sha256(b"slice7::crisis-atlas::task").hexdigest()
        error = self._expect(
            self._declared("task.md", digest), ArtifactRejection.MISSING
        )
        self.assertIn("not evidence", str(error))

    def test_a_digest_mismatch_is_refused(self) -> None:
        self._write("task.md", b"original\n")
        wrong = hashlib.sha256(b"something else").hexdigest()
        error = self._expect(
            self._declared("task.md", wrong), ArtifactRejection.DIGEST_MISMATCH
        )
        self.assertIn("hashes to", str(error))

    def test_mutation_after_declaration_is_caught(self) -> None:
        """A package that validated once must not stay valid once edited."""

        digest = self._write("task.md", b"original\n")
        self._resolve(self._declared("task.md", digest))
        (self.root / "task.md").write_bytes(b"quietly edited\n")
        self._expect(
            self._declared("task.md", digest), ArtifactRejection.DIGEST_MISMATCH
        )

    def test_a_directory_is_not_a_regular_file(self) -> None:
        (self.root / "assets").mkdir()
        digest = hashlib.sha256(b"").hexdigest()
        self._expect(
            self._declared("assets", digest), ArtifactRejection.NOT_A_REGULAR_FILE
        )

    def test_parent_traversal_is_refused_before_touching_the_disk(self) -> None:
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_bytes(b"evaluator oracle\n")
        digest = hashlib.sha256(b"evaluator oracle\n").hexdigest()
        self._expect(
            self._declared("../outside.txt", digest),
            ArtifactRejection.OUTSIDE_PACKAGE_ROOT,
        )

    def test_an_absolute_path_is_refused(self) -> None:
        target = Path(self.temporary.name) / "abs.txt"
        target.write_bytes(b"x\n")
        self._expect(
            self._declared(str(target), hashlib.sha256(b"x\n").hexdigest()),
            ArtifactRejection.OUTSIDE_PACKAGE_ROOT,
        )

    def test_a_symlink_escaping_the_package_is_refused(self) -> None:
        """`exists()` and `is_file()` both pass for this. Neither is enough."""

        outside = Path(self.temporary.name) / "secret.txt"
        outside.write_bytes(b"reference implementation\n")
        link = self.root / "task.md"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks not supported in this environment")
        digest = hashlib.sha256(b"reference implementation\n").hexdigest()
        self.assertTrue(link.exists() and link.is_file())
        error = self._expect(
            self._declared("task.md", digest), ArtifactRejection.SYMLINK_ESCAPE
        )
        self.assertIn("outside the package root", str(error))

    def test_a_symlink_inside_the_package_still_resolves(self) -> None:
        digest = self._write("real.md", b"inside\n")
        link = self.root / "alias.md"
        try:
            link.symlink_to(self.root / "real.md")
        except OSError:
            self.skipTest("symlinks not supported in this environment")
        self.assertEqual(self._resolve(self._declared("alias.md", digest)).sha256, digest)

    def test_a_kind_mismatch_is_refused(self) -> None:
        """Containment, not pedantry: both are UTF-8 files."""

        digest = self._write("oracle.json", b"{}\n")
        error = self._expect(
            self._declared("oracle.json", digest, kind=ArtifactKind.EVALUATOR_ONLY),
            ArtifactRejection.KIND_MISMATCH,
            expected_kind=ArtifactKind.TASK_TEXT,
        )
        self.assertIn("may reach an agent workcell", str(error))

    def test_a_matching_kind_passes(self) -> None:
        digest = self._write("task.md")
        self._resolve(
            self._declared("task.md", digest), expected_kind=ArtifactKind.TASK_TEXT
        )


class EvaluatorSideTests(unittest.TestCase):
    def test_oracle_kinds_are_marked_evaluator_only(self) -> None:
        for kind in (
            ArtifactKind.EVALUATOR_ONLY,
            ArtifactKind.EXPECTED_WITNESS,
            ArtifactKind.REFERENCE_IMPLEMENTATION,
            ArtifactKind.INCOMPLETE_CANDIDATE,
        ):
            with self.subTest(kind=kind):
                self.assertTrue(kind.evaluator_side_only)

    def test_agent_visible_kinds_are_not(self) -> None:
        for kind in (
            ArtifactKind.TASK_TEXT,
            ArtifactKind.SEED_TREE,
            ArtifactKind.PLAN_CONTRACT,
            ArtifactKind.ACCEPTANCE_CRITERIA,
            ArtifactKind.VERIFICATION_COMMANDS,
        ):
            with self.subTest(kind=kind):
                self.assertFalse(kind.evaluator_side_only)


class LabelDerivedDigestTests(unittest.TestCase):
    """The backstop for the exact scheme draft manifest cfe7df7 used."""

    CASES = ("crisis-atlas", "focus-orbit")

    def test_the_draft_scheme_is_recognised(self) -> None:
        for suffix in ("tree", "task", "ac", "cmd"):
            digest = hashlib.sha256(
                f"slice7::crisis-atlas::{suffix}".encode()
            ).hexdigest()
            with self.subTest(suffix=suffix):
                self.assertTrue(is_label_derived(digest, case_ids=self.CASES))

    def test_a_real_content_digest_is_not_flagged(self) -> None:
        self.assertFalse(
            is_label_derived(
                hashlib.sha256(b"build the incident dashboard\n").hexdigest(),
                case_ids=self.CASES,
            )
        )

    def test_a_non_digest_string_is_not_flagged(self) -> None:
        self.assertFalse(is_label_derived("not-a-digest", case_ids=self.CASES))

    def test_every_template_is_covered_for_a_case(self) -> None:
        self.assertEqual(len(label_derived_digests("crisis-atlas")), 7)

    def test_a_package_carrying_one_is_rejected_with_an_accurate_reason(self) -> None:
        declared = (
            DeclaredArtifact(
                relative_path="seed.tar",
                kind=ArtifactKind.SEED_TREE,
                sha256=hashlib.sha256(b"slice7::crisis-atlas::tree").hexdigest(),
                purpose="seed",
            ),
        )
        with self.assertRaises(ArtifactResolutionError) as caught:
            assert_no_label_derived_digests(declared, case_ids=self.CASES)
        self.assertIn("cfe7df7", str(caught.exception))
        self.assertIn("seed.tar", str(caught.exception))

    def test_clean_declarations_pass(self) -> None:
        declared = (
            DeclaredArtifact(
                relative_path="task.md",
                kind=ArtifactKind.TASK_TEXT,
                sha256=hashlib.sha256(b"real content").hexdigest(),
                purpose="task",
            ),
        )
        assert_no_label_derived_digests(declared, case_ids=self.CASES)

    def test_the_backstop_is_not_the_defence(self) -> None:
        """An unrecognised label hash still fails, because bytes are read.

        The point of the recogniser is a better error message, not the
        protection itself.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unknown = hashlib.sha256(b"some::other::label").hexdigest()
            self.assertFalse(is_label_derived(unknown, case_ids=self.CASES))
            with self.assertRaises(ArtifactResolutionError) as caught:
                resolve_artifact(
                    DeclaredArtifact(
                        relative_path="task.md",
                        kind=ArtifactKind.TASK_TEXT,
                        sha256=unknown,
                        purpose="task",
                    ),
                    package_root=root,
                )
            self.assertIs(caught.exception.rejection, ArtifactRejection.MISSING)


if __name__ == "__main__":
    unittest.main()
