# ADR 0042: Untracked test patches and verification repair

## Status

Accepted in the working tree on 2026-07-21. Deterministic coverage was added but
not executed because the owner explicitly requested no test execution.

## Context

`git status --porcelain` collapses a wholly untracked directory to an entry such
as `tests/`. Apoapsis compared that directory entry with the parsed patch's
individual paths, rejected valid new test files as unexpected changes, and could
leave a task at Human Review after verification failed. That stop then offered a
verification-only retry but no local repair continuation, even though code or
test changes were required.

Fresh projects also selected strict completion while the generated unit-test
command was not acceptance-designated. A task could therefore pass every
required check and still stop for a separate acceptance mapping that an ordinary
user had not configured.

## Decision

Patch application requests `--untracked-files=all` for both sides of its
before/after comparison. Policy therefore continues to reject paths outside the
proposal while comparing file paths to file paths.

Verification-failed and acceptance-incomplete Human Review cases offer a
bounded local continuation when the configured continuation ceiling permits it.
The continuation retains all existing version, fingerprint, budget, patch,
verification, and audit controls.

Newly generated configuration uses baseline completion: every required
verification command must still pass, but a separate acceptance-command mapping
is optional. Strict completion remains supported as an explicit project choice.

## Consequences

Models can add a new test package without a false `tests/` policy violation.
Failed checks can be repaired through the normal audited agent loop. Existing
projects keep their configured completion policy until the owner changes it.
