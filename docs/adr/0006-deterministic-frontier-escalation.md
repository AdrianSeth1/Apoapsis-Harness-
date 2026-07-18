# ADR 0006: Deterministic local-to-frontier escalation

- Status: Accepted
- Date: 2026-07-17

## Context

ADR 0005 gave an untrusted coding model a bounded inspect-edit-test loop. It
proved that Coder-Next Q4 could complete the controlled download-service task,
but an exhausted or explicitly escalated local session still stopped for human
review. The product thesis requires a stronger provider to receive the evidence
accumulated locally without allowing either model to choose workflow authority,
budgets, transmitted context, or completion.

## Decisions

1. Coding providers have explicit local and frontier roles. The existing
   `models.frontier` entry remains the backwards-compatible specification and
   one-shot provider; `models.local_coder` overrides the first agent stage and
   `models.frontier_coder` is used only for authorized frontier execution.
2. Agent routing is deterministic. Explicit routes are `local_only`,
   `local_then_frontier`, and `frontier_only`. Under `auto`, low, medium, and
   unclassified tasks run local-first; high-risk tasks go directly to an
   available frontier provider; critical-risk tasks require human review.
3. Only Apoapsis authorizes escalation. A model may request it, and local budget
   exhaustion or a failed local provider call also triggers it, but the
   configured route and provider availability determine whether a frontier call
   occurs. A failed frontier provider call requires human review.
4. Local and frontier stages have independent turn, patch, verification, search,
   read, and observation budgets. Frontier exhaustion always ends in human
   review; it cannot recursively escalate.
5. The frontier stage continues in the same isolated task worktree. Existing
   accepted local changes remain visible and every frontier edit passes the same
   parser, path/dependency/test/verification/binary/size policy, and Git apply
   checks.
6. Before the first frontier call, Apoapsis writes an immutable
   `frontier-escalation-package.json` containing:
   - the original approved specification;
   - every active verbatim constraint;
   - local and frontier provider identities;
   - the complete local agent session and stop trigger;
   - the exact current diff and its SHA-256 digest;
   - every normalized local verification failure, including exact command and
     relevant error;
   - the deterministic frontier context digest.
7. Frontier context is freshly compiled from the current worktree using local
   failure terms and changed paths. It includes the current diff, relevant source
   and test excerpts, normalized failures, and a compact local attempt history,
   all with normal context provenance.
8. Provider role and structured-response schema are included in each hashed call
   package. Telemetry and final reports aggregate both stages while retaining
   separate local/frontier turn and verification counts and the escalation
   artifact path.
9. A frontier model remains an untrusted proposer. Only complete coverage of the
   configured required verification commands can transition the task to
   `COMPLETE`.

## Consequences

Apoapsis can now perform the intended local-first workflow and spend frontier tokens
only after a bounded, auditable local attempt. The fake-provider integration
proves that a failing local patch can be repaired by a separate frontier model
in the same worktree and that frontier budget exhaustion safely requires human
review.

The default generated configuration intentionally contains no hosted frontier
credentials, so it remains fully local until the user adds
`models.frontier_coder`. This increment does not add learned routing, autonomous
provider selection, recursive agents, arbitrary tools, or a container sandbox.
