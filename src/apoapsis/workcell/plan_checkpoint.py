"""Slice 6: every repair passes through one authoritative checkpoint.

The Crisis Atlas trial produced its best result by having Codex repair what Qwen
proposed. That result was also not a deliverable, and the reason is the whole of
this module: the repair happened *beside* the state machine. It was a commit
somebody made, and the plan graph never learned about it. Later slices inherited
files without inheriting the checkpoint, and the final report still projected
the outcome of the run that had failed.

So a repair is not an edit here. It is a **state transition**, and it has
exactly one shape regardless of who proposed it:

    bind -> apply in controller state -> admit -> witness -> readiness
         -> required verification -> append authoritative checkpoint

Local Qwen, a genuinely stronger frontier model, and a human all enter through
`apply_repair` with the same `RepairProposal`. There is no second path, and
that is deliberate: a human repair that skipped verification would be exactly
the out-of-band commit this module exists to refuse, and the fact that a person
made it is not evidence about the tree.

**Binding is what makes staleness detectable.** A proposal names the parent
checkpoint, the base commit, the worktree fingerprint, the contract digest, and
the failure packet it answers. Any drift in any of those means the repair was
written against a tree or a contract that is no longer current, and it is
refused rather than applied hopefully.

**The ledger is append-only.** A superseded checkpoint is never edited and never
deleted; a child is appended and the head moves. That is what lets delivery ask
"what is authoritative now" and get an answer that cannot be an older report.

**Models remain untrusted proposers.** Nothing here gives a model authority to
transition state, run its own verification, declare completion, touch Git, reach
the host, or produce a delivery. A proposal is a request; the controller decides.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Callable

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.acceptance import CheckpointDecision, CheckpointOutcome

_SHA256 = r"^[0-9a-f]{64}$"
_COMMIT = r"^[0-9a-f]{40}$"

LEDGER_SCHEMA_VERSION = "1.0"


class ActorClass(StrEnum):
    """Who proposed a repair. Recorded, never trusted."""

    LOCAL_MODEL = "local_model"
    FRONTIER_MODEL = "frontier_model"
    HUMAN = "human"


class RepairRejection(StrEnum):
    """Why a proposal did not become a checkpoint.

    Distinct values rather than one failure, because each has a different
    repair and the caller needs to know which it got. A stale proposal should be
    rebased; a verification failure should not.
    """

    #: Names a parent that is not the current head.
    STALE_PARENT = "stale_parent"
    #: The tree it was written against is not the tree we have.
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    #: The contract changed under it.
    CONTRACT_DRIFT = "contract_drift"
    #: Answers a failure packet that is not the current one.
    FAILURE_PACKET_MISMATCH = "failure_packet_mismatch"
    #: Touches state the controller does not own.
    OUT_OF_BAND = "out_of_band"
    #: The apply step did not complete cleanly.
    PARTIALLY_APPLIED = "partially_applied"
    #: This exact proposal is already in the ledger.
    ALREADY_APPLIED = "already_applied"
    #: Admitted and re-verified, and still not passing.
    VERIFICATION_FAILED = "verification_failed"
    #: A "frontier" repair from the same endpoint as the local model.
    NOT_A_STRONGER_TIER = "not_a_stronger_tier"


class RepairBinding(StrictModel):
    """Everything a repair must still match to be applicable.

    Five bindings rather than one, because they drift independently. A repair
    can be written against the right commit and the wrong contract, or answer a
    failure packet that a previous repair already resolved.
    """

    parent_checkpoint_id: str = Field(pattern=_SHA256)
    base_commit: str = Field(pattern=_COMMIT)
    worktree_fingerprint: str = Field(pattern=_SHA256)
    contract_digest: str = Field(pattern=_SHA256)
    #: The failure this repair answers. Binding it stops a proposal written for
    #: one failure from being applied to a different one that happens to be
    #: current.
    failure_packet_sha256: str = Field(pattern=_SHA256)


class RepairProposal(StrictModel):
    """A request to change controller-owned candidate state.

    Carries no authority. `actor_fingerprint` is the exact model or human
    identity, recorded so a "frontier" repair can be checked against the local
    endpoint rather than believed.
    """

    proposal_id: str = Field(min_length=1)
    actor: ActorClass
    actor_fingerprint: str = Field(min_length=1)
    binding: RepairBinding
    #: What the proposal claims to touch. Compared against what the apply step
    #: actually changed; a mismatch is `PARTIALLY_APPLIED`.
    declared_paths: list[str] = Field(default_factory=list)
    rationale: str = ""


class PlanCheckpoint(StrictModel):
    """One authoritative state of the plan. Immutable once appended."""

    schema_version: str = LEDGER_SCHEMA_VERSION
    checkpoint_id: str = Field(pattern=_SHA256)
    parent_checkpoint_id: str | None = Field(default=None, pattern=_SHA256)
    slice_id: str = Field(min_length=1)
    contract_digest: str = Field(pattern=_SHA256)
    base_commit: str = Field(pattern=_COMMIT)
    worktree_fingerprint: str = Field(pattern=_SHA256)
    actor: ActorClass
    actor_fingerprint: str = Field(min_length=1)
    decision: CheckpointDecision
    witness_ids: list[str] = Field(default_factory=list)
    obligations_proved: list[str] = Field(default_factory=list)
    obligations_open: list[str] = Field(default_factory=list)
    #: The proposal that produced this, when it came from a repair. `None` for
    #: an original checkpoint.
    repair_proposal_id: str | None = None
    #: SHA-256 of the failure packet this checkpoint hands to a repair context.
    #: `None` on a completed checkpoint, which has no failure to answer.
    #:
    #: Bound because the other four bindings do not catch a specific mistake: a
    #: repair written for failure A, applied after a different repair already
    #: fixed A and left failure B current. Commit, tree, contract and parent can
    #: all still match while the proposal answers a question nobody is asking.
    failure_packet_sha256: str | None = Field(default=None, pattern=_SHA256)

    @staticmethod
    def packet_digest(packet: str) -> str:
        return hashlib.sha256(packet.encode("utf-8")).hexdigest()

    @property
    def is_authoritative_complete(self) -> bool:
        return self.decision.outcome is CheckpointOutcome.COMPLETE

    @staticmethod
    def compute_id(
        *,
        parent_checkpoint_id: str | None,
        slice_id: str,
        contract_digest: str,
        base_commit: str,
        worktree_fingerprint: str,
        actor: ActorClass,
        actor_fingerprint: str,
        outcome: str,
        repair_proposal_id: str | None,
    ) -> str:
        """A checkpoint's identity is its content plus its ancestry.

        Including the parent means two checkpoints with identical trees but
        different histories are different objects, which is what makes the
        ledger a chain rather than a set.
        """

        payload = json.dumps(
            {
                "parent": parent_checkpoint_id,
                "slice": slice_id,
                "contract": contract_digest,
                "commit": base_commit,
                "fingerprint": worktree_fingerprint,
                "actor": str(actor),
                "actor_fingerprint": actor_fingerprint,
                "outcome": outcome,
                "proposal": repair_proposal_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RepairResult(StrictModel):
    """What `apply_repair` concluded. Never raises for an ordinary refusal."""

    accepted: bool
    rejection: RepairRejection | None = None
    detail: str = Field(min_length=1)
    checkpoint: PlanCheckpoint | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> RepairResult:
        if self.accepted and self.checkpoint is None:
            raise ValueError(
                "an accepted repair must produce a checkpoint; without one "
                "there is nothing for a later slice to inherit"
            )
        if not self.accepted and self.rejection is None:
            raise ValueError("a refused repair must say which refusal it was")
        return self


class PlanCheckpointLedger(StrictModel):
    """Append-only chain of authoritative checkpoints.

    Nothing is edited and nothing is removed. A repair appends a child and the
    head moves; the failed parent stays in the ledger as history, which is what
    makes "what does delivery read" answerable without consulting a report.
    """

    schema_version: str = LEDGER_SCHEMA_VERSION
    checkpoints: list[PlanCheckpoint] = Field(default_factory=list)
    #: Proposal ids already applied, so a replay is detectable rather than
    #: silently producing a second checkpoint for the same work.
    applied_proposal_ids: list[str] = Field(default_factory=list)

    def head(self) -> PlanCheckpoint | None:
        return self.checkpoints[-1] if self.checkpoints else None

    def append(self, checkpoint: PlanCheckpoint) -> None:
        parent = self.head()
        expected = parent.checkpoint_id if parent else None
        if checkpoint.parent_checkpoint_id != expected:
            raise ValueError(
                "a checkpoint may only be appended onto the current head; "
                "appending elsewhere would fork the plan graph, which is the "
                "out-of-band repair this ledger exists to prevent"
            )
        self.checkpoints.append(checkpoint)
        if checkpoint.repair_proposal_id:
            self.applied_proposal_ids.append(checkpoint.repair_proposal_id)

    def lineage(self) -> list[str]:
        return [item.checkpoint_id for item in self.checkpoints]

    def authoritative_state(self) -> PlanCheckpoint | None:
        """What later slices and delivery must read.

        The head, always — never a stored report, never a Git commit somebody
        made outside this ledger. Crisis Atlas's final report projected the
        outcome of a superseded run because it had its own copy; a caller that
        goes through this function cannot.
        """

        return self.head()


#: Applies a proposal to controller-owned candidate state. Returns the paths it
#: actually changed. Raising is the correct response to a partial application.
RepairApplier = Callable[[RepairProposal], list[str]]

#: Re-runs admission, witness emission, readiness and required verification
#: against the repaired candidate. Returns the decision plus witness ids,
#: proved and open obligations, and the resulting fingerprint.
RepairVerifier = Callable[
    [RepairProposal], tuple[CheckpointDecision, list[str], list[str], list[str], str]
]


def apply_repair(
    ledger: PlanCheckpointLedger,
    proposal: RepairProposal,
    *,
    apply: RepairApplier,
    verify: RepairVerifier,
    local_model_fingerprint: str | None = None,
) -> RepairResult:
    """Take a repair from anyone through the one authoritative transition.

    Order is load-bearing. Binding is checked *before* anything is applied, so a
    stale proposal never touches candidate state; verification runs *after*
    application and its result decides whether a checkpoint is appended at all.
    A repair that fails verification leaves the ledger head exactly where it was.
    """

    head = ledger.head()
    if head is None:
        return RepairResult(
            accepted=False,
            rejection=RepairRejection.STALE_PARENT,
            detail=(
                "the ledger has no checkpoint to repair from; a repair must "
                "descend from an admitted state, not create one"
            ),
        )

    if proposal.proposal_id in ledger.applied_proposal_ids:
        # Refused rather than silently idempotent. Re-applying is almost always
        # a caller bug, and quietly returning the old checkpoint would hide it.
        return RepairResult(
            accepted=False,
            rejection=RepairRejection.ALREADY_APPLIED,
            detail=(
                f"proposal {proposal.proposal_id!r} is already in the ledger; "
                "re-application is refused so a replay cannot produce a second "
                "checkpoint for the same work"
            ),
        )

    if (
        proposal.actor is ActorClass.FRONTIER_MODEL
        and local_model_fingerprint is not None
        and proposal.actor_fingerprint == local_model_fingerprint
    ):
        # More turns from the same endpoint is a continuation, not a stronger
        # reviewer. The handoff names this explicitly as a non-goal.
        return RepairResult(
            accepted=False,
            rejection=RepairRejection.NOT_A_STRONGER_TIER,
            detail=(
                "a frontier repair may not come from the local model endpoint; "
                f"{proposal.actor_fingerprint!r} is the local model"
            ),
        )

    binding = proposal.binding
    for field, actual, rejection in (
        ("parent checkpoint", head.checkpoint_id, RepairRejection.STALE_PARENT),
        ("base commit", head.base_commit, RepairRejection.FINGERPRINT_MISMATCH),
        (
            "worktree fingerprint",
            head.worktree_fingerprint,
            RepairRejection.FINGERPRINT_MISMATCH,
        ),
        ("contract digest", head.contract_digest, RepairRejection.CONTRACT_DRIFT),
    ):
        claimed = {
            "parent checkpoint": binding.parent_checkpoint_id,
            "base commit": binding.base_commit,
            "worktree fingerprint": binding.worktree_fingerprint,
            "contract digest": binding.contract_digest,
        }[field]
        if claimed != actual:
            return RepairResult(
                accepted=False,
                rejection=rejection,
                detail=(
                    f"repair binds {field} {claimed!r} but the authoritative "
                    f"state is {actual!r}; it was written against a different "
                    "tree or contract and applying it would silently rebase it"
                ),
            )

    # Checked separately because the head only carries a packet when it is
    # unfinished. A repair proposed against a completed checkpoint has no
    # failure to answer and is refused for that reason rather than for a
    # mismatch.
    if head.failure_packet_sha256 is None:
        return RepairResult(
            accepted=False,
            rejection=RepairRejection.FAILURE_PACKET_MISMATCH,
            detail=(
                "the authoritative checkpoint records no failure packet, so "
                "there is no failure for this repair to answer"
            ),
        )
    if binding.failure_packet_sha256 != head.failure_packet_sha256:
        return RepairResult(
            accepted=False,
            rejection=RepairRejection.FAILURE_PACKET_MISMATCH,
            detail=(
                "the repair answers a failure packet that is no longer "
                "current; commit, tree and contract can all still match while "
                "the failure itself has been superseded by an earlier repair"
            ),
        )

    try:
        changed = list(apply(proposal))
    except Exception as exc:  # noqa: BLE001 -- a refusal, not a crash
        return RepairResult(
            accepted=False,
            rejection=RepairRejection.PARTIALLY_APPLIED,
            detail=(
                f"applying the repair did not complete ({type(exc).__name__}: "
                f"{exc}); candidate state is not advanced from a partial apply"
            ),
        )

    if proposal.declared_paths and sorted(changed) != sorted(proposal.declared_paths):
        return RepairResult(
            accepted=False,
            rejection=RepairRejection.PARTIALLY_APPLIED,
            detail=(
                f"the repair declared {sorted(proposal.declared_paths)} and "
                f"changed {sorted(changed)}; a partially applied repair is "
                "refused rather than verified as though it were whole"
            ),
        )

    decision, witness_ids, proved, open_obligations, fingerprint = verify(proposal)

    if decision.outcome is not CheckpointOutcome.COMPLETE:
        # No checkpoint is appended. The head stays where it was, so a later
        # slice inherits the last *authoritative* state rather than a repaired
        # tree that did not pass.
        return RepairResult(
            accepted=False,
            rejection=RepairRejection.VERIFICATION_FAILED,
            detail=(
                f"the repaired candidate did not pass required verification "
                f"({decision.outcome}): {decision.detail}"
            ),
        )

    checkpoint = PlanCheckpoint(
        checkpoint_id=PlanCheckpoint.compute_id(
            parent_checkpoint_id=head.checkpoint_id,
            slice_id=head.slice_id,
            contract_digest=head.contract_digest,
            base_commit=head.base_commit,
            worktree_fingerprint=fingerprint,
            actor=proposal.actor,
            actor_fingerprint=proposal.actor_fingerprint,
            outcome=str(decision.outcome),
            repair_proposal_id=proposal.proposal_id,
        ),
        parent_checkpoint_id=head.checkpoint_id,
        slice_id=head.slice_id,
        contract_digest=head.contract_digest,
        base_commit=head.base_commit,
        worktree_fingerprint=fingerprint,
        actor=proposal.actor,
        actor_fingerprint=proposal.actor_fingerprint,
        decision=decision,
        witness_ids=list(witness_ids),
        obligations_proved=list(proved),
        obligations_open=list(open_obligations),
        repair_proposal_id=proposal.proposal_id,
        failure_packet_sha256=(
            PlanCheckpoint.packet_digest(decision.repair_packet)
            if decision.repair_packet
            else None
        ),
    )
    ledger.append(checkpoint)
    return RepairResult(
        accepted=True,
        detail=(
            f"repair by {proposal.actor} accepted; authoritative state advanced "
            f"to {checkpoint.checkpoint_id[:12]}"
        ),
        checkpoint=checkpoint,
    )


class StaleProjection(RuntimeError):
    """Raised when a caller tries to deliver something other than the head."""


def authoritative_delivery_input(
    ledger: PlanCheckpointLedger,
    *,
    claimed_fingerprint: str | None = None,
) -> PlanCheckpoint:
    """The only thing final verification and delivery may serialise.

    `claimed_fingerprint` exists to catch the Crisis Atlas shape directly: a
    caller holding a report from an earlier run passes the fingerprint that
    report describes, and if it is not the head's, this raises instead of
    letting the stale outcome be projected forward.
    """

    head = ledger.authoritative_state()
    if head is None:
        raise StaleProjection(
            "there is no authoritative checkpoint; delivery has nothing valid "
            "to serialise and must not fall back to a report"
        )
    if not head.is_authoritative_complete:
        raise StaleProjection(
            f"the authoritative checkpoint is {head.decision.outcome}, not "
            "complete; delivering it would present unfinished work as finished"
        )
    if claimed_fingerprint is not None and claimed_fingerprint != head.worktree_fingerprint:
        raise StaleProjection(
            f"delivery was handed fingerprint {claimed_fingerprint[:12]} but "
            f"the authoritative checkpoint is {head.worktree_fingerprint[:12]}; "
            "this is a stale projection of a superseded run"
        )
    return head


def next_slice_base(ledger: PlanCheckpointLedger) -> PlanCheckpoint:
    """What the next slice starts from.

    Deliberately the same object delivery reads. Two accessors returning
    different states is how a later slice inherits repaired files without
    inheriting the repaired checkpoint, which is what Crisis Atlas did.
    """

    head = ledger.authoritative_state()
    if head is None or not head.is_authoritative_complete:
        raise StaleProjection(
            "no completed authoritative checkpoint; the next slice has no "
            "admitted base and must not start from the workcell tree"
        )
    return head
