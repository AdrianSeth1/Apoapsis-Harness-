"""Stopping a loopback model server, and refusing to when identity is unproven.

The safety property is the whole point and it is easy to lose: this must stop
the process serving *the model file the configured endpoint reports*, and must
signal nothing otherwise. Killing by port would be one line shorter and wrong.

No real server is started. `/props` and the shell tool are both injected, so
what is under test is the decision, which is where the behaviour lives.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from apoapsis.workcell.resident_server import (
    RESIDENT_SERVER_TOOL,
    StoppedServer,
    stop_resident_server,
)

REPO = Path(__file__).resolve().parents[1]
MODEL = "/home/arya/models/qwen3.6-27b/Qwen3.6-27B-Q4_K_M.gguf"
BASE = "http://127.0.0.1:8000/v1"


class IdentityIsProvenBeforeAnythingIsSignalledTests(unittest.TestCase):
    def test_an_endpoint_that_reports_no_model_file_is_left_alone(self) -> None:
        """No identity, no signal. A different OpenAI-compatible server, or a
        llama-server too old to report `model_path`, both land here."""

        def runner(*args, **kwargs):
            raise AssertionError("nothing may be signalled without identity")

        with mock.patch(
            "apoapsis.workcell.resident_server.resident_model_path",
            return_value=None,
        ):
            outcome = stop_resident_server(REPO, BASE, runner=runner)

        self.assertFalse(outcome.stopped)
        self.assertEqual(outcome.pids, [])
        self.assertIn("did not report a model file", outcome.reason)

    def test_the_reported_model_file_is_what_gets_matched(self) -> None:
        """Not the port, not the base URL, not a configured alias."""

        seen: list[list[str]] = []

        def runner(argv, **kwargs):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, f"1923\tllama-server -m {MODEL}\n", "")

        with mock.patch(
            "apoapsis.workcell.resident_server.resident_model_path",
            return_value=MODEL,
        ):
            outcome = stop_resident_server(REPO, BASE, runner=runner)

        self.assertTrue(outcome.stopped)
        self.assertEqual(outcome.pids, [1923])
        self.assertEqual(outcome.model_path, MODEL)
        self.assertIn(MODEL, seen[0])
        self.assertIn("stop", seen[0])
        # The port must not be what the tool is asked about.
        self.assertNotIn("8000", " ".join(seen[0]).replace(BASE, ""))

    def test_an_endpoint_served_from_elsewhere_reports_rather_than_lies(self) -> None:
        """The tool found no local process naming that file.

        That is the correct outcome for a server on another machine, and it
        must not be reported as a successful stop.
        """

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch(
            "apoapsis.workcell.resident_server.resident_model_path",
            return_value=MODEL,
        ):
            outcome = stop_resident_server(REPO, BASE, runner=runner)

        self.assertFalse(outcome.stopped)
        self.assertIn("served from outside this machine", outcome.reason)

    def test_a_tool_that_cannot_run_is_reported_not_raised(self) -> None:
        def runner(argv, **kwargs):
            raise OSError("wsl.exe is not installed")

        with mock.patch(
            "apoapsis.workcell.resident_server.resident_model_path",
            return_value=MODEL,
        ):
            outcome = stop_resident_server(REPO, BASE, runner=runner)

        self.assertFalse(outcome.stopped)
        self.assertIn("wsl.exe is not installed", outcome.reason)

    def test_the_shell_tool_that_does_the_matching_exists(self) -> None:
        self.assertTrue((REPO / RESIDENT_SERVER_TOOL).is_file())

    def test_the_tool_matches_on_the_model_path_never_on_a_port(self) -> None:
        """A drift guard on the shell side of the same property."""

        script = (REPO / RESIDENT_SERVER_TOOL).read_text(encoding="utf-8")
        self.assertIn("never on a port", script)
        self.assertIn("/proc/", script)


class StopLocalModelsReleasesVramTests(unittest.TestCase):
    """The behaviour the operator actually clicks."""

    def targets(self):
        from apoapsis.operator_lifecycle import OpenAICompatibleLocalTarget

        return [
            OpenAICompatibleLocalTarget(
                model="qwen3.6-27b",
                roles=("local_coder",),
                base_url="http://127.0.0.1:8000/v1",
                context_window_tokens=32_768,
            )
        ]

    def run_stop(self, *, release: bool, outcome: StoppedServer):
        from apoapsis import operator_lifecycle

        with mock.patch.object(
            operator_lifecycle, "configured_ollama_targets", return_value=[]
        ), mock.patch.object(
            operator_lifecycle,
            "configured_openai_compatible_local_targets",
            return_value=self.targets(),
        ), mock.patch.object(
            operator_lifecycle, "_request_absolute_json", return_value={}
        ), mock.patch.object(
            operator_lifecycle, "_write_last_result"
        ), mock.patch.object(
            operator_lifecycle, "_release_loopback_server", return_value=outcome
        ):
            return operator_lifecycle.stop_local_models(
                Path("."), release_loopback_servers=release
            )

    def test_a_running_server_is_stopped_by_default(self) -> None:
        """This is the fix. Clicking stop must free the VRAM."""

        result = self.run_stop(
            release=True,
            outcome=StoppedServer(
                base_url="http://127.0.0.1:8000/v1",
                model_path=MODEL,
                pids=[1923],
                command_line=f"llama-server -m {MODEL}",
                stopped=True,
            ),
        )
        endpoint = result["unmanaged_local_endpoints"][0]
        self.assertEqual(endpoint["status"], "stopped")
        self.assertIn("releasing the VRAM", result["note"])

    def test_an_unprovable_server_is_left_running_and_said_so(self) -> None:
        result = self.run_stop(
            release=True,
            outcome=StoppedServer(
                base_url="http://127.0.0.1:8000/v1",
                reason="the endpoint did not report a model file",
            ),
        )
        endpoint = result["unmanaged_local_endpoints"][0]
        self.assertEqual(endpoint["status"], "running_not_stopped")
        self.assertIn("could not prove", result["note"])
        # The reason travels with the record, so the note never has to guess.
        self.assertIn("did not report a model file", endpoint["detail"]["reason"])

    def test_opting_out_leaves_it_alone_and_warns_about_the_vram(self) -> None:
        result = self.run_stop(
            release=False,
            outcome=StoppedServer(base_url="unused"),
        )
        endpoint = result["unmanaged_local_endpoints"][0]
        self.assertEqual(endpoint["status"], "running_left_alone_by_request")
        self.assertIn("still occupy VRAM", result["note"])

    def test_the_flag_defaults_to_releasing(self) -> None:
        from apoapsis.operator_lifecycle import build_parser

        parser = build_parser()
        self.assertFalse(parser.parse_args(["stop"]).keep_loopback_servers)
        self.assertTrue(
            parser.parse_args(["stop", "--keep-loopback-servers"]).keep_loopback_servers
        )


class HostMemoryIsExplainedNotSilentlyLeftTests(unittest.TestCase):
    """VRAM returns instantly; host RAM does not, and that needs saying.

    An operator who frees 17 GB of VRAM and sees Task Manager barely move
    concludes the stop did nothing. Apoapsis cannot reclaim that memory --
    dropping caches needs root inside the distribution, and `wsl --shutdown`
    would stop Docker Desktop's backend -- so the requirement is to be precise
    about what is held and hand over the one-line remedy.
    """

    FREE_OUTPUT = (
        "               total        used        free      shared  buff/cache   available\n"
        "Mem:           60265        1702       57188          18        1949        58553\n"
        "Swap:          32768           0       32768\n"
    )

    def report(self, wslconfig: str | None):
        from apoapsis.workcell import resident_server

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, self.FREE_OUTPUT, "")

        home = Path(__file__).resolve().parent / "_nonexistent_home"
        with mock.patch.object(resident_server.os, "name", "nt"), mock.patch.object(
            resident_server.Path, "home", return_value=home
        ), mock.patch.object(
            resident_server.Path, "is_file", return_value=wslconfig is not None
        ), mock.patch.object(
            resident_server.Path, "read_text", return_value=wslconfig or ""
        ):
            return resident_server.host_memory_report(runner=runner)

    def test_the_page_cache_from_loading_the_model_is_reported(self) -> None:
        """~1.9 GB of 'used' memory is clean page cache from reading a 16.8 GB
        GGUF. Reporting only 'used' would make it look like a leak."""

        report = self.report("[wsl2]\nautoMemoryReclaim=gradual\n")
        self.assertEqual(report.distribution_used_mb, 1702)
        self.assertEqual(report.distribution_cache_mb, 1949)

    def test_a_configured_reclaim_needs_no_remedy(self) -> None:
        report = self.report("[wsl2]\nautoMemoryReclaim=gradual\n")
        self.assertEqual(report.auto_memory_reclaim, "gradual")
        self.assertEqual(report.remedy, "")

    def test_the_missing_setting_is_named_with_its_one_line_fix(self) -> None:
        report = self.report("[wsl2]\nmemory=60GB\n")
        self.assertIsNone(report.auto_memory_reclaim)
        self.assertIn("autoMemoryReclaim=gradual", report.remedy)
        self.assertIn(".wslconfig", report.remedy)
        # And it must say why Apoapsis is not doing the destructive thing
        # itself, or the obvious next question goes unanswered.
        self.assertIn("wsl --shutdown", report.remedy)
        self.assertIn("Docker", report.remedy)

    def test_no_wslconfig_at_all_still_yields_the_remedy(self) -> None:
        self.assertIn("autoMemoryReclaim", self.report(None).remedy)

    def test_a_diagnostic_failure_never_breaks_the_shutdown(self) -> None:
        from apoapsis import operator_lifecycle

        with mock.patch(
            "apoapsis.workcell.resident_server.host_memory_report",
            side_effect=RuntimeError("wsl.exe vanished"),
        ):
            self.assertIsNone(operator_lifecycle._host_memory_report())


if __name__ == "__main__":
    unittest.main()
