# ADR 0026: Immutable execution authorization and truthful live UI

- Status: Accepted
- Date: 2026-07-19

## Context

ADR 0024's two-step "Start coding" confirmation shows a preview (predicted
route, models, budgets, completion policy, sandbox, verification commands)
and, once confirmed, hands the operation to the durable execution service.
But nothing tied the *shown* preview to the *executed* reality: between a
preview rendering and the confirmation arriving (or between recording an
operation and a queued worker actually running it), the task could be
re-approved at a new version, the specification could change, the parent
repository could gain uncommitted changes, or the operator could edit
`.apoapsis/config.toml` -- and the operation would still run exactly as if
nothing had changed. Separately, two real bugs were found:

1. **Dirty-parent context/worktree mismatch.** `VerticalSliceRunner
   ._run_from_approved()` compiles the agent's initial context by reading
   directly from the parent checkout (`self.project_root`), but
   `WorktreeManager.create()` creates the task's isolated worktree from
   clean HEAD, carrying none of the parent's uncommitted state. If the
   parent checkout has uncommitted tracked changes or untracked files at
   that moment, the agent's context can describe file content the worktree
   it actually edits does not have -- a silent, un-flagged correctness gap.
2. **Progress wasn't actually live.** The control room's poll loop
   (`pollExecutionOperation` in `app.js`) refreshed the bare operation
   status every two seconds while `RUNNING`, but only refreshed
   `store.task` (which carries persisted workflow events and recent agent
   turns) once the operation reached a terminal status -- an early
   `return` skipped it entirely while running. A user watching "Start
   coding" in progress saw a frozen timeline and turn feed for the entire
   run, only updating once at the very end.

## Decision

### `ExecutionAuthorizationPackage`: one function, three call sites, one hash

`src/apoapsis/execution/authorization.py` is new:
`build_execution_authorization_package(project_root, *, operation_id,
task_id, task_version, specification, config)` deterministically computes,
with zero model calls or mutating side effects, exactly what a "Start
coding" confirmation would authorize right now: the task id/version and a
sha256 of the specification; the full parent-repository fingerprint
(`compute_worktree_fingerprint`, ADR 0017 -- tracked diff *and* untracked
files, not `git rev-parse HEAD` alone) plus the repository root and HEAD
commit; a sha256 of the effective configuration; the predicted route and
reason (`select_agent_route()`, the exact function the real service uses);
provider kinds and model names per role; the local/frontier
`AgentLoopConfig` budgets and the `ContextCompilerConfig` ceilings verbatim;
the completion policy; the verification backend and command-name catalog
plus a sha256 of the full verification configuration; a fixed list of
authority-rule statements; and, last, `package_sha256` -- a sha256 over the
whole package's canonical JSON, deliberately excluding `generated_at`
(expected to differ on every rebuild) and `operation_id` (a fresh one is
chosen client-side only once the user actually confirms, so excluding it
lets the exact same content hash a preview showed be reproduced against the
real operation_id at submission and again at run time).

The **same function** is called from three places, so there is exactly one
definition of what "the same authorization" means:

1. **Preview** (`ApoapsisUIService._execution_preview()`): builds a package
   with a placeholder `operation_id` ("EXOP-PREVIEW") and exposes
   `authorization_sha256` (and `authority_rules`) in the preview response.
   Never writes anything -- purely a read.
2. **Prepare** (`prepare_execution_operation()`, now requiring a `config:
   ApoapsisConfig` parameter): builds the package with the real
   operation_id, writes it to the task's audit area
   (`execution-authorization-<operation-id>.json`, mirroring
   `review-continuation-<operation-id>.json`'s existing naming/kind
   convention) via `write_execution_authorization_package()`, and persists
   `package.package_sha256` as the new `authorization_sha256` column on
   `ExecutionOperationRecord` (additive migration,
   `_ensure_authorization_column()`, following the same `PRAGMA
   table_info` pattern as ADR 0025's lease columns; a legacy row has
   `authorization_sha256 IS NULL` and is simply never rechecked).
3. **Run** (`run_execution_operation()`): after the existing task-state/
   version and repository-HEAD rechecks, and *before* `_build_providers()`
   is ever called, recomputes the package fresh from the current task/
   specification/config/repository state and compares its hash to
   `record.authorization_sha256`; any mismatch raises the new
   `ExecutionAuthorizationDriftError` (a subclass of the existing
   `ExecutionOperationError`), caught by the existing `except Exception`
   block, which marks the operation `FAILED` -- the same place, and the
   same error family, that stale-version/stale-HEAD rejections already
   use. A legacy row (`authorization_sha256 is None`) skips this recheck
   entirely (there is nothing to compare against).

### Never serialize a credential; hash structural config safely

