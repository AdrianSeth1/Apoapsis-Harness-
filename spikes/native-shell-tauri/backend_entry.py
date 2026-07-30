"""Disposable spike entry point (ADR 0050 Phase 1, ADR 0053 Phase 6).

Not part of the shipped `apoapsis` package -- not referenced by
`pyproject.toml`, `src/apoapsis`, or any installed console script. This is the
child-process program a native shell (Tauri host, or this spike's own
deterministic test harnesses) launches to start two things in one process:

1. The existing, unmodified `apoapsis.ui.server` loopback application --
   the browser-facing surface the native window's webview navigates to,
   protected by the existing `X-Apoapsis-Session` capability token
   (`--token`).
2. (New in ADR 0053, optional) `apoapsis.desktop.ipc_server`'s privileged
   local-IPC channel, on a *separate* loopback port, protected by a
   *separate* capability token (`--desktop-token`) the browser-facing
   webview never receives. This is where `select_project`/`preview_import`
   /`attach_reference_project`/etc. (`src/apoapsis/desktop/`, ADR 0051/0052)
   actually become reachable from a native host process -- never from the
   browser-facing surface, which must not gain filesystem-adjacent
   capability (ADR 0035/0051/0052's shared reasoning).

It intentionally does nothing new to the *existing* browser-facing surface:
it does not change the session-token boundary in `src/apoapsis/ui/server.py`,
and it does not grant any model authority anywhere. It gives a native host
process a deterministic way to (a) know each server is actually ready, and
(b) know *why* startup failed, instead of guessing from a fixed sleep or a
bare non-zero exit code.

Usage (never invoked directly by a user -- always by a managing host process):

    python backend_entry.py --project-root PATH --token TOKEN \\
        [--desktop-token TOKEN] [--registry-db PATH] [--port N]

On success, prints one deterministic readiness line per server actually
started:

    APOAPSIS_SPIKE_READY <port>
    APOAPSIS_DESKTOP_READY <port>          (only if --desktop-token was given)

On failure to start either server, prints a single
``APOAPSIS_SPIKE_ERROR: <reason>`` line to stderr and exits with a non-zero
status *before* binding any socket that has not already bound successfully.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

# This file lives under spikes/, two levels below the checkout root; reuse
# the existing checkout's src/ the same way OPEN_APOAPSIS.cmd's PYTHONPATH
# convention does, so this spike never requires `pip install -e .`.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _fail(reason: str) -> "typing.NoReturn":  # type: ignore[name-defined]
    print(f"APOAPSIS_SPIKE_ERROR: {reason}", file=sys.stderr, flush=True)
    raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--token", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--desktop-token",
        default=None,
        help=(
            "If given, also start the privileged desktop IPC channel "
            "(ADR 0053) on a separate loopback port, guarded by this "
            "separate token. Omit to reproduce Phase 1's original "
            "behavior exactly (browser-facing server only)."
        ),
    )
    parser.add_argument(
        "--registry-db",
        type=Path,
        default=None,
        help=(
            "Project-registry database path for the desktop IPC channel. "
            "Defaults to apoapsis.desktop.registry_store."
            "default_registry_database_path() when --desktop-token is set."
        ),
    )
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    if not project_root.is_dir():
        _fail(f"project root does not exist: {project_root}")
    config_path = project_root / ".apoapsis" / "config.toml"
    if not config_path.is_file():
        _fail(
            "project is not an initialized Apoapsis project "
            f"(missing {config_path}); run `apoapsis init` first, "
            "never automatically"
        )

    try:
        from apoapsis.ui.server import create_ui_server
    except Exception as exc:  # pragma: no cover - defensive import guard
        _fail(f"could not import the Apoapsis UI server: {exc!r}")
        return

    try:
        server = create_ui_server(
            project_root,
            host=args.host,
            port=args.port,
            session_token=args.token,
        )
    except Exception as exc:
        _fail(f"backend failed to start: {exc!r}")
        return

    actual_port = server.server_address[1]
    # Exactly one deterministic readiness line per server, emitted only
    # after each one's socket is actually bound -- the signal a managing
    # host process should wait for instead of a fixed sleep.
    print(f"APOAPSIS_SPIKE_READY {actual_port}", flush=True)

    desktop_server = None
    if args.desktop_token is not None:
        try:
            from apoapsis.desktop.ipc_server import create_desktop_ipc_server
            from apoapsis.desktop.registry_store import default_registry_database_path
        except Exception as exc:  # pragma: no cover - defensive import guard
            server.server_close()
            _fail(f"could not import the desktop IPC server: {exc!r}")
            return

        registry_db = args.registry_db or default_registry_database_path()
        try:
            desktop_server = create_desktop_ipc_server(
                registry_db,
                host=args.host,
                port=0,
                privileged_token=args.desktop_token,
            )
        except Exception as exc:
            server.server_close()
            _fail(f"desktop IPC channel failed to start: {exc!r}")
            return
        print(
            f"APOAPSIS_DESKTOP_READY {desktop_server.server_address[1]}", flush=True
        )

    def _shutdown(signum: int, _frame: object) -> None:
        del signum
        # Runs in the main thread via Python's signal delivery; stop the
        # server loop and let `finally` below close both sockets. This is
        # the exact process a native host's "close window" handler
        # triggers by terminating only the child PID it recorded at spawn
        # time -- no other process (e.g. a separately running Ollama
        # service) is ever touched by this script.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _shutdown)

    desktop_thread: threading.Thread | None = None
    if desktop_server is not None:
        desktop_thread = threading.Thread(
            target=desktop_server.serve_forever,
            kwargs={"poll_interval": 0.1},
            daemon=True,
        )
        desktop_thread.start()

    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if desktop_server is not None:
            desktop_server.shutdown()
            desktop_server.server_close()
        if desktop_thread is not None:
            desktop_thread.join(timeout=5)


if __name__ == "__main__":
    main()
