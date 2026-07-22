# ADR 0045: Automatic final verification after unverified edits

## Status

Accepted in the working tree on 2026-07-21. Coverage was added but not run at
the owner's request.

## Context

A repair session correctly fixed a failing test, then exhausted ten turns while
revising the same mock and never requested another check. The harness therefore
returned to Human Review even though the current worktree was newer than its last
verification evidence.

## Decision

When the model turn budget ends, the bounded session runs one harness-owned full
verification if and only if the worktree has changes, verification budget
remains, and the current worktree fingerprint lacks results for one or more
configured commands. A complete current-fingerprint result is never rerun.

The harness alone executes the commands, computes pass/fail and strict acceptance
coverage, records normal verification artifacts, and decides completion. The
model receives no verification or workflow authority and no extra model turn.

## Consequences

A final valid edit can complete without another Human Review round merely because
the model forgot to request verification. A failing automatic check still returns
the task to Human Review with normalized evidence and an explicit stop reason.
