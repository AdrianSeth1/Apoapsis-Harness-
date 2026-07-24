# Apoapsis Harness: Current Architecture and Handoff

Read this before changing the project. This document is the canonical map of
the system as it exists now. ADRs in `docs/adr/` preserve decision history,
dated files in `docs/evaluation/` preserve live evidence, `README.md` is the
user guide, and `NEXT_STEPS.md` contains only active priorities. Do not copy
historical narratives back into this file.

When documentation and code differ, implementation plus deterministic tests are
operational truth and the documentation must be corrected in the same change.

## Snapshot

| Item | Current value |
| --- | --- |
| Last verified | Through ADR 0037 on 2026-07-21, 61 focused tests and the full 722-test deterministic suite passed with 10 expected skips, plus compileall and `git diff --check`. ADRs 0038-0056 are unverified because the owner explicitly requested no test execution (ADR 0049 and later, including 0051-0056, are also blocked by the Python 3.10 environment on the change workspace -- `apoapsis.config` requires Python 3.11+ for `tomllib`); run their documented commands before commit. ADR 0050's `tests/test_native_shell_spike.py`, ADR 0051/0052/0053's `tests/test_desktop_registry.py`/`tests/test_desktop_import.py`/`tests/test_desktop_reference.py`/`tests/test_desktop_home.py`/`tests/test_desktop_ipc_server.py`, ADR 0054's `tests/test_desktop_authority_boundary.py`, and ADR 0055/0056's new/updated tests in `tests/test_research_units.py`/`tests/test_research_integration.py` have likewise not been run in the authoring session at the owner's explicit request; `python -m compileall -q` was run and passed for every changed research/discovery/config/test file, but run the actual test commands before treating any of this as verified. |
| Version/state | Committed `1.0` through ADR 0034. ADR 0035 guided workflows/planning research and ADR 0036 hardening/compaction are working-tree changes. Check `git status` for exact local state. |
| Branch | `main` |
| Preserved tag | `substrate-v0.1` at `4c2e735`; never move or delete it. |
| Live local coding | Qwen3-Coder-Next Q4 has completed controlled tasks, but reliability is not established. A six-run planning comparison reached 0/6; two later single-slice probes both completed. The contrast remains unexplained. |
| Live hosted coding | Not run. Hosted paths have deterministic fake-provider coverage only. |
| Live browser | Task intake/execution, review, plans/slices, discovery/manual frontier, launcher, and guided-workflow surfaces have each been exercised in the real loopback UI. See ADRs 0023-0035 and dated evaluation records. |
| Live Docker | The pinned `python:3.12-slim` sandbox success path and isolation checks passed on 2026-07-20. See `docs/evaluation/apoapsis-d5a-live-docker-evidence-2026-07-20.md`. |

Never turn fake-provider coverage into a live-provider claim. Never describe
working-tree changes as a committed release.

## Product thesis

Apoapsis is a local-first, auditable control plane for verified AI coding. It
makes bounded local or hosted models useful by giving them typed opportunities
to inspect, propose edits, and request configured checks. Apoapsis—not a
model—owns context selection, action execution, patch validation, retry ceilings,
workflow transitions, verification, completion, and audit records.

The intended primary flow is:

```text
request -> structured specification -> user approval -> deterministic context
-> deterministic route -> bounded coding proposals -> patch policy
-> configured verification -> COMPLETE or HUMAN_REVIEW_REQUIRED
```

Larger work may first pass through discovery, optional planning research, a
frontier/manual architecture plan, plan validation and approval, and then one
explicitly selected slice at a time.

## Non-negotiable authority boundary

| Capability | Authority |
| --- | --- |
| Interpret a request or propose a plan | Model proposes typed data; schemas and user approval govern acceptance. |
| Preserve hard constraints | Deterministic validation retains exact user source text. |
| Select repository context | Context compiler. |
| Choose local/frontier/human route | Deterministic routing from risk and configuration. |
| Read/search/edit/request checks | Model requests one typed action; Apoapsis validates and performs it. |
| Access external research | Restricted source adapters fetch allowlisted sources; models receive sanitized evidence only. |
| Apply a patch | Unified-diff parser, policy validator, and Git applier. |
| Decide pass/fail | Verification runner. |
| Retry, escalate, or complete | Fixed controller rules and configured ceilings. |
| Record audit history | Append-only harness stores. |

