// Disposable ADR 0050 Phase 1 spike. NEVER COMPILED in the sandboxed
// environment that wrote this file (no Rust toolchain, no display server,
// no network access to crates.io). This is a specification for the next
// environment with a real Tauri 2 + Rust toolchain to build and run on
// Windows, not a verified artifact. See ../README.md for exactly what was
// and was not exercised.
//
// What this host is written to do, matching ADR 0050 Phase 1's checklist:
//   1. Never call anything that opens the system's default browser.
//   2. Spawn the existing, unmodified Python `apoapsis.ui.server` backend
//      (via backend_entry.py) as a managed child process -- one owned PID.
//   3. Generate a random per-launch capability token and pass it to the
//      child; never write it to disk, argv history, or logs.
//   4. Wait for the child's `APOAPSIS_SPIKE_READY <port>` stdout line with a
//      bounded timeout; on timeout or an `APOAPSIS_SPIKE_ERROR:` line,
//      surface a plain-language dialog and exit without ever showing a
//      broken window.
//   5. Only once ready: point the (already-created, hidden) window at
//      `http://127.0.0.1:<port>/?session=<token>` and show it. The existing
//      `app.js` boot sequence (ADR 0014) already strips the token from the
//      visible URL and carries it as `X-Apoapsis-Session` from then on --
//      unchanged by this host.
//   6. On window-close, terminate *only* the recorded child PID -- never a
//      separately running Ollama service (ADR 0013/0034's existing rule).
//   7. Locate the Python interpreter and this checkout's `src/` the same
//      way `OPEN_APOAPSIS.cmd` already does today, so packaging doesn't
//      have to solve interpreter discovery twice.

// --- ADR 0050 Phase 5 / ADR 0052 addendum -----------------------------
//
// The menu below is real, reviewable Tauri 2 menu-construction code
// (`tauri::menu`), added in the same disposable, never-compiled spirit as
// the rest of this file. It defines exactly the File/View/Help structure
// ADR 0050 Phase 5 specifies.

// --- ADR 0053 (Phase 6) addendum ---------------------------------------
//
// ADR 0052 identified the missing piece: menu clicks had nowhere to send a
// typed operation, because `ProjectCapabilitySessions` and the reference-
// project bindings in `src/apoapsis/desktop/` are deliberately in-memory,
// so a fresh-subprocess-per-click design could never resolve a session id
// a previous click returned. This file now spawns `backend_entry.py` with
// a *second* random capability token (`desktop_token`, distinct from the
// browser-facing `token` above) that starts `apoapsis.desktop.ipc_server`
// -- a second loopback HTTP listener, on its own OS-assigned port, in the
// *same* Python process as the browser-facing server, so both see the
// same in-memory `DesktopServices` capability-session state. The webview
// never receives `desktop_token` or the desktop port; only this Rust
// process ever holds them, and the browser-facing server's own CSP
// (`connect-src 'self'`) means the webview could not reach that port even
// if it tried.
//
// Several menu handlers below (`open_recent`, `close_project`,
// `environment_diagnostics`) now make real HTTP calls over that channel
// and are wired end-to-end. The ones that needed a native folder/file
// picker first (`open_project`, `import_files`, `import_folder`,
// `attach_reference_project`) are wired too as of the ADR 0054 addendum
// below -- only `show_project_folder` (an OS "reveal in file manager"
// call, not an IPC operation at all) remains a stub.

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::{mpsc, Mutex};
use std::time::Duration;

