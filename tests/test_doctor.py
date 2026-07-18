from __future__ import annotations

import http.server
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from apoapsis.doctor import DoctorCheckStatus, run_doctor
from apoapsis.models.provider import ProviderError
from apoapsis.models.telemetry import InstrumentedModelProvider
from tests.fakes import FakeModelProvider


def _make_ollama_handler(models: list[dict]) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            payload = json.dumps({"models": models}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    return Handler


class _FakeOllamaServer:
    def __init__(self, *, models: list[dict] | None = None) -> None:
        self._models = models or []

    def __enter__(self) -> str:
        handler = _make_ollama_handler(self._models)
        self.server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *exc_info: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def _check(report, name: str):
    for check in report.checks:
        if check.name == name:
            return check
    raise AssertionError(f"no check named {name!r} in {[c.name for c in report.checks]}")


def _find(report, prefix: str) -> list:
    return [check for check in report.checks if check.name.startswith(prefix)]


class DoctorToolchainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )

    def test_git_present_and_repository_detected(self) -> None:
        self._git("init", "-b", "main")
        report = run_doctor(self.root)
        self.assertEqual(_check(report, "git").status, DoctorCheckStatus.OK)
        self.assertEqual(_check(report, "git_repository").status, DoctorCheckStatus.OK)

    def test_git_repository_missing_is_an_error(self) -> None:
        report = run_doctor(self.root)
        self.assertEqual(_check(report, "git_repository").status, DoctorCheckStatus.ERROR)
        self.assertEqual(report.overall_status, DoctorCheckStatus.ERROR)

    def test_git_executable_missing_is_an_error(self) -> None:
        report = run_doctor(self.root, git_executable="apoapsis-doctor-missing-git")
        self.assertEqual(_check(report, "git").status, DoctorCheckStatus.ERROR)
        self.assertIn("not found", _check(report, "git").detail)

    def test_ripgrep_present_and_missing_branches(self) -> None:
        self._git("init", "-b", "main")
        present = run_doctor(self.root, ripgrep_executable="git")
        self.assertEqual(_check(present, "ripgrep").status, DoctorCheckStatus.OK)

        missing = run_doctor(
            self.root, ripgrep_executable="apoapsis-doctor-missing-ripgrep"
        )
        missing_check = _check(missing, "ripgrep")
        self.assertEqual(missing_check.status, DoctorCheckStatus.WARNING)
        self.assertIsNotNone(missing_check.remediation)
        # ripgrep is advisory: a missing binary must never raise overall status
        # past WARNING on its own.
        self.assertEqual(missing.overall_status, DoctorCheckStatus.WARNING)

    def test_python_check_reports_version_and_executable(self) -> None:
        self._git("init", "-b", "main")
        report = run_doctor(self.root)
        check = _check(report, "python")
        self.assertEqual(check.status, DoctorCheckStatus.OK)
        self.assertIn("3.1", check.detail)

    def test_missing_project_configuration_is_a_warning_not_a_crash(self) -> None:
        self._git("init", "-b", "main")
        report = run_doctor(self.root)
        check = _check(report, "project_configuration")
        self.assertEqual(check.status, DoctorCheckStatus.WARNING)
        self.assertIn("apoapsis init", check.remediation or "")
        # no model/credential/verification checks without a loaded config
        self.assertEqual(_find(report, "model:"), [])

    def test_invalid_project_configuration_is_an_error(self) -> None:
        self._git("init", "-b", "main")
        metadata = self.root / ".apoapsis"
        metadata.mkdir()
        (metadata / "config.toml").write_text("not = [valid", encoding="utf-8")
        report = run_doctor(self.root)
        check = _check(report, "project_configuration")
        self.assertEqual(check.status, DoctorCheckStatus.ERROR)
        self.assertIn("failed to validate", check.detail)


class DoctorConfiguredProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        (self.root / ".apoapsis").mkdir()

    def _write_toml(self, body: str) -> None:
        (self.root / ".apoapsis" / "config.toml").write_text(body, encoding="utf-8")

    def test_configured_models_and_context_are_reported(self) -> None:
        self._write_toml(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen-test"
context_window_tokens = 8192

[execution]
mode = "one_shot"

[context]
max_total_chars = 4000

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
        )
        report = run_doctor(self.root)
        frontier_check = _check(report, "model:frontier")
        self.assertEqual(frontier_check.status, DoctorCheckStatus.OK)
        self.assertIn("qwen-test", frontier_check.detail)
        self.assertEqual(
            _check(report, "model:local_coder").status, DoctorCheckStatus.SKIPPED
        )
        self.assertEqual(
            _check(report, "context_limits").status, DoctorCheckStatus.OK
        )

    def test_context_budget_larger_than_window_is_a_warning(self) -> None:
        self._write_toml(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen-test"
context_window_tokens = 2048

[execution]
mode = "one_shot"

[context]
max_total_chars = 100000

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
        )
        report = run_doctor(self.root)
        self.assertEqual(
            _check(report, "context_limits").status, DoctorCheckStatus.WARNING
        )

    def test_no_required_verification_command_is_an_error(self) -> None:
        self._write_toml(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen-test"

[execution]
mode = "one_shot"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = false
"""
        )
        report = run_doctor(self.root)
        self.assertEqual(
            _check(report, "verification_commands").status, DoctorCheckStatus.ERROR
        )

    def test_strict_with_no_acceptance_designated_command_is_a_warning(
        self,
    ) -> None:
        self._write_toml(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen-test"

[execution]
mode = "one_shot"
completion_policy = "strict"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
        )
        report = run_doctor(self.root)
        check = _check(report, "completion_policy_acceptance_commands")
        self.assertEqual(check.status, DoctorCheckStatus.WARNING)
        self.assertIn("no verification command is marked", check.detail)

    def test_strict_with_an_acceptance_designated_command_has_no_warning(
        self,
    ) -> None:
        self._write_toml(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen-test"

[execution]
mode = "one_shot"
completion_policy = "strict"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
acceptance = true
"""
        )
        report = run_doctor(self.root)
        names = [check.name for check in report.checks]
        self.assertNotIn("completion_policy_acceptance_commands", names)
        self.assertNotIn("completion_policy_baseline", names)

    def test_baseline_completion_policy_is_a_warning(self) -> None:
        self._write_toml(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen-test"

[execution]
mode = "one_shot"
completion_policy = "baseline"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
        )
        report = run_doctor(self.root)
        check = _check(report, "completion_policy_baseline")
        self.assertEqual(check.status, DoctorCheckStatus.WARNING)
        self.assertIn("completion_policy is baseline", check.detail)

    def test_verification_command_missing_binary_is_a_warning(self) -> None:
        self._write_toml(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen-test"

[execution]
mode = "one_shot"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["apoapsis-doctor-missing-tool", "-v"]
required = true
"""
        )
        report = run_doctor(self.root)
        check = _check(report, "verification_command:tests")
        self.assertEqual(check.status, DoctorCheckStatus.WARNING)
        self.assertIn("not found", check.detail)

    def test_credential_check_never_leaks_the_secret_value(self) -> None:
        self._write_toml(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen-test"

[models.frontier_coder]
provider = "openai_compatible"
base_url = "https://frontier.invalid/v1"
model = "big-coder"
api_key_env = "APOAPSIS_DOCTOR_TEST_KEY"

[execution]
mode = "agent"
route = "frontier_only"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
        )
        secret_value = "sk-super-secret-value-must-not-leak"
        os.environ.pop("APOAPSIS_DOCTOR_TEST_KEY", None)
        try:
            missing_report = run_doctor(self.root)
            missing_check = _check(missing_report, "credential:APOAPSIS_DOCTOR_TEST_KEY")
            self.assertEqual(missing_check.status, DoctorCheckStatus.ERROR)
            self.assertNotIn(secret_value, missing_check.detail)

            os.environ["APOAPSIS_DOCTOR_TEST_KEY"] = secret_value
            present_report = run_doctor(self.root)
            present_check = _check(
                present_report, "credential:APOAPSIS_DOCTOR_TEST_KEY"
            )
            self.assertEqual(present_check.status, DoctorCheckStatus.OK)
            self.assertNotIn(secret_value, present_check.detail)
            for check in present_report.checks:
                self.assertNotIn(secret_value, check.detail)
                self.assertNotIn(secret_value, check.remediation or "")
        finally:
            os.environ.pop("APOAPSIS_DOCTOR_TEST_KEY", None)

    def test_ollama_reachability_ok_and_error_branches(self) -> None:
        with _FakeOllamaServer() as base_url:
            self._write_toml(
                f"""
[models.frontier]
provider = "ollama"
base_url = "{base_url}"
model = "qwen-test"

[execution]
mode = "one_shot"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
            )
            reachable_report = run_doctor(self.root)
            reachable_checks = _find(reachable_report, "ollama_reachability:")
            self.assertEqual(len(reachable_checks), 1)
            self.assertEqual(reachable_checks[0].status, DoctorCheckStatus.OK)

        self._write_toml(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:1"
model = "qwen-test"

[execution]
mode = "one_shot"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
        )
        unreachable_report = run_doctor(self.root)
        unreachable_checks = _find(unreachable_report, "ollama_reachability:")
        self.assertEqual(len(unreachable_checks), 1)
        self.assertEqual(unreachable_checks[0].status, DoctorCheckStatus.ERROR)

    def test_context_window_within_native_support_is_ok(self) -> None:
        with _FakeOllamaServer(
            models=[
                {
                    "name": "qwen-test",
                    "model": "qwen-test",
                    "details": {"context_length": 262144},
                }
            ]
        ) as base_url:
            self._write_toml(
                f"""
[models.frontier]
provider = "ollama"
base_url = "{base_url}"
model = "qwen-test"
context_window_tokens = 131072

[execution]
mode = "one_shot"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
            )
            report = run_doctor(self.root)
        check = _check(report, "context_window_support:frontier")
        self.assertEqual(check.status, DoctorCheckStatus.OK)
        self.assertIn("131072", check.detail)
        self.assertIn("262144", check.detail)

    def test_context_window_exceeding_native_support_is_an_error(self) -> None:
        with _FakeOllamaServer(
            models=[
                {
                    "name": "qwen-test",
                    "model": "qwen-test",
                    "details": {"context_length": 65536},
                }
            ]
        ) as base_url:
            self._write_toml(
                f"""
[models.frontier]
provider = "ollama"
base_url = "{base_url}"
model = "qwen-test"
context_window_tokens = 262144

[execution]
mode = "one_shot"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
            )
            report = run_doctor(self.root)
        check = _check(report, "context_window_support:frontier")
        self.assertEqual(check.status, DoctorCheckStatus.ERROR)
        self.assertIsNotNone(check.remediation)
        self.assertEqual(report.overall_status, DoctorCheckStatus.ERROR)

    def test_context_window_check_is_skipped_when_not_configured(self) -> None:
        self._write_toml(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen-test"

[execution]
mode = "one_shot"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
        )
        report = run_doctor(self.root)
        self.assertEqual(_find(report, "context_window_support:"), [])

    def test_context_window_check_warns_when_model_is_unlisted(self) -> None:
        with _FakeOllamaServer(models=[]) as base_url:
            self._write_toml(
                f"""
[models.frontier]
provider = "ollama"
base_url = "{base_url}"
model = "qwen-test"
context_window_tokens = 65536

[execution]
mode = "one_shot"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
            )
            report = run_doctor(self.root)
        check = _check(report, "context_window_support:frontier")
        self.assertEqual(check.status, DoctorCheckStatus.WARNING)

    def test_probe_is_never_attempted_without_the_flag(self) -> None:
        self._write_toml(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen-test"

[execution]
mode = "one_shot"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
        )
        report = run_doctor(self.root, probe_providers=False)
        self.assertEqual(_find(report, "probe:"), [])

    def test_probe_success_and_failure_use_injected_fake_providers(self) -> None:
        self._write_toml(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen-test"

[execution]
mode = "one_shot"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
        )
        healthy = InstrumentedModelProvider(
            FakeModelProvider([json.dumps({"ok": True})])
        )
        report = run_doctor(
            self.root,
            probe_providers=True,
            provider_overrides={"frontier": healthy},
        )
        healthy_check = _check(report, "probe:frontier")
        self.assertEqual(healthy_check.status, DoctorCheckStatus.OK)
        # a free ollama probe never mentions cost.
        self.assertNotIn("cost", healthy_check.detail)

        failing = InstrumentedModelProvider(
            FakeModelProvider([ProviderError("model unavailable")])
        )
        failure_report = run_doctor(
            self.root,
            probe_providers=True,
            provider_overrides={"frontier": failing},
        )
        failure_check = _check(failure_report, "probe:frontier")
        self.assertEqual(failure_check.status, DoctorCheckStatus.ERROR)
        self.assertIn("model unavailable", failure_check.detail)

    def test_probe_of_hosted_provider_notes_possible_cost(self) -> None:
        self._write_toml(
            """
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen-test"

[models.frontier_coder]
provider = "openai_compatible"
base_url = "https://frontier.invalid/v1"
model = "big-coder"
api_key_env = "APOAPSIS_DOCTOR_TEST_KEY_2"

[execution]
mode = "agent"
route = "frontier_only"

[verification]
[[verification.commands]]
name = "tests"
category = "tests"
argv = ["git", "--version"]
required = true
"""
        )
        healthy = InstrumentedModelProvider(
            FakeModelProvider([json.dumps({"ok": True})])
        )
        report = run_doctor(
            self.root,
            probe_providers=True,
            provider_overrides={
                "frontier": InstrumentedModelProvider(
                    FakeModelProvider([json.dumps({"ok": True})])
                ),
                "frontier_coder": healthy,
            },
        )
        hosted_check = _check(report, "probe:frontier_coder")
        self.assertEqual(hosted_check.status, DoctorCheckStatus.OK)
        self.assertIn("cost", hosted_check.detail)


if __name__ == "__main__":
    unittest.main()
