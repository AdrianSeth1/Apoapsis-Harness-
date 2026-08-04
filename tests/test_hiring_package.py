"""MH-8: the public-facing artifacts, and whether they still tell the truth.

These documents are the ones a stranger reads, and they are the ones nobody
re-checks. Every figure in them was copied from a dated evidence file at the
moment of writing; nothing stops a later edit — or a later *evidence* edit —
from leaving a number in the public README that no longer appears anywhere.

So this is a drift guard, not a style check. It asserts that each load-bearing
number in the public documents can still be found in the evaluation file it
came from. When one of these fails, the fix is to go and read the evidence
again, not to change the assertion.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EVAL = REPO / "docs" / "evaluation"

PUBLIC_README = REPO / "README.public.md"
EXPERIMENT = REPO / "docs" / "crisis-atlas-experiment.md"
DEMO_SCRIPT = REPO / "docs" / "demo-recording-script.md"
CHECKLIST = REPO / "docs" / "publication-checklist.md"

CONTROL = EVAL / "crisis-atlas-qwen-cli-control-2026-07-30.md"
TRIAL = EVAL / "crisis-atlas-64k-codex-frontier-trial-2026-07-30.md"
PILOT = EVAL / "slice-7p4-live-pilot-v4-2026-08-01.md"
PLANNING = EVAL / "apoapsis-planning-comparison-2026-07-20.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TheArtifactsExistTests(unittest.TestCase):
    def test_all_four_mh8_artifacts_are_present(self) -> None:
        for path in (PUBLIC_README, EXPERIMENT, DEMO_SCRIPT, CHECKLIST):
            with self.subTest(artifact=path.name):
                self.assertTrue(path.is_file(), f"{path.name} is missing")

    def test_the_public_readme_stays_under_three_hundred_lines(self) -> None:
        """MH-8's ceiling. A reviewer gives a repo ten minutes; a README that
        needs scrolling past 300 lines has already spent them."""

        lines = read(PUBLIC_README).splitlines()
        self.assertLessEqual(len(lines), 300, f"{len(lines)} lines")


class EveryNumberTracesToItsEvidenceTests(unittest.TestCase):
    """The claim-by-claim check.

    Each entry is (what it is, the string as the evidence file writes it, the
    file). Both the public document and the evidence must contain it, so this
    fails if either side drifts.
    """

    CLAIMS = [
        # The unrestricted control arm.
        ("self-authored suite size", "88", CONTROL),
        ("control input tokens", "2,080,801", CONTROL),
        ("bounded input tokens", "258,632", CONTROL),
        ("bounded output tokens", "55,364", CONTROL),
        ("control output tokens", "35,787", CONTROL),
        ("bounded latency", "1,467.5", CONTROL),
        ("control latency", "1,052.3", CONTROL),
        # The context ceiling, exactly.
        ("rollover prompt tokens", "64,409", CONTROL),
        ("rollover completion tokens", "1,127", CONTROL),
        ("the window", "65,536", CONTROL),
        # The independently scored rebuild.
        ("pilot input tokens", "1,166,038", PILOT),
        ("pilot output tokens", "18,039", PILOT),
        # The negative result.
        ("planning comparison model", "qwen3-coder-next", PLANNING),
    ]

    def test_each_public_figure_appears_in_its_evidence_file(self) -> None:
        public = read(PUBLIC_README) + read(EXPERIMENT)
        for label, needle, source in self.CLAIMS:
            with self.subTest(claim=label):
                self.assertIn(
                    needle, public, f"{label} is no longer stated publicly"
                )
                self.assertIn(
                    needle,
                    read(source),
                    f"{label} is claimed publicly but not in {source.name}",
                )

    def test_the_honest_qualifications_survive(self) -> None:
        """The limitations are the most credible part of the package.

        They are also the first thing that quietly disappears when a document
        gets edited for punch, so they are asserted rather than trusted.
        """

        experiment = read(EXPERIMENT)
        readme = read(PUBLIC_README)
        for text in (experiment, readme):
            self.assertIn("not held out", text)
        # The pilot evidence says this in its own words; the public documents
        # must not quietly upgrade it.
        self.assertIn("Crisis Atlas is not held out", read(PILOT))
        self.assertIn("0 of 6", experiment)
        self.assertIn("17/17", experiment)
        # Detection was proven deterministically, never live. Saying otherwise
        # would be the single most damaging overstatement available.
        self.assertIn("deterministic", experiment.lower())

    def test_the_result_that_embarrassed_the_harness_is_still_stated(self) -> None:
        """MH-8 requires the headline stated honestly, *including the control
        winning against v1*. That sentence is the reason the package is
        credible; a later edit dropping it would be a real loss."""

        readme = read(PUBLIC_README)
        experiment = read(EXPERIMENT)
        self.assertIn("beat", readme + experiment)
        for text in (readme, experiment):
            self.assertIn("false success", text)


class TheQuickstartIsRealTests(unittest.TestCase):
    def test_the_quickstart_names_test_modules_that_exist(self) -> None:
        """A copy-pasted quickstart that fails is worse than none: it is the
        first thing a reviewer runs and the last thing they run."""

        readme = read(PUBLIC_README)
        for module in (
            "tests.test_vertical_slice",
            "tests.test_capability_sandbox_product",
            "tests.test_workcell_checkpoint",
        ):
            with self.subTest(module=module):
                self.assertIn(module, readme)
                path = REPO / (module.replace(".", "/") + ".py")
                self.assertTrue(path.is_file(), f"{module} does not exist")

    def test_the_linked_deep_docs_exist(self) -> None:
        readme = read(PUBLIC_README)
        for target in (
            "docs/crisis-atlas-experiment.md",
            "HANDOFF.md",
            "README.md",
            "LICENSE.txt",
        ):
            with self.subTest(link=target):
                self.assertIn(target, readme)
                self.assertTrue((REPO / target).is_file())


class NoInternalVocabularyTests(unittest.TestCase):
    """The naming tax, asserted.

    `EXOP`, `RVOP`, `SXP`, `CAP-` and friends are meaningful inside the
    harness and meaningless to a reader. They are the clearest signal that a
    document was written for its author.
    """

    OPAQUE = ("EXOP", "RVOP", "SXP-", "INOP", "DISC-", "FPKG")

    def test_the_public_documents_use_no_opaque_identifiers(self) -> None:
        for path in (PUBLIC_README, EXPERIMENT, DEMO_SCRIPT):
            text = read(path)
            for token in self.OPAQUE:
                with self.subTest(document=path.name, token=token):
                    self.assertNotIn(token, text)

    def test_the_checklist_may_name_them_because_it_is_for_the_owner(self) -> None:
        """The publication checklist is the one document written for the person
        publishing, not the person reading, so it names the real artifact it is
        asking about."""

        self.assertIn("FPKG", read(CHECKLIST))


if __name__ == "__main__":
    unittest.main()
