"""Stops and refusals, written for the person who has to act on one.

The harness's own stop text is precise and, to an operator, unreadable. "No
witness survived validation, so nothing current proves anything about this
candidate" is exactly true and tells someone staring at a stopped run neither
what happened nor what to do. The internal vocabulary is not the problem to
solve -- EXOP, witness, obligation, behaviour unit all earn their keep inside
the harness -- but it should not be the first thing a person reads.

So every stop gets three parts, in this order, and no more:

* **what was attempted** -- the thing the operator asked for, in their words;
* **what refused it, and why** -- one sentence, naming the check rather than
  its implementation;
* **the one next action** -- singular on purpose. A stop that offers three
  options offers none, because choosing between them needs the knowledge the
  operator does not have.

The exact internal text is kept in `detail` and shown second. Nothing is
hidden; the ordering is the whole change.
"""

from __future__ import annotations

from apoapsis.reporting.operator_schema import OperatorExplanation
from apoapsis.review.schema import StopReasonKind
from apoapsis.workcell.acceptance import CheckpointOutcome
from apoapsis.workcell.session import SessionOutcome


_CHECKPOINT: dict[CheckpointOutcome, tuple[str, str, str]] = {
    CheckpointOutcome.COMPLETE: (
        "The sandbox finished this slice and Apoapsis inspected the result.",
        "Every check passed and every acceptance criterion is proved by a run "
        "the harness performed itself.",
        "Review the change, then continue to the next slice.",
    ),
    CheckpointOutcome.CONTINUE: (
        "The sandbox asked to be inspected part-way through this slice.",
        "The work was accepted so far, but some of the slice's criteria are "
        "not yet proved, so the run continues.",
        "Nothing to do -- the agent has another attempt and will be inspected "
        "again.",
    ),
    CheckpointOutcome.CANDIDATE_REFUSED: (
        "The sandbox produced a change and Apoapsis inspected it.",
        "The change was refused as a whole: it broke a limit or touched a "
        "path this project does not permit, so none of it was kept.",
        "Open the attempt's detail to see which limit or path was involved; "
        "if the limit is wrong for this project, change it in Settings and "
        "retry the slice.",
    ),
    CheckpointOutcome.HUMAN_REVIEW_REQUIRED: (
        "The sandbox finished this slice and Apoapsis inspected the result.",
        "The change was accepted, but something in this slice was marked as "
        "not automatically measurable, so completion is a human judgement "
        "rather than a check the harness can make.",
        "Review the change yourself and decide whether the slice is done.",
    ),
}


_SESSION: dict[SessionOutcome, tuple[str, str, str]] = {
    SessionOutcome.COMPLETE: (
        "Apoapsis ran this slice in the sandbox.",
        "It finished and passed inspection.",
        "Review the change, then continue to the next slice.",
    ),
    SessionOutcome.BUDGET_EXHAUSTED: (
        "Apoapsis ran this slice in the sandbox.",
        "The agent was still making progress when the run's allowance of "
        "attempts or time ran out.",
        "Retry the slice, or raise the allowance in Settings first if the "
        "slice is genuinely larger than the budget.",
    ),
    SessionOutcome.CANDIDATE_REFUSED: (
        "Apoapsis ran this slice in the sandbox.",
        "The change was refused as a whole: it broke a limit or touched a "
        "path this project does not permit.",
        "Open the attempt's detail to see which limit was involved, then "
        "retry the slice.",
    ),
    SessionOutcome.HUMAN_REVIEW_REQUIRED: (
        "Apoapsis ran this slice in the sandbox.",
        "Something here cannot be decided automatically and needs your "
        "judgement.",
        "Review the change and decide whether the slice is done.",
    ),
    SessionOutcome.COMPACTION_FAILED: (
        "Apoapsis ran this slice in the sandbox.",
        "The conversation outgrew the model's context window and could not be "
        "shortened safely, so the run stopped rather than continue on a "
        "context known to be incomplete.",
        "Retry the slice; if it happens again, the slice is too large for "
        "this model and should be split.",
    ),
    SessionOutcome.KERNEL_DRIFT: (
        "Apoapsis ran this slice in the sandbox.",
        "The approved task definition changed while the run was in progress, "
        "so the work no longer matches what was authorised.",
        "Retry the slice against the current approved plan.",
    ),
    SessionOutcome.AGENT_STOPPED: (
        "Apoapsis ran this slice in the sandbox.",
        "The agent stopped on its own without asking to be inspected, so "
        "there is no result to judge.",
        "Retry the slice; if it stops this way repeatedly, check the model "
        "service is healthy in Models & environment.",
    ),
}


