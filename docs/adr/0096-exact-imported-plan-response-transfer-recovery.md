# ADR 0096: Exact imported plan-response transfer recovery

Date: 2026-08-01

## Status

Accepted and implemented. Deterministic focused coverage only; the owner
explicitly requested that no full suite be run for this change.

## Context

The manual frontier-planning flow invites an operator to paste a response or
upload it as a JSON file. Saving that transfer file in the selected project
root is a natural action. The next automatic plan run then failed with
`DirtyParentRepositoryError`: Git correctly reported the untracked file, even
though Apoapsis had already validated the same response and retained a
canonical copy under `.apoapsis/discovery/`.

Weak fixes are unacceptable. Ignoring every JSON file, every filename
containing `plan`, or every untracked file would hide real project state and
break ADR 0026's guarantee that parent context agrees with the clean-HEAD
worktree. Deleting, moving, stashing, or committing the transfer would also
cross the repository authority boundary.

## Decision

Before an automatic/next-slice run packages its first slice, and again at the
ordinary execution dirty-parent preflight, Apoapsis may add a transfer file to
the repository-local `.git/info/exclude` only when all of these conditions
hold:

1. Git reports it as untracked and not already ignored.
2. It is a top-level file named `apoapsis-plan-response…json` using only the
   bounded transfer-name character set.
3. Its size is within the bounded transfer ceiling.
4. Its JSON, after the same BOM/code-fence normalization permitted by the
   importer, exactly equals a canonical `frontier-response-FPKG-*.json`
   payload already present in a discovery audit.
5. That canonical payload independently validates as a
   `FrontierPlanningResponseEnvelope`.

Only the exact root-relative filename is appended. The source file is not
opened for writing, moved, deleted, staged, or committed. Tracked files are
never eligible. An unreadable audit, altered transfer, nested file, different
name, or unrelated dirty path receives no recovery and the existing
`DirtyParentRepositoryError` remains authoritative.

The auto-run performs this before `package_slice` snapshots the repository so
the planning transfer cannot enter the package's repository inventory.
Ordinary execution repeats the idempotent check before its clean-parent guard,
covering direct task starts and plan runs created before this decision.

## Consequences

- Retrying either plan-run button recovers an already-imported response saved
  under the documented transfer filename without asking the operator to alter
  Git state manually.
- Genuine working changes remain fail-closed and continue to require an
  operator commit, stash, removal, or intentional ignore decision.
- The local exclusion and its explanatory comment are inspectable in Git
  metadata, while the immutable canonical response remains in Apoapsis audit.
- This changes no model role, tool authority, worktree construction,
  verification rule, or completion rule.

## Verification

Observed on Windows, 2026-08-01:

- `tests.test_plan_auto_run`: 8/8 passed, including an end-to-end automatic
  controller/fake-executor branch for an exact, code-fenced response recovery
  and a negative control for an altered lookalike.
- `tests.test_execution_authorization`: 11/11 passed, preserving tracked and
  unrelated-untracked drift refusals.
- `python -m compileall -q src tests` and `git diff --check` passed.
- No full suite was run, by explicit owner request.
- No model, provider, container, network, or live inference was used.
