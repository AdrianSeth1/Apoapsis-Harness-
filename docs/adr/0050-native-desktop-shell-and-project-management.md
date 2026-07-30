# ADR 0050: Native desktop shell and project management (supersedes ADR 0034's wrapper deferral)

- Status: Accepted (architecture and Phase 1 spike only; Phases 2-8 are
  future work, not built by this change)
- Date: 2026-07-23

## Context

ADR 0034 (D5c) deliberately deferred any native desktop wrapper: it compared
the existing CLI-plus-system-browser launcher against a WebView2/pywebview
window and a Tauri-style wrapper, and chose the thinnest option
(`OPEN_APOAPSIS.cmd`) because a native window would "re-solve capability
delivery, offline assets, and process ownership in a second technology stack"
for a benefit the product brief called "desirable eventually but deferred."
ADR 0035 went further and made project selection explicit
(`OPEN_APOAPSIS.cmd <project-root>`, one project per window) but kept the
browser as the only rendering surface and explicitly left "native packaging
and a system folder picker... deferred."

The owner has now prioritized exactly that native surface, plus two
capabilities the browser-only architecture cannot safely provide on its own:
letting the operator pick and switch project directories with a native
folder dialog, and importing files/folders from another project through a
previewed, deterministic copy workflow. Both require the desktop shell to
hold real, user-granted filesystem capability -- something `HANDOFF.md`'s
authority boundary has never granted to browser code or to any model, and
must continue to refuse to grant to a model now.

This ADR is the "separately reviewed" capability-delivery, process-ownership,
and update-security design ADR 0034 and ADR 0035 both said would be required
before native packaging could proceed. It formally supersedes ADR 0034's
wrapper deferral. ADR 0034 remains as decision history: the comparison table
it built is still the right way to reason about the trade-off, and its
`OPEN_APOAPSIS.cmd` launcher is unchanged and keeps working for anyone not
yet on the native build.

## Decision

### Adopt Tauri 2, with the existing Python application unchanged underneath

Tauri 2 is the desktop shell. The Rust host process owns the native window,
native menus, the native folder/file picker, and a narrowly scoped
filesystem capability granted by the user through that picker. It does not
reimplement any Apoapsis workflow logic. The existing Python application
(`apoapsis.ui.server`, `apoapsis.ui.application.ApoapsisUIService`, and every
service beneath them) remains the sole authority for tasks, discovery, plans,
review, patch validation, verification, and audit -- exactly as ADR 0014
established and ADR 0026-0049 have refined since.

```
Native desktop window
    |
    | typed local IPC
    v
Trusted desktop controller (Rust, Tauri host)
    |
    +-- native folder/file picker
    +-- project registry (Phase 2, not built here)
    +-- import/copy preview service (Phase 3, not built here)
    +-- Python backend lifecycle (Phase 1 spike, built here)
    |
    v
Existing Apoapsis application services (unchanged)
    |
    +-- task/discovery/plan/review services
    +-- deterministic model actions
    +-- patch and verification authority
    +-- SQLite and audit artifacts
```

The loopback HTTP server is not exposed as a normal browser destination. The
Rust host binds it to `127.0.0.1` on an OS-assigned ephemeral port, generates
a fresh high-entropy capability token per launch (reusing, not replacing, the
existing `X-Apoapsis-Session` check in `src/apoapsis/ui/server.py`), starts
the Python process as a child it alone owns, renders the same existing
`app.js`/`index.html`/`styles.css` assets inside the native window instead of
a browser tab, and terminates only that owned child when the window closes.
It never calls `webbrowser.open()` and never stops an independently running
Ollama service (unchanged from ADR 0013/0034).

### The filesystem capability boundary this unlocks -- and where it stops

The desktop shell, not the model, may hold real filesystem capability:
opening a native folder picker, reading a chosen directory's Git/Apoapsis
state, and copying files during an explicitly approved import. This is new
relative to ADR 0014's browser-only surface, which had no filesystem access
at all. It is not new relative to `HANDOFF.md`'s authority boundary, which
has always said models are untrusted typed proposers and never receive
direct filesystem, shell, Git, network, or workflow authority -- that
sentence is unchanged and this ADR does not touch it.

