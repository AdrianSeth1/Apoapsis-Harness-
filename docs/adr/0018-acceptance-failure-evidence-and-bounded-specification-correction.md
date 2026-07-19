# ADR 0018: Acceptance-failure evidence and one bounded specification correction

- Status: Accepted
- Date: 2026-07-18

## Context

The first live STRICT evaluation
(`docs/evaluation/apoapsis-strict-live-evaluation-2026-07-18.md`) surfaced
two real, distinct reliability gaps, neither hypothetical:

1. In two of three attempts, the model genuinely discovered the
   acceptance-command catalog and proposed a correct mapping, applied a
   fix with only a narrow remaining bug, ran the mapped acceptance
   command -- and the harness told it `"deterministic verification
   passed"` even though that command had just failed. It then spent its
   entire remaining turn budget re-running the identical, unchanged
   check, because it had no evidence anything was wrong. The root cause:
   `_verify()`'s failure-evidence trigger and `_record_verification()`'s
   turn summary both keyed on `required`, and `resumable-acceptance-check`
   is deliberately `required = false` (ADR 0015 -- acceptance-only checks
   must not become development gates). Neither had ever considered a
   failing *optional but acceptance-designated* command.
2. In one of three attempts, specification extraction failed outright
   (`hard_constraints[*].verification_method: null`, a required field) and
   the task stopped at `FAILED` with no chance to recover, even though the
   fix was a one-line, obviously mechanical correction a second model call
   could very likely make given the exact error.

Both are corrected here. Neither changes retrieval, context compilation,
or the deterministic authority boundary: models still only propose; the
harness still decides evidence, retries, and completion.

## 1. A failing acceptance-designated command now produces real evidence

`VerificationCommandResult` gains `acceptance: bool` (default `False`),
populated by `VerificationRunner.run()` from the configured
`VerificationCommand.acceptance` for every command result, including
`SKIPPED` ones. This carries the authority marker into the immutable,
persisted result record itself -- audit consumers (and the fixes below)
never need to cross-reference current, mutable configuration to know
whether a past result was for an acceptance-designated command.

