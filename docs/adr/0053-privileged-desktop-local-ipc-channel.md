# ADR 0053: Privileged desktop local IPC channel (ADR 0050 Phase 6)

- Status: Accepted (Python IPC server built and unit-testable; Rust-side
  wiring is a disposable, still-unbuilt spike update; native pickers are
  not attempted)
- Date: 2026-07-23

## Context

ADR 0051 and ADR 0052 built real Python services for Phases 2-5
(`DesktopProjectService`, `DesktopImportService`, `DesktopReferenceService`,
`DesktopHomeService`), but left them unreachable from anywhere: adding HTTP
routes to the existing browser-facing `apoapsis.ui.server` would let the
browser itself request filesystem-adjacent operations, exactly the
capability ADR 0035/0051/0052 all deliberately keep out of browser
JavaScript's reach. ADR 0052 identified the correct shape of the fix --
"a second, privileged endpoint set on the same already-running backend
process... gated by a second capability token that only this Rust process
ever holds" -- and rejected spawning a fresh Python subprocess per menu
click, since `ProjectCapabilitySessions` and the reference-project
bindings are deliberately in-memory and cannot survive being recreated
per call. This ADR builds that channel.

## Decision

### One more loopback listener, in the same process, on its own port and token

`src/apoapsis/desktop/ipc_server.py`'s `DesktopIPCHTTPServer` is a second
`ThreadingHTTPServer` instance -- not a second process -- bound to
`127.0.0.1` on its own OS-assigned port, guarded by its own capability
token (`X-Apoapsis-Desktop-Token`, checked with `secrets.compare_digest`,
exactly like the existing browser-facing `X-Apoapsis-Session` check in
`apoapsis.ui.server`). It is started, in a background thread, by the same
`backend_entry.py` process that already starts the browser-facing UI
server -- so both share the same `DesktopServices` object
(`src/apoapsis/desktop/services.py`, a small bundle tying
`DesktopProjectService`/`DesktopImportService`/`DesktopReferenceService`/
`DesktopHomeService` to one `ProjectRegistryStore`) and therefore the same
in-memory capability sessions. The browser-facing webview never receives
this token or this port: `backend_entry.py`'s `--desktop-token` argument
is only ever supplied by the native host process, never embedded in the
URL the webview navigates to. Even if browser JavaScript somehow attempted
to reach the desktop port, the browser-facing server's own
`Content-Security-Policy: connect-src 'self'` header (unchanged, ADR 0014)
means the origin the page actually runs on cannot make a cross-port
fetch to it.

### One typed route per Phase 6 operation, mirroring `ui/server.py`'s error-to-status discipline

`POST /desktop/<operation>` for exactly the fourteen names ADR 0050 Phase 6
listed (`validate_project`, `select_project`, `initialize_project`,
`list_recent_projects`, `forget_recent_project`, `close_session`,
`preview_import`, `approve_import`, `execute_import`,
`attach_reference_project`, `select_reference_evidence`,
`list_reference_evidence`, `detach_reference_project`, `home_summary`).
Each handler is a thin, explicitly typed wrapper (`_require_str` on
exactly the fields that operation needs) around the corresponding
`DesktopServices` method -- there is no route that forwards an arbitrary
JSON body field into a filesystem call unchecked. Errors are mapped to
HTTP status using the same most-specific-first discipline
`ui/server.py`'s handlers already use: not-found-shaped errors
(`ProjectNotFoundError`, `CapabilitySessionError`,
`ImportPreviewNotFoundError`) to 404; conflict-shaped errors
(`ProjectNotGitRepositoryError`, `ProjectAlreadyInitializedError`,
`ImportApprovalError`, `ReferenceProjectInvalidError`) to 409; and
safety/validation errors (`ImportSafetyError`,
`ReferenceEvidenceSafetyError`, `RegistryStoreError`, a `DesktopError`
catch-all, bad JSON, or a missing/malformed field) to 400. An unrecognized
route or a wrong/missing token never even reaches a service call (checked
before route dispatch).

### `backend_entry.py` starts both servers; the Rust host waits for both

