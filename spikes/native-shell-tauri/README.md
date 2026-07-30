# Disposable spike: native desktop shell (ADR 0050, Phase 1)

**This directory is disposable spike material, not a shipped feature.** It
exists only to let ADR 0050 record concrete evidence for or against a Tauri 2
native shell before any real packaging work begins. Nothing here is wired
into `pyproject.toml`, `apoapsis`'s package data, or any build/release step,
and nothing here should be imported by `src/apoapsis`.

## What this spike is trying to prove

Per `HANDOFF.md`'s authority boundary and ADR 0050, Phase 1 requires evidence
that:

1. A native window can open without launching the user's default browser.
2. The existing Python backend (`apoapsis.ui.server.create_ui_server`) can be
   started as a managed child process, unmodified.
3. A random per-launch capability token protects local communication, reusing
   the existing `X-Apoapsis-Session` header check in
   `src/apoapsis/ui/server.py` -- no change to that boundary.
4. Backend startup failure (for example: an uninitialized project root) is
   detected and produces a useful error instead of a hang or a silent crash.
5. Closing the shell terminates only the one backend process it started.
6. The packaged application can locate its Python runtime and static assets.

## What actually ran in this environment, and what did not

This session's sandbox initially had no Rust toolchain, and
`static.rust-lang.org`/`sh.rustup.rs` are both blocked
(`403 blocked-by-allowlist`). A real Rust 1.91.1 toolchain was later obtained
via the sandbox's Ubuntu package mirror instead (`apt-get download` into a
user-writable prefix, since root/`apt-get install` is unavailable). With that
toolchain: `cargo generate-lockfile`/`cargo fetch` against the real crates.io
index and CDN **succeeded** -- the full Tauri 2 dependency graph in
`src-tauri/Cargo.toml` resolves and downloads cleanly. `cargo check` then
compiled roughly 50 crates (proc-macro/serde infrastructure, then
`glib-sys`/`gobject-sys`/`gio-sys` against this sandbox's pre-installed
`libglib2.0-dev`) before failing at `gdk-sys`, which needs GTK3 development
headers this rootless sandbox cannot install (manually reconstructing
GTK3/WebKit2GTK's transitive dependency closure without `dpkg`'s own
resolver was judged impractical and not pursued further).

That failure is Linux-only and orthogonal to the real target: Tauri's Linux
backend links GTK3/WebKit2GTK, while Windows links the OS-provided WebView2
runtime through different code. **`src/main.rs` itself was never actually
type-checked** in this session -- `cargo check` stopped inside a dependency
before reaching our own source -- so whether the spawn/token/window-event
logic in `main.rs` actually compiles against the real `tauri` 2.11 API
remains unverified. No native window has been opened by this spike. Claiming
otherwise would violate the same "measured vs. unmeasured" discipline
`HANDOFF.md` requires for model and provider evidence.

`main.rs` has since grown considerably (ADR 0053/0054): a second,
privileged local-IPC channel (its own token, its own loopback port, real
HTTP calls via `reqwest`), a real `tauri::menu` File/View/Help structure,
and `tauri-plugin-dialog`-based native folder/file pickers wired to that
channel for `open_project`/`import_files`/`import_folder`
/`attach_reference_project`. None of this changes the paragraph above --
it is still never-compiled, disclosed-uncertain Rust source (the exact
`tauri-plugin-dialog` method names used are this session's best-effort
match to the crate's API, explicitly flagged in-line as unverified). Only
`show_project_folder` remains a stub, and only because it needs an
OS-specific "reveal in file manager" call this pass did not add.

What **did** run, deterministically, in this sandbox (`tests/
test_native_shell_spike.py`):

- `backend_entry.py` (below) starts the real, unmodified
  `apoapsis.ui.server.create_ui_server` against a real initialized fixture
  project, as a separate OS process, bound to `127.0.0.1` on an OS-assigned
  ephemeral port -- exactly what the Rust host is written to do via
  `std::process::Command`.
- The child prints `APOAPSIS_SPIKE_READY <port>` on stdout only once the
  server has actually bound its socket, giving the host a deterministic
  readiness signal instead of a fixed sleep.
- A generated high-entropy token is required on every `/api/*` request
  (`X-Apoapsis-Session`); a request with the wrong token is rejected with
  `401`, using the server's existing, unmodified `_authorized()` check.
- Pointing the same entry script at an uninitialized project directory
  (no `.apoapsis/config.toml`) fails fast with a specific stderr message and a
  non-zero exit code, never hangs, and never partially binds a socket -- the
  condition the Rust host's `main.rs` (below) is written to catch and turn
  into a dialog.
- Terminating the child (`SIGTERM`, handled explicitly by `backend_entry.py`)
  causes exactly that one process to exit; the test asserts on that process's
  own PID only, matching the "closing the window kills only the process it
  started" requirement -- it never touches an independently running Ollama
  service, matching ADR 0034/0035's existing "leave the shared service
  running" rule.

**Not measured here, and not claimed:** actual native-window rendering, that
no system browser tab opens, startup time of a compiled Tauri binary,
packaged installer size, code-signing/SmartScreen behavior, or locating a
bundled Python runtime inside a real Tauri `resources/` directory. Those
require a real Windows machine with a Rust toolchain, per the ADR's
completion criteria, and are explicitly out of scope for this session
(the owner chose "skip and document as untested" for Phase 8).

## Files

- `backend_entry.py` -- the exact child-process entry point both the (unbuilt)
  Rust host and the deterministic test harness use. Thin: argument parsing,
  a readiness line, a signal handler, then it hands off to the existing,
  unmodified `create_ui_server`.
- `src-tauri/Cargo.toml`, `src-tauri/tauri.conf.json`, `src-tauri/src/main.rs`
  -- an unverified but carefully written Tauri 2 host implementing the
  child-process lifecycle, token generation, readiness wait with timeout,
  failure-dialog path, and single-owned-process shutdown described above.
  Written to be buildable on a Windows machine with the Tauri 2 toolchain;
  never compiled in this session.

## Next steps before this graduates out of "spike"

1. Build and run `src-tauri` on an actual Windows machine with Rust + the
   Tauri 2 CLI installed; record real startup time, binary/installer size,
   and whether a system browser tab opens (it must not).
2. Confirm the packaged binary can locate a Python interpreter and this
   checkout's `src/` the same way `OPEN_APOAPSIS.cmd` already does (or bundle
   one), and record that evidence in ADR 0050 or a successor ADR.
3. Only then proceed to Phase 2 (native project picker/registry) -- per the
   ADR, full packaging must not proceed until the Phase 1 spike passes on
   real hardware.
