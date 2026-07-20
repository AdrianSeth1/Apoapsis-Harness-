# ADR 0014: Local operator interface and capability-protected API

- Status: Accepted
- Date: 2026-07-18

## Context

Apoapsis had a complete deterministic coding harness, a desktop-first product
brief, and a high-fidelity black/orange/purple design export, but no graphical
application surface. The design export uses a proprietary prototype runtime,
loads external browser dependencies, contains illustrative rather than
authoritative task/model data, and cannot become the production trust boundary.

The application needs to remain local-first and dependency-light. It must show
persisted workflow state without letting browser code call providers, parse
audit files into invented state, execute commands, apply patches, change retry
ceilings, decide verification, or mark work complete.

## Decision

Add `apoapsis ui`, implemented as an offline static application served by
Python's standard-library HTTP server on `127.0.0.1`. The initial product
surface uses local HTML, CSS, and JavaScript assets; it has no CDN, font,
framework, Node, Electron, or model-provider runtime dependency.

The browser talks only to `ApoapsisUIService`, a narrow deterministic
application boundary. The first slice exposes:

- repository, configuration, task, workflow-event, report, evaluation, and
  model-lifecycle summaries;
- an explicit `apoapsis doctor` action without provider prompting;
- version-checked specification approval through the existing
  `SQLiteTaskStore.transition` API; and
- visibly unavailable states for task intake and review/resume actions that do
  not yet have an appropriate resumable application service.

Every API request requires an ephemeral high-entropy session capability. The
CLI places it in the launch URL, the application removes it from the visible URL
and retains it for the tab session, and requests send it in
`X-Apoapsis-Session`. The server binds only to loopback, rejects foreign
`Origin` values, enables no CORS, sends a restrictive Content Security Policy,
disables framing and referrers, and serves only an explicit static-asset map.

The API uses optimistic task versions for mutations. UI specification approval
therefore creates the same `specification_approved` event, user actor, state
edge, and version increment as the CLI. The browser never writes workflow or
audit files directly.

This is the local application architecture, but not yet a packaged native
desktop executable. A future WebView/native wrapper may host the same loopback
surface only after its Python/tool discovery, process ownership, update, and
shutdown behavior receive a separate packaging decision.

## Rejected alternatives

- **Ship the Claude Design export runtime.** Rejected because it loads external
  code, is intended for prototyping, and embeds illustrative state.
- **Add Electron or Tauri immediately.** Deferred because either would add a
  second build/runtime toolchain before the deterministic application API was
  proven.
- **Let the UI read audit files and infer state.** Rejected because persisted
  workflow records and typed final reports are authoritative.
- **Have browser code invoke CLI subprocesses.** Rejected because it produces an
  ambiguous command and authority boundary and cannot safely support resumable
  interactions.
- **Expose the local server without a capability.** Rejected because loopback
  alone is not an authorization boundary.

## Consequences

- The first interface is immediately runnable with the existing Python
  installation and works offline.
- Static assets are packaged with `apoapsis.ui`; the prototype runtime and
  screenshots are design references only.
- Read-only views and specification approval are real. New-task model
  extraction, task execution orchestration, human-review resume actions, and
  native desktop packaging remain explicit follow-up work.
- New UI mutations must be typed service methods, capability protected,
  optimistic where applicable, and covered for CLI/event parity. They may not
  widen the provider or workflow authority boundary.

## Addendum (D5c, ADR 0034, 2026-07-20)

The "future WebView/native wrapper" possibility above was formally
evaluated and deliberately deferred: ADR 0034 compares the existing CLI
plus system browser against a WebView2/pywebview native window and a
Tauri-style wrapper, and adds `OPEN_APOAPSIS.cmd`, a minimal Windows
launcher that runs the exact same `apoapsis ui` entry point this ADR
established -- no new capability-delivery, process-ownership, or update
mechanism. The loopback server, capability-token, and CSP boundary
described above are unchanged.
