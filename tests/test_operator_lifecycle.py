from __future__ import annotations

import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path

from apoapsis.operator_lifecycle import (
    ModelLifecycleError,
    configured_ollama_targets,
    start_local_models,
    stop_local_models,
)


class _FakeOllamaServer:
    def __init__(self, models: list[str]) -> None:
        self.models = models
        self.generate_payloads: list[dict[str, object]] = []

    def __enter__(self) -> str:
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/api/tags":
                    self.send_error(404)
                    return
                body = json.dumps(
                    {"models": [{"name": item, "model": item} for item in owner.models]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/api/generate":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                owner.generate_payloads.append(payload)
                body = json.dumps({"model": payload["model"], "done": True}).encode(
                    "utf-8"
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class OperatorLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def _write_config(self, base_url: str) -> None:
        config = self.root / ".apoapsis" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            f"""
[models.frontier]
provider = "ollama"
base_url = "{base_url}"
model = "coder:q4"
context_window_tokens = 32768

[models.local_coder]
provider = "ollama"
base_url = "{base_url}"
model = "coder:q4"
context_window_tokens = 65536

[models.frontier_coder]
provider = "openai_compatible"
base_url = "https://hosted.example/v1"
model = "hosted-coder"

[models.local_research]
provider = "ollama"
base_url = "{base_url}"
model = "research:27b"
context_window_tokens = 32768
""".strip()
            + "\n",
            encoding="utf-8",
        )

    def test_configuration_is_loopback_only_deduplicated_and_role_aware(self) -> None:
        self._write_config("http://127.0.0.1:11434")

        targets = configured_ollama_targets(self.root)

        self.assertEqual([item.model for item in targets], ["coder:q4", "research:27b"])
        self.assertEqual(targets[0].roles, ("frontier", "local_coder"))
        self.assertEqual(targets[0].context_window_tokens, 65536)
        self.assertTrue(targets[1].is_research_only)

    def test_start_warms_only_coding_by_default_at_configured_context(self) -> None:
        fake = _FakeOllamaServer(["coder:q4", "research:27b"])
        with fake as base_url:
            self._write_config(base_url)
            result = start_local_models(
                self.root, keep_alive="45m", launch_service=False
            )

        self.assertFalse(result["service_launched"])
        self.assertFalse(result["research_included"])
        self.assertEqual(len(result["models"]), 1)
        self.assertEqual(len(fake.generate_payloads), 1)
        self.assertEqual(fake.generate_payloads[0]["model"], "coder:q4")
        self.assertEqual(fake.generate_payloads[0]["keep_alive"], "45m")
        self.assertEqual(
            fake.generate_payloads[0]["options"], {"num_ctx": 65536}
        )

    def test_start_can_explicitly_include_research_model(self) -> None:
        fake = _FakeOllamaServer(["coder:q4", "research:27b"])
        with fake as base_url:
            self._write_config(base_url)

            start_local_models(
                self.root,
                include_research=True,
                keep_alive="15m",
                launch_service=False,
            )

        self.assertEqual(
            [item["model"] for item in fake.generate_payloads],
            ["coder:q4", "research:27b"],
        )
        self.assertTrue(all(item["keep_alive"] == "15m" for item in fake.generate_payloads))

    def test_stop_unloads_every_configured_local_model_and_not_hosted(self) -> None:
        fake = _FakeOllamaServer(["coder:q4", "research:27b"])
        with fake as base_url:
            self._write_config(base_url)

            result = stop_local_models(self.root)

        self.assertEqual(
            [item["model"] for item in fake.generate_payloads],
            ["coder:q4", "research:27b"],
        )
        self.assertTrue(all(item["keep_alive"] == 0 for item in fake.generate_payloads))
        self.assertNotIn("hosted-coder", json.dumps(result))
        self.assertFalse(result["service_stopped"])

    def test_external_ollama_endpoint_is_rejected_before_network_access(self) -> None:
        self._write_config("http://example.com:11434")

        with self.assertRaisesRegex(ModelLifecycleError, "non-loopback"):
            configured_ollama_targets(self.root)

    def test_start_rejects_invalid_keep_alive_without_network_access(self) -> None:
        self._write_config("http://127.0.0.1:11434")

        with self.assertRaisesRegex(ModelLifecycleError, "keep_alive"):
            start_local_models(self.root, keep_alive="forever")
