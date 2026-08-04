"""How a contained session can end, and nothing else any more.

This module used to hold `SessionCoordinator`: a harness-owned loop that
rendered a stable kernel, drove an agent turn by turn, compacted history
against a budget clock, and called `run_checkpoint` itself. It was among the
best context-management code in the project and it was never wired to anything
-- referenced only by its own test, while every live slice ran through the
Capability Sandbox, where the CLI owns its own loop.

ADR 0109 removed it rather than leave a fourth pathway that looked live in the
tree and was not. The design is recorded in that ADR, and its four components
still exist as modules with their own tests: `workcell.context` (kernel
artifact, prompt layout, prefix-stability check), `workcell.compaction` (the
tiered policy), `workcell.budgets` (clock, token ledger, progress tracker) and
`workcell.checkpoint`. Anyone rebuilding a harness-driven mode assembles those
four; nothing about them was lost.

What survives here is the vocabulary a *contained* session still needs.
`SessionOutcome` names every way a session can end, and the operator-facing
renderings in `reporting.operator` are keyed on it, so it describes live
outcomes even though the loop that once produced them is gone.
"""

from __future__ import annotations

from enum import StrEnum


class SessionOutcome(StrEnum):
    """Every way a session can end. All of them are recorded, none inferred."""

    COMPLETE = "complete"
    #: Readiness said continue, but the owner's allowance ran out first.
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANDIDATE_REFUSED = "candidate_refused"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    #: Mechanical compaction was not enough and semantic compaction failed or
    #: was unavailable. A stop, never a silent continuation over a context that
    #: is known to be too full.
    COMPACTION_FAILED = "compaction_failed"
    #: The kernel artifact changed underneath a running session.
    KERNEL_DRIFT = "kernel_drift"
    #: The agent stopped asking for turns without requesting a checkpoint.
    AGENT_STOPPED = "agent_stopped"


__all__ = ["SessionOutcome"]