`FailureNormalizer.extract()`'s command-selection filter widens from
`item.required` to `item.required or item.acceptance`. `BoundedAgentSession
._verify()`'s failure-evidence trigger widens from `result.status !=
PASSED` to also fire whenever any *acceptance-designated* command in the
just-run set is `FAILED`/`TIMED_OUT`/`ERROR`, even while the aggregate
`VerificationResult.status` correctly stays `PASSED` (no required command
failed). `_record_verification()`'s turn-summary selection uses the same
widened `required or acceptance` filter, so the summary never again claims
`"deterministic verification passed"` while a mapped acceptance check just
failed.

**What does not change, by design:** `VerificationRunner`'s aggregate
`status` computation (`required_failures` still filters strictly on
`required`) is untouched -- an optional acceptance command's failure still
never fails the aggregate result, never blocks `_all_required_checks_passed
()`, and never turns into a required development gate. Only whether
*informative failure evidence and an accurate summary* are produced
changed. `_check_completion()`'s own acceptance-coverage computation was
already correct (it reads real per-command status, not the aggregate) and
needed no change. The existing "identical verification already ran"
duplicate-check rejection is unaffected and, structurally, can only ever
apply to a *second* attempt at an unchanged diff -- the first attempt at
any given digest still always executes for real and (with this fix) always
produces genuine evidence before any later duplicate is rejected.

`tests/test_verification.py` adds direct unit coverage of the widened
`FailureNormalizer` filter (failing/timed-out acceptance-only commands
selected; a non-acceptance optional command's failure still produces no
evidence, preserving that boundary) and confirms the aggregate status is
unaffected. `tests/test_acceptance_coverage.py` adds full bounded-agent
integration coverage: a failing optional acceptance command produces real
evidence and an accurate summary; the model can act on that evidence,
repair, and reach genuine `STRICT` completion; an unchanged duplicate
check is still rejected, but only after the original failure's evidence
was already produced; and a failing *required* command's behavior is
explicitly confirmed unchanged.

## 2. Exactly one bounded specification-extraction correction attempt

`SpecificationExtractor` gains `build_correction_prompt(request, task_id,
acceptance_catalog, previous_response, validation_errors)`, sharing the
same rules block, JSON schema, and acceptance-command catalog as
`build_prompt()` (both now built from one shared `_RULES` template, which
also gained an explicit rule that `HardConstraint.verification_method`
must be a non-empty string -- the exact failure mode observed live). The
correction prompt additionally embeds the exact Pydantic/JSON validation
error text and the model's own prior, invalid response verbatim, and
states plainly that this is the one bounded correction attempt.

`VerticalSliceRunner.run()` wraps the first `self.extractor.parse(...)`
call in a `try`/`except SpecificationExtractionError`. On failure, it
persists a `specification-extraction-failure-001.json` audit record (the
failed response and telemetry were already persisted by the preceding
`_model_call`, unconditionally, as for any call), then makes exactly one
more `_model_call` with the correction prompt and parses that response.
If the second parse *also* raises, the exception is not caught again -- it
propagates to the existing outer `except Exception: return self.
_handle_failure(exc)`, and the task stops deterministically at `FAILED`,
exactly as a single extraction failure already did before this change.
Nothing coerces, nulls, or weakens validation to force success; the
correction is a second, real model call subject to exactly the same
`TaskSpecification` validation, verbatim hard-constraint check, and
acceptance-catalog check as the first.

Because the correction call goes through the same `_model_call()` used
everywhere else, it automatically gets its own complete, immutable audit
record (context, context measurement, request, response, telemetry) --
no separate bookkeeping was needed to satisfy "record both calls and their
context packages." The correction call reuses `ModelOperation
.DRAFT_SPECIFICATION` rather than introducing a new operation, so the
existing `specification_think` inference-parameter override
(`models/local.py`, `workflow/vertical_slice.py._inference_parameters`)
continues to apply to it correctly without new wiring.

`tests/test_specification_correction.py` covers: a successful correction
reaching `COMPLETE`, with both calls' audit files and the correction
prompt's exact contents verified; a second failure stopping deterministically
at `FAILED` with a scripted *third* response never consumed (the retry
ceiling); and the correction path still enforcing exact verbatim-constraint
preservation and acceptance-catalog validation identically to the first
attempt. `tests/test_provider_and_specification.py` adds a direct unit
test of `build_correction_prompt()`'s exact contents.

## Authority and safety (unchanged)

Nothing here grants a model new authority. The harness still decides what
counts as evidence, still owns the one-correction ceiling (not the model,
which cannot request a second attempt), and still applies unmodified
validation to every response. The acceptance-designated flag remains
harness-configured; a model still cannot mark a command `acceptance = true`
or manufacture verification evidence. No new agent action, workflow state,
or transition was added.

## Non-goals

- Not a fix for the underlying return-value arithmetic bugs the live
  evaluation's model produced -- those are the model's own coding
  correctness, unrelated to harness evidence quality.
- Not a general retry framework for every model call; this is one
  narrowly-scoped, bounded correction for specification extraction only,
  chosen because it was the one live-observed, clearly mechanical failure
  class.
- Not a change to retrieval, context compilation, patch policy, or the
  held-out oracle.
- Does not re-run the live evaluation; that is the deliberately separate
  next step once this is reviewed.

## Consequences

A model mapping an acceptance criterion to a real, failing check now gets
a fair chance to see that failure and repair it, without acceptance
checks becoming a second required development gate. A single, likely
mechanical specification-extraction failure -- like the null
`verification_method` observed live -- gets one real chance at correction
with the exact error in hand, instead of stopping the task outright. Both
changes are narrowly scoped, fully covered by deterministic fake-provider
tests, and set up a fairer re-run of the live `local-strict` evaluation.