Models never receive direct shell, filesystem, Git, network, credential,
workflow-transition, retry-limit, verification, completion, or audit authority.
Changing this boundary requires an explicit ADR before implementation.

## Current architecture

### Entry points and configuration

- `src/apoapsis/cli/app.py` owns the CLI, default project configuration, and
  command dispatch.
- `src/apoapsis/config.py` contains strict Pydantic configuration. Unknown keys
  fail closed.
- Fresh projects default to baseline completion: required verification remains
  mandatory, while strict per-criterion acceptance mapping is opt-in. Patch
  changed-path accounting expands untracked directories to individual files,
  and verification/acceptance Human Review stops can return to a bounded local
  repair continuation (ADR 0042).
- Plan validation and failed-verification repair are UI-first actions: **Verify
  plan** persists the deterministic validation result, and **Repair and verify**
  submits the existing bounded local review continuation (ADR 0043).
- A completed continuation that returns to Human Review renders as **Repair
  incomplete**; freshest failed verification keeps the repair action available.
  Test-authoring guidance requires concrete mock interfaces and isolated
  filesystem effects plus ignore rules for credential/token files; identical
  replacements are rejected clearly, and changed paths enumerate untracked files
  individually. Terminal repair polling opens a
  completed task's report instead of refetching a now-invalid review case (ADR
  0044).
- Turn-budget exhaustion triggers one harness-owned final full verification only
  when current edits are newer than the recorded command results and verification
  budget remains. Pass/fail, acceptance, completion, and audit authority remain
  entirely deterministic (ADR 0045).
- Plan-slice tasks retain the complete approved work brief, interfaces,
  exclusions, assumptions, stop conditions, and advisory paths/symbols as
  traceable context. Older slice repairs recover that context from their exact
  approved package. Repeated unchanged diff/file observations are rejected, and
  three consecutive violations stop early as no progress (ADR 0046).
- Every Human Review case with an eligible local continuation presents the same
  **Repair and verify** action, including budget-exhausted implementation stops;
  the underlying service and authorization checks are unchanged (ADR 0046).
- A deterministic routing stop that occurred before any worktree or agent session
  offers **Run locally**. Explicit confirmation creates a fresh normal execution
  with an operation-scoped `local_only` route override; project configuration is
  unchanged, and execution authorization, isolation, patch policy, verification,
  reporting, and audit remain mandatory (ADR 0047).
- AUTO high-risk execution uses a maximum finite local profile first and
  escalates to frontier when configured. Critical risk still requires an explicit
  choice; routing review offers fresh local or frontier execution as available.
  Authorization packages record the effective profile, and local continuations
  retain it (ADR 0048).
- Manual frontier repair packages include cloud-safe repository excerpts, full
  verification evidence, prior agent sessions, and the exact approved slice
  package when present (ADR 0048).
- `OPEN_APOAPSIS.cmd` opens one explicitly selected initialized Git project.
  `START_APOAPSIS.cmd`/`STOP_APOAPSIS.cmd` manage only configured loopback
  Ollama model memory and leave the shared service running.
- `.apoapsis/` is runtime state and must be Git-ignored. Initialization never
  installs software, downloads models, or creates a repository.

### Specification and workflow persistence

- `specification/` turns natural language into a typed proposal and enforces
  exact hard-constraint provenance.
- `workflow/engine.py` is the SQLite task/event state machine.
- `workflow/vertical_slice.py` is the primary execution controller.
- Browser and CLI paths call the same services; browser code does not infer
  authoritative state or construct provider/shell actions.

Core task states are persisted and optimistic-versioned. Only valid state
transitions may append events. A model response is never itself a transition.

### Durable operations

Long-running intake, execution, review, discovery, and planning-research work is
represented by durable SQLite operation records and background workers.
Operations use leases and heartbeats, re-read authoritative state at execution
time, and fail terminally on stale versions, changed repository state, lost
leases, or authorization drift.

Execution authorization captures task/version, repository HEAD and fingerprint,
effective config, model identities/roles, budgets, policy, verification catalog,
and hashes before provider construction or worktree mutation.

### Repository context and isolation

- The context compiler deterministically selects bounded files/excerpts and
  records attribution plus measurements.