use rand::RngCore;
use tauri::menu::{Menu, MenuItem, Submenu};
use tauri::{Manager, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

// --- ADR 0054 addendum --------------------------------------------------
//
// This is the operator's only source of a real filesystem path anywhere
// in this architecture: a native folder/file picker, via
// `tauri-plugin-dialog`. Its result is read exactly once per click,
// converted to a canonical path string, and handed to the desktop IPC
// channel (ADR 0053) as a named JSON field (`"path"`/`"reference_path"`
// /one entry of `"sources"`) -- never as a raw file handle, never cached
// beyond the single call that needs it, and never exposed to the webview
// or to a model. Cancelling a dialog (the operator closes it without
// choosing anything) does nothing: no IPC call is made, nothing is
// selected, nothing changes.
//
// The exact method names below (`blocking_pick_folder`,
// `blocking_pick_files`, `MessageDialogBuilder`'s builder chain) are this
// session's best-effort match to `tauri-plugin-dialog` v2's Rust API from
// memory -- **never verified against the actual crate** in this
// environment (same no-Rust-toolchain-until-partway-through, no-display
// limits as the rest of this file; see ../README.md). Treat these calls as
// the intended shape, to be corrected against the real crate docs at
// first real build, not as confirmed-correct code.

const BACKEND_READY_TIMEOUT: Duration = Duration::from_secs(20);

enum BackendEvent {
    BrowserReady { port: u16 },
    DesktopReady { port: u16 },
    Failed { reason: String },
    Exited,
}

/// Holds the privileged desktop-IPC channel's address/token (never given
/// to the webview) and the currently open project's session id, if any.
/// Managed Tauri state, constructed once in `setup` and read by every
/// menu-event handler.
struct DesktopIpc {
    base_url: String,
    token: String,
    client: reqwest::blocking::Client,
    current_session_id: Mutex<Option<String>>,
}

impl DesktopIpc {
    fn call(&self, route: &str, body: serde_json::Value) -> Result<serde_json::Value, String> {
        let response = self
            .client
            .post(format!("{}/desktop/{route}", self.base_url))
            .header("X-Apoapsis-Desktop-Token", &self.token)
            .json(&body)
            .send()
            .map_err(|e| format!("desktop IPC request failed: {e}"))?;
        let status = response.status();
        let payload: serde_json::Value = response
            .json()
            .map_err(|e| format!("desktop IPC response was not JSON: {e}"))?;
        if !status.is_success() {
            return Err(format!(
                "desktop IPC call {route:?} failed ({status}): {payload}"
            ));
        }
        Ok(payload)
    }
}

/// Generates a random per-launch capability token. Never persisted to disk;
/// lives only in this process's memory and the child's argv/env for the
/// duration of the launch, matching the existing loopback session-token
/// model in `src/apoapsis/ui/server.py` (ADR 0014) -- this host does not
/// invent a second capability mechanism, it reuses the same one.
fn generate_capability_token() -> String {
    let mut bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    // URL-safe base64 without padding, matching `secrets.token_urlsafe`'s
    // shape closely enough for the existing header-comparison check, which
    // treats the token as an opaque string.
    base64_url_encode(&bytes)
}

fn base64_url_encode(bytes: &[u8]) -> String {
    const ALPHABET: &[u8] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[((triple >> 18) & 0x3F) as usize] as char);
        out.push(ALPHABET[((triple >> 12) & 0x3F) as usize] as char);
        if chunk.len() > 1 {
            out.push(ALPHABET[((triple >> 6) & 0x3F) as usize] as char);
        }
        if chunk.len() > 2 {
            out.push(ALPHABET[(triple & 0x3F) as usize] as char);
        }
    }
    out
}

/// Finds a Python interpreter the same way `OPEN_APOAPSIS.cmd` already does
/// (`py` launcher first, falling back to `python`), so this host does not
/// introduce a second, divergent discovery path. Never installs Python.
fn find_python_interpreter() -> Result<String, String> {
    for candidate in ["py", "python", "python3"] {
        if Command::new(candidate)
            .arg("--version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false)
        {
            return Ok(candidate.to_string());
        }
    }
    Err(
        "No Python interpreter found (checked `py`, `python`, `python3`). \
         Install Python 3.12+ before launching Apoapsis."
            .to_string(),
    )
}

/// Spawns the existing UI backend (and, since ADR 0053, the privileged
/// desktop IPC channel alongside it in the same process) as one managed
/// child process, and returns both the `Child` handle (so the caller owns
/// exactly one PID to terminate later) and a channel that reports each
/// server's readiness, failure, or unexpected exit -- read from background
/// threads so the main thread never blocks the UI event loop while
/// waiting.
/// Locates `backend_entry.py` two ways: the packaged layout (bundled next
/// to the built executable under `resources/`, once bundling is actually
/// configured -- `tauri.conf.json`'s `bundle.active` is still `false` in
/// this spike, see ADR 0050) and a development fallback using this crate's
/// own `CARGO_MANIFEST_DIR` (this file lives at
/// `spikes/native-shell-tauri/src-tauri/`; `backend_entry.py` is one
/// directory up). Without the fallback, a plain `cargo run` during
/// development would fail immediately with "could not resolve bundled
/// backend_entry.py path" -- a real first-run gap this ADR 0055 addendum
/// closes, found by reasoning through what actually happens before any
/// packaging step exists yet, not by compiling (still not possible in
/// this session).
fn resolve_backend_entry_path() -> Result<std::path::PathBuf, String> {
    if let Some(packaged) = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.join("resources/backend_entry.py")))
    {
        if packaged.is_file() {
            return Ok(packaged);
        }
    }

    let dev_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../backend_entry.py");
    if dev_path.is_file() {
        return Ok(dev_path);
    }

    Err(format!(
        "could not find backend_entry.py in the packaged resources \
         directory or at the development fallback path {}",
        dev_path.display()
    ))
}

