# Apoapsis Harness

Apoapsis Harness is a local-first context, research, and verification layer for AI
coding agents. It contains the deterministic `substrate-v0.1` baseline, a
bounded inspect-edit-test coding loop, the original one-shot patch baseline,
and a quarantined local Research Mode.

For a plain-English tour of how the system works—including what the held-out
oracle does and does not know—start with
[`docs/architecture-explained.md`](docs/architecture-explained.md). Coding
agents should start with [`HANDOFF.md`](HANDOFF.md) for the canonical living
architecture, current implementation status, known limitations, and required
maintenance contract. The ADRs remain the decision history; this README is the
user-facing guide.

Version 1.0 (the 0.7 release adopted the namespace; package metadata and the
research user agent now match HANDOFF's committed 1.0) uses the complete
Apoapsis namespace: the distribution is
`apoapsis-harness`, the Python package and CLI are `apoapsis`, new project state
lives in `.apoapsis/`, product environment variables begin with `APOAPSIS_`, and
managed branches begin with `apoapsis/`. There is no pre-release compatibility
alias. Legacy `.sol/` audit directories remain excluded and read-only so their
content hashes and worktree pointers are not corrupted; see
[ADR 0007](docs/adr/0007-apoapsis-namespace.md).

## What works now

- Structured task, hard-constraint, context-evidence, model I/O, verification,
  and workflow-event schemas.
- Exact preservation of hard-constraint wording and fail-closed model-request
  constraint coverage.
- Atomic, optimistic SQLite workflow transitions with an append-only event log.
- Repository inspection and dedicated Git worktree/branch lifecycle.
- TOML-configured verification commands with timeouts, restricted environment,
  bounded logs, and structured results.
- A dependency-light CLI and standard-library test suite.
- Native loopback-only Ollama and authenticated OpenAI-compatible frontier
  adapters with token, cache, latency, and configured-price telemetry.
- Model-assisted specification extraction with exact hard-constraint source
  validation and explicit user approval, plus a durable, crash-safe
  `apoapsis intake` CLI/service seam for running that same extraction as a
  background-safe operation.
- Reproducible Git/ripgrep/symbol/import/test context packages with line-level
  provenance.
- A typed coding-agent protocol for literal search, bounded reads, diff
  inspection, incremental patches, configured checks, full verification, and
  explicit escalation—with no shell or arbitrary command access.
- Unified-diff parsing, policy validation, safe worktree application, bounded
  iteration, and verifier-owned completion.
- A `create_file` action for new files that lets local models provide literal
  file content while Apoapsis builds, validates, applies, verifies, and audits
  the patch; known llama.cpp tool-template residue on that action is normalized
  without allowing extra model authority.
- An opt-in, experimental Local Power Sandbox execution mode (ADR 0059,
  disabled by default) that lets a local model write whole files and run
  allowlisted commands inside the disposable task worktree while Apoapsis
  computes the diff, mediates every path and command, and keeps verification
  and completion authority. See "Local Power Sandbox" below.
- Deterministic risk routing across local-only, local-then-frontier,
  frontier-only, and human-review paths, with a reproducible escalation package
  and separate budgets for each coding stage.
- A complete per-task audit directory and aggregate usage/outcome report.
- Deterministically triggered GitHub, official-documentation, and opt-in Reddit
  research planned and synthesized by a tool-free local model.
- Native Ollama structured output, thinking controls, model digest, token, and
  duration telemetry, with the OpenAI-compatible interface retained as fallback.
- Source provenance, license classification, content quarantine, injection
  warnings, bounded caching, comparative synthesis, and brief-only frontier
  handoff.
- A read-only `apoapsis doctor` preflight (toolchain, configured models,
  context limits, credential presence, verification commands, and an opt-in
  provider connectivity probe) and an `apoapsis eval` harness that runs every
  execution lane against a fresh copy of a controlled fixture and writes one
  comparison report.
- Windows `START_APOAPSIS.cmd`/`STOP_APOAPSIS.cmd` controls that let the
  operator select one initialized Git project, start the configured loopback
  local coding service (Ollama or `llama-server`), open the UI, and explicitly
  release supported local model memory without touching hosted providers.
- A bounded, local-first Architect Mode discovery workflow followed by an
  optional frontier planning stage (`apoapsis discover`, ADR 0032): a
  configured local model may propose a small, harness-capped set of
  clarification questions and one `IdeaBrief` the user must explicitly
  approve, before an immutable planning package is sent to a frontier
  model over either an explicitly configured, spend-ceilinged API or a
  manual subscription transport -- a returned plan flows into the
  existing, unmodified Architect Mode import/validate/approve machinery.
- A manual subscription-based frontier coding handoff (`apoapsis
  frontier-manual`, ADR 0031): export an immutable, hashed package and a
  self-contained Markdown file to upload by hand to a ChatGPT/Claude
  subscription session, then import, approve, and apply one bounded
  response -- never automating either website, never storing or reusing a
  subscription credential, and never letting the response claim completion.
- An offline black/orange/purple local operator interface for real repository,
  task, specification, plan, Human Review, event, report, evaluation, and
  model-configuration data, including a durable New Task intake screen, a
  durable control-room execution screen with live tool-action progress,
  version-checked specification/plan approval, bounded continuation, crash
  recovery, and explicit fresh-frontier authorization.

See [ADR 0001](docs/adr/0001-mvp-deterministic-substrate.md) for the substrate
and [ADR 0002](docs/adr/0002-frontier-vertical-slice.md) for the frontier flow.
[ADR 0003](docs/adr/0003-local-research-mode.md) records the Research Mode trust
boundary, [ADR 0004](docs/adr/0004-native-ollama-frontier.md) records the native
all-local proposal path, [ADR 0005](docs/adr/0005-bounded-coding-agent-loop.md)
records the agent action boundary,
[ADR 0006](docs/adr/0006-deterministic-frontier-escalation.md) records provider
routing and escalation, [ADR 0007](docs/adr/0007-apoapsis-namespace.md) records
the product/runtime namespace migration, and
[ADR 0008](docs/adr/0008-evaluation-and-diagnostic-tooling.md) records the
evaluation harness and diagnostic tooling contract,
[ADR 0031](docs/adr/0031-manual-subscription-frontier-handoff.md) records the
manual subscription-based frontier coding handoff,
[ADR 0032](docs/adr/0032-discovery-and-frontier-planning-handoff.md) records
local-first Architect Mode discovery and the frontier planning handoff, and
[ADR 0009](docs/adr/0009-execution-sandbox.md) records the execution
sandbox, [ADR 0010](docs/adr/0010-context-measurement-and-wider-profiles.md)
records the 128k/256k context profiles and the deterministic context-
measurement layer,
[ADR 0011](docs/adr/0011-deterministic-context-quality.md) records change/
reference/failure-directed retrieval, bounded observation compaction, and
stable prompt prefixes, and
[ADR 0012](docs/adr/0012-held-out-oracles-and-evaluation-aggregation.md)
records held-out correctness checks and cross-run metrics, and
[ADR 0013](docs/adr/0013-local-model-operator-lifecycle.md) records safe local-
model Start/Stop behavior, and
[ADR 0014](docs/adr/0014-local-operator-interface.md) records the local
application/API and browser-session security boundary, and
[ADR 0034](docs/adr/0034-browser-launcher-and-native-wrapper-deferral.md)
records the D5c decision to add a minimal Windows browser launcher while
deferring any native desktop wrapper, and
[ADR 0035](docs/adr/0035-guided-workflows-and-planning-research.md) records
the guided project/task/plan/slice/recovery journeys and optional planning-
research stage, and
[ADR 0036](docs/adr/0036-operational-hardening-and-documentation-compaction.md)
records clarification source canonicalization, fair research-query allocation,
known-impossible verification preflight, less brittle patch budgets, and the
current-state documentation split, and
[ADR 0050](docs/adr/0050-native-desktop-shell-and-project-management.md)
supersedes ADR 0034's native-wrapper deferral, adopting a Tauri 2 desktop
shell around the existing unchanged Python backend and building only a
disposable Phase 1 spike so far, and
[ADR 0051](docs/adr/0051-native-project-registry-and-safe-import.md)
implements that plan's Phase 2 (project registry) and Phase 3 (safe file
import) as a Python service layer, not yet wired to a native picker or the
browser UI, [ADR 0052](docs/adr/0052-reference-projects-and-desktop-home-menu.md) adds
Phase 4 (read-only reference-project attachment) and Phase 5 (a Home-screen
data service and an unbuilt native menu skeleton), and
[ADR 0053](docs/adr/0053-privileged-desktop-local-ipc-channel.md) builds
Phase 6's privileged local IPC channel connecting the two, and
[ADR 0054](docs/adr/0054-native-picker-wiring-and-phase7-coverage.md) wires
a native picker to the remaining menu actions and fills several Phase 7
deterministic-coverage gaps, and
[ADR 0062](docs/adr/0062-start-launcher-and-llama-server-lifecycle.md)
records the Start launcher becoming the primary Windows path with loopback
`llama-server` lifecycle support. The
[Research Mode guide](docs/research-mode.md)
covers setup and operation.

The owner and coding-agent roadmap is [`NEXT_STEPS.md`](NEXT_STEPS.md). The
standalone black/orange/purple application brief for Claude Design is
[`docs/product-design-handoff.md`](docs/product-design-handoff.md).

## Install for development

