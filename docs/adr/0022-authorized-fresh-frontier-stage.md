# ADR 0022: Explicit, human-authorized fresh frontier stage

- Status: Accepted
- Date: 2026-07-19

## Context

ADR 0020 gave a stopped, `HUMAN_REVIEW_REQUIRED` task exactly one way to
involve a frontier model from the review UI/CLI: `FRONTIER_CONTINUATION`.
That action only ever resumes a frontier session that already exists
(`frontier-agent-session.json` is present) -- it has no meaning for the far
more common case a review finds a stopped task under
`StopReasonKind.LOCAL_AGENT_ESCALATION_UNAVAILABLE` (the local agent
exhausted its budget or requested escalation, but no frontier coder was
configured at the time). There was no reviewable action at all to say
"a frontier model is configured now (or was all along) -- start a fresh,
bounded frontier stage from exactly where the local session stopped,"
short of an operator manually re-running the whole task outside the review
machinery. Overloading `FRONTIER_CONTINUATION` to also mean "start fresh"
would have made its eligibility rules (and its `additional_turns` input,
which only makes sense for an existing session) ambiguous.

Separately, the exact package-construction logic this needs --
normalizing the local session's failures, compiling frontier context,
computing the current diff, and building an `EscalationPackage` -- already
existed, inline, inside `VerticalSliceRunner._run_frontier_escalation`, the
automatic in-process escalation path used when a local agent session
exhausts its budget mid-run with a frontier coder already configured. That
logic needed to be reused exactly, not re-implemented, so a human-authorized
stage and an automatic escalation are guaranteed to build identical
packages from identical inputs.

## Decision

### A new, distinct action: `AUTHORIZE_FRONTIER_STAGE`

`ReviewActionKind.AUTHORIZE_FRONTIER_STAGE` is added alongside (not
replacing) `FRONTIER_CONTINUATION`:

- `FRONTIER_CONTINUATION` -- continues an existing frontier session.
  Unchanged.
- `AUTHORIZE_FRONTIER_STAGE` -- starts a **fresh** frontier stage from the
  local session's stop point, once a human explicitly approves it. Only
  ever offered while no frontier session exists yet for the task; once one
  does, `FRONTIER_CONTINUATION` is the only frontier action offered from
  then on.

`ReviewCase` gains `frontier_stage_exists: bool` (`True` iff
`frontier-agent-session.json` is present) and `frontier_model: str | None`
(the *currently* configured frontier model, or `None`). Both are computed
fresh from the live worktree and the live config every time a `ReviewCase`
is projected -- never cached from whatever was true at the original stop.
This means a user who adds `[models.frontier_coder]` to
`.apoapsis/config.toml` *after* a local-only stop sees
`AUTHORIZE_FRONTIER_STAGE` become eligible on the very next review-case
read, with no re-run of anything required.

`eligible_actions_for` only offers `AUTHORIZE_FRONTIER_STAGE` when:

- the stop reason is `StopReasonKind.LOCAL_AGENT_ESCALATION_UNAVAILABLE`,
- a frontier coder is configured (`frontier_available`), and
- `frontier_stage_exists` is `False`.

If a frontier coder is not configured, both frontier actions are removed.
If a frontier stage already exists, `AUTHORIZE_FRONTIER_STAGE` is removed
regardless of the stop reason (`FRONTIER_CONTINUATION`/other actions take
over per the existing rules).

### Escalation-package construction is extracted, not duplicated

`workflow/escalation.py` gains
`build_local_to_frontier_escalation(*, task_id, specification,
worktree_path, local_result, context_compiler, files_changed,
local_provider_name, local_model_name, frontier_provider_name,
frontier_model_name, frontier_budget, external_research_brief=None,
research_evidence_ids=None) -> tuple[ContextPackage, EscalationPackage]`,
extracted verbatim from what was previously ~40 lines inline in
`VerticalSliceRunner._run_frontier_escalation`. That call site now simply
calls the extracted function. `tests.test_agent_loop`,
`tests.test_vertical_slice`, and `tests.test_evaluation` (62 tests) pass
unchanged against the extraction, confirming it is behavior-preserving.

`EscalationPackage` gains a new required field, `frontier_budget:
AgentLoopConfig` -- the actual configured budget the fresh stage will run
under, so the package is a complete, self-describing record of what was
authorized, independent of whatever the audit trail around it says
elsewhere.

`_execute_authorize_frontier_stage` (`review/execution.py`) calls this same
function to build the package from: the task's existing local session
(`read_agent_session(task_directory, "")`), the current diff/changed files,
and `config.execution.frontier_agent` as the budget -- the full configured
ceiling, never a partial or accumulated one, since this is a new session,
not a continuation. The package is written to
`review-frontier-stage-<operation_id>.json` via the audit store **before**
the first frontier model call, exactly like every other review action's
artifact-before-call ordering.

### Execution reuses ADR 0021's boundary checks unchanged

