from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from apoapsis.cli.app import _init
from apoapsis.desktop.ipc_server import create_desktop_ipc_server


def _git_init(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True, text=True
    )
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Apoapsis Tests",
            "-c",
            "user.email=tests@apoapsis.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


class DesktopIPCServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

        self.project_root = self.base / "project"
        self.project_root.mkdir()
        _git_init(self.project_root)
        _init(self.project_root)

        self.token = "deterministic-desktop-ipc-token"
        self.server = create_desktop_ipc_server(
            self.base / "registry.db", port=0, privileged_token=self.token
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _post(
        self, route: str, body: dict, *, token: str | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.server.origin}/desktop/{route}",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Apoapsis-Desktop-Token": self.token if token is None else token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    # -- authorization ------------------------------------------------------

    def test_missing_token_is_rejected(self) -> None:
        status, payload = self._post(
            "list_recent_projects", {}, token=""
        )
        self.assertEqual(status, 401)
        self.assertIn("error", payload)

    def test_wrong_token_is_rejected(self) -> None:
        status, _payload = self._post(
            "list_recent_projects", {}, token="not-the-real-token"
        )
        self.assertEqual(status, 401)

    def test_unknown_route_is_rejected(self) -> None:
        status, _payload = self._post("delete_everything", {})
        self.assertEqual(status, 404)

    def test_health_check_requires_no_token(self) -> None:
        with urllib.request.urlopen(f"{self.server.origin}/health", timeout=5) as response:
            self.assertEqual(response.status, 200)

    # -- project registry routes --------------------------------------------

    def test_select_project_then_list_recent_projects(self) -> None:
        status, selected = self._post(
            "select_project", {"path": str(self.project_root)}
        )
        self.assertEqual(status, 200)
        self.assertIn("session_id", selected)

        status, recents = self._post("list_recent_projects", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(recents["projects"]), 1)

    def test_select_project_missing_path_is_not_found(self) -> None:
        status, payload = self._post(
            "select_project", {"path": str(self.base / "nope")}
        )
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_initialize_project_requires_valid_session(self) -> None:
        status, payload = self._post(
            "initialize_project", {"session_id": "bogus-session"}
        )
        self.assertEqual(status, 404)

    def test_missing_required_field_is_bad_request(self) -> None:
        status, payload = self._post("select_project", {})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    # -- import routes --------------------------------------------------------

    def test_preview_approve_execute_import_round_trip(self) -> None:
        _status, selected = self._post(
            "select_project", {"path": str(self.project_root)}
        )
        session_id = selected["session_id"]

        source_dir = self.base / "source"
        source_dir.mkdir()
        (source_dir / "a.txt").write_bytes(b"hello")

        status, preview = self._post(
            "preview_import",
            {
                "session_id": session_id,
                "sources": [str(source_dir / "a.txt")],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["new_file_count"], 1)

        status, _approved = self._post(
            "approve_import",
            {"session_id": session_id, "preview_id": preview["preview_id"]},
        )
        self.assertEqual(status, 200)

        status, manifest = self._post(
            "execute_import",
            {"session_id": session_id, "preview_id": preview["preview_id"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(manifest["copied_relative_paths"], ["a.txt"])
        self.assertTrue((self.project_root / "a.txt").is_file())

    def test_execute_import_without_approval_is_conflict(self) -> None:
        _status, selected = self._post(
            "select_project", {"path": str(self.project_root)}
        )
        session_id = selected["session_id"]
        source_dir = self.base / "source2"
        source_dir.mkdir()
        (source_dir / "b.txt").write_bytes(b"data")
        _status, preview = self._post(
            "preview_import",
            {"session_id": session_id, "sources": [str(source_dir / "b.txt")]},
        )
        status, payload = self._post(
            "execute_import",
            {"session_id": session_id, "preview_id": preview["preview_id"]},
        )
        self.assertEqual(status, 409)
        self.assertIn("error", payload)

    # -- reference-project routes --------------------------------------------

    def test_attach_reference_project_and_select_evidence(self) -> None:
        _status, selected = self._post(
            "select_project", {"path": str(self.project_root)}
        )
        session_id = selected["session_id"]

        reference_root = self.base / "reference"
        reference_root.mkdir()
        _git_init(reference_root)
        (reference_root / "notes.txt").write_text("evidence\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "notes.txt"], cwd=reference_root, check=True, capture_output=True
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Apoapsis Tests",
                "-c",
                "user.email=tests@apoapsis.invalid",
                "commit",
                "-m",
                "notes",
            ],
            cwd=reference_root,
            check=True,
            capture_output=True,
        )

        status, attached = self._post(
            "attach_reference_project",
            {"session_id": session_id, "reference_path": str(reference_root)},
        )
        self.assertEqual(status, 200)
        reference_session_id = attached["reference_session_id"]

        status, evidence = self._post(
            "select_reference_evidence",
            {
                "reference_session_id": reference_session_id,
                "relative_paths": ["notes.txt"],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(evidence["evidence"][0]["relative_path"], "notes.txt")

        status, listed = self._post("list_reference_evidence", {"session_id": session_id})
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["evidence"]), 1)

        status, detached = self._post(
            "detach_reference_project", {"reference_session_id": reference_session_id}
        )
        self.assertEqual(status, 200)
        self.assertTrue(detached["detached"])

    # -- home summary ---------------------------------------------------------

    def test_home_summary_route(self) -> None:
        _status, selected = self._post(
            "select_project", {"path": str(self.project_root)}
        )
        session_id = selected["session_id"]
        status, summary = self._post("home_summary", {"session_id": session_id})
        self.assertEqual(status, 200)
        self.assertIn("available_actions", summary)

    # -- session close --------------------------------------------------------

    def test_close_session_then_reuse_fails(self) -> None:
        _status, selected = self._post(
            "select_project", {"path": str(self.project_root)}
        )
        session_id = selected["session_id"]
        status, closed = self._post("close_session", {"session_id": session_id})
        self.assertEqual(status, 200)
        self.assertTrue(closed["closed"])

        status, payload = self._post("home_summary", {"session_id": session_id})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