Requirements are Python 3.12+, Git, and preferably ripgrep. The context compiler
has a deterministic lexical fallback when ripgrep is unavailable.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e .
```

On macOS or Linux, use `.venv/bin/python` instead.

Run the tests:

```bash
python -m unittest discover -s tests -v
```

## Start and stop local models on Windows

Double-click `START_APOAPSIS.cmd` to begin a local session. With no folder
argument, it opens a Windows folder picker; select the initialized Git project
you want Apoapsis to manage. The launcher validates Python, Git, the selected
repository, and `.apoapsis/config.toml`, then starts the configured loopback
local coding service and opens the UI for that project.

For the default Laguna `llama-server` configuration, set
`APOAPSIS_LLAMA_SERVER_COMMAND` once to the explicit command that starts your
local server. `START_APOAPSIS.cmd` will use it only when the configured
loopback OpenAI-compatible endpoint is unavailable. It never pulls a model,
installs software, initializes a repository, or manages hosted endpoints.

The research-only model stays lazy by default because loading two large models
can exceed available RAM/VRAM. Warm it explicitly when needed:

```powershell
.\START_APOAPSIS.cmd --include-research
```

When finished, double-click `STOP_APOAPSIS.cmd`. It sends an explicit zero keep-
alive to every configured local Ollama model, including research, and releases
their memory. The shared Ollama service remains running intentionally. A
`llama-server` process launched from `APOAPSIS_LLAMA_SERVER_COMMAND` remains a
normal operator-owned process for this pass; close it the same way you would
close any other local server. Hosted providers, Docker, repositories,
worktrees, and tasks are untouched.

For terminal automation, set `APOAPSIS_NO_PAUSE=1` so the command files do not
wait for a keypress. The last lifecycle result is recorded under the ignored
`.apoapsis/runtime/` directory.

## Local operator interface

Launch the offline interface from an initialized project:

```powershell
apoapsis ui
```

On Windows, use the Start launcher as the primary path:

```powershell
.\START_APOAPSIS.cmd
```

You can also pass the Git project path explicitly:

```powershell
.\START_APOAPSIS.cmd "C:\path\to\your-project"
```

The browser manages **one Git project per window**. To use another project,
run `apoapsis init` once inside that repository, close the current launcher,
and start again with the other folder. Folder selection happens in the trusted
launcher/native layer, not in browser JavaScript; the browser is deliberately
not allowed to browse arbitrary folders or initialize repositories.

`OPEN_APOAPSIS.cmd` checks for the Python launcher, Git, and an initialized project
(reporting any of those missing in plain language before doing anything
else), then runs `apoapsis ui` from the checkout and opens your system
browser. It never installs, downloads, or reconfigures anything, and never
loads or unloads a model -- it manages only the one UI process it starts.
It remains available as a UI-only fallback if the local model service is already
running. Closing its window (or Ctrl+C) stops just that process; use
`STOP_APOAPSIS.cmd` separately to release supported local model memory. See
[ADR 0034](docs/adr/0034-browser-launcher-and-native-wrapper-deferral.md)
for why this stays a thin launcher around the existing browser surface
rather than a native desktop window.

It opens a capability-protected loopback session at `127.0.0.1:7331`. Use
`apoapsis ui --no-open` to serve without opening a browser, or `--port` to select
a different loopback port. All HTML, CSS, and JavaScript assets ship with
Apoapsis; the interface contacts no CDN and never calls a model provider
directly.

### Native desktop shell (spike only -- not a released feature)

[ADR 0050](docs/adr/0050-native-desktop-shell-and-project-management.md)
supersedes ADR 0034's deferral and adopts Tauri 2 as the future native
desktop shell, rendering the same existing offline UI inside a real window
instead of a browser tab, with the Python backend started as a managed child
process behind an unchanged capability-token boundary. Only a disposable
Phase 1 spike exists today (`spikes/native-shell-tauri/`, explicitly not
wired into packaging); it has not been compiled or run on real Windows
hardware. Native project selection/switching, a safe file-import workflow,
and read-only reference-project attachment are designed in ADR 0050 but not
yet implemented. Use `START_APOAPSIS.cmd`/`apoapsis ui` above until a later
change reports real native-shell evidence.

**Filesystem capability boundary:** even once built, the native shell may
hold user-granted filesystem capability the browser-only surface never had
(a native folder picker, reading a chosen project's Git state, copying files
during an explicitly approved import). This is application-level control,
never model control -- models remain untrusted typed proposers restricted to
Apoapsis-supplied evidence from inside one bound project root, exactly as
`HANDOFF.md`'s authority boundary already requires, with or without a native
shell.

[ADR 0051](docs/adr/0051-native-project-registry-and-safe-import.md) builds
the Python side of that boundary now, ahead of the native picker:
`src/apoapsis/desktop/` has a project registry (recent projects, explicit
initialization only, never automatic), opaque window-scoped capability
sessions (never a raw path), and a preview/approve/execute file-import
workflow that hard-excludes `.git`/`.apoapsis`/`.sol`, dependency/build/
virtual-environment directories, and secret-like filenames by default,
never follows symlinks, requires a second confirmation before replacing any
file (with an automatic backup), and writes a durable JSON audit manifest.
None of this is reachable from the browser UI or a model yet -- it has no
HTTP route and no menu entry; it is direct-Python-call-only until the
native shell (or an HTTP boundary in front of it) is built.

[ADR 0052](docs/adr/0052-reference-projects-and-desktop-home-menu.md) adds
two more pieces on the same terms. **Attach reference project**
(`DesktopReferenceService`) is a third, distinct operation from Open
project and Import files: it grants read-only access to a second Git
repository for inspection, and only ever copies evidence the operator
explicitly selects one file at a time -- each captured piece records its
exact source project, commit, path, and hash, and is cached under the
*primary* project's own `.apoapsis/reference-evidence/`, never written
into the reference project or the primary project's tracked source.
**Home-screen data** (`DesktopHomeService`) assembles project identity, Git
state, initialization state, verification readiness, and the recent-
projects list into one deterministic payload for a future native Home
screen. A real (but still unbuilt and never compiled) `File`/`View`/`Help`
menu skeleton now exists in the disposable Tauri spike.

[ADR 0053](docs/adr/0053-privileged-desktop-local-ipc-channel.md) builds
the local-IPC channel that skeleton needed: a second loopback HTTP
listener started alongside the existing browser-facing UI server, in the
same process, on its own port, guarded by its own capability token the
browser webview never receives (so `ProjectCapabilitySessions`' in-memory
sessions survive across repeated calls, unlike a fresh-subprocess-per-click
design would). Three menu handlers (recent projects, close project,
environment diagnostics) make real calls over it, and
[ADR 0054](docs/adr/0054-native-picker-wiring-and-phase7-coverage.md) wires
the remaining four (open project, import files/folder, attach reference
project) to a real native folder/file picker (`tauri-plugin-dialog`), so
every File-menu action except "Show Project Folder" now has a real,
end-to-end (if still uncompiled/unverified) path from a menu click to a
Python service call. The existing browser-facing server and its
`app.js`/HTTP surface remain completely untouched by any of this --
verified by a static regression test that scans for exactly that.

The first slice provides:

- Home/project status and persisted tasks;
- specification review with exact verbatim hard constraints;
- a two-step, optimistic-version specification approval that writes the same
  workflow event as `apoapsis approve`;
- workflow timelines, change/verification summaries, final usage reports, and
  audit-artifact locations;
- persisted evaluation comparisons and actual configured model roles;
- a **Plans** index and detail view (ADR 0019): architecture summary,
  decisions, dependency-ordered implementation slices, validation findings,
  package/provenance, and a deterministic, optimistic-version-checked
  approve action that states explicitly it does not execute any slice;
- a **Human Review** queue and case-detail view (ADR 0020): exact stop
  reason, current diff, active constraints, verification/acceptance
  results, consumed vs. configured budgets, and only the actions the
  review service actually declares eligible, each behind two-step
  confirmation. Submitting an action returns immediately -- a background
  worker performs the actual work (a resumed model call, a verification
  run, or a worktree cleanup), and the page polls a persisted operation id
  for progress, surviving a reload without resubmitting; and
- an explicit **Run doctor** action. Merely opening the UI does not probe or
  load a model.

The Home screen now starts with the three user journeys instead of internal
subsystem names: **Quick change**, **Plan a larger change**, and **Needs
attention**. Natural-language task extraction (Quick change, ADR 0023),
post-approval task execution (Control room, ADR 0024, hardened by ADR 0026),
the manual subscription-based frontier coding handoff (a Human Review
case-detail section, ADR 0031/0033), and local-first discovery plus
frontier planning (`#/discover`, ADR 0032/0033) are all live from the
browser -- a user can go from a typed request to a completed or
Human-Review-stopped task, or from a one-line idea to an approved plan and
then through one explicitly selected slice at a time, without touching the
CLI. Ready/waiting dependency state is computed from the same Git evidence as
slice packaging. A completed slice still has to be committed and merged by
the user before a dependent slice becomes ready; Apoapsis never does that
automatically. The supplied Claude Design export is a visual reference only;
its external prototype runtime is not shipped.

## Current CLI workflow

Initialize Apoapsis inside an existing Git repository:

```bash
apoapsis init
```

Initialization writes an example Python unittest command, not a universal test
contract. Replace it with this repository's real verification command before
starting coding. New projects allow the bounded coding agent to add and edit
test files by default (`patch.allow_test_changes = true`), so a from-scratch
slice can create its own `tests/` directory. Test deletion remains forbidden,
and dependency manifest edits are also allowed by default
(`patch.allow_dependency_changes = true`) so generated applications can declare
the libraries they actually use. Verification-configuration changes remain
forbidden. Set either allow flag to `false` if a repository must
protect existing tests; if its configured unittest command then points at a
missing directory, execution refuses before creating an operation, worktree, or
model call and shows an actionable configuration error in the browser.

When test edits are allowed, the inverse is enforced during coding: a missing
directory used by a required unittest-discovery command becomes an explicit
implementation obligation. The model must create the importable directory and
meaningful task-focused tests. Apoapsis rejects escalation based only on that
known repairable failure and keeps the bounded session working; it never waives
the required check or manufactures tests itself. Declared Python dependencies in
`requirements*.txt` or `pyproject.toml` are installed automatically into a
task-scoped dependency directory before verification. Package build/install scripts
are allowed, bounded by the configured install timeout, and their command, output,
status, backend, and script permission are retained in the verification artifact.
Generated tests should still mock credentials, browser interaction, and live remote
services unless an owner-configured check explicitly requires them.

For blank-repository work, Apoapsis normalizes a narrow class of malformed
new-file diffs produced by local models: missing outer `+` markers and an
incorrect added-line count in a single `/dev/null` text-file hunk. The original
proposal and normalized applied diff are both audited. Existing-file edits,
deletions, binary patches, multi-hunk new files, and all patch-policy checks
remain strict. The coding prompt also receives the effective test/dependency
edit flags instead of a generic rule that may contradict project policy.

Draft a task without model inference. Repeated flags preserve constraints and
criteria as separate source-backed records:

```bash
apoapsis task "Add resumable downloads" \
  --constraint "Preserve the current public API." \
  --constraint "Do not add runtime dependencies." \
  --acceptance "Interrupted downloads resume from the persisted byte."
```

Review and approve the generated task ID:

```bash
apoapsis inspect TASK-ABC123
apoapsis approve TASK-ABC123 --version 2
```

The lower-level workflow APIs then support repository analysis, context
compilation, routing, patch readiness, and verification as later milestones are
added. Worktree and verification lifecycle commands already exist:

```bash
apoapsis worktree-create TASK-ABC123
apoapsis verify TASK-ABC123
apoapsis rollback TASK-ABC123 --delete-branch
```

`verify` deliberately refuses to run until the persisted task state is
`PATCH_READY`. `rollback` is explicit and may discard uncommitted task-worktree
changes. Normal cleanup APIs refuse dirty worktrees unless force is requested.

## Discovery and frontier planning handoff (ADR 0032)

Before designing an architecture plan by hand, `apoapsis discover` gives
you a bounded, local-first way to firm up an idea first -- never a general
chat:

```bash
apoapsis discover start "Add resumable downloads with a pluggable storage backend"
apoapsis discover propose-questions DISC-ABC123 --expected-version 1
```

Your configured `[models.frontier]` local model may propose up to
`[discovery] max_clarification_questions` (default 5) clarification
questions -- fewer is fine, and the harness caps the count regardless of
how many the model returns. Answer in your own words; they are preserved
verbatim, never rewritten. If a local model copies a prompt-added Markdown list
marker or changes only case/whitespace, Apoapsis resolves it back to the exact
matching characters from your idea or answer; paraphrases still fail:

```bash
apoapsis discover answer-questions DISC-ABC123 --expected-version 2 \
  --answer "Q-1=Store offsets in a local SQLite file." \
  --answer "Q-2=Keep the existing public API unchanged."
apoapsis discover propose-brief DISC-ABC123 --expected-version 3
apoapsis discover approve-brief DISC-ABC123 --expected-version 4
```

Only after you explicitly approve the proposed `IdeaBrief` can a frontier
planning package be exported. In the browser, this is also where optional
planning research appears. Choose Auto, GitHub, Community, or Full, or skip it.
Research uses the existing restricted source adapters and tool-less local
research model; only a compact brief plus provenance-bound evidence IDs enter
the frontier planning package. If `[models.local_research]` is not configured,
the interface says so and planning can continue without research.

The model proposes bounded typed queries; it does not receive a browser, raw
network access, credentials, or arbitrary URL fetching. Apoapsis distributes
the candidate budget across planned queries, performs allowlisted fetches,
sanitizes and attributes evidence, and records sources that yielded no relevant
findings in the research audit directory.

Then choose either frontier transport:

```bash
# Manual subscription transport -- upload FRONTIER-PLANNING-HANDOFF-*.md
# to your ChatGPT/Claude session by hand, paste the response back:
apoapsis discover export-frontier-package DISC-ABC123 --transport manual --expected-version 5
apoapsis discover import-manual-response DISC-ABC123 \
  --package-id FPKG-... --response response.json \
  --declared-model-name "claude-opus-4.6-web"

# API transport -- requires [models.frontier_coder]:
apoapsis discover export-frontier-package DISC-ABC123 --transport api --expected-version 5
apoapsis discover preview-api-call DISC-ABC123
apoapsis discover call-api DISC-ABC123 --authorize-planning-spend-usd 1.00
```

The frontier model may return a small, capped number of further
clarification questions (`[discovery] max_frontier_clarification_rounds`,
default 10 -- answer them with `apoapsis discover answer-frontier-questions`
and export again) or a complete plan. A returned plan becomes an entirely
ordinary Architect Mode plan -- inspect, validate, and approve it exactly
as described below, through the same unmodified commands:

```bash
apoapsis discover inspect DISC-ABC123
apoapsis plan validate PLAN-...
apoapsis plan approve PLAN-... --expected-version 2
```

Neither model can approve a plan, invent a verification-command name,
bypass a ceiling, execute a slice, or choose a workflow transition. The
manual transport never automates a subscription website and never records
a token count or cost (there is nothing to measure on a manual paste); the
API transport shows the configured provider/model and a pessimistic
worst-case cost before any call, requires an explicit spend ceiling, and
persists real measured cost.

### How big can a pasted plan be? (ADR 0065)

Big. A frontier plan is *meant* to be substantial — the handoff explicitly
tells the model that "a shallow list of coding tasks is not an acceptable
plan" — so a real one with a dozen components, its integration contracts, a
pre-mortem, and fifteen-plus slices runs to hundreds of kilobytes.

The ceiling is `discovery.max_response_bytes` in `.apoapsis/config.toml`,
2 MB by default (`manual_frontier.max_response_bytes` is the equivalent for
repair handoffs). If you exceed it you get the numbers, not a shrug:

```text
request body is 214113 bytes; this endpoint accepts at most 4194304
```

You do **not** need to split a plan across several pastes. Paste it whole —
one complete response is what gets hash-checked against the exported package.
Keeping the coder's input small is what slices are for, and that happens later
and automatically: each slice is executed on its own, and the coding model
never sees the whole plan.

## Architect Mode: deterministic planning foundation (ADR 0019)

Architect Mode lets a stronger model (Claude, Codex, Fabel, or any other
model you already have access to -- manually, no new subscription or API
credential required) design an architecture and decompose a large idea into
small implementation slices sized for the local coding model's existing
bounded-agent loop. It never executes anything itself: it produces a plan, a
human reviews it, and only an explicit, version-checked approval action ever
changes its status.

```bash
apoapsis plan export "Add resumable downloads with a pluggable storage backend"
```

This writes an immutable `PlannerRequestPackage` (idea text, repository
identity, deterministic context evidence, the configured verification
catalog, documentation references, the plan JSON schema, and explicit
authority rules) to `.apoapsis/plan-packages/<package_id>/request-package.json`
and prints it. Paste the package into any capable chat model, ask it to
return an `ArchitecturePlan` matching the included schema, and save its
response (wrapped with `package_id` and `request_package_sha256`, matching
the package) to a file:

```bash
apoapsis plan import response.json
apoapsis plan validate PLAN-ABC123
apoapsis plan inspect PLAN-ABC123
apoapsis plan approve PLAN-ABC123 --expected-version 2
```

`plan import` rejects a response whose `request_package_sha256` does not
match the stored package exactly. `plan validate` runs deterministic checks
(unique IDs, no dependency cycles or missing dependencies, no unknown
constraint/criterion references, no invented verification-command names,
every active hard constraint represented in some slice, every slice names a
real configured verification command, configurable ceilings, and
repository-relative non-escaping suggested paths) and never raises for
content problems -- an invalid plan is still stored with concrete findings.
`plan approve` requires the plan's last validation to be valid and uses the
same optimistic-version discipline as `apoapsis approve`. A plan can never
mark itself approved or executed: `ArchitecturePlan` has no such field, and
approving a plan never executes any slice -- executing an approved slice is
explicitly out of scope for this milestone.

Validation ceilings are configurable under `[architect.ceilings]` in
`.apoapsis/config.toml` (`max_slices`, `max_dependency_depth`,
`max_suggested_paths_per_slice`, `max_criteria_per_slice`,
`max_work_brief_chars`); `apoapsis init` writes explicit defaults.

## Human review and resume (ADR 0020)

A task that stops at `HUMAN_REVIEW_REQUIRED` -- a rejected specification,
a routing decision that requires a human, incomplete acceptance coverage,
or an exhausted local/frontier coding agent -- now has a real, deterministic
resume path instead of a dead end:

```bash
apoapsis review list
apoapsis review inspect TASK-ABC123
```

`inspect` shows the exact stop reason, current diff, active constraints,
verification/acceptance results, consumed vs. configured budgets, and the
harness-computed set of actions actually available for this task --
never a fixed menu. Every mutation requires the task's current version, a
fresh worktree fingerprint (when a worktree exists), and an explicit,
caller-supplied `--operation-id`; resubmitting the same operation id is
always rejected, so a retried or ambiguous request can never silently
repeat a model call:

The local UI provides the routine path without terminal commands: proposed
plans show **Verify plan**, and verification-failed Human Review cases show
**Repair and verify** when bounded local continuation budget remains. These
buttons call the same versioned, audited services as the CLI.
Discovery accepts the bounded `<think>...</think>` wrapper emitted by some
local reasoning models before their JSON response, then still applies strict
JSON and schema validation.
When deterministic risk routing stops a task before any coding agent or worktree
exists, Human Review instead shows **Run locally**. Confirming it explicitly
authorizes one fresh bounded local execution. It does not change the project's
default route: the override applies only to that operation, which still uses the
normal execution authorization, isolated worktree, patch policy, verification,
reporting, and audit path.
If `[models.frontier_coder]` is configured, the same untouched routing-review
state also offers **Run with frontier**. This is a fresh frontier-only execution,
not a continuation or a silent hosted call; confirmation displays the model and
bounded budget.
If a fresh run finishes without passing required verification, the operation is
labeled **Local run incomplete** or **Frontier run incomplete**, as appropriate,
and dependent slices stay blocked; it is not presented as successful
implementation.
Budget-exhausted implementation stops use the same **Repair and verify** button
when a bounded local continuation remains available; the user does not need to
choose a technical continuation action from a generic menu.
If a continuation operation finishes but verification still fails, the UI labels
it **Repair incomplete** rather than presenting the operation ledger's technical
`succeeded` status as task success. **Repair and verify** remains available while
the freshest verification is failing and continuation budget remains.
Successful repair automatically opens the completed task's report; incomplete
repair stays on Human Review with its newest failure evidence.
The no-model follow-up is labeled **Verify current changes** in Human Review.
If a bounded coding session ends after edits newer than its last check, Apoapsis
automatically runs one final full verification within the existing verification
budget. This is harness-owned verification, not model authority, and is skipped
when the current fingerprint already has complete results.
An unchanged diff or exact file excerpt can be inspected once. Repeating a
read-only observation that adds no evidence is rejected with a direct instruction
to make a corrected edit or inspect somewhere relevant, and three repeated
violations stop as no progress instead of consuming the remaining turn budget.

```bash
apoapsis review abandon TASK-ABC123 --expected-version 4 --operation-id RVOP-1
apoapsis review retry-verification TASK-ABC123 \
  --expected-version 4 --expected-fingerprint <digest> --operation-id RVOP-2
apoapsis review run-local TASK-ABC123 \
  --expected-version 4 --operation-id RVOP-LOCAL-1
apoapsis review continue-local TASK-ABC123 \
  --expected-version 4 --expected-fingerprint <digest> \
  --operation-id RVOP-3 --additional-turns 6
apoapsis review continue-frontier TASK-ABC123 \
  --expected-version 4 --expected-fingerprint <digest> \
  --operation-id RVOP-4 --additional-turns 6
```

`continue-local`/`continue-frontier` resume the exact bounded agent session
that stopped -- same worktree, same prior turns and observations, same
verification history -- with only the authorized additional turns (and a
matching increase to patch-attempt/verification-run budgets) added on top
of whatever was already consumed; nothing is ever reset. `--additional-turns`
and the number of continuations per task are both capped by
`[review]` in `.apoapsis/config.toml` (`max_additional_turns_per_continuation`,
`max_continuations_per_task`). `continue-frontier` is only ever offered when
a frontier agent session already exists for that task; it never launches a
fresh frontier attempt from a local-only stop.

`run-local` is deliberately separate from `continue-local`: it is only offered
for a routing review that happened before any local session or worktree existed.
If startup fails before normal execution begins, the task returns to the same
routing-review state and can be inspected or explicitly retried.

Manual ChatGPT/Claude coding handoffs include bounded repository excerpts
selected under cloud-exclusion rules, prior local/frontier session history,
complete verification evidence, and the exact approved plan-slice package when
one exists. They do not export credentials, ignored secrets, or unrelated
audit-only data.

Starting a fresh frontier stage from a local-only stop is a distinct,
explicitly confirmed action (ADR 0022), never something `continue-frontier`
does implicitly:

```bash
apoapsis review authorize-frontier-stage TASK-ABC123 \
  --expected-version 4 --expected-fingerprint <digest> --operation-id RVOP-5
```

`authorize-frontier-stage` is only offered while a frontier coder is
configured and no frontier session exists yet for the task -- once one
does, only `continue-frontier` is offered from then on. It always uses the
full configured `[execution.frontier_agent]` budget (there is no
`--additional-turns` flag for it, since this is a new session, not a
continuation); both the CLI and the UI display the exact frontier model
and budget before it runs. Frontier availability is always checked against
the *current* configuration, not whatever was true at the original stop --
adding `[models.frontier_coder]` to `.apoapsis/config.toml` after a
local-only stop is enough to make the action available on the next
`review inspect`.

A failed verification or incomplete acceptance stop can also offer
`continue-local` when continuation budget remains. This is the repair path when
the failed check requires code or test changes; `retry-verification` is only for
rerunning unchanged work. New test files are accounted for individually even
when Git would normally summarize their untracked parent directory.

Fresh `apoapsis init` projects use `completion_policy = "baseline"`: all
required verification commands must pass, while a separate acceptance-command
mapping is optional. Set it to `"strict"` when every active acceptance criterion
must be explicitly mapped to an acceptance-designated command.

Every operation is re-validated against fresh state (task version, worktree
fingerprint, eligibility, budgets) immediately before it does anything,
never only at submission time (ADR 0021) -- and only one operation may be
active per task at once. A `RUNNING` operation is owned by a unique,
renewed lease (ADR 0025): a long-but-healthy continuation survives no
matter how long it actually runs, as long as its own process keeps
renewing; only a lease that has genuinely stopped renewing is reclaimed.
If the process running a continuation is killed, `apoapsis review recover`
(also run automatically whenever `apoapsis ui` starts) reclaims any
operation that never actually started, marks a stale in-progress operation
as ambiguous (never automatically repeated), and returns a stranded task to
human review without claiming what the interrupted call did:

```bash
apoapsis review recover
apoapsis review recover --resume-recorded  # also runs every reclaimed operation
```

`recover` alone only reports what it found; `--resume-recorded` is the
explicit, opt-in action that actually runs every reclaimed operation in the
foreground CLI process -- recovering data and authorizing a model to run
are never conflated.

## Manual subscription-based frontier coding handoff (ADR 0031)

