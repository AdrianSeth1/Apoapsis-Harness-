# ADR 0034: D5c browser-product polish and minimal Windows launcher (native wrapper deferred)

- Status: Accepted
- Date: 2026-07-20

## Context

Apoapsis's local operator interface (`apoapsis ui`, ADR 0014) is a real,
live-verified application surface: New Task intake, the control room,
Human Review, Plans, and Discovery are all exercised end to end in a real
browser against a real local model (ADR 0023/0024/0031/0032/0033). What it
still lacks is ordinary product finish for a personal-first tool that
should also be usable by others: a few surfaces carry slogan-flavored
copy instead of the calm, operational language the rest of the product
already uses, the browser tab title never changes as the user navigates,
and there is no obvious Windows entry point for someone who does not want
to open a terminal — `START_APOAPSIS.cmd`/`STOP_APOAPSIS.cmd` manage local
model lifecycle, but nothing opens the UI itself.

Before adding any launcher, this ADR first records the packaging decision
it depends on, comparing three ways to give a double-click launch
experience:

1. **Existing CLI plus system browser** (`apoapsis ui`, unchanged, plus a
   thin `.cmd` launcher that runs it and lets `webbrowser.open()` — already
   used by `serve_ui()` — open the user's default browser).
2. **A WebView2/pywebview-style native window** embedding the same loopback
   server in an OS-native window instead of a browser tab.
3. **A Tauri-style wrapper** (a small Rust host process embedding a system
   webview, with its own build toolchain, updater, and installer story).

## Decision

**Retain the existing loopback application plus system browser as the
product surface. Add one minimal launcher, `OPEN_APOAPSIS.cmd`. Defer any
native wrapper or installer.** Do not install a packaging framework
(pywebview, WebView2 runtime bindings, Tauri, Electron) and do not
implement a native window in this change.

### Comparison

| Dimension | CLI + system browser | WebView2 / pywebview | Tauri-style wrapper |
| --- | --- | --- | --- |
| Installation size | Zero added bytes; reuses the browser already on the machine | Adds a runtime dependency (WebView2 is preinstalled on current Windows, but pywebview itself is a new pip dependency, and the project currently has none) | Adds a Rust build toolchain and a compiled binary (tens of MB) per platform |
| Code signing | None needed; no new executable is produced | A new Python-launched native window still runs as an unsigned local process; no store distribution, so signing buys little yet | A compiled installer/binary is the kind of artifact that draws SmartScreen warnings without a paid code-signing certificate |
| Updater / security surface | None; the checkout itself is the update unit (`git pull`) | New: window-embedding library becomes an additional dependency to patch | New: a second language runtime, its crates, and an updater mechanism all become part of the trust boundary |
| Python/Git/ripgrep/Ollama/Docker discovery | Unchanged — the same `apoapsis doctor` preflight already used by the CLI and `apoapsis ui` | Same preflight, but now needs to run *before* a native window can even display an error, adding a bootstrap-sequencing problem that does not exist today | Same, plus the wrapper process itself now needs its own discovery of the Python interpreter it is supposed to launch |
| Loopback capability handling | Already solved (ADR 0014): ephemeral session token embedded in the launch URL, stripped from the visible address bar | Same token still has to reach the embedded webview; most embedding libraries pass URLs through the OS shell or a config file, a new place for the capability to leak (e.g., process listings, shell history) | Same problem, plus the Rust host process itself becomes another place the token is visible in memory/logs |
| Offline assets | Already offline (ADR 0014): all HTML/CSS/JS ship with `apoapsis.ui`, no CDN | Unchanged — same static assets, now served into a native window instead of a tab | Unchanged — same static assets, now bundled into a compiled binary |
| Process ownership / shutdown | `apoapsis ui` is one foreground Python process; Ctrl+C or closing its console window stops exactly that process and nothing else | A native window adds a second process (or a second thread hosting the embedded browser engine) whose lifecycle must be kept in lockstep with the Python server's — a new class of "window closed but server still listening" or vice versa bug | Same coupling problem, now across a process *and* language boundary, with its own crash-recovery questions |
| Model unloading | Unaffected either way — Start/Stop lifecycle (ADR 0013) is a separate, already-correct concern; nothing here should touch it | Same — but a new packaging surface increases the chance someone conflates "close the app window" with "unload the model," which the product must actively resist regardless of chrome | Same risk, larger surface |
| Portability | Works on any OS with Python + a browser today; the `.cmd` launcher is Windows-specific chrome only, not a portability regression, since the CLI underneath is already cross-platform | pywebview supports macOS/Linux too, but each platform needs its own embedded-webengine dependency (WebKit, GTK WebKit, WebView2) — three new dependency surfaces instead of one | Tauri also targets multiple platforms, but at the cost of a full second toolchain per contributor machine |
| Maintenance complexity | Effectively zero — one `.cmd` file, no new dependency, no new test surface beyond the launcher's own static checks | New dependency to track for security advisories, new window-lifecycle tests, new bootstrap-error UI needed for the pre-window state | Largest ongoing surface: a second build pipeline, a second dependency ecosystem, and an installer/updater to maintain |

### Why not defer the launcher too

Every dimension above is already solved for the browser tab path because
ADR 0014 solved it once, generally, for the loopback server. A native
window would have to re-solve capability delivery, offline assets, and
process ownership in a second technology stack for a benefit (a window
instead of a tab) that the product-decision brief explicitly calls
"desirable eventually but deferred." A `.cmd` launcher adds none of that
risk: it does not introduce a second process, a second capability-delivery
path, or a second update mechanism — it is exactly `apoapsis ui`, run from
a double-click instead of a typed command, with the failure messages a
first-time user needs before they know to open a terminal at all.

## Consequences

- `OPEN_APOAPSIS.cmd` (repository root) launches `apoapsis ui` from the
  checkout using the same `PYTHONPATH`-based invocation
  `START_APOAPSIS.cmd`/`STOP_APOAPSIS.cmd` already use — no installed
  package required. It reports missing Python, missing Git, and a
  not-yet-initialized project (`.apoapsis/config.toml` absent) with a
  plain-language message and a non-zero exit code, then pauses unless
  `APOAPSIS_NO_PAUSE=1` is set, mirroring the existing lifecycle scripts.
- It never installs, downloads, or pulls anything (no pip install, no
  model pull, no Docker image, no Git operation beyond what `apoapsis`
  itself already does for repository inspection), never changes
  Docker/Ollama configuration, and never calls `operator_lifecycle stop`
  on exit — closing the launcher's console window (or Ctrl+C) stops only
  the one `apoapsis ui` process it owns. Local model memory is released
  only by the existing, unmodified `STOP_APOAPSIS.cmd`, which the launcher
  points users to rather than duplicating.
- The ordinary CLI (`apoapsis ui`, `apoapsis run`, etc.) is completely
  unchanged and remains the debugging/automation path; the launcher is a
  thin, optional convenience wrapper, not a new entry point with its own
  behavior.
- No new model, execution, patch, verification, planning, or workflow
  authority is added anywhere in this change. The UI remains a projection/
  client of `ApoapsisUIService` exactly as ADR 0014 established.
- A native desktop window (WebView2/pywebview) and a packaged
  installer/updater remain explicitly out of scope until their own
  capability-delivery, process-ownership, and update-security design is
  separately reviewed — this ADR is that review's rejected-alternatives
  record, not a commitment to build either later.

## Non-goals

- Does not add pywebview, WebView2 bindings, Tauri, Electron, or any other
  packaging dependency to `pyproject.toml`.
- Does not change `apoapsis ui`'s server, capability-token, or CSP
  behavior (ADR 0014) in any way — the launcher only invokes it.
- Does not change `START_APOAPSIS.cmd`/`STOP_APOAPSIS.cmd` or the Start/Stop
  model lifecycle (ADR 0013).
- Does not produce a signed installer or a code-signing decision; there is
  no compiled artifact to sign.

## Rejected alternatives

See the comparison table above. Both native-window options were rejected
for this milestone specifically because they duplicate already-solved
problems (capability delivery, offline assets, process ownership) in a new
technology stack for a benefit the product decision defers. Revisit only
once a native window is actually prioritized, and re-run this comparison
against whatever WebView2/Tauri tooling exists at that time.
