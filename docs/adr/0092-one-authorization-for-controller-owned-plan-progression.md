# ADR 0092: One authorization for controller-owned plan progression

- Status: Accepted
- Date: 2026-08-01

## Context

An approved Architect plan was unnecessarily difficult to execute. For every
slice the operator had to open the slice, package it, inspect and approve the
package, open the derived task, confirm execution, wait, return to the plan,
and repeat. None of those repeated clicks added a new decision when the desired
policy was already clear: continue through dependency-ready slices while every
controller-owned gate passes, and stop at the first problem.

Browser scripting cannot safely solve this. A closed tab would end the sequence,
and client-side code would have to infer authoritative workflow state.

## Decision

Add a durable plan-run operation with two modes:

- **Auto mode** packages, hash-binds, approves, executes, and verifies each
  dependency-ready slice in order, then repeats only after the current slice is
  authoritatively `COMPLETE`.
- **Next slice only** performs the same controller-owned sequence once and
  stops after one complete slice.

The operator authorizes one exact approved plan version and the digest of the
current effective configuration. This is advance user authorization for the
controller to create the derived tasks and approve only the immutable packages
it deterministically builds from that plan. It is not model approval.
Automatic package approval is recorded as a system event carrying the durable
plan-run id, rather than being misreported as a fresh per-slice user click. If
a slice was packaged manually before the run, the controller rebuilds that
package against the authorized configuration and current repository state
before approving it; an older preview is never silently reused.

Any configuration or plan-version drift stops before packaging. A dependency
block, verification failure, human-review state, superseded package, or failed
operation stops the run. A queued operation can be reclaimed after restart
because no model call began; a run found `RUNNING` after restart becomes
`AMBIGUOUS` and is never repeated automatically. Final integrated verification,
delivery preparation, branch movement, and merging remain separate.

The browser submits and polls the durable record. It never packages a slice,
calls a provider, runs a command, decides completion, or advances state.
The worker participates in the application's explicit shutdown lifecycle, so
closing the app does not leave an idle plan-run thread attached to the project.

## Consequences

The normal happy path needs one confirmation instead of several interactions
per slice. Operators can still use every existing package/approve/task control
individually, and can select one-slice pacing without changing configuration.

This ADR does not select the coding execution implementation. In particular it
does not rename ADR 0071 Local Power as the qualified ADR 0077 Capability
Sandbox, and it does not make either execution path the default.

## Verification

Deterministic coverage exercises the durable ledger, one-active-run rule,
configuration drift refusal, one-slice pacing, multi-slice auto progression,
service authorization, HTTP routing, and the rendered controls. Model execution
is replaced with a fake controller completion in those tests; no live inference
is claimed by this ADR.
