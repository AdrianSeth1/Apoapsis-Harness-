# ADR 0012: Held-out correctness oracles and evaluation aggregation (Apoapsis 1.0c)

- Status: Accepted
- Date: 2026-07-18

## Context

The original evaluation report compared lanes within one run but could not
aggregate rates or deltas across runs. More importantly, normal verification
passing was both Apoapsis's completion signal and the only correctness signal,
so a false-success rate was not independently measurable. Hosted rescue and
savings metrics also lacked real hosted evidence and must not be fabricated
from deterministic fake providers.

## Decisions

1. `EvalLaneResult` records evidence provenance (`deterministic_fake`,
   `live_local`, or `live_hosted`), patch attempts, deterministic policy
   rejections, and an optional held-out oracle result. Provider-call telemetry
   now records the model role so frontier-coding usage can be separated from
   specification and local calls without parsing prompts.
2. The download-service resumable acceptance tests live in their own source
   file. `apoapsis eval download-service` excludes that file before the lane's
   fixture repository is initialized and committed. Before any model call, the
   harness verifies both the declared path and exact source digest are absent
   from tracked fixture files. Only after ordinary verification declares
   `COMPLETE` is the oracle copied under a reserved temporary filename into the
   completed worktree and run through the configured `ExecutionBackend`; it is
   removed afterward. Its identifier, version, source digest, duration, result,
   command result, and audit location are persisted in `held-out-oracle.json`.
3. A valid oracle pass/fail is an independent evaluation result, not a workflow
   transition. An oracle failure after `COMPLETE` is a false success. Oracle
   execution/backend errors invalidate that measurement and are counted
   separately; no oracle or a zero denominator reports `null`/`unmeasured`,
   never zero.
4. `aggregate_evaluations()` combines persisted `comparison.json` reports and
   computes local-only verified completion, live hosted frontier rescue,
   overall completion, human review, unsafe-patch rejection, false success,
   median/p95 latency, transmitted files/lines, per-profile summaries, and
   paired local-versus-one-shot results. The CLI command
   `apoapsis eval-aggregate <comparison.json>...` writes `aggregate.json` and
   `aggregate.md` without invoking a provider.
5. Hosted calls/tokens/cost saved and performance versus direct frontier are
   measured only from an identical-task pair containing a lane explicitly
   recorded as `live_hosted`. With no such pair, every value is null with an
   explanatory reason. Fake-provider tests validate formulas and unmeasured
   states but are never promoted to real evidence.

## Authority and safety

The oracle command, timing, retry count (none), and execution backend are owned
by the evaluation harness. A model never sees or selects the oracle, never gets
its failure as repair context, and receives no additional action. Aggregate
reports consume immutable task/evaluation artifacts and make no provider calls.

## Non-goals

- No secret-task methodology: task requirements may describe expected behavior;
  only the independent oracle implementation stays outside model context.
- No real hosted run or spending in this milestone.
- No claim that fake-provider completion rates describe model quality.
- No second fixture until repeated download-service profile/lane evidence
  justifies its design cost.

## Consequences

False success is now measurable with an independent deterministic oracle, and
zero means an observed zero rather than missing evidence. Cross-run reports can
compare profiles and lanes while preserving the difference between fake, live
local, and live hosted evidence. Hosted economic metrics remain honestly
unmeasured until credentials and paired real runs exist.