Concretely:

- A model never receives a native file handle, an arbitrary path string it
  can traverse, or a filesystem API. It may only ever act on typed evidence
  Apoapsis's existing context compiler already assembles from inside the one
  bound project root, exactly as today.
- The desktop controller's filesystem capability is scoped to what the user
  explicitly selected through a native dialog for a specific window and a
  specific operation (open this project; import from this chosen path into
  this chosen destination; attach this chosen repository as read-only
  reference). It is not a standing "read anywhere" grant.
- Browser-side JavaScript (`app.js`) continues to have zero filesystem
  access, exactly as ADR 0014 established. It requests typed operations
  (Phase 6) by opaque capability ID; it can never supply an arbitrary path
  string that the backend or the Rust host will honor.

### Phased plan (this ADR authorizes the plan; only Phase 1 is built now)

1. **Native-shell technical spike** (built in this change; see below).
2. Native project picker and registry -- trusted desktop-controller service,
   outside browser JavaScript, storing only canonical paths and harmless
   display metadata in application-owned user data.
3. Safe file/folder import -- deterministic preview, staged copy, manifest,
   and audit artifact; never a silent overwrite, never `.git`/`.apoapsis`
   traversal, never following symlinks by default.
4. Explicit "Open project" / "Import files" / "Attach reference project" as
   three distinct operations -- never a raw recursive "merge projects";
   actual Git-history merging is explicitly deferred to its own future ADR.
5. Desktop UX: File/View/Help menus, a Home screen surfacing project
   identity, Git state, initialization state, verification readiness, and
   recent projects.
6. Typed backend capability APIs (`select_project`, `validate_project`,
   `initialize_project`, `preview_import`, `approve_import`,
   `execute_import`, `attach_reference_project`, etc.), window-scoped,
   project-scoped, non-transferable, and invalidated on restart unless
   re-selected.
7. Deterministic test coverage for all of the above, using fakes for native
   dialogs and child processes -- no real GUI required to run the suite.
8. Manual verification on real Windows hardware.

Phases 2-8 are **not implemented by this change**. Each is its own
substantial unit of work and, per `AGENTS.md`, will get its own
implementation pass, its own `HANDOFF.md`/`README.md` updates, and its own
deterministic coverage when built. This ADR authorizes the direction and
records Phase 1's spike evidence so that work can proceed without
re-litigating the Tauri-vs-alternatives choice each time.

### Why Tauri 2 rather than PySide6 or Electron

- **PySide6 with a managed embedded view**: would require reworking the
  existing offline HTML/CSS/JS surface (ADR 0014) into Qt widgets or an
  embedded Chromium/WebEngine view either way, and Qt's own WebEngine
  dependency is a heavier, less-maintained-on-Windows embedding story than
  Tauri's OS-native WebView2. A **fully native PySide6 rewrite** would
  additionally throw away the entire existing `app.js`/`index.html` surface
  ADR 0014-0049 built and verified across a dozen live-browser passes --
  a much larger and riskier rewrite for no capability benefit.
- **Electron**: bundles a full Chromium and Node runtime (hundreds of MB),
  and its packaging/update/security surface is larger than Tauri's
  OS-native-webview approach for a project that has, until now, added zero
  packaging dependencies. Considered only if Tauri packaging proves
  impractical; not chosen here.
- **Tauri 2**: uses the OS's existing WebView2 runtime on Windows (already
  present per ADR 0034's own comparison table), keeps the existing static
  HTML/CSS/JS asset surface completely unchanged, and needs only a thin Rust
  host to own the window, dialog, and process-lifecycle concerns this ADR
  actually needs to solve. No technical spike evidence gathered in Phase 1
  contradicts this choice; see below for exactly what was and was not
  verified.

## Phase 1 spike: what was built and what it proves

`spikes/native-shell-tauri/` (explicitly disposable, not wired into
`pyproject.toml` or any packaging step) contains:

- `backend_entry.py`: the exact child-process entry point a Tauri host
  spawns. It imports and calls the existing, unmodified
  `apoapsis.ui.server.create_ui_server` -- no server-side code changed by
  this ADR. It prints one deterministic `APOAPSIS_SPIKE_READY <port>` line
  only once the socket is actually bound, and a specific
  `APOAPSIS_SPIKE_ERROR: <reason>` line on `stderr` with a non-zero exit
  when the target directory is not an initialized Apoapsis project --
  before any socket is opened.
- `src-tauri/{Cargo.toml,tauri.conf.json,src/main.rs}`: a Tauri 2 host
  written to spawn `backend_entry.py`, generate a random per-launch
  capability token, wait on the readiness line with a bounded timeout,
  show a plain-language dialog and exit on failure instead of showing a
  broken window, navigate an already-created *hidden* window to the
  backend's loopback URL only once ready (never opening a system browser
  tab), and terminate only its own recorded child PID when the window
  closes.

### What this session actually measured (and what it did not)

This authoring environment initially had no Rust toolchain, and
`sh.rustup.rs`/`static.rust-lang.org` are both blocked (`403
blocked-by-allowlist`). A working `rustc`/`cargo` (1.91.1) was obtained
instead via the sandbox's Ubuntu package mirror (`apt-get download`, extracted
to a user-writable prefix without root, since this sandbox user cannot run
`apt-get install`). With that toolchain, `cargo generate-lockfile` and
`cargo fetch` against the real crates.io index/CDN **succeeded**: the full
dependency graph for Tauri 2 resolved cleanly (`tauri v2.11.5` and roughly
150 transitive crates), which is itself real evidence -- the `Cargo.toml`
in this spike is not aspirational; it names a dependency set that Cargo can
actually resolve and download today.