When a stopped task is eligible for frontier help but you only have a
ChatGPT/Claude *subscription* -- no configured `[models.frontier_coder]`
API credential -- `apoapsis frontier-manual` gives you a bounded, auditable
way to use it by hand. Apoapsis never automates either website and never
stores or reuses your subscription session; you upload one file and paste
back one JSON response.

```bash
apoapsis frontier-manual export TASK-ABC123
```

This writes an immutable, hashed package (bound to the task's exact version
and worktree fingerprint, the approved specification and active
constraints, the current diff, relevant failure evidence, the configured
verification catalog, and the exact response schema) plus a self-contained
`FRONTIER-CODING-HANDOFF-<package_id>.md` under
`.apoapsis/tasks/<task-id>/`. Upload that Markdown file to your ChatGPT or
Claude subscription session and ask it to solve the task, returning only
the JSON object the file describes -- one complete unified-diff patch and a
short summary, nothing else. Save that response to a file, then:

```bash
apoapsis frontier-manual import TASK-ABC123 \
  --package-id MFH-... --response response.json \
  --declared-model-name "claude-opus-4.6-web" --preview-id MFPV-1
apoapsis frontier-manual inspect TASK-ABC123 --preview-id MFPV-1
```

`import` rechecks the task's current version, eligibility, worktree
fingerprint, the package's own integrity, active-operation conflicts,
response size (before any JSON parsing), schema validity, package-hash
self-consistency, patch parsing, and patch policy -- it creates a preview
only and never touches the worktree. Applying the patch requires two
explicit steps:

```bash
apoapsis frontier-manual approve TASK-ABC123 --preview-id MFPV-1 --expected-version 4
apoapsis frontier-manual apply TASK-ABC123 --preview-id MFPV-1 \
  --expected-version 4 --expected-fingerprint <digest> --operation-id RVOP-1
```

`apply` runs through the same durable review-operation machinery (ADR
0020/0021/0025) every other review action uses -- only one operation may be
active per task, a crash is recovered the same way, and `apply` applies the
patch with the same patch parser/policy/applier and runs the same
configured `VerificationRunner` every other path uses. **Only a passing
verification result reaches `COMPLETE`** -- nothing in the pasted response
can claim completion, select a command, or expand its own budget; the
response schema has no field for any of that. If verification fails, the
task returns to human review and is eligible for a small, configurable
number of further repair rounds (`[manual_frontier] max_repair_rounds`,
default 2) using the real failure evidence -- never an unbounded
back-and-forth:

```bash
apoapsis frontier-manual status TASK-ABC123
```

`declared_model_name` is operator-typed provenance only (e.g.
`"claude-opus-4.6-web"`) -- Apoapsis never verifies which model actually
produced a response, and no token count or cost is ever recorded for this
path (there is nothing to measure on a manual paste), never a fabricated
`0`. This path is entirely separate from, and does not change, the
existing automated API frontier path
(`apoapsis review authorize-frontier-stage`/`continue-frontier`) --
prefer that path once you have real API credentials configured.

## Durable new-task intake (ADR 0023)

`apoapsis run` already does model-assisted specification extraction, but it
blocks the whole CLI process until the model responds. `apoapsis intake` runs
the same extraction (the same extractor, the same one bounded correction
attempt, the same exact-verbatim-constraint and acceptance-catalog checks) as
a durable, crash-safe operation instead:

```bash
apoapsis intake submit "Add resumable downloads without changing the public API" \
  --operation-id INOP-1
apoapsis intake inspect INOP-1
apoapsis intake recover
apoapsis intake recover --resume-recorded  # also runs every reclaimed operation
```

`submit` allocates a task id, persists the operation and the task's
preliminary specification (holding the exact request text) before any model
call, then runs extraction. A clean result reaches `SPEC_DRAFTED`, approved
through the same `apoapsis approve` transition every other task-creation path
already uses; a double extraction failure (both the original and the one
bounded correction) stops deterministically at `FAILED`. `intake recover`
reclaims an operation that never actually started and marks a stale
in-progress one ambiguous, exactly like `review recover` -- a task stranded
mid-extraction is returned to human review, inspectable and abandonable
through the existing, unmodified `apoapsis review` commands. **This does not
execute the approved task** -- new-task execution orchestration is separate,
still-unreleased work.

The local UI (`apoapsis ui`) has the same flow as a **New Task** screen:
describe the request, watch persisted progress (safe to close the tab and
reconnect), then review and approve the drafted specification on the task
page -- the exact same two-step approval `apoapsis approve` already uses.

## Durable post-approval task execution (ADR 0024)

Once a task reaches `SPEC_APPROVED` -- through `apoapsis run`, `apoapsis
task` + `apoapsis approve`, or `apoapsis intake submit` + `apoapsis
approve` -- it can be executed as its own durable, crash-safe operation
instead of only inside a blocking `apoapsis run` process:

```bash
apoapsis execute start TASK-ABC123 --expected-version 3 --operation-id EXOP-1
apoapsis execute inspect EXOP-1
apoapsis execute recover
apoapsis execute recover --resume-recorded  # also runs every reclaimed operation
```

`execute start` runs the exact same routing, context compilation, worktree
creation, local/frontier coding stage (with escalation), verification, and
reporting that `apoapsis run` always used -- nothing was reimplemented, only
extracted into a shared, resumable continuation. The operation is recorded
before anything happens, marked running before any provider call or worktree
mutation, and rechecked against the task's current state, version, and the
repository's current HEAD immediately before doing anything. The running
operation holds a unique, renewed lease (ADR 0025), so a genuinely long
execution never gets mistaken for a crashed one. If the process running it
is killed, `apoapsis execute recover` (also run automatically whenever
`apoapsis ui` starts) marks a stale in-progress operation ambiguous -- never
automatically repeated -- and returns a task stranded mid-execution to
human review **with its worktree left exactly as it was**, inspectable and
abandonable through the existing `apoapsis review` commands.

The local UI (`apoapsis ui`) has the same flow as the task page's **Control
room** tab: once a task reaches `SPEC_APPROVED`, a "Start coding" action shows
a two-step confirmation with the exact predicted route, models, budgets,
completion policy, sandbox, verification commands, and a hash of exactly what
will be authorized (ADR 0026). Confirming sends that hash back, and it is
rechecked -- before any provider is constructed -- against a fresh
recomputation from the task, specification, repository state, and
configuration; if any of those changed since the preview was shown, the
confirmation is rejected rather than silently running something different
from what was shown. Submission then returns immediately; the control room
polls persisted progress (safe to close the tab and reconnect from any
browser, since it discovers an in-progress operation from the task itself,
not client-side storage) and shows real tool actions live, as the bounded
agent produces them, then a usage/telemetry summary once the task finishes. A
task that stops for a human decision links directly into the existing Human
Review case view.

## Approved-plan to single-slice execution (ADR 0027)

Once an Architect Mode plan (see below) is approved, one explicitly selected
slice can become a real, running task through the exact same durable
execution service above -- never automatically, and never more than one
slice at a time:

```bash
apoapsis plan slice list PLAN-ABC123
apoapsis plan slice inspect PLAN-ABC123 SLICE-1
apoapsis plan slice package PLAN-ABC123 SLICE-1 --expected-plan-version 3
apoapsis plan slice approve PLAN-ABC123 SLICE-1 --expected-package-sha256 <hash>
apoapsis plan slice status PLAN-ABC123 SLICE-1
apoapsis plan slice start PLAN-ABC123 SLICE-1
```

