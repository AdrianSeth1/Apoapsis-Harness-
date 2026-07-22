# ADR 0046: Complete slice contract context and no-progress guard

## Status

Accepted in the working tree on 2026-07-21. Deterministic coverage was added but
not run at the owner's request.

## Context

An approved Slice 4 package contained a detailed work brief, required interface,
exclusions, integration assumptions, stop conditions, and advisory paths. The
derived coding task retained only the one-line objective, inherited constraints,
and acceptance criteria. The local model therefore attempted unrelated scheduling
changes, emitted two malformed diffs, and then requested the same empty worktree
diff twelve times. Apoapsis accepted each redundant inspection and exhausted the
authorized continuation budget without changing files or running verification.

## Decision

Derived plan-slice task specifications preserve the complete approved execution
contract as traceable approved-decision facts. Verification command names are
also retained as verification requirements. Suggested paths and symbols remain
explicitly advisory and do not become a filesystem allowlist.

For tasks created before this decision, a local or frontier continuation reads
the exact immutable package identified by the task's approval event, verifies its
recorded hash when present, and enriches only the in-memory continuation context.
Existing task and audit artifacts are never rewritten.

After one empty diff inspection, another `inspect_diff` request against the same
unchanged worktree is rejected with a corrective instruction. Repeated unchanged
diffs and exact file reads that add no new evidence are rejected as well. The next
prompt states that the model must make a corrected edit and recommends
`replace_text` after malformed unified-diff markers. Three consecutive
no-progress observation violations stop early instead of consuming the rest of
the model-call budget.

The Human Review UI presents every eligible local continuation as **Repair and
verify**, including implementation stops that have not reached verification yet.
This is only a clearer entry point to the existing versioned continuation
service; it does not broaden eligibility or authority.

## Consequences

Coding and repair models receive the implementation contract the user actually
approved, including required interfaces and exclusions. Repeated empty inspection
loops become visible policy feedback and terminate quickly if the model ignores
it. The harness still does not invent source edits, execute model-selected shell
commands, expand patch authority, or decide completion from model output.