- Secret-like paths and `.apoapsis/**` are excluded from cloud transmission.
- Agent inspection is read-only and bounded. Repository search has a pure-Python
  fallback when ripgrep cannot launch.
- Execution requires a clean parent repository, then creates an isolated Git
  worktree. Apoapsis never stashes, resets, commits, merges, or discards user
  work automatically.
- Session-patched compile-time excerpts are labeled stale in transmitted context
  when a fresh worktree version also exists.

### Bounded coding protocol

`agent/` accepts exactly one typed model action per turn:

- `search_repository`
- `read_file`
- `inspect_diff`
- `propose_patch`
- `replace_text`
- `run_check`
- `submit_for_verification`
- `request_escalation`

The loop has separate turn, patch, verification, search/read, observation, and
transmission ceilings. Defaults are 20 local turns with 14 patch attempts and
14 frontier turns with 9 patch attempts (ADR 0049); the slice
`max_criteria_per_slice` ceiling (`[architect.ceilings]` in
`.apoapsis/config.toml`) is 20, paired with a `max_work_brief_chars` of 3,500
so the larger work brief stays consistent with the larger criterion budget.
Raising a ceiling changes configuration, not model authority.

Patch attempts are incremental against the current worktree. Dependency, test,
verification-config, binary, secret, metadata, and out-of-root changes are
governed by `PatchPolicyConfig`. New configurations allow non-deleting test-file
changes and dependency-manifest edits by default so from-scratch work can create
its verification suite and declare required libraries; owners may explicitly
disable either. Test deletion and verification-config changes remain forbidden.
Policy decisions and
rebased applied patches are audited.

For a single-hunk text new-file diff from `/dev/null`, the parser
deterministically restores missing outer addition markers and recomputes the
new-side hunk count before policy validation. The original and normalized forms
are audited. Existing-file edits and other diff shapes remain strict. Agent
prompts receive the effective dependency/test edit flags so instructions agree
with enforced policy.

Known-impossible verification contracts fail before model spend. Currently this
includes required Python unittest discovery from a missing start directory when
test edits are forbidden. CLI and browser submissions surface the actionable
preflight error without creating an execution operation; the browser handler
returns a structured conflict response rather than dropping the connection.
Ordinary failing tests still become repair evidence; Apoapsis does not guess
that they are impossible.

When test edits are allowed, a missing required unittest discovery directory is
instead a live implementation obligation transmitted on every agent turn. After
the matching real verification failure, escalation for that missing scaffold is
rejected and audited so the model must propose meaningful tests within its existing
budgets. Other escalation paths and all verification authority remain unchanged.

### Verification and completion

Only configured command names may run. Commands execute through the configured
host or Docker backend with bounded time/output and a restricted environment.
Failure normalization records root errors and useful locations as repair
evidence. Identical verification on an unchanged worktree is rejected.

Python dependency bootstrap is harness-selected from `requirements*.txt` or
`pyproject.toml` and runs before configured checks by default. Pip installs into a
task-scoped target, which is added to `PYTHONPATH`; package build/install scripts
are explicitly allowed. The bounded installer result is a required audited
verification command. Models still cannot submit raw install commands. Host mode
therefore executes model-influenced package code without isolation; prefer Docker
when that risk is unacceptable.

Under `baseline` (the initialized-project default), all required checks passing
is enough for completion. Under `strict`, every active acceptance criterion
must additionally map to an explicitly owner-designated acceptance command and
be deterministically proven on the current worktree fingerprint. Acceptance is
never auto-designated. A model saying “done” has no effect.

Held-out evaluation oracles are separate from development verification and
never become repair context.

### Routing, providers, and spend

- `models.frontier` remains the specification/legacy provider.
- `models.local_coder` is the local coding role.
- `models.frontier_coder` is optional and separately authorized.
- `models.local_research` is a tool-less local synthesis/extraction role.
- Provider calls pass through instrumentation for tokens, latency, cache use,
  model/role identity, and configured cost estimates.

Hosted calls require explicit provider configuration. Evaluation lanes that can
use hosted models also require an aggregate maximum hosted-spend ceiling and
fail before any call if the pessimistic allowance exceeds it.

### Research Mode