`package` checkpoints completed earlier slice work on its Apoapsis-owned task
branch, then deterministically compiles an immutable record of exactly what
approving the slice would authorize -- its exact inherited hard constraints
and acceptance criteria (copied verbatim from the plan, never reworded),
full work brief, required interfaces, exclusions, integration assumptions, stop
conditions, advisory paths/symbols, configured verification commands, dependency
evidence, and exact inherited execution-base commit -- with no model call and no
task created yet. The complete approved contract is preserved in the coding
task's model context. Repairs of older slice tasks recover it from the exact
hash-bound package approved for that task without rewriting prior audit files. The next
slice's isolated worktree starts from the latest completed earlier slice, so
all accumulated prior code is available to its model and verification. The
user's checked-out branch is never moved or merged automatically. Incomplete,
failed, or Human Review slices are never inherited, and divergent completed
branches fail closed instead of triggering an automatic conflict resolution.
Once every slice reaches COMPLETE, the Plan Overview shows **Prepare finished
project**. This checkpoints the exact integrated tip, records the plan as
EXECUTED, and creates a downloadable source ZIP containing
`APOAPSIS-USING-THE-FINISHED-PROJECT.md`. A separate
`FRONTIER-WHOLE-PROJECT-REVIEW-<plan-id>.md` can be uploaded with that ZIP for a
full architecture, integration, security, operability, and verification-gap
review. Preparing a delivery never moves or merges the checked-out branch.
Each slice's delivered outcome and command results are read from the
current-evidence projection described under
[Original report versus current evidence](#original-report-versus-current-evidence),
so a slice repaired after its first stop is reported as it stands now rather
than as it first stopped; delivery refuses outright when a COMPLETE slice's
deciding artifact can no longer be read.

#### Whole-project verification before delivery

Every slice is verified in isolation, in its own worktree, at the time it
runs. That says nothing about the combined result — a plan can deliver four
green slices and a product whose parts never talk to each other, and no
per-slice check can catch it, because no individual slice is wrong.

So before anything is archived, Apoapsis runs the plan's own
`verification_strategy.whole_project_verification_commands` against the exact
integrated commit (ADR 0074), binds the result to that commit and to the
worktree fingerprint it measured, and writes
`.apoapsis/plans/<plan-id>/final-project-verification.json`. Delivery is
permitted only when that run passed.

```toml
[verification_strategy]              # in the approved plan, not config.toml
whole_project_verification_commands = ["integration-check"]
```

If it fails, or the plan named no whole-project command, or the named
commands are not configured in this project, delivery raises with the reason:
**the plan stays APPROVED, no ZIP is written, and no `delivery.json` is
recorded.** Fix the integrated project and prepare delivery again.

A whole-project command usually needs `required = false` in `config.toml`,
because the configured command set runs for *every* slice and an integration
check cannot succeed inside the worktree of a slice whose counterpart does
not exist yet. Delivery forces it required for the final run — naming it in
the plan is your statement that it must pass before shipping.

`delivery.json` therefore carries two separate sections, and they mean
different things:

| Field | Claim |
| --- | --- |
| `verification_summary` | Per-slice history. Scoped to one task each, with no commit or fingerprint binding. |
| `final_project_verification` | The plan's own contract, executed once, bound to the integrated commit. |

The frontier handoff and the ZIP's usage guide keep the same separation, and
both name which acceptance criteria the integrated run did **not** prove.

#### The operability contract

A plan must say how the finished product is started, in a form Apoapsis can
check (ADR 0076). In `delivery_contract`, set **exactly one** of:

| Field | Meaning |
| --- | --- |
| `launch_verification_command` | The *name* of a configured verification command that launches or smoke-tests the product. It must also be in `whole_project_verification_commands`, so it runs against the integrated commit. |
| `launch_not_runnable_reason` | An explicit statement of why no such command can exist — for a library, a data pipeline, anything with no long-running process. |

It is a command name, never a shell string: Apoapsis does not execute
`launch_or_usage_instructions` or any other prose field, and never will.

`primary_documentation_path` must also be set, be a safe repository-relative
path, and be named in some slice's `suggested_paths` — naming a README nobody
is responsible for updating is how a seed README survives to delivery.

At delivery, every `required_artifacts` entry and the documentation path must
actually exist in the integrated commit. A missing one raises, leaving the plan
`APPROVED` with no ZIP.

The ZIP's usage guide then opens by stating whether the launch path was
exercised, by which command, or why not; renders your plan's own install,
launch, test, and readiness text; and puts the old filename-based guesses
under a heading that says they are inferred. `delivery.json` carries the same
facts under `operability`, separating "artifact present", "launch exercised",
and "launch explicitly unmeasured".

A whole-project launch command usually wants `required = false` in
`config.toml`, for the same reason as any integration check — delivery forces
it required for the final run.

#### Plan consistency checks

`apoapsis plan validate` rejects a plan that cannot be delivered coherently
(ADR 0074): one that names no whole-project verification command, declares an
integration contract no slice builds, requires a delivery artifact no slice
produces, or writes an end-to-end scenario proven only by a command that runs
inside a single slice.

A contract whose `runtime_boundary` is `same_origin_http` or
`cross_origin_http` additionally needs an `end_to_end_scenario` proven by a
command that is both acceptance-designated and run against the integrated
project. Apoapsis cannot detect seed data, a demo-only path, or an
"offline mode" fallback — that would need exactly the prose guessing it refuses
to do in a gate — so it instead refuses to let a networked integration exist
with nothing but static evidence behind it.

It also catches a plan that contradicts itself. Set
`runtime_boundary` on an `IntegrationContract` — `same_origin_http`,
`cross_origin_http`, `in_process`, `filesystem`, `subprocess`, or the default
`unspecified` — and validation checks it against the flags of the commands
that govern it. A contract declaring `same_origin_http` alongside a
`--forbid-runtime-network-apis` check is an error: the plan cannot be
satisfied and passed at the same time. This is a lookup of Apoapsis's own
documented flags, not an inference from the contract's prose;
`unspecified` asserts nothing and produces no finding.
`approve` creates the derived task from that exact package (the normal
specification-approval transitions, unchanged) but does not start it;
`start` hands it to the same durable execution service `apoapsis execute
start` uses. A slice's status is always read live from its derived task's
real state, never a separate, independently-tracked copy of it. Nothing here ever
starts a next slice or merges into the user's branch.

The same flow is available from the browser: a plan's Implementation Slices
tab shows live per-slice status, an Inspect view renders the same immutable
package preview, and a two-step Approve action creates the derived task --
which then behaves exactly like any other task, including the existing
control room's own "Start coding" confirmation. There is no "Run all"
button and no scheduler in the UI.

## Diagnostics and evaluation

Check the local toolchain, configured models, context limits, credential
presence (values are never printed), and verification commands:

```bash
apoapsis doctor
apoapsis doctor --probe
```

`--probe` makes one real minimal completion call per configured provider to
check connectivity and structured-output support. A loopback Ollama probe is
free; a hosted (`openai_compatible`) probe result explicitly says it may
incur real cost. Doctor never makes that call unless `--probe` is given, and
it never requires `apoapsis init` to run — a missing configuration is just
one more reported check.

Run the controlled `download-service` fixture through every execution lane
and get one comparison report:

```bash
apoapsis eval download-service
apoapsis eval download-service --lane local --lane one-shot
apoapsis eval download-service --lane forced-escalation --output-dir .apoapsis-eval/run-1
```

Each requested lane (`local`, `hybrid`, `forced-escalation`, `frontier`,
`one-shot`) runs against its own fresh, isolated copy of the fixture. `hybrid`,
`forced-escalation`, and `frontier` need `[models.frontier_coder]` configured;
without it, they are reported as skipped with a clear reason rather than
failing the whole command or making an unauthorized call. `forced-escalation`
proves a real local-to-frontier handoff by giving the local stage only a
one-turn budget, never by altering the task or the patch. Output is written to
`--output-dir` (default `.apoapsis-eval/<run-id>/`, already gitignored) as
`comparison.json` and `comparison.md`.

**If `[models.frontier_coder]` is configured**, requesting `hybrid`,
`forced-escalation`, or `frontier` also requires `--max-hosted-spend-usd
<AMOUNT>` (ADR 0030) -- an explicit hard aggregate spend ceiling in USD for
every hosted call this invocation makes:

```bash
apoapsis eval download-service --lane frontier --max-hosted-spend-usd 2.00 --output-dir .apoapsis-eval/run-2
```

Refused before any lane starts, before any fixture is even copied, if the
run's own configured worst-case allowance (every hosted lane at
`frontier_agent.max_turns` calls each, at the configured context budget and
`frontier_coder.max_output_tokens` ceiling) already exceeds the amount you
give. Checked again after every real call using its actual recorded cost; a
breach stops the whole invocation immediately, not just the lane it happened
in. The plan is printed to stderr and written to `hosted-spend-plan.json`
before anything starts; actual totals are written to `hosted-spend.json`
alongside the comparison report. Run `apoapsis doctor` first -- it warns if a
configured hosted model's pricing is left at $0, which would otherwise make
every recorded cost (and the ceiling itself) meaningless.

For `download-service`, the resumable acceptance oracle is removed before each
lane repository is initialized and is injected only after normal verification
has already declared completion. A normal pass followed by an oracle failure is
recorded as a false success; an oracle infrastructure error is not.

### The `local-strict` lane (opt-in)

`--lane local-strict` is a deliberately separate, opt-in lane measuring the
`STRICT` completion policy (ADR 0015/0016/0017) against a model-visible
acceptance check, not baseline completion:

```bash
apoapsis eval download-service --lane local-strict --output-dir .apoapsis-eval/strict-1
```

It is never part of the default lane set and every other lane keeps
selecting `BASELINE` explicitly regardless of your project's real
configuration, so historical false-success comparisons stay valid. The
`download-service` fixture ships a model-visible
`tests/test_resumable_visible_acceptance.py` (distinct data and test names
from the held-out oracle) — to use `local-strict` meaningfully, configure a
specifically named, acceptance-designated command for it in your own
`.apoapsis/config.toml` (acceptance designation is never generated
automatically, per ADR 0017):

```toml
[[verification.commands]]
name = "resumable-acceptance-check"
category = "acceptance"
description = "Model-visible resumable-download acceptance checks."
argv = ["python", "-m", "unittest", "tests.test_resumable_visible_acceptance", "-v"]
timeout_seconds = 60
required = false
acceptance = true
```

A model may then propose mapping an extracted acceptance criterion to
`resumable-acceptance-check` from the real catalog; nothing here injects or
rewrites that mapping — a missing or invalid one is a genuine result, not
hidden. Aggregate one or more persisted comparisons without making model
calls:

```bash
apoapsis eval-aggregate .apoapsis-eval/run-1/comparison.json \
  .apoapsis-eval/run-2/comparison.json \
  --output-dir .apoapsis-eval/aggregate
```

This writes `aggregate.json` and `aggregate.md` with completion, human-review,
unsafe-patch, false-success, latency, transmission, profile, and paired-lane
metrics. Hosted rescue and savings remain explicitly `unmeasured` unless the
loaded artifacts contain a paired real hosted-frontier run; fake providers test
the formulas but never populate real-world hosted results.

## Local Power Sandbox (ADR 0059, experimental)

An opt-in second execution path for **local models only**. It exists to test one
hypothesis: that small local models fail the strict one-action loop on protocol
mechanics — hand-authored unified diffs, tool-call wrapper residue,
cross-action fields — rather than on coding ability. In this mode the model
writes **whole files** and Apoapsis computes the diff.

The unrestricted Crisis Atlas control showed that even atomic multi-file JSON
actions do not preserve all of a normal coding CLI's useful behavior. A
baseline-preserving **Capability Sandbox** is therefore the next architecture
assignment, not current product behavior: Qwen gets its ordinary persistent
shell/file/test loop only inside a disposable container, while Apoapsis remains
the sole authority for admitting the resulting delta, running independent
verification, checkpointing, completing, and delivering it. That design and
its required paired non-inferiority gates are specified in
`docs/handoff-2026-07-30-qwen-baseline-preserving-superiority.md`.

It is disabled by default and the strict loop remains the documented default.
In the UI, open **Models & environment**, use **Turn on Local Power**, and
confirm the warning. Apoapsis updates only the known execution settings, reloads
and validates `.apoapsis/config.toml`, and then refreshes the execution preview.
Use **Turn off** in the same place to return future runs to the strict loop.

The equivalent manual config is:

```toml
[execution]
mode = "agent"             # required; the sandbox has no one-shot equivalent

[execution.local_power]
enabled = true             # opt in explicitly
workspace = "isolated_worktree"
allow_shell = true
allow_network = false
max_turns = 8
max_seconds = 1800
max_shell_commands = 40
max_changed_files = 100
max_changed_lines = 10000
require_final_diff_review = true
require_verification = true
atomic_change_sets = true  # ADR 0071; false restores the one-file-per-turn protocol
max_change_set_files = 20
verify_after_change_set = true
```

The model's action vocabulary becomes:

```json
{"action":"read_file","path":"src/app.py"}
{"action":"search","query":"AppConfig"}
{"action":"write_file","path":"src/config.py","content":"...full file..."}
{"action":"delete_file","path":"src/old.py"}
{"action":"propose_change_set","summary":"...","changes":[{"operation":"write","path":"index.html","content":"...full file..."}]}
{"action":"run_shell","command":"python -m unittest discover -s tests -v"}
{"action":"run_verification","command_name":"unit-tests"}
{"action":"finish","summary":"..."}
```

### Proposing a whole slice at once (ADR 0071)

A working web page is `index.html`, `styles.css`, and `app.js` agreeing with
each other. Asked to state that one file per turn, a live local model spent six
consecutive turns rewriting `index.html` and ended its session with no `app.js`
at all. `propose_change_set` lets one turn state the whole increment:

```json
{"action":"propose_change_set",
 "summary":"focus orbit timer",
 "changes":[
   {"operation":"write","path":"index.html","content":"...full file..."},
   {"operation":"write","path":"styles.css","content":"...full file..."},
   {"operation":"write","path":"app.js","content":"...full file..."},
   {"operation":"delete","path":"old.js"}
 ],
 "verification_commands":["unit-tests"],
 "base_worktree_digest":"...as given in the prompt..."}
```

What Apoapsis guarantees about it:

- **All or nothing.** Every path, ceiling, and operation is validated before a
  byte is written. If anything is wrong, nothing is written, the sandbox is
  left exactly as it was, and the model is told *every* problem at once rather
  than the first one.
- **The same boundary.** Each operation passes through the guard that governs a
  single `write_file`. A forbidden path anywhere refuses the whole proposal.
- **No patch operation.** There is no diff syntax anywhere in this mode; that
  is the point of it.
- **Ceilings.** At most `min(max_change_set_files, max_changed_files)` files per
  proposal, and the session's changed-line budget still applies — crossing it
  rolls the entire set back byte-for-byte.
- **Optimistic concurrency.** `base_worktree_digest` is optional; when sent, the
  proposal is refused if the sandbox has changed since. `WORKTREE_DIGEST` is
  stated in the prompt.
- **Checks are not deleted to make them pass.** A `delete` naming a path a
  configured verification command points at is refused.
- **The harness verifies it.** Once a set applies, Apoapsis runs the required
  commands itself and the session ends as soon as they all pass, so a
  successful change-set session runs verification once.

When the sandbox already contains work, the prompt says so, lists the changed
paths, and asks for an atomic *repair* set covering only the files the repair
needs — rather than restating the objective, which is what produced the six
rewrites.

Set `atomic_change_sets = false` to reproduce the one-file-per-turn protocol
exactly: the action disappears from both the prompt and the structured-output
grammar.

### When the session ends (ADR 0069)

The harness ends the session itself the moment every required configured
command has passed **for the current state of the sandbox**. The model is never
asked whether a passing result is sufficient, because on a live run it answered
that question by re-requesting the same passing check on every remaining turn
until its budget ran out.