_STOP_REASON: dict[StopReasonKind, tuple[str, str, str]] = {
    StopReasonKind.SPECIFICATION_NOT_APPROVED: (
        "Apoapsis prepared this task for execution.",
        "It has not been approved yet, and nothing runs without an approved "
        "specification.",
        "Read the specification and approve it to let the task run.",
    ),
    StopReasonKind.ROUTING_REQUIRES_HUMAN: (
        "Apoapsis assessed this task before running it.",
        "The configured routing rules do not permit it to choose a coder for "
        "work of this risk on its own.",
        "Choose explicitly whether to run it locally or escalate it.",
    ),
    StopReasonKind.ACCEPTANCE_COVERAGE_INCOMPLETE: (
        "Apoapsis checked which commands prove which acceptance criteria.",
        "At least one criterion has no command mapped to it, so passing "
        "checks would not actually show that criterion was met.",
        "Map every criterion to a command in Settings, or accept the task's "
        "result yourself.",
    ),
    StopReasonKind.VERIFICATION_FAILED: (
        "The coder finished and Apoapsis ran this project's checks.",
        "At least one check failed, and a failing check is never overridden.",
        "Read the failure, then use Repair and verify to give the coder "
        "another attempt at it.",
    ),
    StopReasonKind.LOCAL_AGENT_ESCALATION_UNAVAILABLE: (
        "The local coder worked on this task and could not finish it.",
        "Escalating to a stronger model was the next step, and no frontier "
        "model is configured.",
        "Add frontier credentials in Models & environment, or continue the "
        "task locally with a fresh attempt.",
    ),
    StopReasonKind.FRONTIER_AGENT_EXHAUSTED: (
        "The task was escalated to the frontier coder after the local coder "
        "could not finish it.",
        "The frontier coder used its full allowance without producing a "
        "verified result.",
        "Read the diff and failures, then either repair it yourself or "
        "download the frontier review handoff.",
    ),
    StopReasonKind.UNKNOWN: (
        "Apoapsis worked on this task.",
        "It stopped for a reason the review service could not classify from "
        "the task's own event history.",
        "Open the task's audit artifacts to see what the last recorded step "
        "was.",
    ),
}


def _build(parts: tuple[str, str, str], detail: str) -> OperatorExplanation:
    attempted, refusal, next_action = parts
    return OperatorExplanation(
        attempted=attempted,
        refusal=refusal,
        next_action=next_action,
        detail=detail,
    )


def explain_checkpoint(
    outcome: CheckpointOutcome, detail: str = ""
) -> OperatorExplanation:
    """The operator rendering of one checkpoint verdict."""

    return _build(_CHECKPOINT[outcome], detail)


def explain_session_outcome(
    outcome: SessionOutcome, detail: str = ""
) -> OperatorExplanation:
    """The operator rendering of how one sandbox session ended."""

    return _build(_SESSION[outcome], detail)


def explain_stop_reason(
    kind: StopReasonKind, detail: str = ""
) -> OperatorExplanation:
    """The operator rendering of why a task is waiting on a human."""

    return _build(_STOP_REASON[kind], detail)


__all__ = [
    "OperatorExplanation",
    "explain_checkpoint",
    "explain_session_outcome",
    "explain_stop_reason",
]
