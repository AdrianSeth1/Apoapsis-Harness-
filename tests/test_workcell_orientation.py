from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apoapsis.workcell.orientation import (
    CHARS_PER_TOKEN_ESTIMATE,
    MAX_ORIENTATION_TOKENS,
    SliceContribution,
    build_orientation_brief,
)


class OrientationBriefTests(unittest.TestCase):
    """Slice N is told what slices 1..N-1 built, instead of rediscovering it.

    CAP-4EE9F101146E4556 spent 44 of its 122 tool calls on `read_file`,
    re-reading code earlier slices had written and the harness already had
    perfect deterministic knowledge of. That cost grows with the repository,
    which is what makes it a trajectory rather than an annoyance.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def _write(self, relative: str, lines: int) -> None:
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n" * lines, encoding="utf-8")

    def _three_slice_plan(self) -> list[SliceContribution]:
        return [
            SliceContribution(
                slice_id="SLICE-001",
                title="Application skeleton",
                paths=["backend/app.py", "tests/test_app.py"],
                behaviour_names=["backend/app.py", "create_app"],
            ),
            SliceContribution(
                slice_id="SLICE-002",
                title="Storage engine",
                paths=["backend/storage/engine.py"],
                behaviour_names=["backend/storage/engine.py", "open_database"],
            ),
        ]

    def test_the_brief_states_what_earlier_slices_built(self) -> None:
        self._write("backend/app.py", 40)
        self._write("backend/storage/engine.py", 80)
        brief = build_orientation_brief(
            self.base,
            contributions=self._three_slice_plan(),
            integration_contracts=["INT-1: storage API - engine to app"],
            commands=["python -m unittest discover -s tests"],
        )
        self.assertIn("Inherited state", brief)
        self.assertIn("SLICE-001", brief)
        self.assertIn("Application skeleton", brief)
        self.assertIn("create_app", brief)
        self.assertIn("SLICE-002", brief)
        self.assertIn("open_database", brief)
        self.assertIn("INT-1: storage API", brief)
        self.assertIn("python -m unittest discover -s tests", brief)

    def test_the_tree_carries_paths_and_line_counts(self) -> None:
        self._write("backend/app.py", 40)
        brief = build_orientation_brief(self.base)
        self.assertIn("backend/app.py - 40 lines", brief)

    def test_repository_metadata_never_appears(self) -> None:
        self._write("backend/app.py", 5)
        self._write(".git/config", 3)
        (self.base / ".git").mkdir(exist_ok=True)
        self._write("__pycache__/app.cpython-312.pyc", 1)
        self._write("node_modules/left-pad/index.js", 2)
        brief = build_orientation_brief(self.base)
        for noise in (".git", "__pycache__", "node_modules"):
            self.assertNotIn(noise, brief)
        self.assertIn("backend/app.py", brief)

    def test_an_empty_base_produces_no_brief_at_all(self) -> None:
        # Slice 1 of a new project inherits nothing, and a section announcing
        # that at length is the tax this exists to cut.
        self.assertEqual(build_orientation_brief(self.base), "")

    def test_the_brief_is_bounded_however_large_the_repository(self) -> None:
        for index in range(400):
            self._write(f"pkg/module_{index:03d}.py", 30)
        brief = build_orientation_brief(
            self.base, contributions=self._three_slice_plan()
        )
        self.assertLessEqual(
            len(brief) // CHARS_PER_TOKEN_ESTIMATE, MAX_ORIENTATION_TOKENS
        )

    def test_over_the_cap_it_keeps_directories_and_this_slice_s_files(self) -> None:
        for index in range(400):
            self._write(f"pkg/module_{index:03d}.py", 30)
        self._write("backend/target.py", 12)
        brief = build_orientation_brief(
            self.base,
            focus_paths=["backend/target.py"],
            max_tokens=400,
        )
        # The directory shape survives, so the agent still knows where things
        # live...
        self.assertIn("pkg/ -", brief)
        # ...and the file it was actually told to work on is named in full,
        # because that is the one it would otherwise go looking for first.
        self.assertIn("backend/target.py", brief)
        self.assertLessEqual(len(brief) // CHARS_PER_TOKEN_ESTIMATE, 400)

    def test_the_cap_holds_when_the_directories_are_the_problem(self) -> None:
        """The summary must be smaller than what it summarises.

        Found by pointing the brief at a real project: the over-cap branch
        emitted one line per directory *unbounded*, so a tree with thousands of
        directories blew the ceiling inside the very branch that exists to
        enforce it. A wide-but-shallow repository does not catch this; a deep
        one does.
        """

        for index in range(600):
            self._write(f"pkg/part_{index:03d}/module.py", 10)
        brief = build_orientation_brief(self.base, max_tokens=500)
        self.assertLessEqual(len(brief) // CHARS_PER_TOKEN_ESTIMATE, 500)
        self.assertIn("more director(ies) not listed", brief)

    def test_harness_state_is_not_described_as_inherited_work(self) -> None:
        # `.apoapsis` is the harness's own audit tree, not the product's code.
        # It is deliberately visible to admission -- writing into it is a
        # violation that must be caught -- but calling it inherited work would
        # be false, and in a real project it is thousands of files.
        self._write("backend/app.py", 5)
        self._write(".apoapsis/tasks/TASK-1/report.json", 200)
        brief = build_orientation_brief(self.base)
        self.assertNotIn(".apoapsis", brief)
        self.assertIn("backend/app.py", brief)

    def test_exploration_is_made_unnecessary_not_forbidden(self) -> None:
        self._write("backend/app.py", 5)
        brief = build_orientation_brief(self.base)
        lowered = brief.lower()
        for prohibition in ("do not read", "must not read", "you may not"):
            self.assertNotIn(prohibition, lowered)
        self.assertIn("read files when you need their contents", lowered)

    def test_the_generated_text_is_ascii(self) -> None:
        # This crosses Windows -> WSL -> container. A completed slice has
        # already been lost once to a decode error on that path, and nothing
        # in a file listing is worth risking it again.
        self._write("backend/app.py", 5)
        brief = build_orientation_brief(
            self.base, contributions=self._three_slice_plan()
        )
        brief.encode("ascii")

    def test_the_brief_is_reproducible_from_the_same_inputs(self) -> None:
        self._write("backend/app.py", 40)
        self._write("backend/storage/engine.py", 80)
        first = build_orientation_brief(
            self.base, contributions=self._three_slice_plan()
        )
        second = build_orientation_brief(
            self.base, contributions=self._three_slice_plan()
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
