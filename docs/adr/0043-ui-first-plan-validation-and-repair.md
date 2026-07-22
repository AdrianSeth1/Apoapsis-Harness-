# ADR 0043: UI-first plan validation and verification repair

## Status

Accepted in the working tree on 2026-07-21. Coverage was added but not run at
the owner's request.

## Context

The plan overview told users to run `apoapsis plan validate` in a terminal.
Human Review exposed generic continuation and verification actions, but a failed
check did not present the common recovery as one clear action. Both made routine
workflow progress depend on remembering command-line syntax.

## Decision

The plan overview exposes **Verify plan** while a plan is proposed. Its UI
service method calls the same deterministic validator, optimistic-versioned
store transition, and audit writer as the CLI path. It performs no model call,
project command, or slice execution.

A verification-failed or acceptance-incomplete Human Review case with remaining
local continuation budget exposes **Repair and verify**. Clicking it explicitly
authorizes a bounded five-turn-or-smaller local continuation through the existing
durable review operation. The existing version, worktree fingerprint, policy,
verification, budget, and audit checks remain authoritative.

## Consequences

Normal plan validation and verification repair are available entirely in the
local UI. CLI commands remain supported for automation and diagnostics.