fn spawn_backend(
    python: &str,
    project_root: &str,
    token: &str,
    desktop_token: &str,
) -> Result<(Child, mpsc::Receiver<BackendEvent>), String> {
    let backend_entry = resolve_backend_entry_path()?;

    let mut child = Command::new(python)
        .arg(backend_entry)
        .arg("--project-root")
        .arg(project_root)
        .arg("--token")
        .arg(token)
        .arg("--port")
        .arg("0")
        .arg("--desktop-token")
        .arg(desktop_token)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("failed to spawn backend process: {e}"))?;

    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
    let (tx, rx) = mpsc::channel();

    // Read stdout for both deterministic readiness lines -- order is not
    // guaranteed (the browser-facing and desktop-IPC servers bind
    // independently), so this thread reports each one as it arrives
    // rather than assuming a fixed order.
    let tx_stdout = tx.clone();
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines().flatten() {
            if let Some(rest) = line.strip_prefix("APOAPSIS_SPIKE_READY ") {
                if let Ok(port) = rest.trim().parse::<u16>() {
                    let _ = tx_stdout.send(BackendEvent::BrowserReady { port });
                }
            } else if let Some(rest) = line.strip_prefix("APOAPSIS_DESKTOP_READY ") {
                if let Ok(port) = rest.trim().parse::<u16>() {
                    let _ = tx_stdout.send(BackendEvent::DesktopReady { port });
                }
            }
        }
    });

    // Read stderr for the explicit failure marker.
    std::thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for line in reader.lines().flatten() {
            if let Some(reason) = line.strip_prefix("APOAPSIS_SPIKE_ERROR: ") {
                let _ = tx.send(BackendEvent::Failed {
                    reason: reason.to_string(),
                });
                return;
            }
        }
        let _ = tx.send(BackendEvent::Exited);
    });

    Ok((child, rx))
}

/// Builds the File/View/Help menu ADR 0050 Phase 5 specifies. Every item id
/// below is the exact name the (not-yet-built) Phase 6 privileged-IPC
/// handler will match on -- adding a menu item here without a
/// corresponding handler in `on_menu_event` is a deliberate, visible gap,
/// not a silent one.
fn build_menu(app: &tauri::AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let open_project = MenuItem::with_id(app, "open_project", "Open Project…", true, None::<&str>)?;
    let open_recent = MenuItem::with_id(app, "open_recent", "Open Recent", true, None::<&str>)?;
    let import_files = MenuItem::with_id(app, "import_files", "Import Files…", true, None::<&str>)?;
    let import_folder =
        MenuItem::with_id(app, "import_folder", "Import Folder…", true, None::<&str>)?;
    let attach_reference_project = MenuItem::with_id(
        app,
        "attach_reference_project",
        "Attach Reference Project…",
        true,
        None::<&str>,
    )?;
    let close_project =
        MenuItem::with_id(app, "close_project", "Close Project", true, None::<&str>)?;
    let file_menu = Submenu::with_items(
        app,
        "File",
        true,
        &[
            &open_project,
            &open_recent,
            &import_files,
            &import_folder,
            &attach_reference_project,
            &close_project,
        ],
    )?;

    let show_project_folder = MenuItem::with_id(
        app,
        "show_project_folder",
        "Show Project Folder",
        true,
        None::<&str>,
    )?;
    let view_menu = Submenu::with_items(app, "View", true, &[&show_project_folder])?;

    let environment_diagnostics = MenuItem::with_id(
        app,
        "environment_diagnostics",
        "Environment Diagnostics",
        true,
        None::<&str>,
    )?;
    let help_menu = Submenu::with_items(app, "Help", true, &[&environment_diagnostics])?;

    Menu::with_items(app, &[&file_menu, &view_menu, &help_menu])
}

