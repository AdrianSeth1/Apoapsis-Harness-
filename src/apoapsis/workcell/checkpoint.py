"""The checkpoint loop: what happens when the agent asks to be inspected.

Slice 4 built the decision kernel and left it with no caller. This is the
caller — the controller-side loop that runs on every `ready_for_evaluation`
signal, in order:

1. freeze the workcell and compute the delta (Slice 3);
2. admit or refuse it, atomically (Slice 3);
3. run the owner's configured commands and **emit witnesses** from the
   artifacts they produce (Slice 4B);
4. evaluate slice readiness against the compiled contract (Slice 4);
5. decide: `COMPLETE`, `CONTINUE`, `CANDIDATE_REFUSED`, or
   `HUMAN_REVIEW_REQUIRED`.

Step 3 is where the two halves meet, and its ordering matters: witnesses are
emitted **after** admission, against the admitted snapshot, so a command can
never be observed running over a file the policy refused.

The loop itself decides nothing. `evaluate_checkpoint` does, and it cannot see
command exit codes — only readiness, which is where they are weighed against
obligations. That is deliberate: ADR 0069 gave a green suite the authority to
end a session, and this module is the shape that authority takes now.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

from pydantic import Field

from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.acceptance import (
    CheckpointDecision,
    SliceAcceptanceContract,
    SliceReadinessReport,
    evaluate_checkpoint,
    evaluate_slice_readiness,
)
from apoapsis.workcell.admission import (
    AdmissionDecision,
    AdmissionPolicy,
    admit_candidate,
)
from apoapsis.workcell.behaviour import BehaviourUnit, changed_behaviour
from apoapsis.workcell.delta import CandidateDelta, compute_delta
from apoapsis.workcell.witness import StructuredWitness

#: Emits the witnesses for one checkpoint. Given the admitted snapshot and the
#: fingerprint the witnesses must be bound to, it returns whatever it could
#: honestly produce. Raising is permitted and preferred over returning a
#: witness with an empty section.
WitnessEmitter = Callable[[Path, str], Sequence[StructuredWitness]]


class CheckpointRecord(StrictModel):
    """Everything one checkpoint observed, for the audit trail."""

    schema_version: str = "1.0"
    slice_id: str = Field(min_length=1)
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    admission: AdmissionDecision
    readiness: SliceReadinessReport | None = None
    decision: CheckpointDecision
    behaviour_units: list[BehaviourUnit] = Field(default_factory=list)
    witness_ids: list[str] = Field(default_factory=list)
    emitter_error: str | None = None


def run_checkpoint(
    contract: SliceAcceptanceContract,
    *,
    base_root: str | Path,
    candidate_root: str | Path,
    snapshot_root: str | Path,
    emit_witnesses: WitnessEmitter,
    base_commit: str | None = None,
    policy: AdmissionPolicy | None = None,
    slice2_spike=None,
) -> CheckpointRecord:
    """Run one checkpoint end to end and return what it concluded.

    Never raises on an ordinary negative outcome. A refused candidate, an
    unready slice, and an emitter that could not produce a witness are all
    results the loop reports, because each one has a different repair and the
    caller needs to see which it got.
    """

    delta = compute_delta(base_root, candidate_root, base_commit=base_commit)
    admission = admit_candidate(
        base_root,
        candidate_root,
        delta,
        snapshot_root=snapshot_root,
        policy=policy,
        slice2_spike=slice2_spike,
    )

    if not admission.admitted:
        decision = evaluate_checkpoint(False, admission.detail, _empty(contract, delta))
        return CheckpointRecord(
            slice_id=contract.slice_id,
            contract_digest=contract.digest(),
            candidate_fingerprint=delta.candidate_fingerprint,
            admission=admission,
            decision=decision,
        )

    snapshot = Path(admission.snapshot_path or snapshot_root)
    behaviour = changed_behaviour(delta, base_root, snapshot)

    witnesses: list[StructuredWitness] = []
    emitter_error: str | None = None
    try:
        # Emitted against the *admitted snapshot*, not the workcell: a command
        # must never be observed running over a file the policy refused.
        witnesses = list(emit_witnesses(snapshot, delta.candidate_fingerprint))
    except Exception as exc:  # noqa: BLE001 -- reported as a checkpoint outcome
        emitter_error = f"{type(exc).__name__}: {exc}"

    readiness = evaluate_slice_readiness(
        contract,
        delta,
        witnesses,
        candidate_paths=_paths_in(snapshot),
        behaviour_units=behaviour,
    )
    decision = evaluate_checkpoint(True, admission.detail, readiness)
    if emitter_error and decision.outcome.value == "complete":
        # Defensive: an emitter that failed cannot have produced the evidence
        # readiness just accepted. Rather than trust the coincidence, refuse.
        decision = evaluate_checkpoint(
            True,
            admission.detail,
            readiness.model_copy(
                update={
                    "ready": False,
                    "detail": (
                        "witness emission failed, so the evidence readiness "
                        f"accepted cannot be current: {emitter_error}"
                    ),
                }
            ),
        )

    return CheckpointRecord(
        slice_id=contract.slice_id,
        contract_digest=contract.digest(),
        candidate_fingerprint=delta.candidate_fingerprint,
        admission=admission,
        readiness=readiness,
        decision=decision,
        behaviour_units=behaviour,
        witness_ids=[item.witness_id for item in witnesses],
        emitter_error=emitter_error,
    )


def _paths_in(root: Path) -> set[str]:
    import os

    found: set[str] = set()
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = [item for item in directories if item != ".git"]
        for name in names:
            found.add((Path(current) / name).relative_to(root).as_posix())
    return found


def _empty(
    contract: SliceAcceptanceContract, delta: CandidateDelta
) -> SliceReadinessReport:
    """A readiness report for a candidate that was never admitted."""

    return SliceReadinessReport(
        slice_id=contract.slice_id,
        contract_digest=contract.digest(),
        ready=False,
        detail="the candidate was refused, so readiness was never evaluated",
    )
