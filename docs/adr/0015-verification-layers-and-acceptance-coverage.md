# ADR 0015: Verification layers and acceptance coverage

- Status: Accepted
- Date: 2026-07-18

## Context

The 1.0 profile-evidence run (`docs/evaluation/apoapsis-1.0-profile-evidence-2026-07-18.md`)
showed the gap this milestone closes: 5/6 attempts reached `COMPLETE` because
configured, model-visible verification passed, but only 1/6 was
independently correct -- 4/5 completions were false successes, caught only
because the *evaluation harness's* held-out oracle (ADR 0012) is a
side-channel that never touches production `apoapsis run`. Today,
`WorkflowState.COMPLETE` is reached purely because configured verification
passed (`agent/session.py`'s `BoundedAgentSession.run()`,
`workflow/vertical_slice.py`'s one-shot path); `TaskSpecification
.acceptance_criteria` is read by context compilation, research triggering,
and the UI, but was never consulted by the completion decision itself.

This ADR adds a real, product-level notion of "proven" that is independent
of the held-out oracle (which stays eval-only) and independent of whatever
the model claims. It does not touch retrieval, context compilation, the 64k
default, specification-extraction reliability, embeddings, multi-agent
execution, unrestricted shell access, or hosted credentials.

## Three verification layers

1. **Development verification** -- everything already in
   `config.verification.commands`; visible to the coding model via
   `run_check`/`submit_for_verification`, unchanged.
2. **User-approved acceptance verification** -- a subset of those same
   commands the user has explicitly designated strong enough to prove a
   criterion, via a new `VerificationCommand.acceptance: bool` flag
   (`verification/runner.py`, default `False`).
3. **Held-out evaluation oracle** -- ADR 0012, `evaluation/oracle.py`,
   completely untouched. It remains eval-harness-only, is never imported by
   `workflow/` or `agent/`, and proves nothing in production completion
   decisions.

## Acceptance coverage

A new module, `workflow/acceptance.py`, defines:

- `AcceptanceCoverageStatus`: `PROVEN`, `FAILED`, `UNPROVEN`.
- `AcceptanceCoverage`: one record per active acceptance criterion --
  `criterion_id`, `status`, `evidence_source`, `evidence_reference`, and a
  human-readable `reason`.
- `compute_acceptance_coverage(specification, configured_commands,
  passed_command_names)`: deterministic and stateless. For each of
  `specification.active_acceptance_criteria`, it reads
  `AcceptanceCriterion.verification_method` (a new optional field mirroring
  `HardConstraint.verification_method`) and classifies it:
  - unset -> `UNPROVEN` ("no verification command is mapped").
  - names a command that isn't configured -> `UNPROVEN`.
  - names a configured command that isn't `acceptance=True` -> `UNPROVEN`
    ("not an approved acceptance check").
  - names an acceptance command that hasn't passed -> `FAILED`.
  - names an acceptance command that has passed -> `PROVEN`.
- `acceptance_coverage_satisfied(coverage)`: `True` only when every entry is
  `PROVEN` (vacuously `True` for a specification with no active acceptance
  criteria -- strict policy never blocks on something never asked for).

A model may only ever *propose* `verification_method` (at
specification-drafting time, gated by the existing human specification
approval step -- no new approval mechanism was added) and may only ever
*request* a configured command by name (`run_check`, already validated
against config, already rejecting unknown names without corrupting the
task). There is no action through which a model can invent a command, mark
one `acceptance=True`, or assert a status directly -- coverage is
recomputed from configuration and real verification results every time it
is checked.

## Completion policy

`config.py` adds `CompletionPolicy`: `BASELINE` (default) or `STRICT`.
`ExecutionConfig.completion_policy` defaults to `BASELINE`, preserving every
existing config, test, and `apoapsis eval` run byte-for-byte -- this keeps
held-out false-success measurement comparable to what the 1.0 evidence run
just measured. `STRICT` additionally requires `acceptance_coverage_
satisfied()` before a workflow reaches `COMPLETE`.

**Agent mode** (`BoundedAgentSession._check_completion`): the single place a
turn is allowed to declare itself complete. If verification failed, this is
unchanged -- return to the model with evidence, exactly as today. If
verification passed and the policy is `BASELINE`, return `True` (today's
behavior). If `STRICT`, compute coverage; if satisfied, complete; if not,
attach one synthetic `FAILURE`-kind evidence entry (`EV-ACCEPTANCE-GAP`)
listing every non-`PROVEN` criterion and its reason, and return `False` --
the loop continues exactly like an ordinary verification failure, spending
the same turn/patch/verification budget, until coverage is proven, the turn
budget exhausts (existing `ESCALATION_REQUIRED` path, unchanged), or the
model explicitly requests escalation.

**One-shot mode** (`workflow/vertical_slice.py`): simpler, matching its
existing baseline-comparison status. At each of the two existing
`VERIFYING -> COMPLETE` call sites, under `STRICT` policy with passing
verification, coverage is computed from the commands that just passed; if
unsatisfied, the task transitions to `HUMAN_REVIEW_REQUIRED` with event type
`acceptance_coverage_incomplete` instead of `COMPLETE` -- one-shot's single
repair budget is not additionally spent chasing coverage. No changes were
needed in `workflow/states.py` or `workflow/engine.py`: `VERIFYING ->
HUMAN_REVIEW_REQUIRED` and the escalation-to-human-review edges already
existed.

## Reporting

`FinalTaskReport` gains `completion_policy`, `acceptance_coverage`,
`local_agent_budget` / `frontier_agent_budget` (configured ceilings --
actual usage already existed as `agent_turns`/`local_agent_turns`/etc.),
`frontier_available`, and `rejected_tool_requests` (a count of turns the
harness refused). `apoapsis eval`/`apoapsis eval-aggregate` need no schema
changes: `EvalLaneResult.report` already embeds the whole `FinalTaskReport`,
so the new fields flow through automatically, and existing aggregate
formulas are unaffected. The operator UI (`ui/static/app.js`) renders a
per-criterion Proven/Failed/Unproven table in the changes view and an agent
budget block (turns/patch/verification used vs. configured ceiling,
frontier availability/escalation state) in the report view, reusing the
existing `.pill`/`.card`/`.metric` styles -- no new route, service method,
or CSS architecture.

## Authority and safety

Nothing here grants a model new authority. The model still only proposes:
patches, verification requests, and (at specification time) a candidate
`verification_method` mapping. The harness alone decides which commands are
`acceptance`-designated, computes coverage, and owns the `COMPLETE` /
`HUMAN_REVIEW_REQUIRED` transition. `workflow/`/`agent/` never import
`evaluation/oracle.py`; the held-out oracle stays invisible to every prompt,
context package, and evidence record produced under either policy
(`tests/test_acceptance_coverage.py` asserts both the import graph and the
absence of oracle-related text in real audit output).

## Non-goals

- No new agent action for mid-session coverage proposals -- proposing a
  mapping happens once, at specification drafting, through the existing
  approval gate.
- No registry of "approved acceptance-check identifiers" beyond verification
  commands; a natural future extension point, not required here.
- No change to retrieval, context compilation, the 64k default, or
  specification-extraction retries.
- `STRICT` is opt-in; `BASELINE` remains the default everywhere, including
  the download-service evaluation fixture, so false-success measurement
  stays comparable across milestones.

## Consequences

Apoapsis can now express, per task, that "verification passed" and "the
product is actually done" are different claims, and can require the
stronger one without inventing a second workflow state machine or a new
authority boundary. Baseline behavior -- and the false-success rate the 1.0
evidence run just measured -- remains exactly reproducible until a project
opts into `STRICT` and maps real acceptance-designated commands.