`FrontierProviderConfig.api_key_env` only ever names an environment
variable, never a secret value, so the rest of `ApoapsisConfig` is already
safe to serialize. The one exception is `VerificationCommand.environment:
dict[str, str]`, a free-form per-command environment-override map a user
could populate with a literal secret. `_safe_config_payload()` replaces
each command's `environment` dict with its sorted key names only, before
that payload is used for *either* hashing input -- so neither the
persisted package, the UI response, nor the transient hash-input
construction ever contains a raw secret value, while a renamed/added/
removed override key still changes the hash (real drift-detection value
preserved).

### The confirmation authorizes exactly what the preview showed

`submit_execution_operation()` gained a required `expected_authorization
_sha256` parameter: it rebuilds the package from the *current* task/
specification/config/repository state and rejects with
`ExecutionAuthorizationDriftError` -- before `prepare_execution_operation`
is even called, so before any audit write -- if the hash no longer matches
what the caller expected. `POST /api/tasks/<id>/execute` now requires
`expected_authorization_sha256` in its body (400 if absent, same as the
existing `operation_id`/`expected_version` checks; 409 via the existing
`ExecutionOperationError` handler on mismatch). `app.js`'s confirmation
panel displays the hash (`AUTHORIZATION: <sha256>`) and carries it on the
"Confirm & start" button (`data-authorization-sha256`); `submitExecution
Start()` sends it back as `expected_authorization_sha256`.

**A live-browser pass against a real local Ollama model caught a real gap
here the deterministic suite alone did not**: the backend changes above
were written and unit-tested first, but `app.js`'s confirmation button and
`submitExecutionStart()` were not yet updated to actually send the new
field -- every real "Start coding" click would have failed with a 400.
Fixed before this ADR was closed; `tests/test_execution_ui.py`'s bundled-
asset test now asserts both `expected_authorization_sha256` and
`authorizationSha256` are present in the shipped script, specifically to
prevent this exact regression from recurring silently.

### Dirty-parent repository: fail closed, never touch the user's work

`src/apoapsis/repository/readiness.py` is new:
`require_clean_parent_repository(project_root)` raises
`DirtyParentRepositoryError` (listing up to 20 changed paths) whenever
`GitRepository(project_root).snapshot().is_clean` is `False` -- reusing
the existing `RepositorySnapshot` (`git status --porcelain`, which already
covers both tracked and untracked changes and already respects
`.gitignore`, so a normal `.apoapsis/` metadata directory never triggers a
false positive). `run_execution_operation()` calls this immediately after
the repository-HEAD recheck, before `_build_providers()` -- the durable
execution service's own explicit chokepoint, satisfying "before any
provider construction" precisely.

**Deliberately scoped narrowly**: this check is not (yet) added inside the
shared `_run_from_approved()` continuation itself, which would also cover
`apoapsis run`'s direct, synchronous path. That path's existing test
suite (300+ tests across `test_vertical_slice.py`, `test_agent_loop.py`,
and others) predates this constraint and was not audited against it;
retrofitting the check there is a larger, separately-reviewable change.
`run_execution_operation()` -- the actual focus of this ADR -- is fully
covered. The user is never asked to lose work: no code path here stashes,
resets, deletes, or commits anything automatically; the error message
states plainly what changed and that the user must resolve it themselves.

### Genuinely live progress, in actual execution order

`pollExecutionOperation()` in `app.js` no longer returns early while
`RUNNING` before refreshing `store.task` -- it now re-fetches the full
task detail (persisted workflow events and `recent_agent_turns`) on every
two-second tick, terminal or not, so the control room's timeline and tool-
action feed genuinely update while an operation is still in flight, not
only once at the end. Reconnecting (a fresh page load, or a second
browser tab) already seeds this same poll loop from `active_execution_
operation` (ADR 0024) and now shows live progress immediately, from
persisted server state, with no client-side storage involved.

Separately, `ApoapsisUIService._recent_agent_turns()`'s sort was
`(item["stage"], item["turn"])` -- alphabetically ordering "frontier"
before "local", the exact opposite of actual execution order (a session
always exhausts every local turn, if any, before ever escalating to
frontier; the two stages are never interleaved). Fixed with an explicit
`_STAGE_EXECUTION_ORDER = {"local": 0, "frontier": 1}` priority. Payloads
remain bounded exactly as before (last 20 turns, `observation_ledger`
excluded).

### JavaScript regression coverage, without a production runtime dependency

`tests/test_app_js_regression.py` is new, in two tiers:

- **Static, Node-independent, always run**: a regex scan for duplicate
  top-level `function` declarations -- deterministically catching the
  exact bug class ADR 0024's live-browser pass found by accident (two
  functions both named `reviewView`; JavaScript's silent last-declaration-
  wins semantics meant the top-level review route always executed the
  wrong one) -- and a cross-reference of `render()`'s route-dispatch table
  against the set of declared function names, catching a route wired to a
  typo'd or removed view function.
