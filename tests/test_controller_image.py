"""MH-9's other half: warm the controller image before a slice waits on it.

The image is tagged by harness commit and built `--no-cache`, so every commit
to Apoapsis invalidates it and the next slice pays a full build inside its own
critical path. Observed on the owner's machine while implementing this:
twenty-four `apoapsis-product-controller` tags, one per commit, 424 MB each --
twenty-four slices that waited, and about ten gigabytes.

Nothing here runs Docker. The build is a subprocess boundary, so it is
injected; what is tested is the decision-making around it, which is where the
behaviour actually lives.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apoapsis.workcell.controller_image import (
    CONTROLLER_IMAGE_REPOSITORY,
    CONTROLLER_TAG_COMMIT_CHARS,
    ControllerImageStatus,
    controller_image_tag,
    prebuild_controller_image,
    prune_controller_images,
    stale_controller_images,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCH_SCRIPT = REPO_ROOT / "tools" / "run_capability_sandbox_task.sh"


class TagAgreesWithTheLaunchScriptTests(unittest.TestCase):
    """A drift guard, because disagreement here is silent.

    The launch script computes the tag in shell and builds when it is absent.
    If Python's idea of the tag ever differs, the warm step builds one image
    and the slice builds a second under another name -- twice the wait and
    twice the disk, with nothing failing to say so.
    """

    def test_the_repository_name_matches_the_shell(self) -> None:
        script = LAUNCH_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(f'TAG="{CONTROLLER_IMAGE_REPOSITORY}:', script)

    def test_the_commit_length_matches_the_shell(self) -> None:
        script = LAUNCH_SCRIPT.read_text(encoding="utf-8")
        # `${COMMIT:0:12}` -- the shell's substring expansion carries the
        # number this module has to agree with.
        self.assertIn(f"${{COMMIT:0:{CONTROLLER_TAG_COMMIT_CHARS}}}", script)

    def test_the_tag_is_the_repository_and_the_short_commit(self) -> None:
        commit = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(
            controller_image_tag(REPO_ROOT, commit),
            f"{CONTROLLER_IMAGE_REPOSITORY}:0123456789ab",
        )

    def test_the_launch_script_records_the_build_as_a_journal_stage(self) -> None:
        """The build happens before the controller exists, so the script
        records it -- otherwise the longest opaque wait in a run is the one
        stage the status view cannot show."""

        script = LAUNCH_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("progress.jsonl", script)
        self.assertIn('"stage": "controller_build"', script)
        # Recorded on both paths. "The image was already there" is the answer
        # to "why was this run fast", and a stage that silently never appears
        # cannot answer it.
        self.assertIn("was already built", script)


def _working_bash() -> str | None:
    """A bash that can actually run, or `None`.

    `shutil.which("bash")` is not enough on Windows: it finds the WSL shim in
    System32, which resolves to whichever distribution is default. When that is
    `docker-desktop` -- as it is on the owner's machine -- the shim exists,
    runs, and fails with `execvpe(/bin/bash)`. A test gated on the shim's
    existence would fail here for a reason that has nothing to do with what it
    asserts.
    """

    candidate = shutil.which("bash")
    if candidate is None:
        return None
    try:
        probe = subprocess.run(
            [candidate, "-c", "printf ok"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return candidate if probe.returncode == 0 and probe.stdout.strip() == "ok" else None


_BASH = _working_bash()


@unittest.skipUnless(
    _BASH,
    "no working bash on PATH (on Windows the System32 shim resolves to the "
    "default WSL distribution, which may have no /bin/bash); run this suite "
    "under WSL, Git Bash or Linux to exercise the shell journal format",
)
class ShellWritesAJournalThePythonSideCanReadTests(unittest.TestCase):
    """The format contract between two independent implementations.

    The launch script emits journal lines with `printf`; `read_progress`
    parses them with pydantic. Nothing links the two but this test. A field
    renamed on either side would otherwise show up as a status page that
    silently never displays the build stage.

    The bash under test is extracted from the real script rather than
    restated, so the two cannot drift.
    """

    def progress_event_function(self) -> str:
        source = LAUNCH_SCRIPT.read_text(encoding="utf-8")
        start = source.index("progress_event() {")
        end = source.index("\n}\n", start) + len("\n}\n")
        return source[start:end]

    def test_a_bash_written_journal_parses_and_projects(self) -> None:
        from apoapsis.reporting.run_status import StageState, project_run_status
        from apoapsis.workcell.progress import (
            PROGRESS_FILENAME,
            ProgressJournal,
            RunStage,
            read_progress,
        )

        root = Path(tempfile.mkdtemp())
        progress = root / PROGRESS_FILENAME
        script = "\n".join(
            [
                "set -euo pipefail",
                f'PROGRESS="{progress.as_posix()}"',
                self.progress_event_function(),
                'progress_event stage_entered \'{"tag": "apoapsis-product-controller:abc"}\'',
                "progress_event stage_left '"
                '{"elapsed_seconds": 34, "note": "built apoapsis-product-controller:abc"}\'',
            ]
        )
        completed = subprocess.run(
            [_BASH, "-c", script], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # The first event writes to a file that does not exist yet. That must
        # not emit a shell redirect error into the launch log.
        self.assertEqual(completed.stderr.strip(), "")

        events = read_progress(progress)
        self.assertEqual([item.sequence for item in events], [1, 2])
        self.assertEqual(events[0].stage, RunStage.CONTROLLER_BUILD)

        status = project_run_status(events)
        build = next(
            item for item in status.stages if item.stage is RunStage.CONTROLLER_BUILD
        )
        self.assertEqual(build.state, StageState.DONE)
        self.assertEqual(build.elapsed_seconds, 34.0)
        self.assertIn("built", build.detail or "")

        # And the controller, appending afterwards, must continue the sequence
        # rather than restart it and collide with the shell's events.
        ProgressJournal(progress).started(run_id="CAP-1")
        self.assertEqual([item.sequence for item in read_progress(progress)], [1, 2, 3])


class PrebuildDecisionTests(unittest.TestCase):
    def status(self, **kwargs) -> ControllerImageStatus:
        with mock.patch(
            "apoapsis.workcell.controller_image._git_head", return_value="abc123def456"
        ):
            return prebuild_controller_image(REPO_ROOT, **kwargs)

    def test_a_missing_docker_is_reported_not_raised(self) -> None:
        """An operator with no Docker still uses everything else."""

        with mock.patch(
            "apoapsis.workcell.controller_image.docker_available", return_value=False
        ):
            status = self.status()
        self.assertFalse(status.attempted)
        self.assertFalse(status.built)
        self.assertIn("Docker is not running", status.reason)
        # The reason has to tell an operator that the rest still works, or a
        # launcher message about a sandbox image reads as a broken install.
        self.assertIn("works without it", status.reason)

    def test_an_image_already_present_is_left_alone(self) -> None:
        with mock.patch(
            "apoapsis.workcell.controller_image.docker_available", return_value=True
        ), mock.patch(
            "apoapsis.workcell.controller_image.image_present", return_value=True
        ):
            status = self.status(runner=self.fail_if_called)
        self.assertTrue(status.already_present)
        self.assertFalse(status.attempted)
        self.assertFalse(status.built)

    @staticmethod
    def fail_if_called(*args, **kwargs):
        raise AssertionError("a present image must not be rebuilt")

    def test_a_missing_image_is_built(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "built", "")

        with mock.patch(
            "apoapsis.workcell.controller_image.docker_available", return_value=True
        ), mock.patch(
            "apoapsis.workcell.controller_image.image_present", return_value=False
        ), mock.patch(
            "apoapsis.workcell.controller_image._build_command",
            return_value=["bash", "build.sh", "abc123def456", "tag", "repo"],
        ):
            status = self.status(runner=runner)
        self.assertTrue(status.built)
        self.assertTrue(status.attempted)
        self.assertEqual(status.reason, "")
        self.assertEqual(len(calls), 1)

    def test_a_failed_build_reports_the_reason_and_does_not_raise(self) -> None:
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "", "no space left on device")

        with mock.patch(
            "apoapsis.workcell.controller_image.docker_available", return_value=True
        ), mock.patch(
            "apoapsis.workcell.controller_image.image_present", return_value=False
        ), mock.patch(
            "apoapsis.workcell.controller_image._build_command",
            return_value=["bash", "build.sh"],
        ):
            status = self.status(runner=runner)
        self.assertTrue(status.attempted)
        self.assertFalse(status.built)
        self.assertIn("no space left on device", status.reason)

    def test_a_build_that_hangs_times_out_rather_than_blocking_the_launcher(
        self,
    ) -> None:
        def runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1.0)

        with mock.patch(
            "apoapsis.workcell.controller_image.docker_available", return_value=True
        ), mock.patch(
            "apoapsis.workcell.controller_image.image_present", return_value=False
        ), mock.patch(
            "apoapsis.workcell.controller_image._build_command",
            return_value=["bash", "build.sh"],
        ):
            status = self.status(timeout_seconds=1.0, runner=runner)
        self.assertTrue(status.attempted)
        self.assertFalse(status.built)
        self.assertIn("did not finish", status.reason)


class StaleImageSelectionTests(unittest.TestCase):
    def images(self, names: list[str]):
        return mock.patch(
            "apoapsis.workcell.controller_image.list_controller_images",
            return_value=names,
        )

    def test_the_current_tag_is_never_stale(self) -> None:
        current = f"{CONTROLLER_IMAGE_REPOSITORY}:aaaaaaaaaaaa"
        with self.images([current, f"{CONTROLLER_IMAGE_REPOSITORY}:bbbbbbbbbbbb"]):
            self.assertNotIn(current, stale_controller_images(current, keep=0))

    def test_recent_images_are_kept_for_comparing_against_older_results(self) -> None:
        names = [f"{CONTROLLER_IMAGE_REPOSITORY}:{i:012d}" for i in range(6)]
        with self.images(names):
            stale = stale_controller_images(names[0], keep=2)
        # names[0] is current; the next two are kept; the rest are stale.
        self.assertEqual(stale, names[3:])

    def test_nothing_is_stale_when_docker_reports_nothing(self) -> None:
        with self.images([]):
            self.assertEqual(stale_controller_images("anything", keep=2), [])


class PruneRefusalTests(unittest.TestCase):
    def test_pruning_refuses_anything_outside_its_own_repository(self) -> None:
        """A caller that computed its list wrongly must not be able to delete
        an unrelated image through this function."""

        with mock.patch("apoapsis.workcell.controller_image._docker") as docker:
            outcomes = prune_controller_images(["python:3.12-slim", "ubuntu:24.04"])
        docker.assert_not_called()
        self.assertTrue(all("refused" in value for value in outcomes.values()))

    def test_an_image_still_in_use_is_reported_kept_rather_than_raised(self) -> None:
        tag = f"{CONTROLLER_IMAGE_REPOSITORY}:aaaaaaaaaaaa"
        with mock.patch(
            "apoapsis.workcell.controller_image._docker",
            return_value=subprocess.CompletedProcess([], 1, "", "image is being used"),
        ):
            outcomes = prune_controller_images([tag])
        self.assertIn("kept", outcomes[tag])
        self.assertIn("image is being used", outcomes[tag])


class LauncherIntegrationTests(unittest.TestCase):
    def test_warming_is_on_by_default_and_can_be_turned_off(self) -> None:
        from apoapsis.operator_lifecycle import build_parser

        parser = build_parser()
        default = parser.parse_args(["start"])
        self.assertFalse(default.no_prebuild_sandbox_image)
        opted_out = parser.parse_args(["start", "--no-prebuild-sandbox-image"])
        self.assertTrue(opted_out.no_prebuild_sandbox_image)

    def test_a_warming_failure_never_fails_the_launch(self) -> None:
        """The launcher's job is to start an app the operator can use."""

        from apoapsis import operator_lifecycle

        with mock.patch(
            "apoapsis.workcell.controller_image.prebuild_controller_image",
            side_effect=RuntimeError("docker exploded"),
        ):
            recorded = operator_lifecycle._warm_controller_image()
        self.assertFalse(recorded["attempted"])
        self.assertIn("docker exploded", recorded["reason"])


if __name__ == "__main__":
    unittest.main()
