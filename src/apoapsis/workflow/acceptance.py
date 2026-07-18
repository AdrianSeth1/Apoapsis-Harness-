from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from apoapsis.specification.schema import StrictModel, TaskSpecification
from apoapsis.verification.runner import VerificationCommand


class AcceptanceCoverageStatus(StrEnum):
    PROVEN = "proven"
    FAILED = "failed"
    UNPROVEN = "unproven"


class AcceptanceEvidenceSource(StrEnum):
    CONFIGURED_VERIFICATION_COMMAND = "configured_verification_command"


class AcceptanceCoverage(StrictModel):
    criterion_id: str = Field(pattern=r"^AC-[A-Za-z0-9._-]+$")
    status: AcceptanceCoverageStatus
    evidence_source: AcceptanceEvidenceSource | None = None
    evidence_reference: str | None = None
    reason: str = Field(min_length=1)


def compute_acceptance_coverage(
    specification: TaskSpecification,
    configured_commands: list[VerificationCommand],
    passed_command_names: set[str],
) -> list[AcceptanceCoverage]:
    """Deterministic, harness-only computation of per-criterion coverage.

    Never influenced by what a model claims: only `criterion.
    verification_method` (set at specification-drafting time, gated by the
    existing user-approval step) and real configured/passed command names
    are consulted. A model can propose a mapping; it cannot make one
    authoritative.
    """

    acceptance_commands = {
        command.name for command in configured_commands if command.acceptance
    }
    configured_names = {command.name for command in configured_commands}
    coverage: list[AcceptanceCoverage] = []
    for criterion in specification.active_acceptance_criteria:
        method = criterion.verification_method
        if method is None:
            coverage.append(
                AcceptanceCoverage(
                    criterion_id=criterion.id,
                    status=AcceptanceCoverageStatus.UNPROVEN,
                    reason="no verification command is mapped to this criterion",
                )
            )
        elif method not in configured_names:
            coverage.append(
                AcceptanceCoverage(
                    criterion_id=criterion.id,
                    status=AcceptanceCoverageStatus.UNPROVEN,
                    evidence_reference=method,
                    reason=f"{method!r} is not a configured verification command",
                )
            )
        elif method not in acceptance_commands:
            coverage.append(
                AcceptanceCoverage(
                    criterion_id=criterion.id,
                    status=AcceptanceCoverageStatus.UNPROVEN,
                    evidence_reference=method,
                    reason=f"{method!r} is not an approved acceptance check",
                )
            )
        elif method in passed_command_names:
            coverage.append(
                AcceptanceCoverage(
                    criterion_id=criterion.id,
                    status=AcceptanceCoverageStatus.PROVEN,
                    evidence_source=AcceptanceEvidenceSource.CONFIGURED_VERIFICATION_COMMAND,
                    evidence_reference=method,
                    reason=f"configured acceptance command {method!r} passed",
                )
            )
        else:
            coverage.append(
                AcceptanceCoverage(
                    criterion_id=criterion.id,
                    status=AcceptanceCoverageStatus.FAILED,
                    evidence_source=AcceptanceEvidenceSource.CONFIGURED_VERIFICATION_COMMAND,
                    evidence_reference=method,
                    reason=f"configured acceptance command {method!r} has not passed",
                )
            )
    return coverage


def acceptance_coverage_satisfied(coverage: list[AcceptanceCoverage]) -> bool:
    """True if every criterion is proven -- vacuously True when there is
    nothing to prove (a specification with no active acceptance criteria),
    since strict policy never blocks on something that was never asked
    for."""

    return all(item.status == AcceptanceCoverageStatus.PROVEN for item in coverage)