- **Real-JavaScript-engine smoke, skipped with a clear reason (not silently
  passed) when Node is unavailable**: `node --check app.js` for syntax
  validity, and a boot smoke test that runs the real `app.js` inside
  Node's built-in `vm` module against a minimal, hand-rolled `document`/
  `window` stub (no npm dependency, nothing added to `apoapsis` itself),
  letting `boot()`'s real no-session-token fast path execute and asserting
  `render()` produces the expected boot screen -- exercising every
  top-level statement and function declaration in the file without needing
  to fake a live API.

A full real-browser smoke pass (New Task → approval → Start coding →
running progress → Human Review navigation) was run against a real local
Ollama model (`qwen3-coder-next:q4_K_M`) end to end: extraction drafted a
specification; approval transitioned it; "Start coding" showed the
authorization hash and the exact predicted route/budgets; confirming it
created a real isolated worktree and ran a real bounded local-agent
session, with the control room's timeline and tool-action feed updating
live, in real time, without a page reload; the session exhausted its local
turn budget with no frontier configured and correctly stopped at
`HUMAN_REVIEW_REQUIRED`; the "Open the Human Review case →" link correctly
opened the real case detail view. No console errors were observed.

## Tests

`tests/test_execution_authorization.py` (11 tests): the package's hash is
stable across repeated builds from identical inputs, is independent of
`operation_id` (proving preview/submit/run all reproduce the same hash),
and never contains a raw `VerificationCommand.environment` value even when
one is configured; and, using `run_execution_operation`'s pre-provider-
construction recheck, drift is rejected -- always with `build_providers
.assert_not_called()` verified -- for a tracked edit, an untracked edit
(both surfaced as `DirtyParentRepositoryError`), a model change, a budget
change, a verification-command change, a completion-policy change, and a
backend change (all surfaced as `ExecutionAuthorizationDriftError`); a
negative control confirms an unmodified re-authorization is *not* flagged.

`tests/test_execution_ui.py` gained: a preview-vs-confirm drift test
(`expected_authorization_sha256` mismatch rejected before `prepare_
execution_operation` ever runs, so no operation record exists afterward);
a turn-ordering test (synthetic local + frontier turn files, asserting
`local, local, frontier`); and the bundled-asset regression guard for the
JS-side authorization fields described above. Five pre-existing tests
needed the new required `config=` parameter added to their `prepare_
execution_operation()` calls, and one pre-existing HTTP test
(`test_http_duplicate_operation_id_returns_409`) was hardened to patch
`_build_providers()` and wait for its background operation to finish
before the test returns -- unmocked, it relied on a real (failing) network
attempt against an invalid URL, and the new authorization pre-check's
added latency was enough to consistently expose a pre-existing Windows
file-lock race between that lingering background thread and the test's
own temp-directory cleanup.

`tests/test_app_js_regression.py` (4 tests, described above).

Full suite: 497 tests, 0 failures, 6 intentional skips.
`python -m compileall -q src tests` and `git diff --check` both clean.

## Non-goals

- Does not add the dirty-parent check to `apoapsis run`'s direct,
  synchronous path (explicitly disclosed above) or to plan-slice execution
  (Phase D3, not yet built).
- Does not change what `RECORDED`/`RUNNING`/terminal statuses mean for the
  execution operation ledger, or any lease/recovery semantics (ADR 0025,
  untouched).
- Does not add a DOM-rendering harness that fakes realistic per-view data
  (specifications, review cases, plans) for every route; the Node smoke
  layer covers syntax, boot, and route-wiring integrity only. Full-view
  rendering fidelity remains covered by the real-browser pass, not by an
  automated headless-DOM layer.
- Does not add jsdom, Puppeteer, or any other npm package to this
  repository -- the Node-based tests invoke only Node's own built-in
  modules (`vm`, `fs`) and are entirely optional (skipped, not failed,
  when Node is absent).

## Consequences

A "Start coding" confirmation now authorizes exactly what its preview
showed -- task version, specification, full parent-repository fingerprint,
and every execution-relevant configuration value -- recomputed and
hash-compared before the confirmation is even accepted, and again
immediately before any provider is constructed. A dirty parent repository
can no longer silently produce a context/worktree mismatch; execution
fails closed with a clear, specific, non-destructive message instead. The
control room's timeline and tool-action feed are now genuinely live and
show turns in real execution order, whether reconnecting fresh or watching
a run already in progress. `app.js` has two independent, deterministic
regression nets (duplicate-declaration detection and route-dispatch
integrity) plus an optional real-Node syntax/boot smoke layer -- exactly
the class of bug a previous live-browser pass could only find by accident
is now caught automatically, with no new production dependency.
