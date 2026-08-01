# ADR 0097: Hash-bind the approved plan inside each slice package

## Status

Accepted and implemented on 2026-08-01.

## Context

Plan validation and approval advance the plan record's optimistic workflow
version even when the plan content does not change. The Capability Sandbox
adapter incorrectly treated that workflow version as a plan-content artifact
number. A package approved at record version 5 could therefore refer to a plan
whose latest content snapshot was `plan-v3.json`; execution stopped before the
first model call while looking for a nonexistent `plan-v5.json`.

## Decision

Every newly built `PlanSliceExecutionPackage` carries the exact approved
`ArchitecturePlan` as `approved_plan`. That field participates in the existing
package hash and is consequently covered by the user's package approval. The
Capability Sandbox reads this embedded plan and never reconstructs authority
from the mutable plan database or guesses a snapshot filename.

`approved_plan` is optional only for backward-compatible reading of packages
created before this decision. Those packages continue to require their exact
version-named audit artifact and fail closed if it is absent; current database
state is never substituted.

## Consequences

- Approval binds the full plan consumed by native Qwen, not only a version
  counter and slice projection.
- Validation-only and approval-only version increments cannot make a newly
  packaged slice unexecutable.
- Existing package hashes and approval events remain valid.
- A pre-ADR package missing its exact legacy artifact requires an explicit,
  verified audit repair before retry.