`research/` is advisory and quarantined. The model proposes a typed plan;
Apoapsis validates allowed sources and budgets. Restricted GitHub, official-doc,
and optional Reddit adapters perform network access. Content is fetched with
domain/content/size/redirect/time limits, sanitized for prompt injection,
license-classified, provenance-bound, cached, and audited before a tool-less
local model extracts evidence or synthesizes patterns.

Candidate capacity is distributed across planned queries. One available source
may fill the fetch budget; diversity limits apply when multiple sources exist,
and, since ADR 0055, a per-research-question cap in `SourceRanker` also
prevents one broad query from consuming the entire fetch allowance meant to
be shared across every viable research question. Sources with no extracted
findings appear in `rejected-evidence.jsonl`.

ADR 0055 fixes a reproduced failure (operation
`DISCOP-796622810B804FE59E87536D`) where an empty `evidence.jsonl` always
raised the same generic "no provenance-valid research evidence remained"
error regardless of cause. `ResearchEngineError` now carries a
`ResearchFailureReason` (no source candidates; a planned source unusable for
its adapter; sources retrieved but nothing relevant extracted; findings
rejected by provenance validation; insufficient source diversity) and a
structured `detail`; the discovery operation service appends a
reason-specific recommended operator action to the persisted failure.
Every planned query is checked for feasibility before retrieval --
concretely, an `official_docs` query with no URLs and no configured search
provider, or whose URLs are all outside `allowed_domains`, is recorded in a
new `unusable-queries.jsonl` audit file and excluded rather than silently
contributing nothing; if literally no query is viable, research fails fast
with that reason instead of continuing on an unrelated adapter. When
retrieval produces sources but the first extraction pass finds nothing
relevant, the engine runs exactly one bounded, audited recovery pass over
the same retrieved sources (no new fetch, no larger budget, never a second
round) before giving up; `recovery.json` always records whether it ran.
`OfficialDocumentationSource` gained an optional
`OfficialDocumentSearchProvider` seam (`sources/search_provider.py`) for
real official-document discovery -- query proposal, then a deterministic
search provider, then domain filtering, then the existing ranker, then the
existing restricted fetcher. ADR 0056 records the owner's explicit
authorization of Tavily as the one concrete provider implemented behind
that seam (`TavilyOfficialDocumentSearchProvider`, `sources/tavily.py`;
Brave Search was the initial pick but was dropped after its free tier
turned out to require a credit card and metered billing, unlike Tavily's
no-card free tier): `search_provider = "tavily"` plus
`search_credentials_env` (default `TAVILY_API_KEY`) enables it, and
`api.tavily.com` must be added to `[research.security].allow_domains`. Any
other provider name still fails clearly (ADR 0055) rather than guessing at
Bing/Brave/Serper/etc. Direct-URL official-doc research is unchanged and
still needs no provider (`search_provider = "none"` remains the default).
The Tavily integration has deterministic fake-fetcher test coverage only --
no live call to the real Tavily API has been made or verified in this
session; no API key was available.

Research never writes project files, executes downloaded code, sees project
secrets, approves a plan, creates a coding task, or authorizes a slice. Coding
agents do not receive general internet access.

### Discovery and Architect Mode

- `discovery/` supports bounded local clarification questions, verbatim user
  answers, one typed `IdeaBrief`, explicit user approval, optional research,
  and an immutable frontier planning package.
- Harmless bullet/case/whitespace noise in a proposed constraint quote is
  resolved back to the exact characters from the user's idea/answer; paraphrase
  still fails.
- Frontier planning may use an explicitly configured API with spend controls or
  a manual ChatGPT/Claude subscription export/import. Subscription sites are
  never automated.
- `architect/` validates a typed plan, verification names, constraints,
  dependencies, paths, and ceilings before explicit approval.
- A plan never executes automatically. One approved, dependency-ready slice is
  packaged and approved at a time. Its derived task preserves the full approved
  slice execution contract rather than only the objective and inherited
  constraints. Completion does not commit or merge; the operator does that in
  normal Git before dependent slices become ready.
- After every slice is COMPLETE, **Prepare finished project** checkpoints the
  final integrated task branch, writes a tracked-source ZIP with a usage guide,
  records the plan as EXECUTED, and emits a whole-project frontier-review handoff.
  It never moves or merges the user's checked-out branch (ADR 0048).

### Human review and manual frontier repair