Two consequences you will see in a transcript:

- A repeated identical `run_verification` at an unchanged sandbox state is
  **refused**, not executed, and appears in the refused-requests record. The
  answer cannot change until a file does.
- Finalization **reuses** a current passing result instead of running the same
  full check again. If the model finished early, verified only part of the
  contract, or edited something after verifying, the harness-owned final
  verification runs exactly as before.

Turn, time, command, file, and diff-line ceilings are unchanged; this is a stop
condition, not a relaxed budget.

### What the model is told about failures (ADR 0070)

Every check that does not pass is normalized, written to the audit as
`local-power-verification-failure-NNN.json`, and put in front of the model as
`<verification:COMMAND_NAME>` evidence — including across a resume, so a
repair continuation starts knowing what it is repairing. Two prompt blocks
state the position explicitly:

- `VERIFICATION_STATE_JSON` — per command, one of `passing_for_current_code`,
  `failed_for_current_code`, `passed_earlier_but_the_code_has_changed_since`,
  or `never_run`.
- `OUTSTANDING_REQUIRED_COMMANDS_JSON` — the required commands that do not
  currently pass.

`finish` is refused, at most twice, while a required command has no result for
the current state and nothing has been edited since the last check. Making any
edit, or simply running that command, lifts the refusal — the model does not
have to succeed. A session that changed nothing is never held open.

### What it still cannot do

Widening the protocol is not widening authority. Every action is executed by the
harness against the disposable task worktree, and the model cannot:

- read, write, or delete anything matching `forbidden_paths` — which always
  includes `.apoapsis/**`, `.git/**`, `.env`, and `.env.*`, plus key and
  certificate material by default. A local override may widen this list; a
  validator refuses any list that drops those four.
- use an absolute path, a drive letter, `~`, or any `..` traversal, or reach
  outside the sandbox through a symlink.
- run anything but allowlisted program prefixes (`python -m unittest`,
  `python -m pytest`, `python -m compileall`, `pytest`, `npm test`, …). `git`,
  `curl`, `rm`, PowerShell, and `cmd` are denied by construction, and a command
  containing shell metacharacters is refused rather than reinterpreted.
- see credentials: the command environment is built from a short allowlist with
  anything key/token/secret-shaped removed.
- reach the network unless `allow_network = true`.
- mutate workflow state or the audit log.
- **complete the task.** `finish` ends the model's turns and nothing more.
  Apoapsis then computes the final diff and runs the configured verification;
  that result, not the model's summary, decides between a normal report package
  and Human Review.

### What you get back

A `local-power-review-package.json` in the task's audit directory containing the
harness-computed final diff, changed files, every command actually run, every
refused request, every change set proposed — applied or refused, with the
worktree digest the harness observed beside the one the model claimed, also
written individually as `local-power-change-set-NNN.json` — the full
transcript, verification results, and the model's own summary — explicitly labelled as a claim rather than a finding. The UI shows the
experimental warning before the run, live turn/command/changed-file/refusal
status during it, and offers an accept action only when verification passed.

Run its deterministic boundary tests with:

```bash
python -m unittest tests.test_local_power_session -v
```

## Planning comparison: monolithic versus plan-then-slices (ADR 0028)

A separate, deterministic comparison between doing a substantial, multi-part
task in one request and doing the exact same task through an approved,
fixed plan executed one slice at a time. Uses its own fixture
(`download-service-v2`, a three-slice extension of the `download-service`
scenario above with a real dependency) and never generates a plan itself --
the fixed plan must already be exported, imported, validated, and approved
against the project directory you point it at:

```bash
apoapsis eval-planning download-service-v2 \
  --plan-id PLAN-ABC123 --expected-plan-version 1 \
  --planned-project-root /path/to/an/already-approved/project \
  --planner-model "claude-opus-4-8-web"
```

This writes `planning-comparison.json`/`.md` with both conditions' outcomes,
per-slice results, resource totals, and whether the held-out cross-slice
oracle passed. Both conditions run under `STRICT` completion policy (a
documented departure from `apoapsis eval`'s lanes, which always force
`BASELINE`) so each slice's own inherited acceptance criterion gates it
independently. `--planner-model` is recorded for provenance only; this
command never calls a planner, and a manually-pasted subscription
session's planner tokens/cost are always recorded as unmeasured, never a
fabricated zero.

### Single-slice diagnostic probe (ADR 0029)

A minimal, evaluation-only companion to `eval-planning`: runs exactly one
already-approved plan slice once, varying only the agent-step prompt
condition or the coding model -- never both, and never the full
monolithic-versus-planned comparison. Built to isolate a repeatable D4b
finding (a live model making one edit and then looping on an identical,
uninformative `read_file` request instead of ever verifying) into a
single controlled variable at a time; see ADR 0029 and
`docs/evaluation/apoapsis-d4c-forensic-diagnosis-2026-07-19.md`.

```bash
# Probe 2: the unchanged production prompt plus one short, explicitly
# advisory (never action-forcing) note appended -- never a replacement.
apoapsis eval-planning-probe download-service-v2 \
  --plan-id PLAN-ABC123 --expected-plan-version 1 \
  --planned-project-root /path/to/an/already-approved/project \
  --slice-id SLICE-JOBS-001 \
  --prompt-condition progress_advisory

# Probe 3: the unchanged production prompt against a different,
# already-installed local coding model. Fails closed on three checks
# before any provider is built: the model must genuinely differ from the
# project's already-configured one, --authorize-alternate-model must
# exactly match --alternate-model, and the model must actually be
# installed at the configured Ollama endpoint. Never downloads a model.
apoapsis eval-planning-probe download-service-v2 \
  --plan-id PLAN-ABC123 --expected-plan-version 1 \
  --planned-project-root /path/to/an/already-approved/project \
  --slice-id SLICE-JOBS-001 \
  --prompt-condition production \
  --alternate-model qwen3-coder:30b --authorize-alternate-model qwen3-coder:30b
```

This writes `diagnostic-probe.json`/`.md` recording the exact prompt
condition and model identity used, plus a deterministic behavior summary
(whether `run_check`/`submit_for_verification` was ever invoked, the
first no-progress turn, and the longest identical-action streak) computed
only from the session's own persisted turn records. Like `eval-planning`,
it never generates or approves a plan itself, and never writes back to
your project's `.apoapsis/config.toml` -- the one setting change (an
alternate model) is applied in memory only for that one run. Unlike
`eval-planning`, there is no `--context-profile` flag here at all -- this
narrowly scoped command always inherits your project's baseline context
window unchanged, so it can never introduce a second, unrecorded variable
alongside the one it's isolating.

## Complete all-local agent flow

`apoapsis init` creates a 32K agent configuration for Laguna S 2.1 served by a
local `llama-server` OpenAI-compatible endpoint on `127.0.0.1:8000`:

```toml
[models.frontier]
provider = "openai_compatible"
base_url = "http://127.0.0.1:8000/v1"
model = "Laguna-S-2.1-UD-Q4_K_S"
api_key_env = "APOAPSIS_LOCAL_CODER_API_KEY"
timeout_seconds = 900
max_output_tokens = 8192
temperature = 0.0
context_window_tokens = 32768
think = false
specification_think = false

[models.frontier.pricing]
input_per_million_usd = 0
output_per_million_usd = 0
cached_input_per_million_usd = 0

[models.local_coder]
provider = "openai_compatible"
base_url = "http://127.0.0.1:8000/v1"
model = "Laguna-S-2.1-UD-Q4_K_S"
api_key_env = "APOAPSIS_LOCAL_CODER_API_KEY"
timeout_seconds = 900
max_output_tokens = 8192
temperature = 0.0
context_window_tokens = 32768
think = false

[models.local_coder.pricing]
input_per_million_usd = 0
output_per_million_usd = 0
cached_input_per_million_usd = 0

[execution]
mode = "agent"
route = "auto"

[execution.agent]
max_turns = 20
max_patch_attempts = 14
max_verification_runs = 7
max_search_results = 24
max_read_lines = 360
max_observation_chars = 72000
max_transmitted_observation_chars = 36000

[execution.frontier_agent]
max_turns = 14
max_patch_attempts = 9
max_verification_runs = 5
max_search_results = 20
max_read_lines = 240
max_observation_chars = 48000
max_transmitted_observation_chars = 24000

[models.local_research]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3.6:27b"
timeout_seconds = 600
max_output_tokens = 8192
temperature = 0.0
context_window_tokens = 32768

[context]
max_files = 24
max_excerpt_lines = 240
max_total_chars = 180000
max_import_depth = 2
```

The default coding endpoint is local and loopback-only in normal use. No fake API
key is needed for loopback OpenAI-compatible endpoints such as `llama-server`;
hosted OpenAI-compatible endpoints still require the configured credential
environment variable. `models.frontier` remains the backwards-compatible
specification/one-shot provider; `models.local_coder` is the first agent stage.
To let `START_APOAPSIS.cmd` launch Laguna for you, set one operator-owned
environment variable to the same command you would otherwise run by hand. For
example:

```bash
export APOAPSIS_LLAMA_SERVER_COMMAND='/home/arya/llama.cpp/build/bin/llama-server \
  -m /home/arya/models/laguna-q4s/UD-Q4_K_S/Laguna-S-2.1-UD-Q4_K_S-00001-of-00003.gguf \
  --parallel 1 \
  --ctx-size 32768 \
  --flash-attn on \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --fit on \
  --fit-target 512 \
  --load-mode none \
  --jinja \
  --reasoning off \
  --reasoning-budget 0 \
  --reasoning-format none \
  --threads 16 \
  --host 127.0.0.1 \
  --port 8000'
```

On Windows with a WSL-hosted build, point the variable at the explicit `wsl`
command that starts the same server. Apoapsis does not invent this command and
does not download the model; it only runs the operator-provided command when the
configured loopback endpoint is down.

The generated default is the `32k` working profile. A run can select a reproducible
comparison profile without editing the project configuration:

| Profile | Ollama window | Files | Lines per excerpt | Total excerpt characters |
| --- | ---: | ---: | ---: | ---: |
| `16k` | 16,384 | 10 | 100 | 24,000 |
| `32k` | 32,768 | 16 | 160 | 72,000 |
| `64k` | 65,536 | 24 | 240 | 180,000 |
| `128k` | 131,072 | 32 | 320 | 360,000 |
| `256k` | 262,144 | 40 | 400 | 600,000 |

```bash
apoapsis run "Add resumable downloads without changing the public API" --context-profile 64k
```

`64k`/`128k`/`256k` exist to be explicitly measured, not assumed safe because a
model or GPU happens to have the VRAM for them (ADR 0010) — `32k` is the Laguna
default. `apoapsis doctor` checks configured native Ollama windows against the
installed model's reported context length; for `llama-server`, confirm the
server was started with the matching `--ctx-size` before relying on a wider
profile. Every model call also writes a `ContextMeasurement` (model window,
file/excerpt limits, transmitted chars, estimated tokens, window
utilization, composition, and stable-versus-newly-introduced evidence) as
its own audit artifact, surfaced on the task report and in `apoapsis eval`'s
comparison output — so a profile's actual effect is something you can read,
not guess.

The deterministic compiler also expands changed Python symbols to one-hop AST
call sites and related tests, and centers post-failure excerpts on validated
traceback locations. Agent observation history remains complete in
`agent-turn-*.json`, while only a current compacted view (24,000 characters by
default) is retransmitted. `context-attribution.json` reports the conservative
fraction of transmitted evidence whose file was actually changed by the
accepted patch. Prompt builders place a byte-stable instruction prefix first;
actual provider cache benefit is still reported only from token/cache telemetry.

