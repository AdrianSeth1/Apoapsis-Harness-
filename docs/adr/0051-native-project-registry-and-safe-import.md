# ADR 0051: Native project registry and safe file/folder import (ADR 0050 Phases 2-3)

- Status: Accepted (Python service layer only; native Rust/Tauri wiring
  and real GUI dialogs are not built by this change)
- Date: 2026-07-23

## Context

ADR 0050 authorized a phased plan toward a native desktop shell and named
Phase 2 (native project picker/registry) and Phase 3 (safe file/folder
import) but deliberately built neither -- only Phase 1's disposable
process-lifecycle spike. This ADR implements Phases 2 and 3's concrete
service-layer decisions: where the project registry lives, how a window's
filesystem capability is scoped and revoked, and exactly how a file/folder
import is previewed, approved, and executed without ever silently
overwriting anything or letting a path escape the destination project.

Consistent with `HANDOFF.md`'s authority boundary and this codebase's
existing architecture (every workflow capability is a typed Python service
behind `apoapsis.ui.application.ApoapsisUIService`-style methods, never
logic embedded in browser JavaScript or, here, in the not-yet-built Rust
host), both phases are implemented as new Python modules under
`src/apoapsis/desktop/`. The Rust host described in ADR 0050 is the
*trusted native picker and process owner*; once a user picks a path
through it, everything downstream -- registry storage, path validation,
capability scoping, import safety rules, staged copying, and audit
manifests -- is exactly the kind of deterministic, testable authority this
codebase always keeps in Python, not in the shell around it.

## Decisions

### Project registry: a new application-owned store, separate from any one project's `.apoapsis/`

`src/apoapsis/desktop/registry_store.py`'s `ProjectRegistryStore` is a
SQLite store following `discovery.store.SQLiteDiscoveryStore`'s exact
connection/migration discipline (`isolation_level=None`, `BEGIN IMMEDIATE`,
`PRAGMA busy_timeout`, additive `PRAGMA table_info` migrations). It stores
one row per project: `canonical_path`, `display_name`, `added_at`,
`last_opened_at`, `initialized` -- nothing else. No credentials, no
repository contents, no model-visible data.

This is deliberately **not** rooted under any single project's
`.apoapsis/` directory -- a registry of *all* recently opened projects
cannot live inside just one of them. No prior convention in this codebase
for an application-owned, cross-project user-data directory existed;
`default_registry_database_path()` introduces one (`%APPDATA%/Apoapsis/`
on Windows, `$XDG_DATA_HOME/apoapsis/` or `~/.local/share/apoapsis/`
elsewhere), documented in its own docstring as new rather than presented as
a pre-existing pattern. Every test passes an explicit `database_path`
instead of relying on this default, matching this codebase's existing
test discipline.

### One project per window, enforced by an opaque, in-memory capability session