Budget exhaustion, unavailable escalation, policy stops, provider failures, and
verification/acceptance gaps become deterministic Human Review cases. Review
actions are computed from persisted state. A continuation requires explicit
authorization and has additive bounded budgets recorded in its package.

A pre-agent deterministic routing stop is not a continuation. **Run locally** is
an explicit user authorization for one fresh local execution and is only eligible
while no worktree or local session exists. A failed start returns to the same
routing-review class so the operator can inspect or retry without an unknown-state
dead end.

Manual frontier repair exports one hash-bound Markdown package, imports a typed
response, requires explicit approval, applies through normal patch policy, runs
normal verification, and records subscription usage as unmeasured.

### Local operator interface

`ui/application.py` is the server-side authority boundary and
`ui/static/app.js` is a state renderer/action client. The loopback API uses a
capability token. The UI covers Home, New Task, specification approval, task
control/changes/review/report, Plans and slices, Discovery, Evaluations, and
Models & environment.

The interface must distinguish user authority, model proposals, control-plane
actions, repository evidence, and deterministic results. Missing measurements
say `Unmeasured`, never zero. Detail-route errors must clear stale prior content.

### Native desktop shell (spike only, ADR 0050)

ADR 0050 supersedes ADR 0034's native-wrapper deferral and adopts Tauri 2 as
the target desktop shell, with the existing Python `ui/server.py` application
unchanged underneath it. Only Phase 1 (a disposable technical spike) is built
so far: `spikes/native-shell-tauri/` contains a `backend_entry.py` child
process entry point (spawns the existing, unmodified
`apoapsis.ui.server.create_ui_server`) and a Tauri 2 host (`src-tauri/`)
written to spawn it, wait for a deterministic readiness line, show a
plain-language error instead of a broken window on failure, and terminate
only its own owned child on close. `tests/test_native_shell_spike.py`
deterministically proves the backend child-process lifecycle and
capability-token behavior that host relies on. A later pass installed a real
Rust 1.91 toolchain and confirmed the Tauri 2 dependency graph in
`Cargo.toml` actually resolves/downloads and partially compiles (through
glib/gio bindings) before hitting a Linux-only GTK3 system-library gap
unrelated to the actual Windows/WebView2 target; `src/main.rs` itself has
still never been type-checked against the real `tauri` API, and no native
window has been opened. See ADR 0050 for the exact evidence boundary.

