"""Whole-project verification of a finished plan's integrated result (ADR 0074).

Every slice in a plan is verified in isolation, against its own worktree, at
the time it runs. `prepare_plan_delivery` then checked that each task state
was `COMPLETE` and that the commits formed an integrated ancestry chain --
and shipped. Nothing ever executed a command against the *combined* result.

Crisis Atlas (`PLAN-E1B90639E58D`, 2026-07-29) is what that permits. All four
slices reached `COMPLETE`, each slice's configured checks passed, the
delivery was green, and the delivered product was a working backend plus a
separate UI prototype that never called it. No individual slice check could
have caught that, because no individual slice was wrong. The defect lived
between them, which is exactly the region nothing was looking at.

This module runs the plan's own owner-approved
``verification_strategy.whole_project_verification_commands`` against the
exact integrated commit, and binds the result to that commit and to the
worktree fingerprint it was measured at, so a later delivery cannot present
it as evidence for a different state.

Authority, unchanged: the commands come from the approved plan and must
resolve to configured `VerificationCommand` entries. No model chooses,
reorders, edits, adds, or waives one. This module never transitions a plan,
never commits, and never merges; it computes a record and hands it back.
`prepare_plan_delivery` is the only caller that acts on it.

What this does not claim: a passing whole-project run proves the configured
commands passed at the integrated commit. It does not prove the product is
correct, and the evidence-strength reporting of ADR 0069/0073 applies here
exactly as it does to a slice.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field, ValidationError

from apoapsis.architect.audit import PlanAuditStore
from apoapsis.architect.schema import ArchitecturePlan
from apoapsis.repository.fingerprint import compute_worktree_fingerprint
from apoapsis.specification.schema import ConstraintStatus, StrictModel, utc_now
from apoapsis.verification.results import VerificationResult, VerificationStatus
from apoapsis.verification.runner import VerificationConfig, VerificationRunner
from apoapsis.workflow.acceptance import (
    AcceptanceCoverage,
    AcceptanceCoverageStatus,
    AcceptanceEvidenceSource,
)

FINAL_VERIFICATION_ARTIFACT = "final-project-verification.json"


class FinalVerificationStatus(StrEnum):
    """Why the integrated project is, or is not, deliverable.

    The three non-passing members are deliberately distinct: an owner whose
    plan named no whole-project command needs a different instruction from
    one whose command was deleted from configuration, and both need a
    different instruction from one whose integrated project simply failed.
    """

    PASSED = "passed"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"
    COMMANDS_UNAVAILABLE = "commands_unavailable"


class FinalProjectVerification(StrictModel):
    """One whole-project verification run, bound to what it measured.

    ``final_commit`` and ``worktree_fingerprint`` are the binding. A record
    whose commit or fingerprint no longer matches the integrated tip is
    stale by construction and is discarded rather than reused -- the same
    fail-closed reasoning ADR 0072 applied to task evidence.
    """

    schema_version: str = "1.0"
    plan_id: str
    plan_version: int = Field(ge=1)
    status: FinalVerificationStatus
    final_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    final_branch: str
    worktree_path: str
    worktree_fingerprint: str
    # The task whose isolated worktree holds the integrated tip. Recorded
    # because `VerificationRunner` is task-scoped and its result carries
    # that id; the run is nonetheless about the whole plan.
    measured_in_task_id: str
    requested_command_names: list[str] = Field(default_factory=list)
    executed_command_names: list[str] = Field(default_factory=list)
    missing_command_names: list[str] = Field(default_factory=list)
    result: VerificationResult | None = None
    acceptance_coverage: list[AcceptanceCoverage] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def is_sufficient_for_delivery(self) -> bool:
        """The single gate `prepare_plan_delivery` consults.

        Acceptance coverage is reported but is deliberately *not* a second
        gate here: a criterion can only reach `FAILED` because a command
        failed, which already fails `status`, and blocking on `UNPROVEN`
        would refuse delivery for plans whose criteria are simply unmapped
        -- a configuration gap the contract assessment (ADR 0069) already
        reports in its own voice.
        """

        return (
            self.status == FinalVerificationStatus.PASSED
            and self.result is not None
            and self.result.status == VerificationStatus.PASSED
        )

    def unproven_criterion_ids(self) -> list[str]:
        return sorted(
            item.criterion_id
            for item in self.acceptance_coverage
            if item.status != AcceptanceCoverageStatus.PROVEN
        )

    def matches(self, *, final_commit: str, worktree_fingerprint: str) -> bool:
        return (
            self.final_commit == final_commit
            and self.worktree_fingerprint == worktree_fingerprint
        )

    def command_results(self) -> list[dict[str, object]]:
        if self.result is None:
            return []
        return [
            {
                "name": item.name,
                "status": item.status.value,
                "exit_code": item.exit_code,
            }
            for item in self.result.commands
        ]


def plan_criterion_commands(plan: ArchitecturePlan) -> dict[str, list[str]]:
    """Which commands the plan says prove each acceptance criterion.

    Structured mappings only. ``verification_strategy
    .acceptance_proof_obligations`` is the plan's explicit statement and
    wins; a criterion's own ``verification_method`` is the fallback. Nothing
    is inferred from criterion prose -- ADR 0073's keyword warning is
    advisory and has no place in a gate.
    """

    mapping: dict[str, list[str]] = {}
    for obligation in plan.verification_strategy.acceptance_proof_obligations:
        if obligation.verification_commands:
            mapping[obligation.criterion_id] = list(obligation.verification_commands)
    for criterion in plan.acceptance_criteria:
        if criterion.id in mapping or not criterion.verification_method:
            continue
        mapping[criterion.id] = [criterion.verification_method]
    return mapping


def whole_plan_acceptance_coverage(
    plan: ArchitecturePlan, result: VerificationResult | None
) -> list[AcceptanceCoverage]:
    """Per-criterion coverage for the plan as a whole.

    Deliberately parallel to `workflow.acceptance.compute_acceptance_coverage`
    rather than reusing it: that function is task-scoped and reads a
    ``TaskSpecification``, while a plan's criteria live on the plan and may
    be proven by several commands at once via an acceptance proof
    obligation. The tri-state discipline is identical -- never executed,
    executed and failed, and executed and passed are three different
    evidentiary states and must not collapse.

    Authority for ``acceptance`` comes from each executed
    ``VerificationCommandResult``'s own immutable flag (ADR 0018), not from
    live configuration.
    """

    mapping = plan_criterion_commands(plan)
    executed: dict[str, tuple[VerificationStatus, bool]] = {}
    if result is not None:
        executed = {
            item.name: (item.status, item.acceptance)
            for item in result.commands
            if item.status != VerificationStatus.SKIPPED
        }

    coverage: list[AcceptanceCoverage] = []
    for criterion in plan.acceptance_criteria:
        if criterion.status != ConstraintStatus.ACTIVE:
            continue
        names = mapping.get(criterion.id, [])
        if not names:
            coverage.append(
                AcceptanceCoverage(
                    criterion_id=criterion.id,
                    status=AcceptanceCoverageStatus.UNPROVEN,
                    reason=(
                        "the plan maps no verification command to this "
                        "criterion, so the integrated run proves nothing "
                        "about it"
                    ),
                )
            )
            continue
        reference = ", ".join(names)
        statuses = [executed.get(name) for name in names]
        if any(
            entry is not None and entry[0] != VerificationStatus.PASSED
            for entry in statuses
        ):
            coverage.append(
                AcceptanceCoverage(
                    criterion_id=criterion.id,
                    status=AcceptanceCoverageStatus.FAILED,
                    evidence_source=(
                        AcceptanceEvidenceSource.CONFIGURED_VERIFICATION_COMMAND
                    ),
                    evidence_reference=reference,
                    reason=(
                        f"{reference} did not pass against the integrated "
                        "project"
                    ),
                )
            )
            continue
        if any(entry is None for entry in statuses):
            coverage.append(
                AcceptanceCoverage(
                    criterion_id=criterion.id,
                    status=AcceptanceCoverageStatus.UNPROVEN,
                    evidence_reference=reference,
                    reason=(
                        f"{reference} was not executed against the "
                        "integrated project; add it to the plan's "
                        "whole_project_verification_commands to prove this "
                        "criterion at delivery"
                    ),
                )
            )
            continue
        if not all(entry[1] for entry in statuses if entry is not None):
            coverage.append(
                AcceptanceCoverage(
                    criterion_id=criterion.id,
                    status=AcceptanceCoverageStatus.UNPROVEN,
                    evidence_reference=reference,
                    reason=(
                        f"{reference} passed against the integrated project "
                        "but is not an owner-designated acceptance command, "
                        "so its pass cannot prove this criterion"
                    ),
                )
            )
            continue
        coverage.append(
            AcceptanceCoverage(
                criterion_id=criterion.id,
                status=AcceptanceCoverageStatus.PROVEN,
                evidence_source=(
                    AcceptanceEvidenceSource.CONFIGURED_VERIFICATION_COMMAND
                ),
                evidence_reference=reference,
                reason=(
                    f"{reference} passed against the integrated project at "
                    "the delivered commit"
                ),
            )
        )
    return coverage


def load_final_project_verification(
    project_root: str | Path, plan_id: str
) -> FinalProjectVerification | None:
    """Read a persisted record, or ``None`` if absent or unreadable.

    A malformed record reads as absent rather than raising, so a corrupted
    artifact causes a fresh run rather than an unrecoverable delivery. It
    can never cause a *pass*: the fresh run is what decides.
    """

    path = (
        Path(project_root).resolve()
        / ".apoapsis"
        / "plans"
        / plan_id
        / FINAL_VERIFICATION_ARTIFACT
    )
    if not path.is_file():
        return None
    try:
        return FinalProjectVerification.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError):
        return None


def run_final_project_verification(
    project_root: str | Path,
    plan: ArchitecturePlan,
    verification_config: VerificationConfig,
    *,
    plan_id: str,
    plan_version: int,
    final_commit: str,
    final_branch: str,
    final_worktree_path: str,
    measured_in_task_id: str,
) -> FinalProjectVerification:
    """Execute the plan's whole-project commands at the integrated commit.

    Runs only the commands the approved plan named, in the order the
    configuration declares them, against a `VerificationConfig` narrowed to
    that subset. The full configured set is deliberately *not* run: the
    plan's whole-project contract is an owner decision recorded at approval
    time, and quietly running more than it asked for would make the record
    describe something the owner never approved.

    Persists the record before returning, so the artifact exists on disk
    even when the caller subsequently refuses to deliver.
    """

    root = Path(project_root).resolve()
    requested = list(plan.verification_strategy.whole_project_verification_commands)
    by_name = {command.name: command for command in verification_config.commands}
    missing = [name for name in requested if name not in by_name]
    # `required = True` is forced for this run regardless of how the command
    # is configured for ordinary slice execution, and the reason is
    # structural rather than stylistic.
    #
    # `VerificationRunner` executes the whole configured command set for
    # every task, so a genuine whole-project check -- one that asserts two
    # slices agree -- necessarily fails inside the isolated worktree of any
    # slice that ran before its counterpart existed. The workable
    # configuration is therefore `required = false`, so early slices are not
    # failed by a check that could not yet succeed. But `VerificationResult`
    # only aggregates to FAILED on a *required* command, so honouring that
    # flag here would let the delivery gate pass on a failing integrated
    # project -- the exact hole this ADR closes.
    #
    # Naming a command in `whole_project_verification_commands` is the
    # owner's statement that it must pass before delivery. That statement
    # governs here.
    selected = [
        by_name[name].model_copy(update={"required": True})
        for name in requested
        if name in by_name
    ]

    # Captured before the run: this is the state being verified. A
    # verification command may leave byproducts behind (ADR 0063), so a
    # fingerprint taken afterwards would describe the tree the check
    # produced rather than the tree it examined.
    fingerprint = compute_worktree_fingerprint(final_worktree_path)

    def _record(
        status: FinalVerificationStatus,
        reason: str,
        *,
        result: VerificationResult | None = None,
    ) -> FinalProjectVerification:
        record = FinalProjectVerification(
            plan_id=plan_id,
            plan_version=plan_version,
            status=status,
            final_commit=final_commit,
            final_branch=final_branch,
            worktree_path=str(final_worktree_path),
            worktree_fingerprint=fingerprint.digest,
            measured_in_task_id=measured_in_task_id,
            requested_command_names=requested,
            executed_command_names=[item.name for item in selected],
            missing_command_names=missing,
            result=result,
            acceptance_coverage=whole_plan_acceptance_coverage(plan, result),
            reason=reason,
        )
        PlanAuditStore(root, plan_id).write_json(
            FINAL_VERIFICATION_ARTIFACT,
            record,
            kind="final_project_verification",
        )
        return record

    if not requested:
        return _record(
            FinalVerificationStatus.NOT_CONFIGURED,
            "the approved plan names no whole_project_verification_commands, "
            "so nothing has ever been executed against the integrated "
            "project; per-slice verification is not evidence about the "
            "combined result",
        )
    if missing:
        return _record(
            FinalVerificationStatus.COMMANDS_UNAVAILABLE,
            "the approved plan's whole-project verification command(s) "
            f"{', '.join(sorted(missing))} are not configured in this "
            "project, so the approved final contract cannot be executed",
        )

    runner = VerificationRunner(
        verification_config.model_copy(update={"commands": selected})
    )
    result = runner.run(measured_in_task_id, final_worktree_path, attempt=1)
    if result.status == VerificationStatus.PASSED:
        return _record(
            FinalVerificationStatus.PASSED,
            "every whole-project verification command the approved plan "
            f"named passed at integrated commit {final_commit}",
            result=result,
        )
    failed = sorted(
        item.name
        for item in result.commands
        if item.status != VerificationStatus.PASSED
        and item.status != VerificationStatus.SKIPPED
    )
    return _record(
        FinalVerificationStatus.FAILED,
        "the integrated project failed whole-project verification at commit "
        f"{final_commit}: {', '.join(failed) or result.status.value}",
        result=result,
    )


def ensure_final_project_verification(
    project_root: str | Path,
    plan: ArchitecturePlan,
    verification_config: VerificationConfig,
    *,
    plan_id: str,
    plan_version: int,
    final_commit: str,
    final_branch: str,
    final_worktree_path: str,
    measured_in_task_id: str,
) -> FinalProjectVerification:
    """Reuse a matching passing record, otherwise run a fresh one.

    Only a record that passed *and* is bound to this exact commit and
    worktree fingerprint is reused. A stale binding, a malformed artifact,
    or any non-passing status causes a re-run rather than a refusal, so an
    owner who fixes the integrated project and retries delivery is not made
    to delete a file by hand.
    """

    existing = load_final_project_verification(project_root, plan_id)
    if (
        existing is not None
        and existing.is_sufficient_for_delivery
        and existing.matches(
            final_commit=final_commit,
            worktree_fingerprint=compute_worktree_fingerprint(
                final_worktree_path
            ).digest,
        )
    ):
        return existing
    return run_final_project_verification(
        project_root,
        plan,
        verification_config,
        plan_id=plan_id,
        plan_version=plan_version,
        final_commit=final_commit,
        final_branch=final_branch,
        final_worktree_path=final_worktree_path,
        measured_in_task_id=measured_in_task_id,
    )


__all__ = [
    "FINAL_VERIFICATION_ARTIFACT",
    "FinalProjectVerification",
    "FinalVerificationStatus",
    "ensure_final_project_verification",
    "load_final_project_verification",
    "plan_criterion_commands",
    "run_final_project_verification",
    "whole_plan_acceptance_coverage",
]