`backend_entry.py` gained an optional `--desktop-token` argument (and
`--registry-db`, defaulting to a new
`apoapsis.desktop.registry_store.default_registry_database_path()`).
When given, it also constructs `create_desktop_ipc_server(...)`, runs its
`serve_forever` in a daemon thread, and prints a second deterministic
readiness line, `APOAPSIS_DESKTOP_READY <port>`, alongside the existing
`APOAPSIS_SPIKE_READY <port>` -- omitting `--desktop-token` reproduces
Phase 1's original browser-only behavior exactly, so the existing
`tests/test_native_shell_spike.py` needed no changes. On `SIGTERM`, both
servers are shut down and closed before the process exits; killing the one
Python PID a native host owns stops both listeners, so window-close
behavior is unchanged from Phase 1's "closing the window kills only the
process it started" rule.

`spikes/native-shell-tauri/src-tauri/src/main.rs` (still disposable, still
never compiled in this session -- see `../README.md` for exactly what that
means) now generates a second random token, passes it to `spawn_backend`,
and waits for *both* readiness signals before showing the window at all --
a browser-ready-but-desktop-IPC-not-ready window would be a silently
half-working native shell, worse than a clear startup failure. The
privileged base URL and token are stored in Tauri-managed state
(`DesktopIpc`), never exposed to the webview. Three menu handlers
(`open_recent`, `close_project`, `environment_diagnostics`) are wired to
make real HTTP calls over this channel end to end. The remaining four
(`open_project`, `import_files`, `import_folder`,
`attach_reference_project`) still need a native folder/file picker first
-- adding `tauri-plugin-dialog` (or an equivalent) is separate,
not-yet-attempted surface, and is called out explicitly in the source
rather than faked with a placeholder path.

## Non-goals

- Does not add `tauri-plugin-dialog` or any native picker. Four menu
  handlers remain documented stubs for exactly this reason.
- Does not add HTTP routes to the existing browser-facing
  `apoapsis.ui.server` or JavaScript to `app.js` -- that server is
  completely untouched by this change, preserving the existing "browser
  cannot browse arbitrary folders" guarantee.
- Does not let a model reach this channel. Nothing in
  `src/apoapsis/desktop/` is imported by `agent/`, `architect/`,
  `discovery/`, or any provider-facing code.
- Does not persist the privileged token or the desktop port anywhere; both
  live only in the native host process's memory for the run's duration,
  matching the existing browser-facing token's lifecycle.
- Does not change how `ProjectCapabilitySessions` or the reference-project
  bindings work -- they are exactly as in-memory and restart-invalidated
  as ADR 0051/0052 built them; this ADR only gives a real process a way to
  call them repeatedly within one run.
- Does not attempt to compile `src-tauri` in this session. The Linux-only
  GTK3 wall documented in ADR 0050 still applies, now with one more
  dependency (`reqwest`) added to `Cargo.toml`; `reqwest` itself does not
  need GTK, but the `tauri` crate's window-system dependency chain still
  does, so the same blocker is expected to reproduce unchanged.

## Verification

`tests/test_desktop_ipc_server.py` starts a real `DesktopIPCHTTPServer` in
a background thread (mirroring `tests/test_execution_ui.py`'s existing
`create_ui_server`-in-a-thread pattern) and exercises it over real loopback
HTTP: missing/wrong token rejection (401), unknown route (404),
unauthenticated `/health`; `select_project` then `list_recent_projects`;
a missing project path (404) and a missing required field (400); a full
`preview_import` -> `approve_import` -> `execute_import` round trip that
actually copies a file, and executing without approval (409); attaching a
reference project, selecting evidence, listing it, and detaching; a
`home_summary` call; and that closing a session makes a later call on that
same session id fail (404).

**Not run in the authoring session**, for the same reason as ADR
0051/0052: this sandbox's default Python (3.10) lacks `tomllib`, and a
separately obtained Python 3.11.0rc1 interpreter had no working
`pip`/`ensurepip` to install `pydantic`. No pass/fail result is claimed.
Run:

```powershell
python -m unittest tests.test_desktop_ipc_server -v
python -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
```

before treating Phase 6 as verified. The Rust side remains entirely
unverified regardless of Python test results -- it requires a real Windows
machine with a Rust + Tauri 2 toolchain, per ADR 0050's original
completion criteria, and (per the owner's explicit choice recorded in ADR
0050) that manual pass has not happened yet.
