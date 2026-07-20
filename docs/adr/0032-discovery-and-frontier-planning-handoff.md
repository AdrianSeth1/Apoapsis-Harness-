# ADR 0032: Local-first Architect Mode discovery and frontier planning handoff

- Status: Accepted
- Date: 2026-07-20

## Context

ADR 0019 gave Apoapsis Architect Mode: a strong external model can propose an
`ArchitecturePlan` for an idea, but the operator has to hand-write the idea
text themselves and paste the exported package into a chat session with no
harness-mediated clarification step at all -- a plan generated from an
under-specified idea is a plan generated once, with no structured way to
improve the brief first. Separately, ADR 0006/0022/0031 already established
two distinct transports for reaching a frontier model (an explicitly
configured, authenticated API, or a manual subscription upload/paste), but
neither exists yet for the *planning* stage specifically -- only for coding.

This milestone adds a bounded discovery workflow in front of planning:

1. The user enters an idea.
2. A configured local model may propose up to a small, harness-enforced
   number of clarification questions.
3. The user answers in their own words; answers are preserved verbatim.
4. The local model may propose an `IdeaBrief`; only the user can approve it.
5. Only after approval does Apoapsis build an immutable frontier planning
   request package and send it to a frontier model over either transport.
6. The frontier stage may ask for a small, capped number of further
   clarification rounds, or return a complete plan.
7. A returned plan continues through the existing, completely unmodified
   Architect Mode import/validation/approval machinery (ADR 0019) -- this
   milestone adds no second way to create, validate, or approve a plan.

This is a bounded planning workflow throughout, never a general chat:
question counts and clarification rounds are always harness-enforced
ceilings, never trusted from a model's own claim about how many it needs.

## Decision

### Local discovery: reuses the local coding model, one bounded correction each

`discovery.local_model` calls the project's already-configured
`[models.frontier]` provider (the same "local/frontier drafting" model
`specification.extractor` already uses for turning natural language into
structured JSON the user approves -- no new configuration surface) for two
distinct, narrow proposals:

- `propose_clarification_questions()`: returns a `LocalQuestionsProposal`
  (`extra="forbid"`, no field but `questions`). The harness always caps the
  accepted count at `config.discovery.max_clarification_questions` (default
  5) regardless of how many the model actually returned -- never trusted
  from the model's own output.
- `propose_idea_brief()`: returns an `IdeaBrief` (`extra="forbid"`, no
  status/approval field of any kind). Every `key_constraints` entry's
  `verbatim_source` must be an exact, case-sensitive substring of the idea
  text plus the user's own answers -- the same verbatim-constraint
  discipline `specification.extractor` already enforces for
  `TaskSpecification`, applied here to the same `HardConstraint` schema.

