"""Authority must be read from Git objects, never from the working tree.

The 7P.2 lock named an evaluator commit that did not contain `pilot.py`, and
every test passed anyway because every test imported the module from the
checkout. These tests are written so that could not happen again: each one
builds a real repository, commits known bytes, and then asks about a commit
that deliberately lacks them while the working tree has them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from apoapsis.qualification.authority import (
    REQUIRED_AUTHORITY_MODULES,
    AuthorityError,
    AuthorityRejection,
    BoundModule,
    bind_modules,
    blob_at,
    commit_exists,
    digest_at,
    package_authority_modules_unchanged,
    verify_authority,
)

REPO = Path(__file__).resolve().parents[1]


class _GitFixture(unittest.TestCase):
    """A real repository, because the subject under test is Git objects."""

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not available on this host")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        self._git("init", "-q", "-b", "main")

    def _git(self, *argv: str) -> str:
        return subprocess.run(  # noqa: S603
            ["git", *argv],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        ).stdout.strip()

    def _commit(self, files: dict[str, str], message: str) -> str:
        for relative, body in files.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-qm", message)
        return self._git("rev-parse", "HEAD")


class WorkingTreeCannotSatisfyAuthorityTests(_GitFixture):
    def test_a_file_present_only_in_the_working_tree_is_not_at_the_commit(
        self,
    ) -> None:
        """The exact 7P.2 defect, reproduced and refused."""

        earlier = self._commit({"README.md": "first\n"}, "earlier")
        # The module now exists on disk, and is committed *later*.
        later = self._commit(
            {"src/apoapsis/qualification/pilot.py": "SCHEMA = 1\n"}, "later"
        )

        # The working tree has it...
        self.assertTrue(
            (self.repo / "src/apoapsis/qualification/pilot.py").is_file()
        )
        # ...and the earlier commit does not, which is the only thing that
        # matters when that earlier commit is named as the authority.
        self.assertIsNone(
            blob_at(earlier, "src/apoapsis/qualification/pilot.py", repo=self.repo)
        )
        self.assertIsNotNone(
            blob_at(later, "src/apoapsis/qualification/pilot.py", repo=self.repo)
        )

    def test_verify_refuses_an_authority_missing_pilot_py(self) -> None:
        earlier = self._commit({"README.md": "first\n"}, "earlier")
        self._commit({"src/apoapsis/qualification/pilot.py": "SCHEMA = 1\n"}, "later")

        declared = (
            BoundModule(path="src/apoapsis/qualification/pilot.py", sha256="0" * 64),
        )
        result = verify_authority(
            earlier, declared, repo=self.repo, required=("src/apoapsis/qualification/pilot.py",)
        )
        self.assertFalse(result.satisfied)
        self.assertEqual(
            {item.rejection for item in result.findings},
            {AuthorityRejection.MODULE_MISSING_AT_COMMIT},
        )
        with self.assertRaises(AuthorityError):
            result.require()

    def test_a_changed_byte_invalidates_the_binding(self) -> None:
        first = self._commit({"runner.py": "VERSION = 1\n"}, "first")
        bound = bind_modules(first, ("runner.py",), repo=self.repo)
        self.assertTrue(verify_authority(first, bound, repo=self.repo, required=()).satisfied)

        second = self._commit({"runner.py": "VERSION = 2\n"}, "second")
        result = verify_authority(second, bound, repo=self.repo, required=())
        self.assertFalse(result.satisfied)
        self.assertEqual(
            result.findings[0].rejection, AuthorityRejection.BLOB_DIGEST_MISMATCH
        )

    def test_a_missing_required_module_is_a_finding_even_if_undeclared(self) -> None:
        commit = self._commit({"runner.py": "VERSION = 1\n"}, "only runner")
        bound = bind_modules(commit, ("runner.py",), repo=self.repo)
        result = verify_authority(
            commit,
            bound,
            repo=self.repo,
            required=("runner.py", "src/apoapsis/qualification/pilot.py"),
        )
        self.assertFalse(result.satisfied)
        self.assertIn(
            "src/apoapsis/qualification/pilot.py",
            {item.path for item in result.findings},
        )

    def test_a_nonexistent_commit_is_refused(self) -> None:
        self._commit({"a.txt": "x\n"}, "one")
        result = verify_authority("0" * 40, (), repo=self.repo, required=())
        self.assertFalse(result.satisfied)
        self.assertEqual(
            result.findings[0].rejection, AuthorityRejection.COMMIT_MISSING
        )

    def test_binding_a_module_absent_at_the_commit_raises(self) -> None:
        commit = self._commit({"a.txt": "x\n"}, "one")
        with self.assertRaises(AuthorityError) as caught:
            bind_modules(commit, ("missing.py",), repo=self.repo)
        self.assertIs(
            caught.exception.rejection, AuthorityRejection.MODULE_MISSING_AT_COMMIT
        )


class PackageEvidenceReuseTests(_GitFixture):
    """The eight real proofs may be reused only if their code has not moved."""

    def test_identical_blobs_permit_reuse(self) -> None:
        first = self._commit(
            {"case_package.py": "RULE = 1\n", "unrelated.md": "a\n"}, "first"
        )
        second = self._commit({"unrelated.md": "b\n"}, "unrelated change")
        unchanged, changed = package_authority_modules_unchanged(
            first, second, ("case_package.py",), repo=self.repo
        )
        self.assertTrue(unchanged)
        self.assertEqual(changed, ())

    def test_a_changed_authority_module_forces_requalification(self) -> None:
        first = self._commit({"case_package.py": "RULE = 1\n"}, "first")
        second = self._commit({"case_package.py": "RULE = 2\n"}, "rule changed")
        unchanged, changed = package_authority_modules_unchanged(
            first, second, ("case_package.py",), repo=self.repo
        )
        self.assertFalse(unchanged)
        self.assertEqual(changed, ("case_package.py",))


class RealRepositoryAuthorityTests(unittest.TestCase):
    """The same checks against this repository, where the defect was found."""

    def setUp(self) -> None:
        if not (REPO / ".git").exists() or shutil.which("git") is None:
            self.skipTest("not a git checkout")

    def test_22cd8af_cannot_satisfy_pilot_authority(self) -> None:
        """The historical evaluator commit, refused for the recorded reason."""

        if not commit_exists("22cd8af", repo=REPO):
            self.skipTest("historical commit not present in this clone")
        self.assertIsNone(
            digest_at("22cd8af", "src/apoapsis/qualification/pilot.py", repo=REPO),
            "22cd8af does not contain pilot.py; if this now passes the history "
            "has been rewritten",
        )
        result = verify_authority(
            "22cd8af",
            (),
            repo=REPO,
            required=("src/apoapsis/qualification/pilot.py",),
        )
        self.assertFalse(result.satisfied)

    def test_the_required_module_list_names_pilot_and_the_runner(self) -> None:
        self.assertIn("src/apoapsis/qualification/pilot.py", REQUIRED_AUTHORITY_MODULES)
        self.assertIn(
            "src/apoapsis/qualification/rehearsal.py", REQUIRED_AUTHORITY_MODULES
        )
        self.assertIn(
            "src/apoapsis/qualification/fake_pilot_provider.py",
            REQUIRED_AUTHORITY_MODULES,
        )


if __name__ == "__main__":
    unittest.main()
