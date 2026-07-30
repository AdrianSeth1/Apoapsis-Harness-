# ADR 0064: Legible unborn-HEAD errors and no silent UI dead ends

Date: 2026-07-26

Status: Accepted

Extends ADR 0013 (local operator interface) and ADR 0032 (discovery and
frontier planning handoff).

## Context

During the live Laguna run on 2026-07-26, discovery session
`DISC-7D87B2379D8E` in project `C:\Users\aryam\coding stuff\test project 4`
became a dead end at step 3 ("Optional research"). Neither "Run GitHub
research" nor "Skip research and continue" did anything; both surfaced only
`Failed to fetch`, with the server still running and every other page working.

Two separate defects, one visible and one hidden.

**The hidden one.** `test project 4` was `git init`-ed and `apoapsis init`-ed
but never committed. Its only file was the `.gitignore` Apoapsis wrote, still
untracked. Almost everything Apoapsis does is anchored to a base commit, so
`git rev-parse HEAD` failed. The operation store recorded it verbatim:

```text
GitCommandError: git command failed (128): git rev-parse HEAD:
fatal: ambiguous argument 'HEAD': unknown revision or path not in the
working tree.
```

That message describes Git's parser, not the operator's problem, and never
says "commit something first."

**The visible one.** `_handle_discovery_export_frontier_package` — the "skip
research" path — calls `GitRepository.snapshot()` synchronously inside the
request handler. `GitCommandError` is a bare `RuntimeError` and appears in no
route's `except` clause, so it escaped into `socketserver`, which closed the
connection without writing a response. `fetch` rejects such a request with the
string `Failed to fetch` and nothing else: no status, no body, nothing the UI
can render and nothing the operator can report. Every route in `server.py`
maps its own well-typed errors carefully, but there was no floor beneath them.

The second defect is the more serious one. It converts *any* unanticipated
failure anywhere in the service layer into an unactionable dead end, and it
had been latent since the UI was introduced.

## Decision

### A last-resort handler, and an explicit statement that it is one

`do_GET` and `do_POST` now delegate to `_dispatch_get`/`_dispatch_post` through
`_guarded()`, which catches every remaining exception, records the traceback in
a bounded in-memory ring (`ApoapsisUIHTTPServer.service_error_log`, 50 entries),
and returns `500` with `unhandled <ExceptionType>: <message>`.

This is a floor, not a replacement for per-route error mapping. An exception
that reaches it is a defect, and the response says so in those words. Routes
keep their specific `404`/`409`/`400` mappings; nothing about authorization,
CSP, or the security headers changes.

### `HEAD` has a name for not existing

`GitRepository` gains two methods:

- `has_commits()` — resolves `HEAD` with `--verify --quiet` and returns a bool.
  Never raises for the ordinary "no commits yet" case, so callers can check
  cheaply before starting work.
- `head_commit()` — returns `HEAD`, or raises `RepositoryHasNoCommitsError`
  naming the repository and the fix (`git add -A`, `git commit`).

`GitRepository.snapshot()` and `ContextCompiler.compile()` both use
`head_commit()`, so the two places that most often meet an unborn branch
produce the explanation rather than Git's parser message.

### Discovery refuses before spending anything

`prepare_discovery_operation` checks `has_commits()` immediately after the
session-version check and raises `DiscoveryError` if the repository has none.
The operator gets a `409` with the actionable message, no operation record is
created, no lease is taken, and no model is called. Previously the request was
accepted, the worker ran, and the failure landed as a recorded failed
operation whose error text explained nothing.

`_handle_discovery_export_frontier_package` also maps
`RepositoryHasNoCommitsError` to `409` explicitly, rather than relying on the
new last-resort handler, because it is an expected operator condition and not
a defect.

## Consequences

- A live server can no longer answer a UI action with a closed socket. The
  worst case is a readable `500` naming the exception type.
- Working in a never-committed repository fails immediately, at the point of
  the request, with the fix in the message.
- `service_error_log` holds tracebacks in memory only. It is not written to
  disk and not exposed over HTTP; it exists so an operator who hit a `500` can
  be handed the actual cause. Exposing it through a route would need its own
  decision about what belongs in a response body.
- The authority boundary is untouched. This changes error reporting and one
  precondition check, never who may act.

## Alternatives rejected

- **Auto-create an initial commit.** Apoapsis would be writing history in the
  operator's repository without being asked. Refusing with instructions is
  correct.
- **Catch `GitCommandError` in each discovery route only.** Fixes this one
  path and leaves the same class of dead end everywhere else.
- **Let the exception escape and tell operators to read the console.** The
  console is hidden when the UI is launched from `START_APOAPSIS.cmd`, and
  "Failed to fetch" gives no hint that a console would help.

## Verification performed

```powershell
python -m unittest tests.test_ui                       # 31/31
python -m unittest tests.test_discovery tests.test_discovery_ui `
    tests.test_execution_ui tests.test_repository_and_worktree `
    tests.test_worktree_fingerprint tests.test_ui_copy_and_accessibility `
    tests.test_context_compiler tests.test_vertical_slice   # 115/115, 2 skips
python -m compileall -q src tests                      # passed
```

New coverage: `UIServerTests.test_an_unhandled_handler_error_returns_500_not_a_dropped_connection`
and `RepositoryWithoutCommitsTests` (3 tests) in `tests/test_ui.py`.

The fix was additionally confirmed against the live project that produced the
defect: `submit_discovery_operation` for `DISC-7D87B2379D8E` now raises
`DiscoveryError` with the actionable message and creates no operation record.