Both proposals get exactly one bounded correction attempt on schema/
verbatim failure (ADR 0018's established precedent, reused verbatim): the
exact validation errors, the model's own prior response, and the same
schema/rules are sent once more; a second failure raises
`DiscoveryModelError` and the session's local-clarification step must be
retried explicitly, never silently coerced.

### Harness-owned, optimistically-versioned session state

`discovery.store.SQLiteDiscoveryStore` (own database,
`.apoapsis/discovery-sessions.db`) mirrors
`architect.store.SQLitePlanStore`'s concurrency discipline exactly: every
mutation checks both a caller-supplied `expected_version` and that the
session's *current* `DiscoveryStatus` is a valid source for the requested
transition (`IDEA_ENTERED -> LOCAL_QUESTIONS_PROPOSED ->
LOCAL_ANSWERS_RECORDED -> BRIEF_PROPOSED -> BRIEF_APPROVED ->
FRONTIER_PACKAGE_EXPORTED -> (FRONTIER_CLARIFICATION_PROPOSED ->
FRONTIER_ANSWERS_RECORDED -> FRONTIER_PACKAGE_EXPORTED)* -> PLAN_IMPORTED`,
plus a `FAILED` terminal reachable from any non-terminal state). Local
clarification questions are optional ("may propose"), so `record_idea_brief`
is also reachable directly from `IDEA_ENTERED`, not only after a local Q&A
round actually happened. No field on `DiscoverySessionRecord` is ever set
from anything a model claims about the session's own state.

### The immutable frontier planning request package

`discovery.frontier_package.build_frontier_planning_request_package()`
reuses `ContextCompiler`/`GitRepository` exactly like Architect Mode's own
`architect.package.build_planner_request_package()` -- no parallel evidence
format -- and adds what that package intentionally omits: the approved
`IdeaBrief` and verbatim local (and, on a later round, prior frontier)
Q&A, `active_hard_constraints` (the brief's own `key_constraints`, already
verbatim-checked), and the configured `[architect.ceilings]`. It also
carries the exact `ArchitecturePlan` JSON schema and the exact
`FrontierPlanningResponseEnvelope` JSON schema, so a frontier model (either
transport) can be schema-checked deterministically. `package_sha256`
excludes `package_id`/`generated_at` (ADR 0026's established convention),
and `verify_package_integrity()` detects on-disk tampering, exactly
mirroring `manual_frontier.package`'s (ADR 0031) equivalent functions.

### The strict response envelope: exactly one variant, never completion authority

`FrontierPlanningResponseEnvelope` (`extra="forbid"`) may set exactly one
of `clarification_questions` (a bounded list) or `plan` (a complete
`ArchitecturePlan`) -- a `model_validator` enforces this exclusivity
outright; a response claiming both, or neither, fails schema validation
before anything else runs. There is no status, completion, approval, or
command-selection field anywhere in this schema. Neither model can approve
a plan (`ArchitecturePlan` itself still has no such field, ADR 0019,
unchanged), invent a verification-command name (`architect.validation
.validate_plan()` still rejects that, unchanged), bypass a ceiling, execute
a slice, or choose a workflow transition.

### Clarification rounds are capped at a small, deterministic maximum

`config.discovery.max_frontier_clarification_rounds` (default 2) bounds how
many times the frontier stage may return `clarification_questions` instead
of a plan. `discovery.response.apply_frontier_planning_response()` --
the single function both transports hand a validated envelope to -- rejects
a `clarification_questions` response outright
(`ClarificationRoundCeilingExceededError`) once the package's own
`frontier_round` exceeds the configured maximum, forcing the frontier
model to return a complete plan instead on the next response for that
package. `apply_frontier_planning_response()` also rejects a response
whose package/session no longer matches the session's current outstanding
package (`StaleSessionError`) -- a response to a stale or superseded
package is never silently applied.

### Two transports, one shared response path

- **Manual** (`discovery.manual`): writes the canonical JSON package plus a
  single, self-contained `FRONTIER-PLANNING-HANDOFF-<package_id>.md` (the
  same "one file, nothing external referenced" discipline as
  `manual_frontier`'s ADR 0031 Markdown export) the user uploads by hand.
  `import_manual_frontier_planning_response()` checks response size (before
  any JSON parsing), UTF-8 validity, schema validity, and package-hash
  self-consistency, then calls `apply_frontier_planning_response()`.
  Tokens and cost are never recorded for this transport -- there is
  nothing to measure on a manual paste, and none is ever displayed as a
  fabricated `0`. `declared_model_name` is required, non-empty,
  operator-typed provenance only; Apoapsis never verifies which model
  actually produced a response. **No subscription website is ever
  automated.**
- **API** (`discovery.api`): requires `[models.frontier_coder]` to be
  configured (`FrontierPlanningApiNotConfiguredError` otherwise).
  `preview_frontier_planning_api_call()` deterministically shows the
  configured provider/model and a pessimistic worst-case cost for one
  call (reusing `evaluation.spend_ceiling.estimate_worst_case_call_cost_usd`
  exactly, ADR 0030 -- no duplicated estimation logic), with zero calls
  made, before any separate authorization. `run_frontier_planning_api_call()`
  requires an explicit `authorized_max_spend_usd` ceiling for that one
  call, wraps the real provider in `evaluation.spend_ceiling
  .SpendCeilingModelProvider` (the exact same class ADR 0030 built,
  reused unmodified) so the call is refused outright before it is even
  attempted if the worst-case estimate would exceed the ceiling, and
  re-checked after completion using the real recorded cost. Real,
  measured token/cache/latency/cost telemetry is always persisted as an
  audit artifact -- this transport never reports `unmeasured`.

### Final plans reuse the existing, completely unmodified import/validation/approval machinery

When a response's `kind` is `plan`, `apply_frontier_planning_response()`
writes `response.json`/`plan-v1.json` (the same audit-artifact shape
`architect.importer.import_planner_response()` already writes) and calls
`SQLitePlanStore.create_plan()` -- the *exact same function*
`import_planner_response()` calls internally, not a reimplementation.
`PlanRecord.package_id`'s pattern is widened additively from
`^PKG-[A-Za-z0-9._-]+$` to `^(PKG|FPKG)-[A-Za-z0-9._-]+$` so a
discovery-flow package id (`FPKG-...`) is accepted by the same store a
manually-exported Architect Mode package id (`PKG-...`) always was --
existing `PKG-...` rows and callers are completely unaffected. The
resulting `PLAN-...` record is from that point on an entirely ordinary
plan: `apoapsis plan validate`/`apoapsis plan inspect`/`apoapsis plan
approve` (unmodified, ADR 0019) work on it exactly as they always have,
and `tests/test_discovery.py` proves this directly by running the
unmodified `validate_plan()`/`SQLitePlanStore.record_validation()`/
`SQLitePlanStore.approve_plan()` against a plan that arrived through this
new path. **Planning never executes a slice** -- nothing here changes
Architect Mode's own "produces a plan, never runs it" boundary (ADR
0019/0027).

### CLI: a full seam, no UI yet

`apoapsis discover start/inspect/propose-questions/answer-questions/
propose-brief/approve-brief/export-frontier-package/preview-api-call/
call-api/import-manual-response/answer-frontier-questions`
(`src/apoapsis/cli/app.py`). `export-frontier-package --transport
{api,manual}` requires the idea brief already approved. The API transport's
`call-api` requires `--authorize-planning-spend-usd`; `preview-api-call` is
a separate, zero-cost, zero-call command an operator runs first. No local
UI surface is added in this milestone -- see "Non-goals" below for the
exact next UI seam.

## Non-goals

- Does not automate ChatGPT's or Claude's website in any way for the
  manual transport -- no browser driver, no session cookie, no scraping.
- Does not store, reuse, or infer subscription credentials.
- Does not add a local UI surface for this path in this milestone.
  The exact next UI seam: a "New idea" / discovery screen mirroring the
  New Task screen's durable-operation pattern (ADR 0023) --
  `ApoapsisUIService` would need `start_discovery_session()`,
  `discovery_session_detail()`, `submit_local_questions_operation()`/
  `submit_idea_brief_operation()` (model calls, so these belong behind a
  background worker like `IntakeWorker`, not a synchronous HTTP handler),
  `approve_idea_brief()` (a fast, synchronous, version-checked mutation
  like `approve_specification()`), `export_frontier_planning_package()`,
  and `submit_frontier_planning_operation()` (the API call, also
  worker-backed) plus one new route group and screen; the manual-transport
  import step mirrors `manual_frontier`'s own not-yet-built UI seam (ADR
  0031's Non-goals) exactly.
- Does not add a second way to create, validate, or approve a plan --
  every plan produced by this path is an entirely ordinary `PlanRecord`
  from the moment `create_plan()` returns, indistinguishable from one
  created via `apoapsis plan import`.
- Does not change `ArchitecturePlan`'s own no-status/no-approval
  invariant, `architect.validation.validate_plan()`, or any part of
  Architect Mode's existing plan-to-slice execution boundary (ADR 0027).
- Does not persist a multi-process, cross-invocation spend ledger for the
  API transport -- `authorized_max_spend_usd` is a per-call ceiling for
  the one CLI invocation making that call, not a session-cumulative total
  tracked across separate process runs. A future UI-backed multi-round API
  flow within one long-lived process could share one `SpendLedger` across
  rounds; this milestone's CLI seam does not need to, since each round is
  its own separate, explicitly re-authorized invocation.

## Tests

New `tests/test_discovery.py` (20 tests, deterministic fake-provider
coverage throughout): local-model question-count capping regardless of
model output, one-bounded-correction-attempt recovery and double-failure,
brief verbatim-constraint-check pass/fail; full session state-machine
happy path, stale-version rejection, invalid-source-status rejection;
frontier package hash determinism and tamper detection; a full manual-
transport flow whose resulting plan is proven to flow, unmodified, through
`validate_plan()`/`record_validation()`/`approve_plan()`; stale-package,
response-hash-mismatch, malformed-response, and answer-mismatch rejection;
the clarification-round ceiling correctly accepting rounds 1-2 and
rejecting round 3; export-before-brief-approval rejection; and API-
transport coverage for "not configured", a worst-case preview, pre-call
spend-ceiling refusal (zero calls made), and a successful call persisting
real measured cost and completing the session. Full suite: 643 tests, 0
failures, 10 intentional skips.

## Consequences

An owner can now go from a one-line idea to an approved, ready-to-package
Architect Mode plan through a bounded, auditable discovery-then-planning
workflow -- with a local model doing the cheap clarification work first,
the user in full control of what gets approved at every step, and a
frontier model (API or manual subscription, operator's choice) doing the
actual architecture work only once, against a genuinely well-specified
brief. Every existing Architect Mode guarantee (no model approves,
executes, or invents authority) is completely unchanged; this milestone
only adds a better way to arrive at the idea a plan gets built for.
