from __future__ import annotations

import http.server
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from unittest import mock

from apoapsis.doctor import DoctorCheckStatus, run_doctor
from apoapsis.models.provider import ProviderError
from apoapsis.models.telemetry import InstrumentedModelProvider
from tests.fakes import FakeModelProvider
from tests.test_docker_backend import _DIGEST, _FakeDockerProcess


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


class DoctorHostedPricingTests(unittest.TestCase):
    """Deterministic coverage for the D5b readiness check (ADR 0030):
    a hosted (`openai_compatible`) model left at its all-zero pricing
    default silently makes every recorded cost -- and therefore any hosted
    spend ceiling checked against it -- read $0 regardless of real usage."""

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

    def test_zero_pricing_on_a_hosted_model_is_a_warning(self) -> None:
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
api_key_env = "APOAPSIS_DOCTOR_PRICING_TEST_KEY"

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
        report = run_doctor(self.root)
        check = _check(report, "hosted_pricing:frontier_coder")
        self.assertEqual(check.status, DoctorCheckStatus.WARNING)
        self.assertIn("all pricing fields at 0", check.detail)

    def test_nonzero_pricing_on_a_hosted_model_has_no_warning(self) -> None:
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
api_key_env = "APOAPSIS_DOCTOR_PRICING_TEST_KEY"

[models.frontier_coder.pricing]
input_per_million_usd = 3.0
output_per_million_usd = 15.0

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
        report = run_doctor(self.root)
        self.assertEqual(_find(report, "hosted_pricing:"), [])

    def test_a_local_only_ollama_model_never_gets_a_pricing_warning(self) -> None:
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
        self.assertEqual(_find(report, "hosted_pricing:"), [])


class DoctorVerificationBackendTests(unittest.TestCase):
    """Deterministic coverage for `apoapsis doctor`'s sandbox diagnostics
    (ADR 0009), injected the same way `tests.test_docker_backend` injects
    Docker CLI/engine behavior: by patching `subprocess.run` at the
    `apoapsis.execution.docker_backend` module boundary, never a real
    Docker installation. Confirms doctor distinguishes every fail-closed
    state (CLI missing, engine/Desktop unreachable, image absent, image
    present at the wrong digest) from each other and from a genuine
    successful hardened self-test, matching exactly what a real `docker
    run` preflight would report."""

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

    def _write_toml(self, *, docker_executable: str = "docker") -> None:
        (self.root / ".apoapsis" / "config.toml").write_text(
            f"""
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

[verification.backend]
backend = "docker"

[verification.backend.docker]
image = "python"
image_digest = "{_DIGEST}"
docker_executable = "{docker_executable}"
""",
            encoding="utf-8",
        )

    def _patch(self, fake: _FakeDockerProcess):
        """Route only `docker ...` invocations to `fake`; every other
        `subprocess.run` call (git, ripgrep, the configured `git
        --version` verification command) still runs for real. Necessary
        because `apoapsis.execution.docker_backend.subprocess` is the same
        module object as `subprocess` everywhere else in the process --
        patching its `run` attribute is process-global, not scoped to the
        Docker backend alone."""

        real_run = subprocess.run

        def side_effect(argv, **kwargs):
            if argv and argv[0] == "docker":
                return fake(argv, **kwargs)
            return real_run(argv, **kwargs)

        return mock.patch(
            "apoapsis.execution.docker_backend.subprocess.run", side_effect=side_effect
        )

    def test_host_backend_default_is_a_single_warning(self) -> None:
        (self.root / ".apoapsis" / "config.toml").write_text(
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
""",
            encoding="utf-8",
        )
        report = run_doctor(self.root)
        check = _check(report, "verification_backend")
        self.assertEqual(check.status, DoctorCheckStatus.WARNING)
        self.assertIn("unsandboxed", check.detail)
        self.assertEqual(_find(report, "docker_"), [])

    def test_docker_cli_not_on_path_is_a_distinct_error(self) -> None:
        self._write_toml(docker_executable="apoapsis-doctor-no-such-docker")
        report = run_doctor(self.root)
        check = _check(report, "docker_sandbox")
        self.assertEqual(check.status, DoctorCheckStatus.ERROR)
        self.assertIn("was not found on PATH", check.detail)
        self.assertEqual(_find(report, "docker_self_test"), [])

    def test_engine_unreachable_is_distinct_from_cli_missing(self) -> None:
        self._write_toml()
        fake = _FakeDockerProcess(engine_ok=False)
        with self._patch(fake):
            report = run_doctor(self.root)
        check = _check(report, "docker_sandbox")
        self.assertEqual(check.status, DoctorCheckStatus.ERROR)
        self.assertIn("did not respond", check.detail)
        self.assertIn("Desktop running", check.detail)
        self.assertEqual(_find(report, "docker_self_test"), [])

    def test_image_absent_is_distinct_from_engine_unreachable(self) -> None:
        self._write_toml()
        fake = _FakeDockerProcess(image_present=False)
        with self._patch(fake):
            report = run_doctor(self.root)
        check = _check(report, "docker_sandbox")
        self.assertEqual(check.status, DoctorCheckStatus.ERROR)
        self.assertIn("is not present locally", check.detail)
        self.assertIn("docker pull", check.detail)
        # doctor's preflight never pulls an image itself.
        self.assertFalse(any(call[1] == "pull" for call in fake.calls))
        self.assertEqual(_find(report, "docker_self_test"), [])

    def test_digest_mismatch_is_distinct_from_image_absent(self) -> None:
        self._write_toml()
        other_digest = "sha256:" + "c" * 64
        fake = _FakeDockerProcess(
            image_present=False,
            locally_present_digests=[f"python@{other_digest}"],
        )
        with self._patch(fake):
            report = run_doctor(self.root)
        check = _check(report, "docker_sandbox")
        self.assertEqual(check.status, DoctorCheckStatus.ERROR)
        self.assertIn("does not match any locally present digest", check.detail)
        self.assertIn(other_digest, check.detail)
        self.assertNotIn("is not present locally", check.detail)
        self.assertEqual(_find(report, "docker_self_test"), [])

    def test_successful_hardened_execution_reports_ok_and_sandboxed(self) -> None:
        self._write_toml()
        fake = _FakeDockerProcess()
        with self._patch(fake):
            report = run_doctor(self.root)
        sandbox_check = _check(report, "docker_sandbox")
        self.assertEqual(sandbox_check.status, DoctorCheckStatus.OK)
        self.assertIn("python@" + _DIGEST, sandbox_check.detail)
        self_test_check = _check(report, "docker_self_test")
        self.assertEqual(self_test_check.status, DoctorCheckStatus.OK)
        run_calls = [call for call in fake.calls if call[1] == "run"]
        self.assertEqual(len(run_calls), 1)
        # the self-test container must carry the same hardening flags as a
        # real verification run -- never a relaxed "just check it works" path.
        self.assertIn("--network", run_calls[0])
        self.assertIn("none", run_calls[0])
        self.assertIn("--read-only", run_calls[0])
        self.assertIn("--pull=never", run_calls[0])


if __name__ == "__main__":
    unittest.main()
