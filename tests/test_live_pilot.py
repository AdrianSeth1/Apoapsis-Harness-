from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apoapsis.qualification.live_pilot import (
    LivePilotError,
    OPERATOR_ACKNOWLEDGEMENT,
    _prepare_containment_workspace,
    load_authorized_inputs,
    run_live_pilot,
)
from apoapsis.qualification.pilot import PilotLock, PilotManifest
from apoapsis.qualification.slot_driver import WORKCELL_UID, controller_address, run_qwen

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "docs/qualification/slice7-crisis-atlas-pilot-manifest-v8.json"
LOCK = REPO / "docs/qualification/slice7-crisis-atlas-pilot-lock-v8.json"
REHEARSAL = REPO / "docs/evaluation/slice-7p3-rehearsal-v8-evidence/rehearsal-report.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def authorization(module: Path) -> dict:
    manifest = PilotManifest.model_validate_json(MANIFEST.read_text(encoding="utf-8"))
    lock = PilotLock.model_validate_json(LOCK.read_text(encoding="utf-8"))
    return {
        "authorization_id": "test-live-pilot",
        "manifest_path": MANIFEST.relative_to(REPO).as_posix(),
        "manifest_digest": manifest.digest(),
        "lock_path": LOCK.relative_to(REPO).as_posix(),
        "lock_digest": lock.digest(),
        "rehearsal_report_path": REHEARSAL.relative_to(REPO).as_posix(),
        "rehearsal_report_sha256": sha(REHEARSAL),
        "live_runner_commit": "a" * 40,
        "controller_image_id": "sha256:" + "b" * 64,
        "bound_live_modules": [
            {"path": module.relative_to(REPO).as_posix(), "sha256": sha(module)},
            {"path": "src/apoapsis/qualification/slot_driver.py", "sha256": sha(REPO / "src/apoapsis/qualification/slot_driver.py")},
        ],
    }


class LiveAuthorizationTests(unittest.TestCase):
    def test_fake_provider_is_controller_local_even_inside_host_networking(self):
        self.assertEqual(controller_address(), "127.0.0.1")

    def test_containment_workspace_is_owned_by_the_pinned_workcell_user(self):
        with tempfile.TemporaryDirectory() as raw, patch(
            "os.chown", create=True
        ) as chown:
            workspace = Path(raw) / "workspace"
            _prepare_containment_workspace(workspace)
            self.assertTrue(workspace.is_dir())

        chown.assert_called_once_with(workspace, WORKCELL_UID, WORKCELL_UID)

    def test_operator_launcher_mounts_bound_docs_read_only(self):
        launcher = (REPO / "tools/run_crisis_atlas_live_pilot.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('${REPO}/docs:/opt/apoapsis/docs:ro', launcher)

    def test_server_detection_reads_procfs_without_external_tools(self):
        from apoapsis.qualification.live_pilot import _llama_server_pids

        with tempfile.TemporaryDirectory() as raw:
            proc = Path(raw)
            for pid, command in (("41", "python"), ("73", "llama-server")):
                process = proc / pid
                process.mkdir()
                (process / "comm").write_text(command + "\n", encoding="utf-8")
            (proc / "not-a-pid").mkdir()
            unreadable = proc / "99"
            unreadable.mkdir()

            self.assertEqual(_llama_server_pids(proc), (73,))

    def test_no_acknowledgement_means_no_preflight_or_model_start(self):
        with self.assertRaisesRegex(LivePilotError, "not authorized"):
            run_live_pilot(
                repo=Path("/not/read"), authorization_path=Path("/not/read"),
                evidence_root=Path("/not/write"), seed_repository=Path("/not/read"),
                acknowledgement="no",
            )

    def test_the_passed_rehearsal_and_live_modules_are_bound(self):
        module = REPO / "src/apoapsis/qualification/live_pilot.py"
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "authorization.json"
            path.write_text(json.dumps(authorization(module)), encoding="utf-8")
            loaded, manifest, lock, _, _ = load_authorized_inputs(REPO, path)
        self.assertEqual(loaded.operator_acknowledgement, OPERATOR_ACKNOWLEDGEMENT)
        self.assertEqual(manifest.digest(), loaded.manifest_digest)
        self.assertEqual(lock.digest(), loaded.lock_digest)

    def test_one_changed_live_module_refuses_the_run(self):
        module = REPO / "src/apoapsis/qualification/live_pilot.py"
        payload = authorization(module)
        payload["bound_live_modules"][0]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "authorization.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(LivePilotError, "bound live module differs"):
                load_authorized_inputs(REPO, path)


class ResumeCommandTests(unittest.TestCase):
    class Session:
        def __init__(self):
            self.commands = []

        def exec(self, argv, timeout_seconds=0):
            self.commands.append(argv)
            if "cat /tmp/qwen" in argv[-1]:
                return 0, "", ""
            return 0, "EXIT=0", ""

    def test_supervised_continuation_uses_native_resume_and_stream_events(self):
        session = self.Session()
        run_qwen(session, "repair", continue_session=True, stream_json=True)
        command = session.commands[0][-1]
        self.assertIn("--continue", command)
        self.assertIn("--output-format stream-json", command)


if __name__ == "__main__":
    unittest.main()
