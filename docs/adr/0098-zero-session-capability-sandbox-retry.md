# ADR 0098: Recover zero-session Capability Sandbox failures with a fresh run

## Status

Accepted and implemented on 2026-08-01.

## Context

The product launcher checked `test -d <task-worktree>/.git`. Git represents
`.git` as a file in an attached worktree, so every real plan-slice worktree
failed the silent shell check before Docker or Qwen started. The resulting
human-review case nevertheless offered `local_continuation`; selecting it had
to fail because no local agent session existed.

## Decision

The launcher validates the harness with `git rev-parse`. For the task seed it
accepts a normal `.git` directory or resolves an attached-worktree `.git`
pointer, including the Windows absolute pointer produced by Windows Git. It
requires the seed to be unchanged, resolves its exact commit and common object
directory, and makes a disposable normal clone under the response runtime
directory. Docker receives that clone; the operator's worktree is never
rewritten to make its metadata Linux-readable. Every launcher preflight
failure writes a specific diagnostic, and
the Windows adapter records an exit-code diagnostic when a launcher produces
neither output nor a result artifact.

Human review derives continuation eligibility from actual session evidence. A
zero-session local stop never offers continuation. When its managed worktree is
present and unchanged, review instead offers an explicitly authorized fresh
local run. Execution rechecks the displayed fingerprint, removes only that
pristine managed worktree and task branch without force, transitions through
the existing approved-task path, and invokes the ordinary execution operation.

## Consequences

- A real Windows-created attached Git worktree becomes a normal disposable
  Linux-readable seed and passes Capability Sandbox preflight.
- The UI cannot promise to resume a session that does not exist.
- Changed worktrees, existing sessions, fingerprint drift, and unrecognized
  paths still fail closed.
- No compatibility fallback is introduced; the retry uses the configured
  local product path and all normal authorization, patch, verification, and
  audit controls.
