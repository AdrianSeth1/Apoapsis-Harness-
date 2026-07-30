"""Compile a slice's acceptance contract from the approved plan, before spend.

The handoff is specific about the timing:

> Compile every approved slice into a `SliceAcceptanceContract` **before model
> spend**.

Before, because a contract written afterwards is written by someone who has
already seen what the model produced. Crisis Atlas Slice 2's obligations were
never written down at all, so "done" was decided by whatever the configured
commands happened to say, and they happened to say green.

Everything here is derived from fields the planner already fills in and the
owner already approved. Nothing is invented: a slice that names no paths and no
test obligations compiles to a contract that says so, and
`SliceAcceptanceContract` refuses a criterion no obligation could prove, so a
thin slice fails at compile time rather than at delivery.
"""

from __future__ import annotations

from apoapsis.architect.schema import ArchitecturePlan, ImplementationSlice
from apoapsis.workcell.acceptance import (
    AcceptanceObligation,
    ObligationKind,
    SliceAcceptanceContract,
)


class ContractCompilationError(RuntimeError):
    """The slice cannot be compiled into a contract that could be satisfied."""


def _slice_by_id(plan: ArchitecturePlan, slice_id: str) -> ImplementationSlice:
    for candidate in plan.slices:
        if candidate.slice_id == slice_id:
            return candidate
    raise ContractCompilationError(
        f"the plan contains no slice {slice_id!r}; a contract cannot be compiled "
        "for work the plan does not describe"
    )


def compile_slice_contract(
    plan: ArchitecturePlan,
    slice_id: str,
    *,
    unmeasured_reasons: dict[str, str] | None = None,
    independent_criteria: set[str] | None = None,
) -> SliceAcceptanceContract:
    """Turn one approved slice into the contract its completion must meet.

    `unmeasured_reasons` maps an obligation id to the owner's written reason
    it is not measured. Supplying one does not satisfy the obligation — it
    routes the slice to human review — which is the only way an owner can
    honestly say "we cannot test this here" without it becoming silence.

    `independent_criteria` marks criteria that model-authored evidence may not
    discharge. The unrestricted control wrote 87 passing tests and still
    shipped a broken filter; some criteria need someone else's eyes.
    """

    unmeasured_reasons = unmeasured_reasons or {}
    independent_criteria = independent_criteria or set()
    target = _slice_by_id(plan, slice_id)

    criteria = list(target.acceptance_criterion_ids)
    if not criteria:
        raise ContractCompilationError(
            f"slice {slice_id!r} names no acceptance criteria, so there is no "
            "definition of done to compile"
        )

    obligations: list[AcceptanceObligation] = []

    # 1. Each declared path is a production artifact that must exist *at that
    #    path* and be reached. The path check is what would have caught Slice
    #    2's wrong-package service; the reachability check is what would have
    #    caught its never-imported skeleton.
    production_paths = [
        path
        for path in target.suggested_paths
        if not path.endswith((".md", ".rst", ".txt"))
    ]
    for index, path in enumerate(sorted(production_paths), start=1):
        obligation_id = f"{slice_id}-artifact-{index}"
        obligations.append(
            AcceptanceObligation(
                obligation_id=obligation_id,
                kind=ObligationKind.PRODUCTION_ARTIFACT,
                description=f"{path} exists at its declared path and is reached",
                required_paths=[path],
                must_be_exercised=[path],
                unmeasured_reason=unmeasured_reasons.get(obligation_id, ""),
            )
        )

    # 2. Declared symbols are interfaces later slices will consume.
    for index, symbol in enumerate(sorted(target.suggested_symbols), start=1):
        obligation_id = f"{slice_id}-interface-{index}"
        obligations.append(
            AcceptanceObligation(
                obligation_id=obligation_id,
                kind=ObligationKind.INTERFACE,
                description=f"{symbol} is provided for consuming slices",
                criteria=[],
                required_paths=[],
                must_be_exercised=[],
                unmeasured_reason=unmeasured_reasons.get(
                    obligation_id, f"no structured witness maps to symbol {symbol}"
                ),
            )
        )

    # 3. Every criterion needs an obligation that could prove it. One per
    #    criterion, discharged by a witness that claims it.
    for criterion in criteria:
        obligation_id = f"{slice_id}-criterion-{criterion}"
        obligations.append(
            AcceptanceObligation(
                obligation_id=obligation_id,
                kind=ObligationKind.TEST_OR_WITNESS,
                description=f"{criterion} is proved by current-state evidence",
                criteria=[criterion],
                requires_independent_evidence=criterion in independent_criteria,
                unmeasured_reason=unmeasured_reasons.get(obligation_id, ""),
            )
        )

    # 4. Integration edges this slice introduces or consumes. A networked
    #    contract with only static evidence behind it is what ADR 0076 already
    #    refuses at plan level; here it becomes a slice obligation.
    for contract_id in sorted(target.integration_contract_ids):
        obligation_id = f"{slice_id}-integration-{contract_id}"
        obligations.append(
            AcceptanceObligation(
                obligation_id=obligation_id,
                kind=ObligationKind.INTEGRATION_EDGE,
                description=f"integration contract {contract_id} is exercised",
                criteria=[],
                unmeasured_reason=unmeasured_reasons.get(
                    obligation_id,
                    f"no structured witness is mapped to {contract_id}",
                ),
            )
        )

    # 5. Documentation the slice is responsible for.
    documentation_paths = [
        path for path in target.suggested_paths if path.endswith((".md", ".rst", ".txt"))
    ]
    for index, path in enumerate(sorted(documentation_paths), start=1):
        obligation_id = f"{slice_id}-doc-{index}"
        obligations.append(
            AcceptanceObligation(
                obligation_id=obligation_id,
                kind=ObligationKind.DOCUMENTATION,
                description=f"{path} is written or updated by this slice",
                required_paths=[path],
                unmeasured_reason=unmeasured_reasons.get(obligation_id, ""),
            )
        )

    if not obligations:
        raise ContractCompilationError(
            f"slice {slice_id!r} produced no obligations; nothing about it could "
            "be proved or disproved"
        )

    return SliceAcceptanceContract(
        slice_id=slice_id,
        plan_id=getattr(plan, "plan_id", None),
        criteria=criteria,
        obligations=obligations,
        required_commands=list(target.verification_commands),
    )
