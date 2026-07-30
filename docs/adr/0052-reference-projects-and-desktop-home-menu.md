# ADR 0052: Read-only reference projects and desktop Home/menu building blocks (ADR 0050 Phases 4-5)

- Status: Accepted (Python service layer and a disposable, unbuilt Tauri
  menu skeleton only -- no privileged local IPC channel yet)
- Date: 2026-07-23

## Context

ADR 0051 implemented Phase 2 (project registry) and Phase 3 (safe import)
as Python services under `src/apoapsis/desktop/`, explicitly deferring
Phase 4 (attach a second repository as read-only reference evidence) and
Phase 5 (desktop menus and a Home screen). This ADR implements both, and
resolves a design question Phase 5 exposed: how does a native menu click
actually reach one of these Python services?

## Decisions

### Phase 4: `DesktopReferenceService` -- read-only, provenance-bound, never expanding patch destinations

`src/apoapsis/desktop/reference_service.py` adds a third, distinct
operation alongside "Open project" (ADR 0051) and "Import files" (ADR
0051): **Attach reference project**. `attach_reference_project(session_id,
reference_path)` binds a new opaque `reference_session_id` to
`(primary_root, reference_root)` after rejecting a reference path that is
not a Git repository, is the same path as the primary project, or is
nested inside/around it. Nothing is copied at attach time -- it only
records the reference repository's live Git snapshot (branch, HEAD commit,
clean/dirty), reusing `GitRepository(...).snapshot()` exactly as ADR 0051's
import preview and ADR 0026's dirty-parent check already do.