Profiles affect the native local-coding window and deterministic retrieval;
Research Mode retains its separately configured budget. Apoapsis records the active
window and generation settings in every frontier request package and the exact
retrieval limits in every context package.

For sampling comparisons, Coder-Next temperature can be changed explicitly:

```toml
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3-coder-next:q4_K_M"
temperature = 1.0
context_window_tokens = 65536
think = false
specification_think = false
```

Temperature is configurable for native Ollama and hosted providers and is
recorded in each request package. Zero remains the generated deterministic
sampling default; Coder-Next's published model settings recommend `1.0`.

Apoapsis displays the extracted Pydantic specification and waits for approval. The
`--yes` flag is available for controlled non-interactive evaluation. Approval
does not grant the model workflow authority: Apoapsis deterministically mediates
every search, read, patch, and configured check; records each request package;
and accepts completion only after full verification.

Use the retained one-shot baseline for a direct controlled comparison:

```bash
apoapsis run "Add resumable downloads without changing the public API" \
  --execution-mode one_shot --context-profile 64k
```

Agent mode is bounded by `[execution.agent]`. With no frontier coder configured,
an escalation request stops for human review. To enable automatic handoff, add
a separately authenticated provider:

```toml
[models.frontier_coder]
provider = "openai_compatible"
base_url = "https://provider.example/v1"
model = "frontier-coder"
api_key_env = "APOAPSIS_FRONTIER_CODER_API_KEY"
timeout_seconds = 900
max_output_tokens = 16384
temperature = 0.0

[models.frontier_coder.pricing]
input_per_million_usd = 0
output_per_million_usd = 0
cached_input_per_million_usd = 0
```

`route = "auto"` sends low, medium, and unclassified tasks local-first and
escalates only after the local stage stops. High-risk tasks also run local-first,
using Apoapsis's maximum finite local turns, patch attempts, verification runs,
search/read limits, and repository context sized to the configured model's
declared window; they escalate to frontier when configured. Critical-risk tasks
require an explicit human choice between the available local/frontier paths.
Routes can be
overridden with `--agent-route local_only`, `local_then_frontier`, or
`frontier_only`.

If AUTO routing selects a frontier or human path that is unavailable, the task
can stop before an agent runs. Human Review makes that explicit and offers
**Run locally** when a fresh local execution is safe to authorize; this is a
one-operation user decision, not a silent weakening of future routing policy.
When a frontier coder is configured, **Run with frontier** is offered alongside
it for an explicit fresh hosted execution.

Before the first frontier coding call, Apoapsis writes
`frontier-escalation-package.json` containing the approved task and constraints,
the exact current diff, complete local action history, verification commands and
normalized failures, provider identities, and the frontier context digest. The
frontier agent continues in the same isolated worktree with its own deterministic
budget. If it cannot verify the task, Apoapsis stops for human review.

Every task writes `.apoapsis/tasks/<task-id>/report.json`. `apoapsis inspect <task-id>`
returns the persisted state/events and embeds that report when present.

### Original report versus current evidence

`report.json` is written **once**, when the task first stops, and is never
updated afterwards. If you then continue the task, retry verification, or
apply a manual-frontier patch, the repair reaches `COMPLETE` while the report
on disk still describes the original stop. That is intentional -- the original
stop is an audit record -- but it means the report alone must not be read as
the task's current outcome.

Apoapsis therefore computes a separate **current-evidence projection** (ADR
0072) from persisted task state, the append-only event history, and the
operation artifact the deciding stage actually wrote. `apoapsis inspect`
returns it under `current_evidence` alongside the untouched `report`; the
Report page, the task list, the plan slice status, and `delivery.json` all
label the task from the projection.

The projection reports where its evidence came from:

| Field | Meaning |
| --- | --- |
| `outcome` | Current outcome, from workflow state. `null` while a task is mid-flight or rolled back. |
| `original_report_outcome` | What the preserved `report.json` says, kept beside the current outcome rather than replacing it. |
| `evidence_generation` | Which artifact family the current result came from: `original_report`, `verification_retry`, `manual_frontier_apply`, `local_stage_session`, or `frontier_stage_session`. |
| `evidence_sources` | Repository-relative path of that artifact. |
| `evidence_integrity` | `intact`, or `missing`/`malformed` when the artifact the event history points at cannot be read. |

If a task's deciding artifact is missing or malformed, the projection reports
empty verification results rather than falling back to the older report. This
is deliberate: substituting a superseded pass for unreadable current evidence
is exactly how a stale green result survives. **Preparing a finished project
refuses in that case** -- the slice is named in the error along with the
generation and the integrity problem, the plan stays `APPROVED`, and no ZIP or
`delivery.json` is written. Restore the task's audit directory, or re-run the
verification, and prepare delivery again.

The controlled download-service fixture and direct-versus-Apoapsis procedure are in
[`examples/download-service`](examples/download-service) and
[`docs/evaluation/direct-vs-apoapsis.md`](docs/evaluation/direct-vs-apoapsis.md). The first
measured local Qwen smoke results are in
[`docs/evaluation/local-qwen-smoke.md`](docs/evaluation/local-qwen-smoke.md).
The installed Coder-Next Q4 evaluation is in
[`docs/evaluation/qwen3-coder-next-smoke.md`](docs/evaluation/qwen3-coder-next-smoke.md).
The Crisis Atlas 64K Qwen-plus-Codex checkpoint trial and the isolated
unrestricted-Qwen CLI control are recorded separately in
[`docs/evaluation/crisis-atlas-64k-codex-frontier-trial-2026-07-30.md`](docs/evaluation/crisis-atlas-64k-codex-frontier-trial-2026-07-30.md)
and
[`docs/evaluation/crisis-atlas-qwen-cli-control-2026-07-30.md`](docs/evaluation/crisis-atlas-qwen-cli-control-2026-07-30.md).
The control built substantially more coherent code than raw sliced Qwen, but
used about eight times as many input tokens and still falsely claimed full
acceptance while browser filtering was broken.
The first bounded-agent run to complete the controlled fixture used ten agent
turns and three verification runs; all three tests passed with one source file
changed. The earlier one-shot failures remain documented as the comparison
baseline rather than being discarded. The first live `--lane local-strict`
evaluation (3 attempts, 0 completions, and a genuine harness gap it
surfaced) is in
[`docs/evaluation/apoapsis-strict-live-evaluation-2026-07-18.md`](docs/evaluation/apoapsis-strict-live-evaluation-2026-07-18.md).
A second round after fixing that gap (3 more attempts, 1 genuine
completion independently confirmed by the held-out oracle) is in
[`docs/evaluation/apoapsis-strict-live-evaluation-2026-07-19.md`](docs/evaluation/apoapsis-strict-live-evaluation-2026-07-19.md).

## Research Mode

Research has two bounded entry points. For a coding task it runs only after
specification approval. For larger-change planning it runs only after the user
approves the discovery `IdeaBrief` and before the frontier planning handoff.
In `auto` mode, deterministic rules activate it for research, precedent,
product/UX, public API, CLI, report, dashboard, and similar judgment-heavy work,
while localized mechanical work is skipped. Explicit modes are also available:

```bash
apoapsis run "Improve the task report UX" --research auto
apoapsis run "Add resumable downloads" --research github
apoapsis run "Why do users dislike coding-agent logs?" --research community
apoapsis run "Research and improve the onboarding report" --research full
```

Configure `[models.local_research]` in `.apoapsis/config.toml` with a locally
available Ollama model. GitHub and configured official documentation are enabled
by default. Reddit remains disabled until its approved API credentials and
applicable terms are configured.

