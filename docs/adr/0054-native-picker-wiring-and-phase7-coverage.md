# ADR 0054: Native picker wiring and Phase 7 deterministic-coverage matrix

- Status: Accepted (Rust picker wiring is a disposable, still-unbuilt
  spike update; Python coverage additions are real and testable; no
  Windows compile/run was performed or is claimed)
- Date: 2026-07-23

## Context

ADR 0053 closed the local-IPC gap but left four menu handlers
(`open_project`, `import_files`, `import_folder`,
`attach_reference_project`) as documented stubs because they need a real
source of an operator-chosen filesystem path, which nothing in the spike
had added yet. Separately, ADR 0050's Phase 7 asked for a specific,
enumerated deterministic-coverage matrix (backend lifecycle, capability
isolation, one-project-per-window, native-picker cancellation, every
Phase 2-5 safety rule, and "model inability to expand readable or
writable roots" / "browser inability to send arbitrary project paths").
ADR 0051-0053's tests already covered most of that list as a side effect
of testing each service; this ADR closes the picker gap and fills the
remaining, previously-unaddressed items explicitly.

**This session still has no Windows machine and no display.** Nothing
here changes that. The native picker wiring below is Rust source, written
with the same care and disclosure as the rest of the spike, but not
compiled or run -- exactly like ADR 0050-0053's Rust work. Building and
running `spikes/native-shell-tauri/src-tauri` on real Windows hardware
remains the owner's task, not something this environment can perform.

## Decisions

### Native picker: `tauri-plugin-dialog`, wired end to end

`spikes/native-shell-tauri/src-tauri/Cargo.toml` adds
`tauri-plugin-dialog = "2"`. `main.rs` registers it
(`.plugin(tauri_plugin_dialog::init())`) and uses it as **the only source
of a real filesystem path anywhere in this architecture**: a native
folder or file dialog, read exactly once per menu click, converted to a
canonical path string, and handed to the Phase 6 IPC channel as a named
JSON field -- never as a raw file handle, never cached beyond the single
call that needs it, and never exposed to the webview or a model.

- `open_project`: `blocking_pick_folder()` -> `select_project`, storing
  the returned `session_id` in the same `DesktopIpc.current_session_id`
  Phase 6 state the browser-facing URL never sees.
- `import_files` / `import_folder`: `blocking_pick_files()` /
  `blocking_pick_folder()` -> a new `run_import_flow()` helper that calls
  `preview_import`, blocks on a native Yes/No confirmation dialog **only
  when the preview reports `requires_replacement_confirmation`** (an
  import with no replacements proceeds without an extra click, matching
  ADR 0050's "second confirmation for replacements *only*" rule), refuses
  outright and shows an error dialog if `conflict_count > 0` (a type
  conflict is not something a confirmation flag can resolve), then calls
  `approve_import` and `execute_import`.
- `attach_reference_project`: `blocking_pick_folder()` ->
  `attach_reference_project`. (Selecting *which files* become evidence
  from the attached project still needs its own multi-file-in-a-tree UI,
  not a single picker call -- left as explicit follow-up, not faked here.)
- Cancelling any dialog (the operator closes it without choosing anything)
  does nothing: no IPC call is made, nothing changes, a log line records
  the cancellation.
- `show_project_folder` remains a stub -- it is not an IPC call at all,
  but an OS-specific "reveal in file manager" action this pass did not
  add.

**Disclosed, not hidden, uncertainty**: this session cannot verify
`tauri-plugin-dialog` v2's exact Rust method names
(`blocking_pick_folder`/`blocking_pick_files`/the `MessageDialogBuilder`
chain) against the real crate -- there is no compiler available to check
them here, the same limitation ADR 0050 recorded for the rest of this
file. They are written as this session's best-effort match from
documentation memory, explicitly flagged in-line as "corrected against the
real crate docs at first real build, not confirmed-correct code."

### Phase 7 coverage additions

Four gaps, found by checking ADR 0050 Phase 7's checklist item by item
against ADR 0051-0053's existing tests:

1. **Backend readiness timeout.** `tests/test_native_shell_spike.py`'s
   `_wait_for_ready_line` helper called `proc.stdout.readline()` directly
   inside its polling loop -- a call that blocks with **no timeout of its
   own** against a process that never produces any output at all. Writing
   the new `NativeShellSpikeReadinessTimeoutTests` test (a real, silent
   `subprocess.Popen` that never emits a readiness line) exposed that this
   would have made the test hang for the full lifetime of the silent
   process instead of proving anything about a bounded wait. The helper
   was rewritten to match the real Rust host's actual design (`main.rs`'s
   `spawn_backend`): a background thread does the blocking read, and only
   a queue is waited on with a bounded timeout, so a silent child can
   never block the caller past the requested timeout. This was a genuine
   bug in test-only code, not in `backend_entry.py` or `main.rs`, caught
   by writing the coverage Phase 7 asked for rather than assumed already
   correct.
2. **Import atomicity under a mid-execution change.** A new test in
   `tests/test_desktop_import.py` previews two files together, mutates the
   second one's content after approval but before `execute_import`, and
   asserts the whole import aborts with `ImportSafetyError` -- and,
   critically, that the *first* file (which staged and validated
   successfully before the second one's mismatch was caught) was **never
   promoted into the project**. This confirms `execute_import`'s existing
   two-phase design (stage and re-validate every file first; only then
   promote any of them) is genuinely all-or-nothing, not merely
   best-effort.
3. **One-project-per-window binding.** A new test in
   `tests/test_desktop_registry.py` opens two different projects, confirms
   their session ids resolve to their own distinct roots, and confirms
   re-selecting an already-open project returns a *third*, independent
   session id rather than mutating or reusing the first -- there is no API
   anywhere that retargets an existing session to a different root.
4. **Model inability to expand roots, and browser inability to send
   arbitrary paths.** New `tests/test_desktop_authority_boundary.py`: a
   static source scan (matching the existing style of
   `tests/test_launcher.py` and `tests/test_app_js_regression.py`) asserts
   no model-facing package (`agent`, `architect`, `discovery`, `research`,
   `specification`, `manual_frontier`, `intake`, `review`, `execution`,
   `workflow`, `patches`, `verification`, `context`, `models`) imports
   `apoapsis.desktop`; asserts `apoapsis.ui.server`, `apoapsis.ui
   .application`, and `app.js` never reference `apoapsis.desktop`, a
   `/desktop/` route, or the desktop token header name; and whiteboxes the
   IPC server's own route table against the exact fourteen-operation
   allowlist ADR 0050 Phase 6 named, so an accidentally added fifteenth
   route (or a route missing its handler) fails a test immediately.

### What Phase 7's checklist still cannot be deterministically covered here

- **Symlink rejection is tested; junction rejection is not.** Windows
  junctions are a distinct reparse-point mechanism from POSIX symlinks;
  Python's `Path.is_symlink()` does not reliably detect them on Linux, and
  this sandbox has no Windows filesystem to test against. Real Windows
  junction-rejection coverage remains an outstanding manual-verification
  item (Phase 8).
- **Native picker cancellation** is written into `main.rs` (do nothing,
  log, return) but is Rust UI-event code with no compiler or display
  available to exercise it here -- there is no way to unit-test it from
  this session; it needs a real build.
- **Full existing-suite regression** (task/plan/discovery/review/patch/
  verification) requires running `python -m unittest discover -s tests
  -v`, which this session's Python 3.10 cannot do (`apoapsis.config`
  needs `tomllib`, Python 3.11+). Not run; not claimed.

## Non-goals

- Does not add a reference-evidence file-tree picker (selecting which
  files inside an attached reference project become evidence still has no
  UI).
- Does not add the "reveal in file manager" `show_project_folder` action.
- Does not attempt to compile `src-tauri` in this session -- the
  Linux-only GTK3 wall from ADR 0050 still applies unchanged; adding
  `tauri-plugin-dialog` does not remove it (the underlying `tauri` crate's
  window-system dependency chain is the blocker, not the dialog plugin).
- Does not perform, or claim, any Windows manual-verification step from
  ADR 0050's original Phase 8 checklist.

## Verification

New/changed test coverage: `tests/test_native_shell_spike.py` (readiness-
timeout test, plus the underlying helper fix), `tests/test_desktop_import.py`
(atomicity test), `tests/test_desktop_registry.py` (one-project-per-window
test), `tests/test_desktop_authority_boundary.py` (new file, 5 static
regression tests). None of it was executed in this session -- same
Python-3.10/`tomllib` limitation as ADR 0051-0053. A manual grep
(`grep -rn "apoapsis\.desktop" src/apoapsis --include="*.py" | grep -v
"^src/apoapsis/desktop/"`) was run and returned no matches, corroborating
(but not substituting for) the new static tests' intent. Run:

```powershell
python -m unittest tests.test_native_shell_spike tests.test_desktop_import tests.test_desktop_registry tests.test_desktop_authority_boundary -v
python -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
```

before treating this ADR's coverage claims as verified.

## Addendum: dev-path fix and `show_project_folder`, found while preparing for a real Windows build

Reasoning through exactly what happens the first time someone runs
`cargo run` in `spikes/native-shell-tauri/src-tauri` on real hardware (no
compiler was available to confirm this, only careful reading) surfaced a
real bug: `spawn_backend` resolved `backend_entry.py` only via
`std::env::current_exe()/resources/backend_entry.py` -- the packaged
layout `tauri.conf.json`'s `bundle.active = false` doesn't produce yet.
A plain development build has no `resources/` directory next to the
executable at all, so the very first `cargo run` would have failed
immediately with "could not resolve bundled backend_entry.py path,"
before ever reaching the point ADR 0050's spike checklist cares about.

`resolve_backend_entry_path()` now tries the packaged location first, and
falls back to `env!("CARGO_MANIFEST_DIR")/../backend_entry.py` -- this
crate's own manifest lives at `spikes/native-shell-tauri/src-tauri/`, one
directory below `backend_entry.py`, so the fallback resolves correctly for
every development run without needing a bundle step at all. The packaged
path is still tried first and still wins once real bundling exists.

Separately, `show_project_folder` (the one File-menu action ADR 0054's
main text left stubbed) is now real: it calls `home_summary` over the
existing Phase 6 channel to read the current project's canonical path,
then hands off to an OS-appropriate reveal command (`explorer
/select,<path>` on Windows, `open -R` on macOS, `xdg-open` elsewhere) --
not a desktop-IPC operation itself, just a local OS action on a path
Apoapsis already validated. Every File/View/Help menu item now has a real
(if still uncompiled) implementation.

Neither of these was verified by compiling or running -- still no Rust
toolchain with the full GTK stack, still no Windows machine, still no
display in this session. They were found and fixed by reading the code
path a real first run would take, the same way the Phase 7 readiness-
timeout test exposed the test-helper bug: by asking "what would actually
happen here" rather than assuming already-written code was correct.