`AUTHORIZE_FRONTIER_STAGE` is added to `_WORKTREE_CHECKED_ACTIONS`, so it
gets the same fresh re-projection, task-version check, worktree-fingerprint
check, and one-active-operation-per-task guarantee as every other review
action -- no new precondition-checking logic was written for it. A stale
worktree or task version between `prepare_review_operation` and
`run_review_operation` is rejected exactly like it is for continuations or
abandon.

On completion, the fresh `BoundedAgentSession` run either reaches
`COMPLETE` (via the existing generic
`review_continuation_patch_ready`/`_verification_recorded`/`_verification_
passed` event sequence, reused rather than duplicated) or exhausts its
budget, in which case the task returns to `HUMAN_REVIEW_REQUIRED` via new
event types `review_frontier_stage_escalation_required` and
`review_frontier_stage_requires_human` -- the latter classifies as
`StopReasonKind.FRONTIER_AGENT_EXHAUSTED`, which is exactly the state
`FRONTIER_CONTINUATION` already knows how to resume from.

### CLI and UI: no budget input, model and budget always shown

Unlike `local_continuation`/`frontier_continuation` (which take a
human-authorized `additional_turns` delta), `AUTHORIZE_FRONTIER_STAGE`
takes no turns/budget argument at all -- it always uses the full,
unmodified `config.execution.frontier_agent` budget. `apoapsis review
authorize-frontier-stage <task-id> --expected-version ... --expected-
fingerprint ... --operation-id ...` reflects this (no `--additional-turns`
flag exists for it). The UI's confirmation panel displays the exact
frontier model name and the configured turn/patch-attempt/verification-run
ceiling before the user can confirm -- the same fresh-not-cached
`frontier_model`/`configured_frontier_budget` values `ReviewCase` computes,
never a stale value from the original stop. Neither the CLI nor the UI ever
launches a frontier stage without this explicit confirmation step; nothing
in the harness starts one automatically.

## Tests

New `tests/test_review_frontier_stage.py` (8 tests): eligibility is
`False` while frontier is unconfigured, becomes `True` the moment a config
adds `frontier_coder` (re-checked fresh, not from the original stop, and
`FRONTIER_CONTINUATION` is correctly *not* offered instead), and flips back
to `False` (with `FRONTIER_CONTINUATION` taking over) once a frontier stage
already exists; forcing the action while ineligible raises
`InvalidReviewActionError`; a worktree changed between prepare and run is
rejected (`WorktreeChangedError`, operation `FAILED`, task untouched); two
prepares for the same task are rejected; a full fake-provider run
completing the task writes a correctly-populated
`review-frontier-stage-<id>.json` (checked for `frontier_model`,
`local_session`, `active_constraints`, `normalized_failures`, and
`frontier_budget.max_turns` matching the configured value) and a real
`frontier-agent-session.json`; a budget-exhausting run returns the task to
`HUMAN_REVIEW_REQUIRED` under `FRONTIER_AGENT_EXHAUSTED` with the eligible
actions correctly flipped (`FRONTIER_CONTINUATION` in, `AUTHORIZE_FRONTIER_
STAGE` out).

`tests/test_review_ui.py` adds
`test_authorize_frontier_stage_completes_via_background_worker`, run via
both `ReviewUIServiceTests` and its `ReviewUIServerTests` (HTTP-level)
subclass: rewrites the fixture's on-disk config to add `[models.
frontier_coder]` (simulating the user configuring frontier credentials
after the original local-only stop) and to raise `frontier_agent.max_turns`
enough for the fake session's action sequence (a fresh stage always uses
the full configured budget, with no `additional_turns` override available
to compensate), confirms `authorize_frontier_stage` is offered with the
correct `frontier_model` fresh from `review_case_detail`, submits it
through `ApoapsisUIService.submit_review_operation` and the real background
`ReviewWorker`, polls to a terminal status, and confirms both the operation
succeeded and the task reached `COMPLETE`, with the audit package on disk.

## Non-goals

- Does not add a CLI flag or UI input for overriding the fresh stage's
  budget -- it always uses the full configured `frontier_agent` ceiling.
- Does not change `FRONTIER_CONTINUATION`'s behavior, eligibility, or
  `additional_turns` semantics in any way.
- Does not add a way to run more than one fresh frontier stage per task;
  once `frontier-agent-session.json` exists, only `FRONTIER_CONTINUATION`
  is ever offered again.
- Does not change `workflow/states.py`; the fresh stage's transitions reuse
  edges (`HUMAN_REVIEW_REQUIRED` \<-\> `IMPLEMENTING`/`VERIFYING`/
  `ESCALATION_REQUIRED`/`COMPLETE`) that already existed for continuations.

## Consequences

A reviewer can now explicitly authorize spending a hosted/frontier model's
budget on a task that stopped locally, with the exact model and budget
shown before confirming, without that authorization ever being confused
with (or silently reusing the input shape of) resuming a session that
already exists. The escalation-package construction that automatic
in-process escalation and this human-authorized path both need now has
exactly one implementation, verified behavior-identical to what
automatic escalation already did.
