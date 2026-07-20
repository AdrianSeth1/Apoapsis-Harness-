from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "OPEN_APOAPSIS.cmd"

_FORBIDDEN_SNIPPETS = (
    "pip install",
    "pip3 install",
    "docker pull",
    "docker run",
    "ollama pull",
    "winget install",
    "choco install",
    "Invoke-WebRequest",
    "curl -o",
    "curl -O",
    "operator_lifecycle stop",
    "operator_lifecycle.py stop",
)


class LauncherStaticContentTests(unittest.TestCase):
    """Deterministic checks on OPEN_APOAPSIS.cmd's text -- no shell
    required. Mirrors the D5c decision (ADR 0034): the launcher is a thin
    wrapper around the existing `apoapsis ui` CLI entry point, never a new
    packaging surface, and must never install, download, or reconfigure
    anything."""

    def setUp(self) -> None:
        self.source = LAUNCHER.read_text(encoding="utf-8")

    def test_launcher_file_exists(self) -> None:
        self.assertTrue(LAUNCHER.is_file())

    def test_never_installs_or_downloads_anything(self) -> None:
        for snippet in _FORBIDDEN_SNIPPETS:
            self.assertNotIn(
                snippet,
                self.source,
                f"OPEN_APOAPSIS.cmd must never install/download/reconfigure "
                f"anything, but contains {snippet!r}",
            )

    def test_checks_python_launcher_before_anything_else(self) -> None:
        python_check_index = self.source.find("where py")
        ui_launch_index = self.source.find("apoapsis.cli.app")
        self.assertGreater(python_check_index, -1)
        self.assertGreater(ui_launch_index, -1)
        self.assertLess(
            python_check_index,
            ui_launch_index,
            "the Python launcher must be checked before the UI is started",
        )

    def test_checks_git_before_launching_the_ui(self) -> None:
        git_check_index = self.source.find("where git")
        ui_launch_index = self.source.find("apoapsis.cli.app")
        self.assertGreater(git_check_index, -1)
        self.assertGreater(ui_launch_index, -1)
        self.assertLess(git_check_index, ui_launch_index)

    def test_checks_project_initialization_before_launching_the_ui(self) -> None:
        init_check_index = self.source.find(".apoapsis\\config.toml")
        ui_launch_index = self.source.find("apoapsis.cli.app")
        self.assertGreater(init_check_index, -1)
        self.assertGreater(ui_launch_index, -1)
        self.assertLess(init_check_index, ui_launch_index)

    def test_launches_the_real_ui_entry_point(self) -> None:
        self.assertIn("apoapsis.cli.app", self.source)
        self.assertIn(" ui", self.source)

    def test_accepts_an_explicit_project_folder_without_installing_or_initializing_it(self) -> None:
        self.assertIn('set "APOAPSIS_PROJECT=%~1"', self.source)
        self.assertIn('--project-root "%APOAPSIS_PROJECT%"', self.source)
        self.assertNotIn(
            '-m apoapsis.cli.app --project-root "%APOAPSIS_PROJECT%" init',
            self.source,
        )

    def test_points_to_stop_apoapsis_for_model_memory_release(self) -> None:
        self.assertIn("STOP_APOAPSIS.cmd", self.source)

    def test_does_not_claim_to_be_a_packaged_native_application(self) -> None:
        lowered = self.source.lower()
        for claim in ("installer", "setup wizard", "native application"):
            self.assertNotIn(claim, lowered)

    def test_respects_no_pause_environment_variable_like_the_lifecycle_scripts(
        self,
    ) -> None:
        self.assertIn("APOAPSIS_NO_PAUSE", self.source)
        start_script = LAUNCHER.parent / "START_APOAPSIS.cmd"
        self.assertIn("APOAPSIS_NO_PAUSE", start_script.read_text(encoding="utf-8"))


@unittest.skipUnless(sys.platform == "win32", "OPEN_APOAPSIS.cmd is a Windows batch file")
@unittest.skipUnless(shutil.which("cmd"), "cmd.exe is not available on this machine")
class LauncherLiveGuardTests(unittest.TestCase):
    """Runs the real launcher script (copied into an isolated, uninitialized
    temp directory containing no `.apoapsis/`) to prove its fail-closed
    initialization guard actually fires, rather than only asserting it in
    the source text."""

    def setUp(self) -> None:
        import tempfile

        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        target = Path(self.tempdir.name) / "OPEN_APOAPSIS.cmd"
        shutil.copy(LAUNCHER, target)
        self.target = target
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.tempdir.name,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_reports_uninitialized_project_and_exits_nonzero(self) -> None:
        result = subprocess.run(
            ["cmd", "/c", str(self.target)],
            capture_output=True,
            text=True,
            cwd=self.tempdir.name,
            env={"APOAPSIS_NO_PAUSE": "1", **_inherited_path_env()},
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            1,
            f"expected a non-zero exit for an uninitialized project.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}",
        )
        self.assertIn("has not been initialized", result.stdout)
        self.assertIn("apoapsis init", result.stdout)


def _inherited_path_env() -> dict:
    import os

    return {"PATH": os.environ.get("PATH", ""), "SystemRoot": os.environ.get("SystemRoot", "")}


if __name__ == "__main__":
    unittest.main()