/// Runs the full preview -> (confirm replacements) -> approve -> execute
/// import flow (ADR 0051's three-call contract) for one already-picked set
/// of source paths. Blocks on a native confirmation dialog only when the
/// preview says a replacement or conflict exists -- an import with neither
/// proceeds without an extra click, matching ADR 0050's "second
/// confirmation for replacements *only*" rule (not for every import).
fn run_import_flow(
    app: &tauri::AppHandle,
    ipc: &DesktopIpc,
    session_id: &str,
    sources: Vec<String>,
) -> Result<serde_json::Value, String> {
    let preview = ipc.call(
        "preview_import",
        serde_json::json!({"session_id": session_id, "sources": sources}),
    )?;

    let conflict_count = preview.get("conflict_count").and_then(|v| v.as_i64()).unwrap_or(0);
    if conflict_count > 0 {
        // `show_fatal_dialog` below takes `&tauri::App` (only available
        // inside `.setup()`); `on_menu_event` only gives an `AppHandle`, so
        // this uses the message-dialog plugin directly instead of sharing
        // that helper. A real build should show this non-fatally (the app
        // keeps running) rather than reusing the startup-failure dialog's
        // styling.
        app.dialog()
            .message(
                "This import has one or more destination conflicts (a \
                 directory already exists where a file would go). Choose \
                 a different destination and try again.",
            )
            .kind(MessageDialogKind::Error)
            .blocking_show();
        return Ok(serde_json::json!({"aborted": "conflict"}));
    }

    let requires_confirmation = preview
        .get("requires_replacement_confirmation")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let replacements_confirmed = if requires_confirmation {
        // Best-effort match to tauri-plugin-dialog v2's Rust
        // `MessageDialogBuilder` API -- unverified, see the note above
        // `use tauri_plugin_dialog`.
        app.dialog()
            .message("This import will replace one or more existing files. Continue?")
            .kind(MessageDialogKind::Warning)
            .buttons(MessageDialogButtons::YesNo)
            .blocking_show()
    } else {
        false
    };

    let preview_id = preview
        .get("preview_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "preview response missing preview_id".to_string())?;

    ipc.call(
        "approve_import",
        serde_json::json!({
            "session_id": session_id,
            "preview_id": preview_id,
            "replacements_confirmed": replacements_confirmed,
        }),
    )?;
    ipc.call(
        "execute_import",
        serde_json::json!({"session_id": session_id, "preview_id": preview_id}),
    )
}

/// Opens the OS's file manager with `path` revealed/selected. Not a
/// desktop-IPC call at all -- a purely local OS action using each
/// platform's standard "reveal" command. Never executes anything inside
/// the project; it only asks an already-running file manager to show a
/// path Apoapsis already validated exists (via `home_summary`, called by
/// the `show_project_folder` handler below before this function runs).
fn reveal_in_file_manager(path: &str) -> Result<serde_json::Value, String> {
    let spawn_result = if cfg!(target_os = "windows") {
        Command::new("explorer").arg(format!("/select,{path}")).spawn()
    } else if cfg!(target_os = "macos") {
        Command::new("open").arg("-R").arg(path).spawn()
    } else {
        Command::new("xdg-open").arg(path).spawn()
    };
    spawn_result
        .map(|_| serde_json::json!({"revealed": path}))
        .map_err(|e| format!("failed to open the file manager for {path}: {e}"))
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .menu(|app| build_menu(app))
        .on_menu_event(|app, event| {
            let ipc = app.state::<DesktopIpc>();
            // `MenuId` implements `AsRef<str>` in Tauri 2 -- unverified in
            // this session (never compiled), see ../README.md.
            let id: &str = event.id().as_ref();
            let result = match id {
                // -- wired for real over the Phase 6 privileged channel ---
                "open_recent" => ipc.call("list_recent_projects", serde_json::json!({})),
                "close_project" => {
                    let mut current = ipc.current_session_id.lock().unwrap();
                    match current.take() {
                        Some(session_id) => {
                            ipc.call("close_session", serde_json::json!({"session_id": session_id}))
                        }
                        None => Ok(serde_json::json!({"closed": false, "reason": "no open project"})),
                    }
                }
                "environment_diagnostics" => {
                    let current = ipc.current_session_id.lock().unwrap();
                    match current.as_ref() {
                        Some(session_id) => ipc.call(
                            "home_summary",
                            serde_json::json!({"session_id": session_id}),
                        ),
                        None => Ok(serde_json::json!({"error": "no open project"})),
                    }
                }
                // -- wired for real, via a native picker (ADR 0054) -------
                "open_project" => match app.dialog().file().blocking_pick_folder() {
                    Some(folder) => {
                        let path = folder.to_string();
                        let outcome =
                            ipc.call("select_project", serde_json::json!({"path": path}));
                        if let Ok(payload) = &outcome {
                            if let Some(session_id) =
                                payload.get("session_id").and_then(|v| v.as_str())
                            {
                                *ipc.current_session_id.lock().unwrap() =
                                    Some(session_id.to_string());
                            }
                        }
                        outcome
                    }
                    None => {
                        eprintln!("open_project: cancelled by operator");
                        return;
                    }
                },
                "import_files" | "import_folder" => {
                    let Some(session_id) = ipc.current_session_id.lock().unwrap().clone()
                    else {
                        eprintln!("{id}: no project is currently open");
                        return;
                    };
                    let picked = if id == "import_files" {
                        app.dialog()
                            .file()
                            .blocking_pick_files()
                            .map(|files| files.iter().map(|f| f.to_string()).collect::<Vec<_>>())
                    } else {
                        app.dialog()
                            .file()
                            .blocking_pick_folder()
                            .map(|folder| vec![folder.to_string()])
                    };
                    match picked {
                        Some(sources) => run_import_flow(app, &ipc, &session_id, sources),
                        None => {
                            eprintln!("{id}: cancelled by operator");
                            return;
                        }
                    }
                }
                "attach_reference_project" => {
                    let Some(session_id) = ipc.current_session_id.lock().unwrap().clone()
                    else {
                        eprintln!("attach_reference_project: no project is currently open");
                        return;
                    };
                    match app.dialog().file().blocking_pick_folder() {
                        Some(folder) => ipc.call(
                            "attach_reference_project",
                            serde_json::json!({
                                "session_id": session_id,
                                "reference_path": folder.to_string(),
                            }),
                        ),
                        None => {
                            eprintln!("attach_reference_project: cancelled by operator");
                            return;
                        }
                    }
                }
                // Not a desktop-IPC operation -- reads the current
                // project's canonical path via `home_summary` (already
                // wired), then hands off to an OS-specific "reveal in
                // file manager" command.
                "show_project_folder" => {
                    let current = ipc.current_session_id.lock().unwrap().clone();
                    let Some(session_id) = current else {
                        eprintln!("show_project_folder: no project is currently open");
                        return;
                    };
                    match ipc.call(
                        "home_summary",
                        serde_json::json!({"session_id": session_id}),
                    ) {
                        Ok(summary) => {
                            let path = summary
                                .get("project")
                                .and_then(|p| p.get("canonical_path"))
                                .and_then(|v| v.as_str());
                            match path {
                                Some(path) => reveal_in_file_manager(path),
                                None => Err(
                                    "home_summary response missing project.canonical_path"
                                        .to_string(),
                                ),
                            }
                        }
                        Err(reason) => Err(reason),
                    }
                }
                _ => return,
            };
            // A real build renders this in the window; the spike only
            // demonstrates that the round trip over the privileged
            // channel works. Once `open_project`/`import_files`/etc. are
            // wired to a real picker, whichever call returns a
            // `session_id` (i.e. `select_project`) is what should populate
            // `ipc.current_session_id` -- not done here, since none of
            // those calls are reachable yet (they return early above).
            match result {
                Ok(payload) => eprintln!("menu event {id:?} succeeded: {payload}"),
                Err(reason) => eprintln!("menu event {id:?} failed: {reason}"),
            }
        })
        .setup(|app| {
            let token = generate_capability_token();
            let desktop_token = generate_capability_token();

            let python = match find_python_interpreter() {
                Ok(p) => p,
                Err(reason) => {
                    show_fatal_dialog(app, &reason);
                    std::process::exit(1);
                }
            };

            // Prefer an explicit CLI argument (useful for scripted
            // testing), but the real double-click launch path has none --
            // in that case, ask the operator to pick a project folder with
            // the exact same native picker `open_project` uses later
            // (`app.dialog().file().blocking_pick_folder()`, ADR 0054),
            // before any backend is spawned at all. Declining to pick a
            // project on first launch is not an error; it exits quietly
            // rather than showing a fatal-error dialog.
            let project_root = match std::env::args().nth(1) {
                Some(path) => path,
                None => match app.dialog().file().blocking_pick_folder() {
                    Some(folder) => folder.to_string(),
                    None => {
                        std::process::exit(0);
                    }
                },
            };

            let (mut child, rx) =
                match spawn_backend(&python, &project_root, &token, &desktop_token) {
                    Ok(pair) => pair,
                    Err(reason) => {
                        show_fatal_dialog(app, &reason);
                        std::process::exit(1);
                    }
                };

            let window = app.get_webview_window("main").expect("main window configured");

            // Wait for *both* readiness signals before showing anything --
            // a browser-only-ready window with no working desktop IPC
            // channel behind its menu would be a silently half-working
            // native shell, which is worse than a clear startup failure.
            let mut browser_port: Option<u16> = None;
            let mut desktop_port: Option<u16> = None;
            let deadline = std::time::Instant::now() + BACKEND_READY_TIMEOUT;
            let mut startup_failure: Option<String> = None;
            while (browser_port.is_none() || desktop_port.is_none())
                && startup_failure.is_none()
            {
                let remaining = deadline.saturating_duration_since(std::time::Instant::now());
                if remaining.is_zero() {
                    startup_failure = Some(
                        "Apoapsis backend did not report readiness in time. \
                         Check that this is an initialized Apoapsis project."
                            .to_string(),
                    );
                    break;
                }
                match rx.recv_timeout(remaining) {
                    Ok(BackendEvent::BrowserReady { port }) => browser_port = Some(port),
                    Ok(BackendEvent::DesktopReady { port }) => desktop_port = Some(port),
                    Ok(BackendEvent::Failed { reason }) => {
                        startup_failure = Some(format!("Apoapsis backend failed to start: {reason}"));
                    }
                    Ok(BackendEvent::Exited) | Err(_) => {
                        startup_failure = Some(
                            "Apoapsis backend exited before becoming ready.".to_string(),
                        );
                    }
                }
            }

            if let Some(reason) = startup_failure {
                let _ = child.kill();
                show_fatal_dialog(app, &reason);
                std::process::exit(1);
            }

            let browser_port = browser_port.expect("checked above");
            let desktop_port = desktop_port.expect("checked above");

            app.manage(DesktopIpc {
                base_url: format!("http://127.0.0.1:{desktop_port}"),
                token: desktop_token,
                client: reqwest::blocking::Client::new(),
                current_session_id: Mutex::new(None),
            });

            let url = format!("http://127.0.0.1:{browser_port}/?session={token}");
            // Never opens a system browser tab -- this navigates the
            // already-created native webview window in place.
            window
                .navigate(url.parse().expect("valid loopback URL"))
                .expect("navigate hidden window to backend");
            window.show().expect("show window only once ready");

            // Recorded PID this window owns; the close handler below kills
            // exactly this process and nothing else (no other Ollama
            // process, no other Apoapsis window's backend). Killing the
            // one Python process also stops both the browser-facing and
            // desktop-IPC servers running inside it -- there is nothing
            // else to separately shut down.
            let owned_child = Mutex::new(Some(child));
            let window_for_close = window.clone();
            window_for_close.on_window_event(move |event| {
                if let WindowEvent::CloseRequested { .. } = event {
                    if let Some(mut child) = owned_child.lock().unwrap().take() {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Apoapsis native shell spike");
}

fn show_fatal_dialog(app: &tauri::App, message: &str) {
    // Always print, since a double-clicked exe has no attached terminal a
    // user would see this in -- the dialog below is what an operator
    // actually sees, this line is for whoever's watching a log/console.
    eprintln!("Apoapsis native shell error: {message}");
    app.dialog()
        .message(message)
        .title("Apoapsis")
        .kind(MessageDialogKind::Error)
        .buttons(MessageDialogButtons::Ok)
        .blocking_show();
}
