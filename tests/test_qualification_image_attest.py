"""Provenance must be read off the image, not transcribed beside it.

The defect these cover is specific and was mine: manifest v2's `labels` block
agreed with its own `source_commit` and disagreed with the image. A validator
comparing two fields of one document cannot catch a document that is uniformly
wrong, so these tests always put the artefact on one side of the comparison.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apoapsis.qualification.image_attest import (
    BUILD_CONTEXT_LABEL,
    SOURCE_COMMIT_LABEL,
    SOURCE_TREE_LABEL,
    AttestationRejection,
    attest_image,
    read_image_labels,
    rederive_build_context_digest,
)

REPO = Path(__file__).resolve().parents[1]


def _labels(**overrides: str) -> dict[str, str]:
    base = {
        SOURCE_COMMIT_LABEL: "a" * 40,
        SOURCE_TREE_LABEL: "b" * 40,
        BUILD_CONTEXT_LABEL: "c" * 64,
    }
    base.update(overrides)
    return base


class AttestationTests(unittest.TestCase):
    """Synthetic: the daemon is stubbed so these run on any host."""

    def _attest(self, observed, **declared):
        arguments = {
            "image_id": "sha256:" + "d" * 64,
            "declared_source_commit": "a" * 40,
            "rederive_context": False,
        }
        arguments.update(declared)
        with patch(
            "apoapsis.qualification.image_attest.read_image_labels",
            return_value=observed,
        ):
            return attest_image(**arguments)

    def test_matching_labels_attest(self) -> None:
        result = self._attest(_labels())
        self.assertTrue(result.attested, [f.detail for f in result.findings])

    def test_the_manifest_v2_defect_is_caught(self) -> None:
        """A declaration that is self-consistent and wrong about the image.

        This is the exact shape of the real defect: both manifest fields came
        from one variable, so the old field-versus-field check passed while the
        image said something else entirely.
        """

        result = self._attest(
            _labels(**{SOURCE_COMMIT_LABEL: "0" * 40}),
            declared_source_commit="a" * 40,
        )
        self.assertFalse(result.attested)
        self.assertEqual(
            {item.rejection for item in result.findings},
            {AttestationRejection.COMMIT_MISMATCH},
        )
        self.assertIn("manifest-v2 defect", result.findings[0].detail)

    def test_a_missing_image_is_not_an_unlabelled_image(self) -> None:
        result = self._attest(None)
        self.assertFalse(result.attested)
        self.assertIs(result.findings[0].rejection, AttestationRejection.IMAGE_ABSENT)

    def test_an_image_with_no_labels_is_refused_for_the_right_reason(self) -> None:
        result = self._attest({})
        self.assertFalse(result.attested)
        self.assertIs(result.findings[0].rejection, AttestationRejection.LABEL_MISSING)

    def test_a_tree_mismatch_is_reported_separately(self) -> None:
        result = self._attest(
            _labels(**{SOURCE_TREE_LABEL: "9" * 40}),
            declared_source_tree="b" * 40,
        )
        self.assertFalse(result.attested)
        self.assertIn(
            AttestationRejection.TREE_MISMATCH,
            {item.rejection for item in result.findings},
        )

    def test_a_context_mismatch_is_reported_separately(self) -> None:
        result = self._attest(
            _labels(**{BUILD_CONTEXT_LABEL: "7" * 64}),
            declared_context_sha256="c" * 64,
        )
        self.assertFalse(result.attested)
        self.assertIn(
            AttestationRejection.CONTEXT_MISMATCH,
            {item.rejection for item in result.findings},
        )


class ContextRederivationTests(unittest.TestCase):
    """A right commit label on an image built from another tree still fails."""

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        self.env = env
        subprocess.run(  # noqa: S603
            ["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True, env=env
        )

    def _commit(self, body: str) -> str:
        source = self.repo / "src"
        source.mkdir(exist_ok=True)
        (source / "module.py").write_text(body, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, env=self.env)  # noqa: S603
        subprocess.run(  # noqa: S603
            ["git", "commit", "-qm", "c"], cwd=self.repo, check=True, env=self.env
        )
        return subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_rederivation_is_stable_for_one_commit(self) -> None:
        commit = self._commit("VERSION = 1\n")
        first = rederive_build_context_digest(commit, repo=self.repo, paths=("src",))
        second = rederive_build_context_digest(commit, repo=self.repo, paths=("src",))
        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_two_commits_produce_different_context_digests(self) -> None:
        first = self._commit("VERSION = 1\n")
        second = self._commit("VERSION = 2\n")
        self.assertNotEqual(
            rederive_build_context_digest(first, repo=self.repo, paths=("src",)),
            rederive_build_context_digest(second, repo=self.repo, paths=("src",)),
        )

    def test_an_image_labelled_with_the_wrong_context_is_refused(self) -> None:
        commit = self._commit("VERSION = 1\n")
        real = rederive_build_context_digest(commit, repo=self.repo, paths=("src",))
        assert real is not None

        with patch(
            "apoapsis.qualification.image_attest.read_image_labels",
            return_value={
                SOURCE_COMMIT_LABEL: commit,
                BUILD_CONTEXT_LABEL: "e" * 64,  # built from some other tree
            },
        ), patch(
            "apoapsis.qualification.image_attest.CONTROLLER_CONTEXT_PATHS", ("src",)
        ):
            result = attest_image(
                image_id="sha256:" + "d" * 64,
                declared_source_commit=commit,
                repo=self.repo,
            )
        self.assertFalse(result.attested)
        self.assertIn(
            AttestationRejection.CONTEXT_MISMATCH,
            {item.rejection for item in result.findings},
        )


@unittest.skipUnless(shutil.which("docker"), "docker unavailable")
class LiveDaemonTests(unittest.TestCase):
    """Against the real daemon, where one exists."""

    def test_an_absent_image_reports_absent_rather_than_unlabelled(self) -> None:
        self.assertIsNone(read_image_labels("sha256:" + "f" * 64))

    def test_the_committed_manifest_v2_controller_fails_attestation(self) -> None:
        """The defect, against the real image, if it is still present.

        Recorded as a test rather than only in prose so the correction cannot
        be quietly dropped: when v3 rebuilds and rebinds, this starts failing
        and must be rewritten to assert the image now attests.
        """

        path = (
            REPO
            / "docs"
            / "qualification"
            / "slice7-crisis-atlas-pilot-manifest-v2.json"
        )
        if not path.is_file():
            self.skipTest("manifest v2 not present")
        controller = json.loads(path.read_text(encoding="utf-8"))["controller_image"]
        observed = read_image_labels(controller["image_id"])
        if observed is None:
            self.skipTest("the v2 controller image is not on this host")
        self.assertNotEqual(
            observed.get(SOURCE_COMMIT_LABEL),
            controller["source_commit"],
            "v2's controller now attests; rewrite this test to assert success",
        )


if __name__ == "__main__":
    unittest.main()
