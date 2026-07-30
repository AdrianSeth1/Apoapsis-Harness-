"""The gate Slice 3 has to pass through, expressed as code rather than a note.

Every previous slice ended with a sentence in a document saying the next one
was blocked. A sentence is not a gate: it is advice that survives exactly as
long as the next person's memory of having read it. The handoff makes Slice 2's
exit condition specific -- no useful control capability missing, and containment
demonstrated -- so that condition can be a function, and delta admission can
refuse to start without it.

The rule this enforces:

> Slice 3 may not begin unless a capability spike report exists, its verdict is
> `CAPABILITY_PRESERVED`, and both `contained` and `conformant` are true.

`NOT_MEASURABLE` is not a soft pass, a missing report is not a pass, and a
report whose verdict was written by hand into JSON is caught by re-deriving the
blockers from the underlying containment and conformance objects rather than
trusting the verdict field.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.spike import CapabilitySpikeReport, SpikeVerdict


class Slice3Blocked(RuntimeError):
    """Raised when candidate delta admission is not permitted to start."""


class Slice3GateDecision(StrictModel):
    schema_version: str = "1.0"
    allowed: bool = False
    verdict: SpikeVerdict | None = None
    contained: bool = False
    conformant: bool = False
    workcell_manifest_digest: str | None = None
    blockers: list[str] = Field(default_factory=list)
    detail: str = Field(min_length=1)


def evaluate_slice3_gate(report: CapabilitySpikeReport | None) -> Slice3GateDecision:
    """Decide whether Slice 3 may begin. Fails closed on every uncertainty."""

    if report is None:
        return Slice3GateDecision(
            allowed=False,
            blockers=["no capability spike report was supplied"],
            detail=(
                "Slice 3 is blocked: candidate delta admission may not begin "
                "before the Slice 2 capability spike has produced a report"
            ),
        )

    blockers: list[str] = []
    if not report.containment.contained:
        blockers.append(f"containment did not hold: {report.containment.detail}")
    if not report.conformance.conformant:
        blockers.append(f"conformance did not hold: {report.conformance.detail}")
    if report.lost_capabilities:
        blockers.append(
            "the workcell did not demonstrate: "
            + ", ".join(item.value for item in report.lost_capabilities)
        )
    if report.verdict != SpikeVerdict.CAPABILITY_PRESERVED:
        blockers.append(
            f"the spike verdict is {report.verdict.value!r}, not "
            f"{SpikeVerdict.CAPABILITY_PRESERVED.value!r}"
        )
    elif blockers:
        # The verdict claims preservation while the evidence underneath it does
        # not. Recomputing rather than trusting the field is the whole reason
        # this function takes the report apart instead of reading one value.
        blockers.append(
            "the report claims CAPABILITY_PRESERVED but its own containment, "
            "conformance, or capability evidence contradicts that claim"
        )
    if report.acceptance_repair_performed:
        blockers.append(
            "the spike performed acceptance repair, so it is not the "
            "no-repair capability measurement Slice 2 requires"
        )

    if blockers:
        return Slice3GateDecision(
            allowed=False,
            verdict=report.verdict,
            contained=report.containment.contained,
            conformant=report.conformance.conformant,
            workcell_manifest_digest=report.workcell_manifest_digest,
            blockers=blockers,
            detail=(
                f"Slice 3 is blocked by {len(blockers)} unmet condition(s): "
                + "; ".join(blockers)
            ),
        )
    return Slice3GateDecision(
        allowed=True,
        verdict=report.verdict,
        contained=True,
        conformant=True,
        workcell_manifest_digest=report.workcell_manifest_digest,
        detail=(
            "the Slice 2 spike reports CAPABILITY_PRESERVED with containment and "
            "conformance both holding, so candidate delta admission may begin "
            f"against workcell {report.workcell_manifest_digest}"
        ),
    )


def load_spike_report(path: Path) -> CapabilitySpikeReport:
    """Load a spike report, refusing anything that is not a complete one."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Slice3Blocked(
            f"could not read the capability spike report {path}: {exc}"
        ) from exc
    return CapabilitySpikeReport.model_validate(payload)


def require_slice3_unblocked(
    report: CapabilitySpikeReport | None,
) -> Slice3GateDecision:
    """Return the decision, or raise. The raising form is the one to call.

    Delta admission should not be able to proceed by ignoring a return value,
    so the entry point Slice 3 will use is the one that stops the process.
    """

    decision = evaluate_slice3_gate(report)
    if not decision.allowed:
        raise Slice3Blocked(decision.detail)
    return decision
