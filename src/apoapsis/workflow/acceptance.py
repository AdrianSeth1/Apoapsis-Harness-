from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from apoapsis.specification.schema import StrictModel, TaskSpecification
from apoapsis.verification.results import VerificationStatus
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


_NOT_PASSED_STATUSES = frozenset(
    {
        VerificationStatus.FAILED,
        VerificationStatus.TIMED_OUT,
        VerificationStatus.ERROR,
    }
)


def compute_acceptance_coverage(
    specification: TaskSpecification,
    configured_commands: list[VerificationCommand],
    command_results: dict[str, VerificationStatus],
) -> list[AcceptanceCoverage]:
    """Deterministic, harness-only computation of per-criterion coverage.

    Never influenced by what a model claims: only `criterion.
    verification_method` (set at specification-drafting time, gated by the
    existing user-approval step, and validated against the deterministic
    acceptance-command catalog at extraction time -- ADR 0016) and real
    configured/executed command results are consulted.

    `command_results` must map a command name to its `VerificationStatus`
    from the most recent execution **at the current worktree digest only**
    (`SKIPPED` entries must be omitted by the caller, since a skipped
    command was never actually executed). A name absent from this mapping
    is treated as never executed for the current code state -- a result
    recorded against an earlier digest must not be passed in here, or it
    would silently "prove" code that has since changed. This tri-state
    (never executed / executed and failed / executed and passed) is why the
    parameter is a per-command status map rather than a flat "ever passed"
    set: a command that has simply never run and a command that has run
    and failed are different evidentiary states and must not collapse into
    one another.
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
        else:
            status = command_results.get(method)
            if status == VerificationStatus.PASSED:
                coverage.append(
                    AcceptanceCoverage(
                        criterion_id=criterion.id,
                        status=AcceptanceCoverageStatus.PROVEN,
                        evidence_source=(
                            AcceptanceEvidenceSource.CONFIGURED_VERIFICATION_COMMAND
                        ),
                        evidence_reference=method,
                        reason=(
                            f"configured acceptance command {method!r} passed "
                            "for the current worktree state"
                        ),
                    )
                )
            elif status in _NOT_PASSED_STATUSES:
                coverage.append(
                    AcceptanceCoverage(
                        criterion_id=criterion.id,
                        status=AcceptanceCoverageStatus.FAILED,
                        evidence_source=(
                            AcceptanceEvidenceSource.CONFIGURED_VERIFICATION_COMMAND
                        ),
                        evidence_reference=method,
                        reason=(
                            f"configured acceptance command {method!r} did not "
                            f"pass (status={status.value}) for the current "
                            "worktree state"
                        ),
                    )
                )
            else:
                coverage.append(
                    AcceptanceCoverage(
                        criterion_id=criterion.id,
                        status=AcceptanceCoverageStatus.UNPROVEN,
                        evidence_reference=method,
                        reason=(
                            f"configured acceptance command {method!r} has not "
                            "yet been executed for the current worktree state"
                        ),
                    )
                )
    return coverage


def acceptance_coverage_satisfied(coverage: list[AcceptanceCoverage]) -> bool:
    """True if every criterion is proven -- vacuously True when there is
    nothing to prove (a specification with no active acceptance criteria),
    since strict policy never blocks on something that was never asked
    for."""

    return all(item.status == AcceptanceCoverageStatus.PROVEN for item in coverage)
