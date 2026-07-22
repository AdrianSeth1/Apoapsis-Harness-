# ADR 0038: Deterministic reconstruction of malformed new-file diffs

- Status: Accepted
- Date: 2026-07-21

## Context

A live blank-repository slice used all eight patch attempts proposing complete
new Python files. Every proposal had the required Git file headers but omitted
the outer `+` marker from one or more top-level function definitions and reused
an inaccurate new-hunk line count. The parser rejected every proposal before a
worktree change, and the model repeated the same formatting defect despite
receiving the exact rejection in session history. No verification command ran.

The same run also exposed a contradictory prompt: project policy allowed test
creation, while the static agent instructions still said never to modify tests.

## Decision

The unified-diff parser reconstructs only the narrow, unambiguous case of a
single-hunk text new-file diff whose old path is `/dev/null` and whose hunk has
zero old-side lines. Every physical body line in that shape is necessarily new
file content. Apoapsis adds a missing outer `+` marker and recomputes the added
line count before normal patch policy and `git apply --check` run.

The original proposal remains in the audit trail. When reconstruction changes
it, the existing normalized-patch artifact records the exact canonical diff
that policy validated and Git applied.

Existing-file edits, deletions, binary patches, malformed metadata, and
multi-hunk new-file patches remain strict. This does not infer code semantics,
select paths, bypass policy, run commands, or grant completion.

The bounded-agent prompt now receives the effective `allow_test_changes` and
`allow_dependency_changes` values. It permits test additions/edits only when
configured, always forbids test deletion, and leaves Apoapsis as the sole patch
and verification authority.

## Consequences

- A common local-model formatting defect no longer consumes every patch and
  turn without changing the worktree.
- Incorrect new-file hunk counts are repaired from the actual proposed body,
  not guessed from model prose.
- Existing-file patch strictness is unchanged, where an unmarked line could be
  ambiguous between context, addition, or prose.
- Effective test policy and model instructions no longer contradict each
  other.

## Verification

Deterministic tests cover marker and hunk-count reconstruction, real Git
application of the normalized new file, continued rejection for an unmarked
existing-file edit, and effective patch-policy text in the agent prompt. Per the
owner's request, these tests were added but not run in this change.
