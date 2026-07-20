# ADR 0033: Local UI for manual frontier coding handoff and discovery/frontier planning

- Status: Accepted
- Date: 2026-07-20

## Context

ADR 0031 (manual subscription-based frontier coding handoff) and ADR 0032
(local-first Architect Mode discovery and frontier planning handoff) both
shipped CLI/service seams only, with no local UI surface -- explicitly
deferred in both ADRs' Non-goals. This milestone builds that UI surface for
both, on top of the existing ADR 0014 loopback application boundary and the
ADR 0020/0021/0023/0024/0025 durable-operation/lease/recovery conventions.

## Decision

### Manual frontier coding handoff UI

A new section on the existing Human Review case-detail page
(`#/review/<task-id>`), shown whenever `manual_frontier_handoff` is
eligible or a prior preview exists for the task:

- **Export**: `ApoapsisUIService.export_manual_frontier_handoff()` calls
  the exact same `build_manual_frontier_handoff_package()`/
  `write_handoff_artifacts()` functions the CLI uses, returning both the
  project-relative and absolute paths of the canonical JSON package and
  the self-contained `FRONTIER-CODING-HANDOFF-<package_id>.md` -- the UI
  shows the absolute path with a copy-to-clipboard button and the
  instruction "Upload this file to ChatGPT or Claude."
- **Import**: a form accepting either pasted JSON text or an uploaded
  file (`<input type="file">`, read client-side with `File.text()`, never
  uploaded as a raw multipart request) posts to
  `export_manual_frontier_handoff()`'s companion,
  `import_manual_frontier_response()` -- the same deterministic checks
  (package integrity, task version, worktree fingerprint, response size,
  schema, hash-binding, patch parse/policy) as the CLI. Declared
  subscription model identity is a required text field, always rendered
  back as "operator-declared, unverified"; tokens/cost are always shown
  as the literal string `Unmeasured`, never a numeric `0`.
- **Two-step approval**: `approve_manual_frontier_preview()` (step 1,
  records intent, no mutation) is a distinct action from applying, which
  reuses the *exact* existing `submit_review_operation()` seam (ADR
  0020) with `action="manual_frontier_handoff"` and a new
  `manual_frontier_preview_id` field threaded through
  `prepare_review_operation()`/`ReviewOperationRecord` (additive,
  optional) -- step 2 is therefore not a new authority path at all, but
  the same durable, lease-owned, crash-recoverable `ReviewOperationStore`
  every other review action already uses, with the same two-step UI
  confirmation pattern (`reviewConfirmPanel`) reused for consistency.
- Repair-round availability, verification outcome, and audit artifacts
  are all read from the existing `ReviewCase` (`manual_frontier_rounds_
  used`/`max_manual_frontier_rounds`, already computed by ADR 0031) --
  no new read path was needed there.

### Discovery and frontier planning UI

A new top-level section (`#/discover`, `#/discover/<session-id>`)
structurally mirroring the New Task screen's durable-operation pattern
(ADR 0023 Commit D1b), because two of its three steps call a model and
must never block an HTTP handler:

- **New durable operation ledger**: `discovery/operation_schema.py`/
  `operation_store.py`/`operation_service.py`/`operation_recovery.py`/
  `worker.py` are new files, structurally identical to
  `review`/`intake`/`execution`'s own operation ledgers (same lease-owned,
  optimistically-versioned, one-active-operation-per-session,
  crash-recoverable pattern, reusing `operations/lease.py` unmodified).
  `DiscoveryOperationAction` has three values: `LOCAL_QUESTIONS`,
  `IDEA_BRIEF` (both call the configured local model), and
  `FRONTIER_API_CALL` (calls the configured hosted frontier model, with
  the same explicit per-call spend-ceiling authorization ADR 0032's CLI
  already requires). `DiscoveryWorker` mirrors `IntakeWorker` exactly,
  including one startup recovery pass. Unlike review/intake/execution
  recovery, a crashed discovery operation never needs to "return"
  anything to human review -- `discovery.store.SQLiteDiscoveryStore` is
  only ever written to *after* the underlying model call already
  succeeded, so a crashed `RUNNING` operation simply leaves the session
  at whatever status it already had; the operator retries with a fresh
  operation.
- **Local clarification and brief**: `propose_local_clarification_
  questions()`/`propose_idea_brief_step()` (ADR 0032's existing service
  functions) gained an optional `local_provider` parameter purely for
  deterministic test injection -- production behavior is unchanged.
- **Idea brief approval**: a fast, synchronous, version-checked mutation
  (`approve_discovery_idea_brief()`), never auto-triggered by any model
  response.
- **Transport choice, export, and frontier response handling**: `export_
  discovery_frontier_package()` (synchronous, no model call -- building
  the package itself never calls a model) shows the same absolute-path/
  upload-instruction UX as the coding handoff for the manual transport.
  The API transport shows the configured provider, exact model, the
  worst-case per-call cost (`discovery.api.preview_frontier_planning_
  api_call()`, reusing `evaluation.spend_ceiling` unmodified, ADR 0030),
  and requires an explicit spend ceiling before `FRONTIER_API_CALL` is
  submitted as a durable, worker-executed operation.