ADR 0051 implements Phases 2-3's Python service layer (no native/Rust or
HTTP wiring yet): `src/apoapsis/desktop/` contains `ProjectRegistryStore`
(a new application-owned SQLite "recent projects" store, deliberately
separate from any one project's `.apoapsis/`), `ProjectCapabilitySessions`
(in-memory, opaque, window/project-scoped capability ids -- never a raw
path), `DesktopProjectService` (`validate_project`/`select_project`
/`initialize_project`/`list_recent_projects`/`forget_recent_project`, the
last only ever calling the existing unmodified `apoapsis.cli.app._init()`,
never automatically), and `DesktopImportService`
(`preview_import`/`approve_import`/`execute_import`: staged, hash-verified,
previewed copying that hard-excludes `.git`/`.apoapsis`/`.sol`,
dependency/build/virtualenv directories, and secret-like filenames by
default; never follows symlinks; rejects traversal/absolute/reserved-name
destinations; requires explicit confirmation for replacements with an
automatic backup; and writes a durable JSON audit manifest under the
project's own `.apoapsis/import-manifests/`). `tests/test_desktop_registry.py`
and `tests/test_desktop_import.py` cover this deterministically; neither
has been executed in the authoring session (Python 3.10 sandbox, see
Snapshot) -- run them before treating Phase 2/3 as verified.

ADR 0052 adds Phase 4 (`DesktopReferenceService`: `attach_reference_project`
/`select_reference_evidence`/`list_reference_evidence`/
`detach_reference_project` -- read-only, one-file-at-a-time evidence
selection recording exact source project/commit/hash into an append-only
`.apoapsis/reference-evidence/<id>/evidence.jsonl` ledger, reusing ADR
0051's containment/exclusion checks) and Phase 5 (`DesktopHomeService
.home_summary()`: project identity, Git state, init state, verification
readiness via the existing `ApoapsisUIService.doctor()`, recent projects,
and a deterministic available-actions list; plus a real but unbuilt
`tauri::menu` File/View/Help structure in the ADR 0050 spike, whose
`on_menu_event` handler is an intentional stub pending a second, privileged
local-IPC channel on the same backend process -- a fresh-subprocess-per-
click design was considered and rejected because it cannot honor
`ProjectCapabilitySessions`' deliberately in-memory, restart-invalidated
lifetime). `tests/test_desktop_reference.py` and `tests/test_desktop_home.py`
cover this deterministically; like ADR 0051's tests, neither has actually
executed successfully in this sandbox (Python 3.10 lacks `tomllib`; a
separately obtained Python 3.11.0rc1 had no working `pip`) -- run them
before treating Phase 4/5 as verified.

ADR 0053 builds Phase 6's actual local IPC channel: `src/apoapsis/desktop
/ipc_server.py`'s `DesktopIPCHTTPServer` is a second `ThreadingHTTPServer`
in the *same* Python process as the browser-facing UI server (started by
`backend_entry.py` alongside it, in a background thread, when
`--desktop-token` is supplied), on its own OS-assigned loopback port,
guarded by its own capability token the browser-facing webview never
receives. It exposes exactly the fourteen typed operations Phase 6 named
as `POST /desktop/<operation>` routes, backed by `DesktopServices`
(`src/apoapsis/desktop/services.py`, bundling all four Phase 2-5 services
behind one project registry), with the same most-specific-first
error-to-HTTP-status discipline `ui/server.py` already uses. The disposable
Tauri spike (`spikes/native-shell-tauri/src-tauri/`) now generates a
second token, waits for both servers' readiness lines, and wires three
menu handlers (`open_recent`/`close_project`/`environment_diagnostics`) to
real HTTP calls over this channel; the four handlers needing a native
picker (`open_project`/`import_files`/`import_folder`/
`attach_reference_project`) remain documented stubs -- `tauri-plugin-dialog`
was not added. `tests/test_desktop_ipc_server.py` exercises the channel
over real loopback HTTP (token auth, routing, a full import round trip,
reference-evidence capture, session close) but has not actually executed
in this sandbox, same Python-version reason as ADR 0051/0052's tests.

ADR 0054 wires the spike's remaining four menu handlers
(`open_project`/`import_files`/`import_folder`/`attach_reference_project`)
to a real native picker (`tauri-plugin-dialog`, added to `Cargo.toml`),
completing every File-menu action except `show_project_folder`. It also
fills four Phase 7 coverage gaps found by checking ADR 0050's checklist
item by item: a backend readiness-timeout test (which caught and fixed a
real bug in `tests/test_native_shell_spike.py`'s own helper -- it blocked
with no timeout against a genuinely silent child process), an
import-atomicity test (a source file changed mid-execution aborts the
*whole* import, promoting nothing), a one-project-per-window binding test,
and a new `tests/test_desktop_authority_boundary.py` proving via static
source scan that no model-facing package or the browser-facing
`ui/server.py`/`ui/application.py`/`app.js` ever references
`apoapsis.desktop`. Junction rejection (vs. symlink rejection, which is
tested) and native-picker cancellation remain undeterminable outside real
Windows hardware -- explicitly disclosed as such, not silently skipped.
An ADR 0054 addendum also fixed a real dev-workflow bug found by reading
(not compiling) the spawn path -- `backend_entry.py` resolution only
checked a packaged `resources/` layout that does not exist yet, so a
plain `cargo run` would have failed immediately -- and wired the last
stubbed menu action, `show_project_folder` (an OS-appropriate "reveal in
file manager" call, not a desktop-IPC operation). Every File/View/Help
menu item now has a real, if still uncompiled, implementation.
Phases 2-8 (native project picker/registry, safe import workflow, reference-
project attachment, desktop UX, typed capability API, full deterministic
coverage, and real-Windows manual verification) are not built. A model gains
no new filesystem, shell, Git, or network authority from this work; only the
desktop controller may hold user-granted filesystem capability, and only
within the scope the user explicitly selects through a native dialog.
ADR 0055 fixes the reproduced Research Mode failure described above in the
Research Mode subsection: classified failure reasons/detail on
`ResearchEngineError`, pre-retrieval query-feasibility checks (a new
`unusable-queries.jsonl` audit file), a per-research-question fairness cap
in `SourceRanker` alongside the existing per-source one, exactly one
bounded/audited recovery pass when retrieval succeeds but extraction finds
nothing, and a harness-owned `OfficialDocumentSearchProvider` seam for real
official-document discovery with no concrete vendor implemented yet (the
vendor choice is explicitly deferred to the owner, with a recommendation
recorded in the ADR). Existing direct-URL official-doc research, the
existing GitHub/Reddit adapters, and the existing security/quarantine
pipeline are unchanged.

### Audit and reports

Task audit directories preserve prompts, requests/responses, telemetry, context
measurements, turn actions, policy decisions, normalized failures, patches,
verification results, research provenance, routing, authorization, continuation,
and final reports. SQLite operation/event databases are authoritative for live
state; JSON/Markdown artifacts are immutable evidence and handoff material.

## Operating the project

Requirements: Python 3.12+, Git, and the declared project dependencies. Ripgrep
is recommended. Ollama and Docker are optional unless selected by configuration.

Typical development checks:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
```

For a target repository:

```powershell
apoapsis init
apoapsis doctor
apoapsis ui --project-root .
```

`apoapsis init` writes an example configuration, not a universal verification
contract. Replace its verification command with a real project check before
execution. In strict mode, explicitly designate acceptance commands and map
criteria only when their pass genuinely proves the criterion.

## Evidence index

- Context profiles: `docs/evaluation/apoapsis-1.0-profile-evidence-2026-07-18.md`
- Strict live rounds: `docs/evaluation/apoapsis-strict-live-evaluation-2026-07-18.md`
  and `apoapsis-strict-live-evaluation-2026-07-19.md`
- Planning comparison: `docs/evaluation/apoapsis-planning-comparison-2026-07-20.md`
- D4c diagnosis/probes: `docs/evaluation/apoapsis-d4c-forensic-diagnosis-2026-07-19.md`
- Docker proof: `docs/evaluation/apoapsis-d5a-live-docker-evidence-2026-07-20.md`
- Earlier local smoke records: remaining files in `docs/evaluation/`

Use these dated records for exact setups and observed results. Keep new live
claims there; the Snapshot above should contain only a short current summary.

## Known limitations and active risks

- Live local coding reliability is not established; run-to-run sensitivity in
  planned slice execution remains unexplained.
- No live hosted coding call has been made.
- The default initialized verification command is only an example and may not
  fit blank/non-Python projects; the known impossible unittest case now fails
  fast, but general project-check selection remains operator configuration.
- Research quality depends on allowed domains, source configuration, query
  quality, upstream search behavior, and available authentication. It is
  advisory, not proof.
- Real official-document web search has a secure provider seam (ADR 0055)
  and one implemented provider, Tavily (ADR 0056), but no live call has
  been made or verified against the real Tavily API in any session; treat
  it as untested until an API key is configured and a real run is recorded.
- Browser JavaScript still relies heavily on static regression tests; important
  flows need periodic real-browser checks.
- Packaging a later plan slice checkpoints completed prior work on isolated task
  branches and records the exact inherited base commit. The user's checked-out
  branch remains untouched; incomplete slices and divergent histories fail closed.
- Native desktop packaging remains a disposable, unbuilt spike (ADR 0050,
  `spikes/native-shell-tauri/`); it has never been compiled or run on real
  Windows hardware. Live hosted evidence also remains deferred.

See `NEXT_STEPS.md` for the prioritized actionable list only.

## Architecture decision index

ADRs 0001-0014 establish the deterministic substrate, providers, research,
bounded agent, routing, evaluation, sandbox, context, lifecycle, and UI.
ADRs 0015-0018 establish strict acceptance and proof integrity. ADRs 0019-0029
establish planning, review/resume, durable operations, authorization, slices,
comparative evaluation, and diagnostic probes. ADRs 0030-0041 establish hosted
spend, manual frontier paths, discovery, browser/launcher workflows, planning
research, hardening/compaction, default bounded test authoring, and deterministic
new-file diff reconstruction, default dependency authoring, plan-local slice
inheritance, required verification scaffolding as implementation work, and
harness-controlled Python dependency installation.
ADRs 0042-0048 add verification repair, UI-first validation/repair, truthful
repair results and test-side-effect guidance, automatic final verification, and
complete slice-contract/no-progress recovery, plus explicit fresh local execution
after a pre-agent routing review, strong risk-aware local execution, richer
frontier handoffs, and explicit finished-plan delivery.
ADR 0049 bumps the coupled `[architect.ceilings].max_criteria_per_slice`
ceiling (12 → 20, paired with `max_work_brief_chars` 2000 → 3500) and the
local+frontier coder budgets in lockstep so a 13–20 criterion slice validates
and is actually implementable inside the same one-coder-cycle scope; applies
to every future `apoapsis init`, never silently rewrites an existing
`.apoapsis/config.toml`.
ADR 0050 supersedes ADR 0034's native-wrapper deferral, adopts Tauri 2 as the
target desktop shell around the existing unchanged Python backend, and builds
only a disposable Phase 1 process-lifecycle/capability-token spike; Phases
2-8 (project registry, safe import, reference projects, desktop UX, typed
capability API, full coverage, real-Windows verification) remain future work.
ADR 0051 implements Phases 2-3 as a Python service layer only
(`src/apoapsis/desktop/`: project registry, capability sessions, and a
preview/approve/execute file-import workflow with hard-coded safety
exclusions); native Rust wiring, HTTP routes, and browser UI for these
services remain future work, and neither new test module has been run in
the authoring session.
ADR 0052 adds Phase 4 (read-only reference-project attachment/evidence
capture) and Phase 5 (a Home-screen data-assembly service plus a real,
unbuilt Tauri File/View/Help menu skeleton with intentionally stubbed
handlers) -- also Python-service-layer-only.
ADR 0053 builds Phase 6's privileged local-IPC channel (a second loopback
HTTP listener in the same backend process, its own token, fourteen typed
routes over `DesktopServices`) and wires three of the spike's menu
handlers to it for real.
ADR 0054 wires the remaining four native-picker-dependent menu handlers
via `tauri-plugin-dialog` and fills four Phase 7 coverage gaps (readiness
timeout, import atomicity, one-project-per-window, and static
authority-boundary regression tests). ADR 0055 fixes the misleading
research-mode failure message from operation `DISCOP-796622810B804FE59E87536D`:
classified `ResearchFailureReason`s and structured detail on
`ResearchEngineError`, pre-retrieval official-doc query-feasibility checks,
a per-research-question fairness cap in `SourceRanker`, one bounded/audited
recovery pass on total extraction failure, and a harness-owned
`OfficialDocumentSearchProvider` seam for real official-document discovery
with no concrete vendor implemented at that point (the choice was
explicitly deferred to the owner). ADR 0056 records that choice: Tavily,
authorized after Brave Search (the initial pick) turned out to require a
credit card and metered billing where Tavily has a genuinely free,
no-card tier -- `TavilyOfficialDocumentSearchProvider` is now the one
implemented provider, unverified against the real API in this session.
None of the six new/changed test
modules across ADR 0051-0054 have executed successfully in this sandbox
(Python 3.10 lacks `tomllib`; a separately obtained Python 3.11.0rc1 had no
working `pip`). Junction rejection and native-picker cancellation remain
outside what this environment can determine at all -- real Windows
hardware is required.

Read the relevant ADR completely before altering its area. Preserve old ADRs as
history; supersede them with a new ADR rather than rewriting the old decision.

## Maintenance contract

For changes affecting architecture, workflow behavior, configuration, model
roles, context, patch policy, verification, audit artifacts, tests, or evidence:

1. Update this current-state map in the same change.
2. Update `README.md` for user-visible behavior.
3. Add an ADR for a new architectural decision; never rewrite accepted history.
4. Add deterministic fake-provider coverage for model-driven branches.
5. Run focused tests, the full suite, `python -m compileall -q src tests`, and
   `git diff --check`.
6. Update Snapshot only with results actually observed and label fake, live
   local, and live hosted evidence distinctly.
7. Update `NEXT_STEPS.md` only when active priority/order changes; remove done
   items instead of appending milestone essays.
8. Put detailed live observations in a dated `docs/evaluation/` file and link it.
9. Preserve uncommitted user work and the `substrate-v0.1` tag.

Before handoff, verify source, tests, README, this file, the relevant ADR, and
active priorities agree. Do not declare success from model output or from a
partial test run.