Official documentation research is direct-URL-only by default and only ever
reaches domains explicitly listed in **both**
`[research.sources.official_docs].allowed_domains` and
`[research.security].allow_domains` — add every vendor you need (for example
`developers.google.com`, `www.twilio.com`, `developer.vonage.com`) to both
lists, or that research question is reported as an unusable query rather than
silently producing nothing. A harness-owned seam for real official-document
search exists (`OfficialDocumentSearchProvider`, ADR 0055), and Tavily
is the one concrete, owner-authorized provider implemented behind it (ADR
0056; Brave Search was the initial pick but was dropped after its free tier
turned out to require a credit card and metered billing) — set
`search_provider = "tavily"`, add `api.tavily.com` to
`[research.security].allow_domains`, and provide an API key via
`TAVILY_API_KEY` (or your own `search_credentials_env` name) to enable
it. This integration has deterministic fake-fetcher test coverage only; no
live call to the real Tavily API has been made. Any other `search_provider`
value still fails clearly rather than guessing at another vendor. When a research task
retrieves sources but genuinely finds nothing relevant, Apoapsis runs exactly
one bounded, audited recovery pass over the same sources before reporting a
classified, actionable failure (for example: "5 sources were retrieved and
all 5 produced no relevant findings") instead of a generic provenance error.

Research can also be run independently for an already approved task:

```bash
apoapsis research TASK-ABC123 --mode full
apoapsis research inspect TASK-ABC123
apoapsis research refresh TASK-ABC123 --mode full
apoapsis research cache inspect
apoapsis research cache clear
```

The deterministic harness owns URLs, network access, budgets, provenance,
license classification, caching, and audit writes. The local model receives no
tools and may only propose structured questions, rankings, evidence, and
synthesis. Retrieved text is sanitized and marked `UNTRUSTED_EXTERNAL_CONTENT`.
The frontier model receives only the compact research brief and evidence IDs,
never full threads or fetched pages. External sources remain advisory: only the
approved task, repository policy, patch validation, and verification authorize
a change.

Research artifacts are written below `.apoapsis/tasks/<task-id>/research/`;
planning research uses a deterministic discovery-scoped task id and stores the
exact audit path on the discovery session. The final `report.json` includes the
selected mode, patterns, evidence IDs, local-model calls, tokens, latency, and
whether the brief influenced the proposed plan.

## Verification configuration

`apoapsis init` creates `.apoapsis/config.toml`. Commands are argument arrays, not shell
snippets:

```toml
[verification]
stop_on_failure = false
output_limit_chars = 100000

[[verification.commands]]
name = "unit-tests"
category = "tests"
argv = ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
timeout_seconds = 120
required = true
```

By default (`backend = "host"`, implicit) commands run directly on the host —
deterministic, but not a security sandbox. Opt into the Docker-based sandbox
(ADR 0009) for network denial, CPU/memory/process limits, and a throwaway
worktree copy instead of the real one:

```toml
[verification.backend]
backend = "docker"

[verification.backend.docker]
image = "python:3.12-slim"
image_digest = "sha256:<pin this — see below>"
cpu_limit = 2.0
memory_limit_mb = 2048
pids_limit = 256
tmpfs_size_mb = 256
wall_clock_timeout_seconds = 300
```

Apoapsis never pulls an image automatically. Pull and pin one yourself, then
run `apoapsis doctor` to validate the whole preflight (CLI, engine, Linux
containers, image presence, a real minimal self-test) before relying on it.
Doctor's message names exactly what's wrong -- Docker CLI missing, engine/
Desktop unreachable, the image never pulled at all, or the image present
locally but at a different digest than pinned (re-pin `image_digest`, don't
just re-pull) -- and never pulls or retags anything itself:

```bash
docker pull python:3.12-slim
docker image inspect --format '{{index .RepoDigests 0}}' python:3.12-slim
apoapsis doctor
```

The Docker backend materially improves isolation but is not a defense
against container-runtime or kernel vulnerabilities; see ADR 0009's threat
model for exactly what it does and does not cover.

### The verification environment (ADR 0063)

Commands run with a restricted environment: the keys in
`environment_allowlist` copied from your shell, plus a small set Apoapsis
imposes itself. Today that set is `PYTHONDONTWRITEBYTECODE=1`, so a Python
check does not scatter `__pycache__` into the worktree it is measuring — a
verification run should not change the thing it is reporting on. If a command
genuinely needs bytecode written, set it explicitly on that command:

```toml
[[verification.commands]]
name = "unit-tests"
category = "tests"
argv = ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]

[verification.commands.environment]
PYTHONDONTWRITEBYTECODE = "0"
```

Per-command `environment` is the only thing that overrides a harness default.

### Your project needs at least one commit (ADR 0064)

`apoapsis init` works in a brand-new repository, but almost everything after
it — context compilation, worktree isolation, fingerprints, planning packages,
audit records — is anchored to a base commit. In a repository with no commits,
Apoapsis refuses up front and tells you so:

```text
repository <path> has no commits yet, so there is no base commit to anchor
planning to. Make one commit (for example `git add -A` then
`git commit -m "initial commit"`) and retry.
```

Make that commit and retry. Apoapsis will not create it for you — writing
history in your repository is your decision, not the harness's.

### What "changed files" means in a report (ADR 0063)

`files_changed` in a task report, and `changed_files` in a Local Power review
package, answer exactly one question: **what did the model change?** Build and
test byproducts — `__pycache__/`, `*.pyc`, `.pytest_cache/`, coverage and
build caches, `node_modules/` — are reported separately as
`generated_byproducts` rather than mixed in, so the list you review is the
list you can act on.

Two things worth knowing about how that split is decided:

- It does **not** read your `.gitignore`. The classification is by path name,
  so the report is correct even in a repository that was never run through
  `apoapsis init` and has no ignore rules at all.
- A file already tracked in Git is always treated as your work, whatever it is
  named. If you deliberately committed something under `vendor/node_modules/`,
  a change to it shows up for review.

Nothing is silently discarded. The audit trail still records every path in the
worktree that changed, including byproducts.

## Acceptance coverage and the completion policy (ADR 0015, 0016, 0017, 0018)

Configured verification passing is the default completion gate.
`apoapsis init` writes `completion_policy = "baseline"`; every required
command must pass. Its generated command is **never** marked
`acceptance = true` automatically -- acceptance designation remains an
explicit decision for projects that opt into strict criterion-by-criterion
proof:

```toml
[[verification.commands]]
name = "unit-tests"
category = "tests"
description = "Runs the project's full test suite."
argv = ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
timeout_seconds = 120
required = true
acceptance = false   # opt in explicitly once you decide this is real proof

[execution]
completion_policy = "baseline"   # apoapsis init's default
```

To use strict completion, set `completion_policy = "strict"`, set
`acceptance = true` on a command yourself when you're ready, then map
`AcceptanceCriterion.verification_method` to its name (or let a model
propose that mapping -- see below). In strict mode, `apoapsis doctor` and
the UI overview warn when no acceptance command is designated, and tasks
with active unmapped criteria stop at `HUMAN_REVIEW_REQUIRED`.

Specification extraction receives a deterministic **acceptance-command
catalog** built fresh from `[verification.commands]` on every call (name,
category, `description`, and whether each is `acceptance_designated`); an
extracted `AcceptanceCriterion.verification_method` may name only a catalog
entry or stay `null` -- extraction rejects anything else, so a model can
propose a mapping but never invent one. The user still approves the whole
specification, mapping included, before it takes effect; the local UI's
specification view shows each criterion's proposed check.

Under `completion_policy = "strict"`, `COMPLETE` additionally requires
every active acceptance criterion to be computed as **Proven** -- mapped to
a command that is both configured and `acceptance = true`, and that has
actually **passed for the current worktree state**. "Current worktree
state" is a single shared fingerprint (ADR 0017): HEAD identity, the
canonical tracked diff, and every permitted untracked file's exact content
hash -- so a brand-new file a patch created without `git add`ing it (the
normal result of applying a diff) changes the fingerprint exactly like a
tracked edit does. A pass recorded before the worktree changed, tracked or
untracked, does not count: never-executed, failed, and passed are three
distinct, non-stale states, always scoped to the exact current fingerprint.
Unmapped, misconfigured, failing, or stale mappings stay
**Unproven**/**Failed** regardless of what a model claims; only the harness
computes and grants that status. A gap returns control to the bounded agent
with evidence (same budget, same loop) or, in one-shot mode, stops at
`HUMAN_REVIEW_REQUIRED` rather than spending its single repair attempt on
it. `inspect_diff` shows a model the same untracked-file state being
fingerprinted, as a bounded synthetic diff; untracked binary content and
symlink targets are never rendered, only a path-only placeholder, matching
existing binary/symlink policy elsewhere.

A failing acceptance-designated command always produces real, informative
failure evidence and an accurate turn summary (ADR 0018) -- even though it
is `required = false` and correctly never fails ordinary aggregate
verification or becomes a required development gate. Before this, a
failing optional acceptance command could be misreported as
`"deterministic verification passed"`, since the summary/evidence logic
only ever checked `required`.

If specification extraction's first response fails schema/Pydantic/
verbatim/catalog validation, the harness makes exactly one bounded
correction call containing the exact validation errors, the model's own
prior response, and the same schema/catalog/rules (ADR 0018) -- never a
second attempt, never coerced or nulled fields. If the correction also
fails, the task stops deterministically at `FAILED`.

`apoapsis eval` always explicitly selects `completion_policy = "baseline"`
for every lane, regardless of what a real project's configuration selects,
so false-success measurement against the held-out oracle stays comparable
across runs -- this is recorded on every persisted report and as a
"Completion Policy" column in the comparison Markdown, not silent
inheritance. The Pydantic configuration default (for code that builds a
config directly, bypassing `apoapsis init`) remains `"baseline"`.

## What a passing contract actually proves (ADR 0069)

A contract can pass completely and still say almost nothing. Live task
`TASK-33E0EB6476C4` built a browser application whose seven configured checks
all passed and which did not run: `app.js` attached listeners to four element
ids `index.html` never defined, and `styles.css` styled five classes the markup
never carried. Every check asked whether a fragment existed. None asked whether
the fragments referred to one another.

Apoapsis now grades the configured contract's **evidence level** — computed
from structure only, never from guessing what a command does:

| Level | Meaning |
| --- | --- |
| `none` | Nothing configured, or nothing required. A success would rest on no deterministic evidence. |
| `development_only` | Required commands exist; none carries `acceptance = true`. Passing means the commands exited zero, nothing more. |
| `acceptance_designated` | Acceptance commands exist but do not cover every active criterion. |
| `criterion_mapped` | Every active criterion maps to an owner-approved acceptance command. |

You see it in `apoapsis doctor` before spending anything, on the start-coding
confirmation, in `report.json` (`verification_contract`), in the Local Power
review package, and beside the outcome in the UI. Below `criterion_mapped`, a
`COMPLETE` carries the qualification in its recorded stop reason.

**This never blocks a run.** Baseline completion is unchanged, and a blank
repository with no product yet is a legitimate state. What changed is that
`COMPLETE` no longer arrives unqualified from a contract that cannot support
it. Apoapsis deliberately does not inspect a command's `argv` to guess whether
it "really" tests the product — a command that greps a file and one that drives
a browser look identical from here, and a confident wrong answer would be the
same kind of error it is trying to surface.

### A real check for browser products

If your product is dependency-free HTML/CSS/JavaScript, configure this and the
above failure becomes impossible to reach:

```toml
[[verification.commands]]
name = "web-product-integrity"
category = "acceptance"
description = "Cross-references the product's HTML, CSS, and JavaScript."
argv = ["python", "-m", "apoapsis", "verify-web-product",
        "--forbid-external-resources", "--treat-warnings-as-errors"]
timeout_seconds = 60
required = true
acceptance = true
```

Or run it directly:

```bash
apoapsis verify-web-product --root ./site --entry index.html
```

It checks that every id and class a script looks up is provided by the markup
or created by a script; that every CSS rule can match something; that ids are
unique and top-level function names do not collide; that referenced local files
exist; and, optionally, that the product depends on no third-party origin.
Selectors too complex to analyze with confidence are counted and reported as
unchecked rather than assumed fine. Use `--optional-element NAME` (repeatable)
for an element the product genuinely creates at runtime — an explicit statement
rather than a silent exception.

It is stdlib-only, offline, and deterministic, and exits non-zero on error
findings. Dead style rules are warnings by default; `--treat-warnings-as-errors`
promotes them.

#### Two different request policies

These are separate options because they are separate requirements (ADR 0073):

| Option | Means |
| --- | --- |
| `--forbid-external-resources` | No third-party origin. Cross-origin URLs, protocol-relative URLs, WebSockets, absolute loopback URLs like `http://localhost:8000/x`, and external `<script src>`/`<link href>` assets are errors. **A same-origin request such as `fetch('/incidents')` is allowed** — talking to the server that served the page is not an external dependency. |
| `--forbid-runtime-network-apis` | No runtime request of any kind, same-origin included: `fetch`, `XMLHttpRequest`, `WebSocket`, `EventSource`, `sendBeacon`. |

Before ADR 0073 the first option did both jobs, which meant a product
forbidden to depend on a CDN was equally forbidden to call its own backend.
A plan requiring UI-to-API integration could then only go green by deleting
the integration. **If you were relying on `--forbid-external-resources` as a
blanket ban on requests, add `--forbid-runtime-network-apis` to keep that
behaviour.**

A request whose target is computed at runtime (`fetch(`${base}/x`)`) is
reported as unproven, not as compliant: the check cannot show it stays on
your origin. Under `--forbid-external-resources` that is a warning.

#### What the check actually examined

Every run prints its evidence counts and a one-sentence ceiling:

```text
web product check: 1 document(s), 1 script(s), 1 stylesheet(s), 2 element reference(s) cross-checked
  evidence: 2 element reference(s), 2 CSS selector(s), 2 local asset(s) resolved,
            1 same-origin API reference(s), 0 cross-origin API reference(s), 0 reference(s) unproven
  ceiling: Static cross-reference only: ... end-to-end browser behavior was NOT measured;
           nothing here executes the product.
```

A check can legitimately pass having cross-referenced *nothing* — a product
whose markup is driven entirely by data attributes gives it nothing to
resolve. That is a valid static result and a nearly worthless one, so it is
reported as a warning and its ceiling says so plainly.
`--treat-warnings-as-errors` turns it into a failure.

When an acceptance criterion is about persistence, browser/API integration,
or interaction behavior, `apoapsis doctor` and the contract report now raise
a warning naming the criterion: configure a project-specific acceptance
command that exercises the behavior, because no check that never executes the
product can prove it.

`--behavior` asks for real in-browser verification. **No browser probe provider
is implemented**, so it fails rather than passing. That is deliberate: a
requested behavioral check that quietly degrades into a pass would recreate the
exact problem this feature exists to prevent.

## Repository layout

```text
src/apoapsis/
  agent/            bounded typed inspect-edit-test sessions
  cli/              CLI entry points
  context/          provenance-aware evidence schemas; deterministic context measurement
  execution/        managed Git worktrees; host/Docker execution backends
  models/           provider-neutral model request/response schemas
  repository/       deterministic Git inspection
  specification/    task and constraint schemas
  verification/     command runner and results
  workflow/         persisted state machine and events
  audit/            reproducible per-call and per-task artifacts
  patches/          unified-diff parsing, policy, and application
  reporting/        aggregate outcome and usage reports
  ui/               offline local operator interface and protected application API
  doctor.py         read-only environment/credential/provider preflight
  evaluation/       fixture-isolated lane runner and comparison report
tests/               deterministic unit and integration tests
docs/adr/            architectural decisions
HANDOFF.md           living architecture and project handoff
AGENTS.md            mandatory upkeep instructions for coding models
```