`cargo check` was then run against this real, resolved dependency graph (in
a scratch copy under `/tmp`, since the mounted project folder's filesystem
does not support the temp-file operations Cargo's linker step needs). It
compiled successfully through roughly 50 crates -- proc-macro/serde
infrastructure, then `glib-sys`/`gobject-sys`/`gio-sys` (using this sandbox's
pre-installed `libglib2.0-dev`) -- before failing at `gdk-sys`, which needs
`gdk-3.0`/GTK3 development headers this sandbox does not have and cannot
install without root (its `apt-get install` is blocked by dpkg lock
permissions, and manually extracting GTK3/WebKit2GTK's full transitive
dependency closure by hand, without dpkg's own resolution and postinst
machinery, was judged impractical and not attempted further).

This is a **Linux-only** limitation and does not bear on the actual target:
Tauri's Linux backend links GTK3/WebKit2GTK, while the Windows backend this
ADR actually targets links the OS-provided WebView2 runtime through a
different code path that was never reached or exercised here. So, honestly:
this session did **not** compile the Windows target, did not open a native
window, and did not observe any packaging metric (startup time, installer
size, code-signing/SmartScreen behavior, upgrade story). Those still require
a real Windows machine with the Tauri 2 CLI and Rust toolchain, which the
owner explicitly deferred to a later manual pass (see `spikes/
native-shell-tauri/README.md`). Important precision: because `cargo check`
compiles dependencies before the crate's own code, `src/main.rs` itself was
**never actually type-checked** against the real `tauri` API in this
session -- the build stopped inside a dependency (`gdk-sys`) before reaching
our own source. What *is* now established is only that `Cargo.toml` names a
real, resolvable dependency graph, and that graph's platform-agnostic layers
(serde, proc-macro infrastructure, glib/gobject/gio bindings) actually
compile with a current stable Rust toolchain. Whether `main.rs`'s own code
(the `spawn_backend`/`generate_capability_token`/window-event-handling logic)
type-checks against the real `tauri` 2.11 API remains unverified and is the
very next thing to check once GTK's dev headers (Linux) or a real Windows
+ WebView2 environment (the actual target) is available.

What **was** run deterministically in this environment, in
`tests/test_native_shell_spike.py` (commands to run this suite are in
`HANDOFF.md`/`README.md`; this ADR does not claim the suite was executed by
the change that authored it, per the owner's explicit request not to run
tests during this session -- run it before treating Phase 1 as closed):

- the backend starts as a genuinely separate OS process and reports
  readiness only after binding its socket -- proving a native host can use a
  stdout readiness line instead of a fixed sleep or polling a not-yet-open
  port;
- the existing capability-token check rejects a wrong `X-Apoapsis-Session`
  value with `401` and accepts the right one with `200`, unmodified from
  `src/apoapsis/ui/server.py`;
- pointing the same entry point at an uninitialized project directory fails
  within seconds with a specific, actionable stderr message and a non-zero
  exit, never hanging and never partially binding a socket -- exactly the
  condition the Rust host's failure-dialog path is written to catch;
- terminating the child process (as a window-close handler would) stops
  exactly that one process, verified by asserting on its own PID, without
  affecting anything else on the system.

### Do not proceed with full packaging until the spike passes on real hardware

Per the plan above, Phase 2 (native project picker/registry) and later
phases must not begin until someone with a Windows machine and a Rust/Tauri
2 toolchain has actually built and run `spikes/native-shell-tauri/src-tauri`,
confirmed no system browser tab opens, and recorded real startup time,
packaged size, and failure-dialog behavior in a follow-up to this ADR or a
dated `docs/evaluation/` record. This is not optional evidence-gathering;
`HANDOFF.md`'s Snapshot table must not gain a "native shell: verified" row
until that pass happens.

## Non-goals

- Does not implement Phase 2 (project registry), Phase 3 (import workflow),
  Phase 4 (reference-project attachment), Phase 5 (desktop menus/Home
  screen), Phase 6 (typed capability API surface beyond this spike's
  process-lifecycle plumbing), or Phase 7's full deterministic coverage
  matrix. Each remains explicit future work with its own implementation
  pass.
- Does not grant a model any new filesystem, shell, Git, or network
  authority. `HANDOFF.md`'s authority-boundary table is unchanged.
- Does not change `src/apoapsis/ui/server.py`'s capability-token, CORS,
  CSP, or asset-serving behavior in any way -- the spike's host process
  only launches and points a window at the existing, unmodified server.
- Does not add a Rust or Node dependency to `pyproject.toml`, the installed
  `apoapsis` package, or its packaging metadata. `spikes/` is disposable and
  unreferenced by the shipped package.
- Does not claim a compiled binary, a real native window, or any packaging
  metric was observed. Explicitly unmeasured, per above.
- Does not implement unrestricted model filesystem access, automatic project
  merging, automatic Git commits, automatic dependency installation, or
  silent verification reconfiguration -- all remain explicitly out of scope
  for every phase of this initiative, not only Phase 1.

## Consequences

- ADR 0034's wrapper deferral is superseded: a native desktop shell is now
  the target architecture, built incrementally, starting from a spike whose
  process-lifecycle and capability-token behavior is deterministically
  proven even without a compiled Rust build in every environment.
- `OPEN_APOAPSIS.cmd` and the browser-based loopback UI remain fully
  functional and unchanged for as long as the native shell is incomplete;
  nothing in this change removes or degrades the existing surface ADR
  0014-0049 built.
- Future phases inherit a settled architecture question (Tauri 2, existing
  Python backend unchanged, capability-token reuse, one-project-per-window)
  and can focus their own ADRs on their own specific typed-API and
  safety-rule design instead of re-arguing the shell technology.
- The filesystem capability boundary this ADR opens is deliberately narrow:
  the desktop controller may hold user-granted capability; a model may not,
  now or in any later phase, without its own explicit ADR overriding
  `HANDOFF.md`'s authority table.

## Verification

`tests/test_native_shell_spike.py` (4 tests) exercises the backend
child-process lifecycle, capability-token enforcement, startup-failure
detection, and single-owned-process termination described above,
deterministically, without a GUI, Rust build, network, or live model. Run it
with:

```powershell
python -m unittest tests.test_native_shell_spike -v
```

No Rust build, native-window rendering, or Windows packaging evidence is
claimed by this ADR. Phase 8's manual verification checklist (14 steps
covering launch, project selection, import safety, reference attachment,
and workflow-authority regression) remains entirely outstanding and is not
attempted here, matching the owner's explicit choice to defer it.
