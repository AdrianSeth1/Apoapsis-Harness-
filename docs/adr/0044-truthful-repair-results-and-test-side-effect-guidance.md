# ADR 0044: Truthful repair results and test-side-effect guidance

## Status

Accepted in the working tree on 2026-07-21. Coverage was added but not run at
the owner's request.

## Context

A Human Review continuation operation can finish successfully as an operation
while the resumed coding session still exhausts its budget and returns the task
to Human Review. The UI rendered the operation ledger status `succeeded` as a
green success pill, which could be mistaken for successful verification.

One observed repair added tests, ran them, received a concrete mock-interface
failure, and then spent its final two turns sending identical `replace_text`
requests. The test also left `token.json` and Python bytecode in the task
worktree. Changed-file reporting summarized those untracked files as directories.

## Decision

The UI derives a separate **Repair incomplete** presentation whenever a completed
continuation leaves the task at Human Review. It states explicitly that required
verification did not pass and dependencies remain blocked. The durable operation
record still truthfully remains `succeeded`: the authorized operation ran without
an infrastructure exception.

The primary repair action remains available whenever the freshest verification
failed, even when the newest stop classification is budget exhaustion, and it
authorizes up to ten additional turns within the existing configured ceiling.
When a terminal repair reaches `COMPLETE`, polling reads the authoritative task
record and navigates to its report instead of trying to reload a Human Review
case that correctly no longer exists. Incomplete repairs reload the fresh case.

Agent guidance requires realistic test-double return types and isolation of
filesystem side effects. Credential/token files must receive project-appropriate
version-control ignore rules and never be emitted as test data. Identical
replacement requests receive a precise diagnostic. Repository inspection
requests individual untracked file paths.

## Consequences

Users can no longer confuse a finished repair attempt with passing verification.
Models receive more actionable feedback for broken tests, and later slices do not
receive misleading directory-only changed-file summaries.
