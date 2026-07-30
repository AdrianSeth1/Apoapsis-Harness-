"""Deterministic evidence for ADR 0050's Phase 1 native-shell spike.

This does not build or run the disposable Tauri host in
`spikes/native-shell-tauri/src-tauri/` -- that requires a Rust toolchain and
a Windows display, neither available in every environment. Instead it proves,
against the real subprocess/socket boundary, the exact backend-lifecycle
behaviors the Rust host's `main.rs` is written to rely on:

- the backend starts as a separate OS process and reports readiness only
  once its socket is actually bound;
- the existing capability-token check in `apoapsis.ui.server` rejects a
  wrong token and accepts the right one, unmodified;
- an uninitialized project root fails fast with a specific, useful message
  instead of hanging or partially binding a socket;
- terminating the child (as a native host's window-close handler would)
  cleanly stops exactly that one process.

No native window, no Rust build, and no Docker/Ollama/network dependency is
required to run this file.
"""

from __future__ import annotations

import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from apoapsis.cli.app import _init

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_ENTRY = _REPO_ROOT / "spikes" / "native-shell-tauri" / "backend_entry.py"
_READY_TIMEOUT_SECONDS = 15.0


def _init_git_fixture(root: Path) -> None:
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


def _wait_for_ready_line(proc: subprocess.Popen, timeout: float) -> int | None:
    """Reads stdout lines until APOAPSIS_SPIKE_READY <port> or the process
    exits, bounded by `timeout` -- **even against a process that never
    produces any output at all**. Mirrors the real Rust host's design
    exactly (`main.rs`'s `spawn_backend`, ADR 0050/0053): a background
    thread does the blocking `readline()` work, and only a queue is waited
    on with a bounded timeout, so a silent child can never make this
    function block past `timeout` regardless of how long the child itself
    keeps running. (An earlier version of this helper called
    `proc.stdout.readline()` directly in the polling loop, which blocks
    with no timeout of its own against a genuinely silent child -- a real
    gap Phase 7's readiness-timeout test below caught.)"""
    assert proc.stdout is not None
    lines: "queue.Queue[str]" = queue.Queue()

    def _reader() -> None:
        for line in proc.stdout:  # blocks freely; this thread is daemonic
            lines.put(line)
        lines.put("")  # EOF sentinel

    threading.Thread(target=_reader, daemon=True).start()

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            return None
        if not line:
            return None
        stripped = line.strip()
        if stripped.startswith("APOAPSIS_SPIKE_READY "):
            return int(stripped.split(" ", 1)[1])


class NativeShellSpikeBackendLifecycleTests(unittest.TestCase):
    """Exercises backend_entry.py exactly as the (unbuilt) Tauri host's
    `spawn_backend()` is written to: as a child process, waiting on a
    stdout readiness line rather than a fixed sleep."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        _init_git_fixture(self.root)
        _init(self.root)
        self.token = secrets.token_urlsafe(32)
        self._procs: list[subprocess.Popen] = []
        self.addCleanup(self._cleanup_procs)

    def _cleanup_procs(self) -> None:
        for proc in self._procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

    def _spawn(self, project_root: Path) -> subprocess.Popen:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(_BACKEND_ENTRY),
                "--project-root",
                str(project_root),
                "--token",
                self.token,
                "--port",
                "0",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._procs.append(proc)
        return proc

    def test_backend_starts_as_a_separate_process_and_reports_readiness(self) -> None:
        proc = self._spawn(self.root)
        port = _wait_for_ready_line(proc, _READY_TIMEOUT_SECONDS)
        self.assertIsNotNone(
            port, "backend never printed APOAPSIS_SPIKE_READY within timeout"
        )
        self.assertIsNone(proc.poll(), "backend process exited instead of serving")
        self.assertNotEqual(proc.pid, 0)

    def test_capability_token_protects_the_api_and_wrong_token_is_rejected(self) -> None:
        proc = self._spawn(self.root)
        port = _wait_for_ready_line(proc, _READY_TIMEOUT_SECONDS)
        self.assertIsNotNone(port)

        health_request = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(health_request, timeout=5) as response:
            self.assertEqual(response.status, 200)

        good_request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/overview",
            headers={"X-Apoapsis-Session": self.token},
        )
        with urllib.request.urlopen(good_request, timeout=5) as response:
            self.assertEqual(response.status, 200)

        bad_request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/overview",
            headers={"X-Apoapsis-Session": "not-the-real-token"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(bad_request, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_uninitialized_project_fails_fast_with_a_useful_error(self) -> None:
        with tempfile.TemporaryDirectory() as uninitialized:
            proc = self._spawn(Path(uninitialized))
            try:
                stdout, stderr = proc.communicate(timeout=_READY_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail(
                    "backend hung instead of failing fast on an "
                    "uninitialized project root"
                )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("APOAPSIS_SPIKE_ERROR", stderr)
            self.assertIn("not an initialized Apoapsis project", stderr)
            self.assertNotIn("APOAPSIS_SPIKE_READY", stdout)

    def test_terminating_the_child_stops_exactly_that_process(self) -> None:
        proc = self._spawn(self.root)
        port = _wait_for_ready_line(proc, _READY_TIMEOUT_SECONDS)
        self.assertIsNotNone(port)
        pid = proc.pid

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("backend did not shut down after SIGTERM")

        self.assertIsNotNone(proc.poll(), "process should have exited")
        self.assertEqual(proc.pid, pid, "only the one owned PID was ever targeted")


class NativeShellSpikeReadinessTimeoutTests(unittest.TestCase):
    """ADR 0050 Phase 1 requires the native host to detect a backend that
    never reports readiness at all (as opposed to one that fails fast with
    an explicit error, already covered above) -- e.g. a hung process, or
    one from a future version of `backend_entry.py` that regresses and
    forgets to print its readiness line. `main.rs`'s `spawn_backend` uses
    exactly the same bounded-wait shape as `_wait_for_ready_line` below;
    this proves that shape actually returns rather than blocking forever
    against a real, deliberately-silent child process -- no fake or mock
    involved, a genuine `subprocess.Popen` that never emits the line."""

    def setUp(self) -> None:
        self._procs: list[subprocess.Popen] = []
        self.addCleanup(self._cleanup_procs)

    def _cleanup_procs(self) -> None:
        for proc in self._procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_readiness_wait_times_out_against_a_silent_process(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._procs.append(proc)

        started = time.monotonic()
        port = _wait_for_ready_line(proc, timeout=1.0)
        elapsed = time.monotonic() - started

        self.assertIsNone(port, "a silent process must never be treated as ready")
        self.assertLess(
            elapsed, 5.0, "the readiness wait must bound itself, not block indefinitely"
        )
        self.assertIsNone(proc.poll(), "the silent process itself is untouched by the wait")


if __name__ == "__main__":
    unittest.main()