`src/apoapsis/desktop/capability.py`'s `ProjectCapabilitySessions` is an
in-memory (never persisted) `dict[str, Path]` behind a lock.
`DesktopProjectService.select_project()` is the only place a session is
created, and it binds one opaque `session_id` to one canonical, already-
validated project root. Every later operation that needs a project root
(`initialize_project`, and every `DesktopImportService` method) takes a
`session_id`, never a path -- so a caller (eventually: browser JavaScript
running inside the native window) can request an operation but can never
supply an arbitrary path the backend will honor, exactly as ADR 0050
requires ("Every backend request must derive its project from the
window's capability-bound session, never from a browser-supplied arbitrary
path").

Being in-memory rather than persisted is the mechanism that satisfies
"invalid after application restart unless the operator selects the path
again" for free: there is no expiry logic to get wrong, because the
session simply does not exist after a process restart.

### `validate_project` never raises for a recoverable state; `select_project`/`initialize_project` do

`DesktopProjectService.validate_project()` (used by both `select_project`
and `list_recent_projects`) returns a status string
(`ok` / `missing` / `inaccessible` / `not_git_repository` /
`not_initialized`) rather than raising for every non-`ok` case, because a
missing Git repository or an uninitialized one are exactly the states the
UI must display and offer a next action for (per ADR 0050 Phase 2: "Offer
explicit initialization when the repository is not initialized... Detect
moved, missing, inaccessible, or non-Git directories"). `select_project`
raises `ProjectNotFoundError` only for the states with no usable next
action (`missing`, `inaccessible`); it happily creates a session and a
registry entry for an uninitialized Git repository so the UI can offer
**Initialize this project** as an explicit follow-up action --
`initialize_project` never runs implicitly from `select_project`.

`initialize_project(session_id)` takes no path parameter at all -- it can
only ever act on whatever project the caller's own session is already
bound to, so there is no way to initialize a project the operator never
opened. It calls the existing, unmodified `apoapsis.cli.app._init()` --
the same function `apoapsis init` already uses -- rather than
reimplementing repository/config bootstrap.

### Safe import: preview -> approve -> execute, exactly as three separate calls

`src/apoapsis/desktop/import_service.py`'s `DesktopImportService` matches
ADR 0050 Phase 6's named API surface (`preview_import`, `approve_import`,
`execute_import`) as three distinct methods with distinct effects:

1. **`preview_import`** reads and hashes candidate source files but writes
   nothing. It enumerates every selected file (or, for a selected
   directory, every file under it via `os.walk(..., followlinks=False)`,
   which never descends into a symlinked subdirectory and never descends
   into an always-excluded directory at all -- both a correctness guard and
   an efficiency one). For every candidate it determines a disposition:
   `new`, `replacement` (destination file already exists), `conflict`
   (destination path exists as the *wrong kind* -- e.g. a directory sits
   where a file would go), `skipped_symlink` (never followed by default),
   or `skipped_excluded` (see below). It also records the destination
   repository's clean/dirty state, reusing `GitRepository(project_root)
   .snapshot().is_clean` exactly as `repository.readiness
   .require_clean_parent_repository` (ADR 0026) already does, rather than
   reimplementing a Git status check.
2. **`approve_import`** requires `replacements_confirmed=True` whenever
   any file would be replaced (a hard `ImportApprovalError` otherwise --
   the "second confirmation for replacements" rule), and refuses outright
   if any `conflict` entries exist (a type conflict is not something a
   confirmation flag can resolve; the operator must choose a different
   destination and re-preview).
3. **`execute_import`** requires a prior, matching `approve_import` decision
   for the same `preview_id`/`session_id` pair. It re-hashes every source
   file immediately before copying and aborts with `ImportSafetyError` if
   the content changed since the preview -- protecting against a
   time-of-check/time-of-use gap between preview and execution. It copies
   into a temporary staging directory under the *destination* project's own
   `.apoapsis/import-staging/<import-id>/` first, backs up any file about
   to be replaced into `.apoapsis/import-backups/<import-id>/`, then
   promotes every staged file into its final destination with `os.replace`
   (atomic within the same filesystem), and finally writes a durable JSON
   audit manifest to `.apoapsis/import-manifests/<import-id>.json`
   recording the full preview, the decision, every copied/skipped/conflict
   path, and every backup path. Source files are never deleted, moved, or
   modified by any step.

### Hard-excluded categories are never copied, regardless of confirmation

`src/apoapsis/desktop/safety.py` defines three always-excluded categories,
checked per candidate file (and, for efficiency, pruned from directory
traversal outright where possible):

- **Root/metadata directories**: `.git`, `.apoapsis`, `.sol` -- the same
  three names `patches.validator.PatchValidator._safe_path` already
  refuses to touch.
- **Dependency/build/virtual-environment directories**: `node_modules`,
  `__pycache__`, `.venv`, `venv`, `site-packages`, `.mypy_cache`,
  `.pytest_cache`, `.ruff_cache`, `dist`, `build`, `.next`, `.nuxt`,
  `.cache`, `target`, `.gradle`, `.tox`, `vendor`, `.terraform`, and
  platform metadata files (`.DS_Store`, `Thumbs.db`, `desktop.ini`). No
  such exclusion list existed anywhere in this codebase before; this is a
  new, import-specific list, not a reuse of an existing one (the closest
  existing list, `ContextCompilerConfig.cloud_excluded_paths`, is a
  transmission-redaction list for the coding agent's context, a different
  concern with a different, narrower set of patterns).
- **Secret-like filenames**: `.env`/`.env.*`, `*.pem`, `*.key`, `*.pfx`,
  `*.p12`, `id_rsa`/`id_ed25519`/`id_dsa`/`id_ecdsa` (and their `.pub`-style
  siblings), `credentials`/`credentials.*`, `.npmrc`, `.netrc`,
  `secrets.*`, `*.secret` -- a superset of `cloud_excluded_paths`'s secret
  patterns, extended for file-copy rather than prompt-transmission risk.

### Destination-path safety: containment, traversal, and reserved names

`safety.is_safe_destination_relative_path()` rejects an empty path, null
bytes, backslashes, a leading `/`, a drive letter (`C:`-style), any `..`
segment, and any path segment matching a Windows-reserved device name
(`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9`) -- rejected
regardless of the host platform actually running Apoapsis, since a project
may later be opened on Windows. `safety.resolve_within_root()` additionally
resolves the candidate destination and verifies it is still inside the
project root via `Path.relative_to`, mirroring
`patches.validator.PatchValidator._safe_path`'s existing containment
check. Both the destination *directory* the operator chose and every
individual file's computed destination path are checked this way -- a bad
top-level choice is rejected immediately by `preview_import`, before any
file is even enumerated.

## Non-goals

- Does not build or wire up the native Rust/Tauri side of Phase 2/3 --
  there is still no native folder/file picker, no menu, and no Home-screen
  UI (ADR 0050 Phase 5). This ADR is the trusted service layer those
  pieces will call once built.
- Does not add HTTP routes to `apoapsis.ui.server` or JavaScript to
  `app.js` for these operations yet -- Phase 6's typed capability API
  surface is implemented as direct Python methods only; wiring an HTTP
  boundary in front of them (with the same capability-token discipline
  `ui/server.py` already uses) is separate follow-up work.
- Does not implement Phase 4's "attach reference project" (read-only
  cross-project evidence) -- only "select/open" and "import" exist so far.
- Does not read import limits (`ImportLimits`) from
  `.apoapsis/config.toml`; they are fixed defaults (2,000 files / 200 MB)
  isolated behind a typed model so wiring configuration in later is
  additive.
- Does not implement actual Git-history merging, submodules, or remote
  configuration -- explicitly deferred to its own future ADR per ADR 0050.
- Does not grant a model any access to this package. Nothing in
  `src/apoapsis/desktop/` is reachable from `agent/`, `architect/`,
  `discovery/`, or any provider-facing code path.

## Verification

`tests/test_desktop_registry.py` and `tests/test_desktop_import.py` were
written to deterministically cover: registry CRUD and last-opened
ordering; moved/missing project detection on re-list; capability-session
isolation and revocation; `initialize_project`'s explicit-confirmation-only
behavior (including rejecting a non-Git directory and a second
initialization); import preview/approve/execute happy path; directory
imports preserving relative structure; replacement confirmation and
backup/recovery; type conflicts blocking approval; `.git`/`.apoapsis`,
secret-filename, and dependency/build-directory exclusion; symlink
rejection; absolute/traversal/reserved-name destination rejection; preview
determinism for identical input; audit-manifest JSON validity; and that
source files are never touched by execution.

**Not run in the authoring session, at the owner's explicit request**: this
sandbox's default Python is 3.10, and `apoapsis.config` requires `tomllib`
(Python 3.11+), so even importing the existing `apoapsis` package requires
a newer interpreter than this sandbox ships by default -- a pre-existing,
already-documented environment limitation (`HANDOFF.md`'s Snapshot already
notes ADR 0049 is "blocked by the Python 3.10 environment on the change
workspace"). A manual, non-test smoke script (not the unittest suite) was
run against a separately obtained Python 3.11 interpreter far enough to hit
a missing `pip`/`ensurepip` in that minimal package before the owner's
"don't run tests" preference and diminishing returns stopped further
environment work; no test result, pass or fail, is claimed here. Run:

```powershell
python -m unittest tests.test_desktop_registry tests.test_desktop_import -v
python -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
```

before treating Phase 2/3 as verified.