- **Final plans**: a returned `kind="plan"` response routes into the
  *exact*, completely unmodified Plans UI (`#/plan/<id>/overview`) --
  proven directly by live-testing a full discovery-to-plan round trip and
  confirming the resulting plan renders on the ordinary Plans page with
  its normal sidebar/tabs, indistinguishable from a plan created via
  `apoapsis plan import`. Neither the local model nor the frontier model
  can auto-approve a brief or plan, or auto-start a slice -- every
  transition requires an explicit, separate user action, unchanged from
  ADR 0019/0027's existing boundary.

## Live evidence (2026-07-20)

Both flows were exercised end to end in a real Chrome browser against
disposable, freshly-initialized projects, using the real local
`qwen3-coder-next:q4_K_M` model over loopback Ollama for every local-model
step -- no hosted call was made anywhere (the API frontier-planning
transport was left unconfigured and not exercised).

- **Manual frontier coding handoff**: a task was driven to
  `HUMAN_REVIEW_REQUIRED` (`LOCAL_AGENT_ESCALATION_UNAVAILABLE`) via
  `apoapsis run` against the `download-service` fixture with a one-turn
  local budget. In the browser: exported the handoff package (absolute
  path shown, copy-path button present), pasted a hand-crafted response
  containing a real, correct resumable-download patch, saw the
  deterministic patch-policy preview and `TOKENS/COST: UNMEASURED` label,
  approved (step 1), applied (step 2, through the real background
  `ReviewWorker`) -- the patch applied cleanly, the project's real
  `unit-tests` command genuinely passed, and the task correctly stopped
  again at `HUMAN_REVIEW_REQUIRED` because no acceptance-designated
  command was configured (the project's default, un-opted-in
  `STRICT`-policy config, ADR 0017) -- exactly the intended fail-closed
  behavior, not a defect.
- **Discovery and frontier planning**: started a session with a typed
  idea; the real local model proposed 4 coherent clarification questions
  (harness-capped at the configured maximum); answered them verbatim;
  the real local model proposed an `IdeaBrief` with verbatim-checked
  constraints derived from the idea and answers; approved it (two-step);
  chose the manual transport; exported `FRONTIER-PLANNING-HANDOFF-
  <package_id>.md` (absolute path shown); imported a hand-crafted `plan`
  response bound to the exact package hash; the session reached
  `PLAN_IMPORTED` and linked directly to the resulting plan's normal,
  unmodified Plans UI page.
- Both flows were checked at 1440px and 1100px viewport widths with no
  layout breakage and no browser console errors observed.

### Two real, live-discovered bugs fixed in this same commit

1. **`review.case._fresh_evidence()` never recognized a manual-frontier
   apply round as "state advanced since the report."** After a failed
   manual-frontier apply correctly reclassified `ReviewCase.stop_reason_
   kind` to `VERIFICATION_FAILED`, `stop_reason_text` still showed the
   *original* escalation message from the never-updated `report.json`,
   because the staleness check only ever counted `LOCAL_CONTINUATION_
   STARTED`/`FRONTIER_CONTINUATION_STARTED` events. Fixed by also
   checking for `manual_frontier_apply_started` events. The same gap
   applied to `verification_results`/`acceptance_coverage`: they stayed
   empty/stale after a manual-frontier apply, even though real
   verification had just run and passed. Fixed by adding a
   `MANUAL_FRONTIER_ROUND_CONSUMED_EVENT` branch to `_fresh_evidence()`
   that reads `manual-frontier-verification-<operation_id>.json` (already
   written by `manual_frontier.apply`) and, when present, the STRICT
   acceptance-coverage payload now also carried on the workflow event
   (mirroring the existing `review_verification_retry_incomplete`
   convention exactly). Both fixes are covered by new deterministic
   assertions in `tests/test_manual_frontier.py`, not only by the live
   pass that found them.
2. **The generic eligible-actions grid duplicated the manual-frontier
   handoff as a raw, un-humanized `manual_frontier_handoff` card**
   alongside its own dedicated, fully self-contained section. Fixed by
   excluding it from the generic grid in `reviewActionPanel()`.

## Non-goals

- Does not exercise the API frontier-planning transport live (no
  `[models.frontier_coder]` was configured for this pass) -- the UI code
  path is covered only by deterministic fake-provider tests
  (`tests/test_discovery_ui.py`), consistent with "no hosted calls" for
  this milestone.
- Does not perform the unrelated full-site visual polish pass.
- Does not change `ArchitecturePlan`, `architect.validation.validate_
  plan()`, or Architect Mode's plan-to-slice execution boundary in any way.

## Tests

New `tests/test_manual_frontier_ui.py` (16 tests: export/import/approve/
apply two-step confirmation, stale task version, response hash mismatch,
replayed operation id, concurrent active-operation conflict, HTTP session/
origin authorization, HTTP reconnect via a fresh service instance, bundled
`app.js` action-string regression) and `tests/test_discovery_ui.py` (26
tests: session start/inspect, local-questions/idea-brief operations
completing via a real `DiscoveryWorker` background thread with a patched
fake local provider, question-count capping, full manual-transport flow
reaching `PLAN_IMPORTED`, stale-package rejection, clarification-round
ceiling enforcement, replayed/duplicate operation id rejection, concurrent
active-operation conflict, stale session-version rejection, crash recovery
reclaiming a `RECORDED` operation and marking a stale `RUNNING` one
`AMBIGUOUS`, HTTP session/origin authorization, HTTP reconnect, bundled
`app.js` action-string regression). `tests/test_manual_frontier.py` gained
two new assertions on the live-discovered staleness fixes. Full suite: 685
tests, 0 failures, 10 intentional skips.
