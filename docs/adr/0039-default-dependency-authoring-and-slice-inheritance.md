# ADR 0039: Default dependency authoring and plan-local slice inheritance

- Status: Accepted
- Date: 2026-07-21

## Context

Generated applications commonly need to declare runtime libraries. Rejecting all
dependency-manifest edits by default made otherwise-correct work predictably stop.
Plan slices also ran in isolated worktrees based on the user's current `HEAD`.
Completed slice changes therefore remained invisible to later slices until a human
manually committed and merged each task branch.

The owner prioritized consistent end-to-end execution, explicitly authorized
dependency edits by default, and required later slices to receive earlier slice
work. Models must still receive no Git, workflow-transition, verification, or
completion authority.

## Decision

`PatchPolicyConfig.allow_dependency_changes` and newly initialized project
configuration now default to `true`. The existing dependency-file allowlist,
path validation, changed-line/file ceilings, verification controls, and audit
records remain authoritative. A project may explicitly set the flag to `false`.

When packaging a plan slice, the harness examines other already-executed slices in
the approved plan. An inherited task must genuinely be `COMPLETE`; Human Review,
failed, running, or absent tasks are excluded. Dirty completed worktrees are checkpointed
with a deterministic harness-authored commit on their existing isolated task
branch. The user's checked-out branch and `HEAD` are never moved or merged.

The new immutable slice package records `execution_base_commit` and
`inherited_slice_ids`. Approval copies those values into the harness-owned task
event. Execution resolves that exact commit, creates the new task worktree from it,
and recompiles implementation context from the inherited worktree before any coding
call. Completed prior branches must form one ancestry chain; divergence fails closed
instead of allowing Apoapsis or a model to guess a merge.

Old package artifacts remain readable: a missing execution base falls back to the
package's recorded repository `HEAD`.

## Consequences

- Later slices see and verify the accumulated code from completed earlier slices.
- Main or the user's current branch is never changed automatically.
- Packaging is no longer entirely repository-read-only: it may commit verified,
  completed work on an Apoapsis-owned isolated task branch.
- Incomplete work cannot leak into another slice.
- Non-linear completed histories require an explicit human integration decision.

## Verification

Deterministic coverage was updated to require checkpointing, unchanged project
`HEAD`, an inherited execution-base commit, and real prior code in the next slice's
worktree. Configuration coverage now expects dependency edits enabled by default.
Per the owner's explicit request, no tests, compilation check, Git apply probe, or
diff check were run for this change.
