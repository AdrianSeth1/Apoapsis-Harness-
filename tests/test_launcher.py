from __future__ import annotations

import unittest
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "OPEN_APOAPSIS.cmd"
START_LAUNCHER = Path(__file__).resolve().parents[1] / "START_APOAPSIS.cmd"

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
    packaging surface, and must never install or download anything."""

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

    def test_prepares_the_selected_project_before_launching_the_ui(self) -> None:
        init_check_index = self.source.find("apoapsis.project_setup")
        ui_launch_index = self.source.find("apoapsis.cli.app")
        self.assertGreater(init_check_index, -1)
        self.assertGreater(ui_launch_index, -1)
        self.assertLess(init_check_index, ui_launch_index)

    def test_launches_the_real_ui_entry_point(self) -> None:
        self.assertIn("apoapsis.cli.app", self.source)
        self.assertIn(" ui", self.source)

    def test_accepts_and_prepares_an_explicit_project_folder(self) -> None:
        self.assertIn('set "APOAPSIS_PROJECT=%~1"', self.source)
        self.assertIn("apoapsis.project_setup", self.source)
        self.assertIn('--project-root "%APOAPSIS_PROJECT%"', self.source)

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
        self.assertIn("APOAPSIS_NO_PAUSE", START_LAUNCHER.read_text(encoding="utf-8"))


class StartLauncherStaticContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = START_LAUNCHER.read_text(encoding="utf-8")

    def test_start_launcher_file_exists(self) -> None:
        self.assertTrue(START_LAUNCHER.is_file())

    def test_selects_project_when_no_project_argument_is_supplied(self) -> None:
        self.assertIn("FolderBrowserDialog", self.source)
        self.assertIn("Select a project folder", self.source)
        self.assertIn("ShowNewFolderButton = $true", self.source)

    def test_starts_configured_local_service_for_selected_project(self) -> None:
        self.assertIn("apoapsis.operator_lifecycle start", self.source)
        self.assertIn('--project-root "%APOAPSIS_PROJECT%"', self.source)
        self.assertIn("APOAPSIS_LLAMA_SERVER_COMMAND", self.source)

    def test_opens_ui_after_lifecycle_start(self) -> None:
        lifecycle_index = self.source.find("apoapsis.operator_lifecycle start")
        ui_index = self.source.find("apoapsis.cli.app")
        self.assertGreater(lifecycle_index, -1)
        self.assertGreater(ui_index, -1)
        self.assertLess(lifecycle_index, ui_index)

    def test_prepares_project_before_launching(self) -> None:
        setup_index = self.source.find("apoapsis.project_setup")
        lifecycle_index = self.source.find("apoapsis.operator_lifecycle start")
        self.assertGreater(setup_index, -1)
        self.assertLess(setup_index, lifecycle_index)

    def test_does_not_install_or_download(self) -> None:
        for snippet in _FORBIDDEN_SNIPPETS:
            self.assertNotIn(snippet, self.source)


if __name__ == "__main__":
    unittest.main()