`select_reference_evidence(reference_session_id, relative_paths)` is the
**only** path from "attached" to "readable": the operator selects
individual files one at a time (never a directory -- sweeping a whole
subtree in wholesale is what the *import* workflow is for, and import
copies into the tracked project source; this never does). Each selected
file is validated with the same containment (`resolve_within_root`) and
hard-exclusion (`hard_exclusion_reason`) checks ADR 0051's import service
uses -- a reference project's secrets, `.git`, and dependency directories
are exactly as off-limits as a source project's are for import. A selected
file is hashed, sniffed for binary content, copied read-only into the
*primary* project's own `.apoapsis/reference-evidence/<reference-session-
id>/` cache (never into the reference project, and never into the
primary project's *tracked* source), and appended as one line to an
append-only `evidence.jsonl` ledger recording `reference_session_id`,
`source_canonical_path`, `source_commit`, `relative_path`, `sha256`, and
`captured_at` -- so every piece of evidence a model might eventually see
carries exactly the source project, commit, path, and hash ADR 0050
requires, and can never quietly become unattributed context later.

`detach_reference_project` revokes the in-memory binding (so no further
`select_reference_evidence` calls succeed) but never deletes evidence
already captured -- that is a durable, provenance-bound record of a past
explicit decision, the same "audit history is append-only" principle
`HANDOFF.md` already applies everywhere else.

**Consequences for the three ADR 0050 reference-project rules**: the
reference project remains outside the writable project root because
nothing in this class ever opens a file inside it for writing; a model
cannot browse it freely because there is no "list files" operation at
all, only "attach" (Git-state only) and "select this exact file" (one at
a time, by the operator); and reference access cannot expand patch
destinations because patch policy code is untouched and reference evidence
is cached under `.apoapsis/`, never inside the primary project's tracked
source tree a patch could target.

### Phase 5: `DesktopHomeService` -- a pure-read Home-screen data assembly

`src/apoapsis/desktop/home_service.py`'s `home_summary(session_id)`
assembles exactly what ADR 0050's Home screen brief asks for: project
identity and canonical path, Git branch/clean state (via the existing
`ApoapsisUIService.overview()`), Apoapsis initialization state (via
`DesktopProjectService.validate_project`), verification readiness (via the
existing `ApoapsisUIService.doctor()`, called with `probe_providers=False`
exactly as the current browser UI's Models & environment page already
does -- no new model-touching code path), the cross-project recent-projects
list Phase 2 introduced, and a deterministic `available_actions` list
(`import_files`/`import_folder`/`attach_reference_project`/
`show_project_folder`/`close_project` when initialized;
`initialize_project`/`close_project` when not; `forget_recent_project`
when missing or inaccessible) -- so a future frontend renders state
instead of re-deriving it. This is a pure read: it never mutates anything,
and it degrades instead of raising if `overview()`/`doctor()` fail (for
example, on a project directory that no longer exists), returning `None`
values with an explanation rather than crashing the Home screen.

### Phase 5's menu: real Rust menu-construction code, honestly stubbed handlers

`spikes/native-shell-tauri/src-tauri/src/main.rs` gained a real
`tauri::menu` File/View/Help structure (`Open Project…`, `Open Recent`,
`Import Files…`, `Import Folder…`, `Attach Reference Project…`,
`Close Project` under File; `Show Project Folder` under View;
`Environment Diagnostics` under Help) -- exactly ADR 0050 Phase 5's list.
Still unbuilt/never compiled, matching Phase 1's existing disclosed status.

Its `on_menu_event` handler is a deliberate stub, not a fake
implementation. The reason is architectural, not laziness: a first design
considered spawning a fresh Python subprocess per menu click to invoke a
CLI wrapper around the new `src/apoapsis/desktop/` services, but
`ProjectCapabilitySessions` and the reference-project bindings this ADR
and ADR 0051 both rely on are *deliberately* in-memory, so that they become
invalid the instant the process restarts (the exact behavior ADR 0050
requires). A fresh subprocess per call cannot honor that -- each call
would recreate empty session state and immediately fail to resolve the
very session id the previous call returned. The correct design is a
**second, privileged endpoint set on the same already-running backend
process** `spawn_backend()` already starts for the browser-facing UI,
gated by a **second capability token that only the Rust host ever holds**
-- structurally identical to, but never overlapping with, the existing
browser-facing token and URL. That is real design and implementation work
(Phase 6's "typed local IPC" proper), not something to fake here merely to
make a menu item appear functional. Each menu item's id is documented
in-line with exactly which `src/apoapsis/desktop/` method it will call
once that channel exists.

## Non-goals

- Does not build the privileged local-IPC channel itself (Phase 6 proper).
  No menu click currently does anything beyond printing which handler it
  would call.
- Does not add HTTP routes to `ui/server.py` or JavaScript to `app.js` for
  reference projects or Home data -- same reasoning as ADR 0051: the
  browser-facing loopback surface must not gain filesystem-adjacent
  capability before the native shell's own privileged channel exists to
  replace it, or the existing "browser cannot browse arbitrary folders"
  guarantee (ADR 0035, `README.md`) would quietly regress.
- Does not let a model call anything in `src/apoapsis/desktop/`. Nothing
  here is reachable from `agent/`, `architect/`, `discovery/`, or any
  provider-facing code.
- Does not implement actual cross-project evidence *use* -- i.e., feeding
  captured reference evidence into the context compiler or a coding
  agent's prompt. Evidence is captured, hashed, and attributed; consuming
  it is separate, later work.
- Does not implement real Git-history merging, submodules, or remote
  configuration -- still explicitly deferred per ADR 0050.

## Verification

`tests/test_desktop_reference.py` covers: attach returns Git state; attach
rejects self-reference, a non-Git directory, and nested paths; selecting
evidence records exact source project/commit/hash and never writes into
the reference project; secret-like/`.git` exclusion and traversal/
directory rejection for evidence selection; detaching revokes the session
but preserves already-captured evidence; and the on-disk ledger is valid
JSONL. `tests/test_desktop_home.py` covers: an initialized project's full
action list; an uninitialized project offering only `initialize_project`;
recent projects spanning multiple sessions; and that a since-deleted
project degrades to `forget_recent_project` rather than raising.

**Not run in the authoring session.** Beyond `python -m py_compile` syntax
checks, no test in either new module -- nor, in fact, any module from ADR
0051 -- has been executed successfully yet: this sandbox's default Python
(3.10) lacks `tomllib`, which `apoapsis.config` requires, and a
separately-obtained Python 3.11.0rc1 interpreter (via the same rootless
`apt-get download` technique used for the Rust toolchain in ADR 0050) had
no working `pip`/`ensurepip` to install `pydantic` before the owner's
standing "don't run tests, I'll run them" preference and diminishing
returns stopped further environment work. No pass/fail result is claimed
for any `src/apoapsis/desktop/` code in this session. Run:

```powershell
python -m unittest tests.test_desktop_reference tests.test_desktop_home -v
python -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
```

before treating Phase 4/5 as verified.
