# ADR 0037: Allow bounded test authoring by default

- Status: Accepted
- Date: 2026-07-21

## Context

The initialized project configuration previously set
`patch.allow_test_changes = false` while its example verification command used
Python unittest discovery from `tests/`. In a blank repository, a slice that was
explicitly required to create tests could not make its own verification
contract viable. Apoapsis correctly failed before model spend, but the owner
then had to relax the same policy manually in every new project.

The owner explicitly requested that future projects allow the coding workflow
to create tests automatically. This is a patch-policy default, not a transfer
of filesystem or verification authority to a model.

## Decision

`PatchPolicyConfig.allow_test_changes` and the configuration emitted by
`apoapsis init` default to `true`. The bounded coding model may propose additions
or edits under recognized test paths, and Apoapsis continues to parse, validate,
apply, audit, and verify each accepted patch.

The existing safeguards remain unchanged:

- deleting tests is forbidden;
- dependency-file changes remain forbidden by default;
- verification-configuration changes remain forbidden;
- binary, secret-like, metadata, out-of-root, and excessive changes remain
  governed by deterministic patch policy;
- only configured verification commands can run, and only Apoapsis interprets
  their results or grants completion.

An owner can still set `patch.allow_test_changes = false` for a repository that
must protect its tests. The known-impossible verification preflight remains in
place for that explicit configuration.

Existing project configuration files are not silently rewritten by
`apoapsis init`; the current setting remains authoritative until the owner edits
it.

## Consequences

- Newly initialized blank projects can create their own test directory and
  test files as part of an approved coding task.
- Source and test changes remain subject to the same bounded proposal, patch
  validation, audit, and verification pipeline.
- A model still cannot delete tests, install dependencies, rewrite verification
  policy, run arbitrary commands, or decide that its own work passed.
- Repositories that forbid test edits must opt out explicitly and provide a
  viable pre-existing verification contract.

## Verification

Deterministic coverage proves that the configuration model and newly generated
project configuration allow non-deleting test changes by default, that an
explicit `false` still rejects them, and that test deletion remains rejected.
On 2026-07-21, 61 focused configuration/patch/execution/UI tests passed, and the
full deterministic suite passed 722 tests with 10 expected skips. Compileall
and `git diff --check` also passed. No live model execution was performed.
