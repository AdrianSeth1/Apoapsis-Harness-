# ADR 0005: Bounded coding-agent action loop

- Status: Accepted
- Date: 2026-07-17

## Context

The one-shot vertical slice proved deterministic context packaging, patch
policy, worktree isolation, verification, repair limits, telemetry, and audit
recording. Local coding-model evaluations then showed that replacing the
frontier provider with a local model did not create the intended local coding
agent: the model could only return one complete patch and one repair patch. It
could not request more repository evidence, inspect the applied diff, run a
configured check, or respond across multiple bounded turns.

The original SOL thesis requires useful model iteration without transferring
workflow authority to the model.

## Decisions

1. SOL supports `one_shot` and `agent` execution modes. The one-shot path remains
   available as a reproducible baseline; newly generated configuration selects
   agent mode.
2. An agent response is exactly one schema-validated action. Allowed actions are
   literal repository search, bounded file read, current-diff inspection,
   incremental unified-diff proposal, exact text replacement, configured check
   execution, full verification submission, and escalation request.
3. Models receive no shell, filesystem handle, process API, or arbitrary command
   parameter. SOL validates and executes every action.
4. Repository reads are confined to tracked or unignored worktree files. `.git`,
   `.sol`, binary files, absolute paths, drive paths, and parent traversal are
   rejected. Search uses ripgrep without a shell and returns bounded,
   line-provenanced evidence.
5. Every turn receives an immutable context package written before the provider
   call. Newly observed code, working diffs, and normalized failures enter a
   bounded evidence ledger with path, line, commit/worktree state, reason, and
   digest.
6. Patch proposals continue through the existing parser, policy validator, and
   `git apply --check` path. Test, dependency, verification-configuration,
   binary, excessive, and escaping changes remain forbidden by policy.
   Exact replacements must match current text once; SOL deterministically turns
   them into unified diffs before applying the same checks.
7. A model may request only a named configured check. It may submit the worktree
   for full configured verification, but only a passing required verification
   result completes the session. Identical checks are not rerun against an
   unchanged diff, and individually requested checks complete the session only
   when they collectively cover every required configured command.
8. Turn, patch-attempt, verification-run, read, search, and observation budgets
   are deterministic configuration. Budget exhaustion or an explicit model
   request produces `ESCALATION_REQUIRED`; it does not declare the task failed or
   complete.
9. This increment originally stopped at a persisted human-review state. Separate
   local/frontier provider roles and automatic bounded escalation are specified
   by ADR 0006.

## Consequences

Local coding models can now inspect, edit, test, and repair within a persistent
isolated worktree while remaining untrusted proposers. The fake-provider
integration proves a failing initial patch can receive exact verification
feedback, be incrementally repaired, and complete only after verification.

The controller is still not a container sandbox. Agent mode also increases
provider calls and repeated context transmission; its value must be measured by
accepted-patch rate, elapsed time, and cost rather than call count alone.
