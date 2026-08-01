from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path

from pydantic import Field

from apoapsis.architect.audit import PlanAuditStore
from apoapsis.architect.errors import SlicePackagingError
from apoapsis.architect.final_verification import (
    FINAL_VERIFICATION_ARTIFACT,
    FinalProjectVerification,
    ensure_final_project_verification,
)
from apoapsis.architect.schema import ArchitecturePlan, PlanStatus
from apoapsis.architect.slice_package import checkpoint_completed_prior_slices
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.execution.worktree import WorktreeManager
from apoapsis.reporting.current_state import (
    CurrentTaskEvidence,
    project_current_task_evidence,
)
from apoapsis.repository.git import GitRepository
from apoapsis.specification.schema import StrictModel
from apoapsis.verification.runner import VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.states import WorkflowState

# Defense in depth beyond `git archive`'s natural exclusion of untracked
# and `.gitignore`d content (ADR 0057's `.apoapsis` gitignore guarantee
# covers the normal case). If a repository's tracked tree somehow contains
# Apoapsis's own runtime state, git metadata, or an obvious credential
# file at the delivered commit -- e.g. an operator force-added it, or a
# repository predates `apoapsis init` ensuring `.gitignore` coverage --
# delivery refuses to ship it rather than silently include or silently
# strip it from what the archive's file inventory claims to contain.
_FORBIDDEN_DELIVERY_PATH_PREFIXES: tuple[str, ...] = (".apoapsis/", ".git/")
_FORBIDDEN_DELIVERY_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".netrc",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
    }
)
_FORBIDDEN_DELIVERY_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".pfx", ".p12")


def _forbidden_delivery_paths(files: list[str]) -> list[str]:
    forbidden: list[str] = []
    for path in files:
        name = path.rsplit("/", 1)[-1]
        if (
            any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in _FORBIDDEN_DELIVERY_PATH_PREFIXES)
            or name in _FORBIDDEN_DELIVERY_FILENAMES
            or name.endswith(_FORBIDDEN_DELIVERY_SUFFIXES)
        ):
            forbidden.append(path)
    return forbidden


class PlanDelivery(StrictModel):
    """The record of one finished plan's export.

    Two verification sections, deliberately named apart (ADR 0074):
    ``verification_summary`` is per-slice history -- each slice verified in
    isolation, against its own worktree, at the time it ran --- and
    ``final_project_verification`` is the plan's own whole-project contract
    executed once against the integrated commit. The first has never been
    evidence for the second, and presenting it as such is what let Crisis
    Atlas deliver a backend and a UI that never spoke to each other.
    """

    schema_version: str = "1.1"
    plan_id: str
    plan_version: int
    final_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    final_branch: str
    final_worktree_path: str
    completed_slice_ids: list[str]
    task_ids: list[str]
    repository_files: list[str]
    verification_summary: list[dict[str, object]]
    final_project_verification: FinalProjectVerification
    operability: DeliveredOperability
    archive_path: str
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frontier_review_handoff_path: str


class DeliveredOperability(StrictModel):
    """What the delivery record can honestly say about running the product.

    ADR 0076. The Crisis Atlas usage guide said "read `README.md`; it is the
    project's primary usage guide" -- inferred purely from that filename
    appearing in the archive. The README was still the seed, no command had
    ever started the product, and the guide could not have known either way.

    This record separates three different states that the old guide
    collapsed into one reassuring sentence: the artifact is present, the
    launch command ran and passed, and the launch was explicitly not
    measured for a reason the owner wrote down.
    """

    schema_version: str = "1.0"
    primary_documentation_path: str = ""
    primary_documentation_present: bool = False
    required_artifacts: list[str] = Field(default_factory=list)
    missing_artifacts: list[str] = Field(default_factory=list)
    launch_command: str = ""
    launch_measured: bool = False
    launch_unmeasured_reason: str = ""
    install_instructions: str = ""
    launch_or_usage_instructions: str = ""
    test_instructions: str = ""
    readiness_checks: list[str] = Field(default_factory=list)


def assess_delivered_operability(
    plan: ArchitecturePlan, delivered_files: list[str]
) -> DeliveredOperability:
    """Compare the plan's structured delivery contract to the delivered tree.

    Reads structured fields only. The prose instruction fields are carried
    through verbatim for the usage guide to render -- rendering is not
    executing, and Apoapsis still never runs planner-authored prose.

    ``launch_measured`` is true only when the plan named a launch command
    *and* that command is one the plan runs against the integrated project,
    which `validate_plan` already requires. Delivery has separately proven
    that every whole-project command passed, so a named launch command that
    reaches here has genuinely been exercised at the delivered commit.
    """

    contract = plan.delivery_contract
    present = set(delivered_files)
    required = list(contract.required_artifacts)
    if contract.primary_documentation_path:
        required.append(contract.primary_documentation_path)
    missing = sorted({item for item in required if item not in present})
    launch_command = contract.launch_verification_command
    whole_project = set(
        plan.verification_strategy.whole_project_verification_commands
    )
    return DeliveredOperability(
        primary_documentation_path=contract.primary_documentation_path,
        primary_documentation_present=(
            bool(contract.primary_documentation_path)
            and contract.primary_documentation_path in present
        ),
        required_artifacts=sorted(set(required)),
        missing_artifacts=missing,
        launch_command=launch_command,
        launch_measured=bool(launch_command) and launch_command in whole_project,
        launch_unmeasured_reason=contract.launch_not_runnable_reason,
        install_instructions=contract.install_instructions,
        launch_or_usage_instructions=contract.launch_or_usage_instructions,
        test_instructions=contract.test_instructions,
        readiness_checks=list(contract.readiness_checks),
    )


def _verification_summary_entry(
    slice_id: str, task_id: str, evidence: CurrentTaskEvidence
) -> dict[str, object]:
    """One slice's row of the delivered per-slice verification history.

    Sourced entirely from the current-evidence projection (ADR 0072).
    Delivery previously read each task's `report.json` directly, which is
    the *original stop* snapshot and is never updated: Crisis Atlas
    (`PLAN-E1B90639E58D`) shipped a `delivery.json` that reported
    `human_review_required` with a failed verification for a slice whose
    persisted state was `COMPLETE` and whose manual-frontier repair had
    passed. The stale snapshot is still preserved verbatim on disk and
    surfaced here as `original_report_outcome`; it is simply no longer
    mistaken for the current result.
    """

    return {
        "slice_id": slice_id,
        "task_id": task_id,
        "outcome": (
            evidence.outcome.value
            if evidence.outcome is not None
            else evidence.workflow_state.value
        ),
        # `verification_results` is a list of aggregate `VerificationResult`
        # runs, not individual commands -- the per-command
        # name/status/exit_code this summary needs live on each run's nested
        # `.commands`. The last run is the one that determined the outcome
        # (the `[-1]` convention used elsewhere for the same field, e.g.
        # `agent/session.py`, `evaluation/report.py`), so only its results
        # are surfaced rather than every historical retry.
        "verification": evidence.command_results(),
        "evidence_generation": evidence.evidence_generation.value,
        "evidence_event_type": evidence.evidence_event_type,
        "evidence_sources": evidence.evidence_sources,
        "evidence_integrity": evidence.evidence_integrity.value,
        "supersedes_original_report": evidence.supersedes_original_report,
        "original_report_outcome": (
            evidence.original_report_outcome.value
            if evidence.original_report_outcome is not None
            else None
        ),
    }


def _slice_review_sections(plan: ArchitecturePlan) -> str:
    """Resolves each slice's referenced acceptance criteria and inherited
    hard constraints into their actual verbatim text, so a reviewing
    frontier model does not have to cross-reference IDs against the raw
    plan JSON below to know what each slice was actually supposed to do."""

    criteria_by_id = {item.id: item for item in plan.acceptance_criteria}
    constraints_by_id = {item.id: item for item in plan.hard_constraints}
    blocks: list[str] = []
    for slice_obj in plan.slices:
        lines = [f"### {slice_obj.slice_id}: {slice_obj.title}", ""]
        lines.append(f"**Objective:** {slice_obj.objective}")
        lines.append("")
        if slice_obj.work_brief:
            lines.append(f"**Work brief:** {slice_obj.work_brief}")
            lines.append("")
        criteria = [
            criteria_by_id[item].text
            for item in slice_obj.acceptance_criterion_ids
            if item in criteria_by_id
        ]
        if criteria:
            lines.append("**Acceptance criteria:**")
            lines.extend(f"- {text}" for text in criteria)
            lines.append("")
        constraints = [
            constraints_by_id[item].text
            for item in slice_obj.inherited_constraint_ids
            if item in constraints_by_id
        ]
        if constraints:
            lines.append("**Inherited hard constraints:**")
            lines.extend(f"- {text}" for text in constraints)
            lines.append("")
        if slice_obj.exclusions:
            lines.append("**Exclusions:**")
            lines.extend(f"- {text}" for text in slice_obj.exclusions)
            lines.append("")
        if slice_obj.dependencies:
            lines.append(f"**Dependencies:** {', '.join(slice_obj.dependencies)}")
            lines.append("")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def _final_verification_section(record: FinalProjectVerification) -> str:
    """The whole-project half of the handoff, kept structurally apart.

    A reviewing model is told which commands ran, at which commit and
    worktree fingerprint, and which acceptance criteria the integrated run
    did *not* prove -- so it can direct attention at the unproven part
    rather than assuming the green result covered everything.
    """

    unproven = record.unproven_criterion_ids()
    lines = [
        "## Final integrated-project verification",
        "",
        "This section, unlike the one above, is about the combined result. "
        "It is the plan's own owner-approved whole-project contract, "
        "executed once against the integrated commit (ADR 0074).",
        "",
        f"- Status: `{record.status.value}`",
        f"- Commit verified: `{record.final_commit}`",
        f"- Worktree fingerprint: `{record.worktree_fingerprint}`",
        f"- Commands requested by the plan: "
        f"`{', '.join(record.requested_command_names) or 'none'}`",
        f"- Commands executed: "
        f"`{', '.join(record.executed_command_names) or 'none'}`",
        f"- Reason: {record.reason}",
        "",
        "### Per-command results",
        "",
        "```json",
        json.dumps(record.command_results(), indent=2, sort_keys=True),
        "```",
        "",
        "### Whole-plan acceptance coverage",
        "",
    ]
    if unproven:
        lines.extend(
            [
                "The integrated run did **not** prove these acceptance "
                f"criteria: `{', '.join(unproven)}`. Treat any claim about "
                "them as unverified and say so in your review.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "Every active acceptance criterion the plan mapped to a "
                "command was proven by this integrated run.",
                "",
            ]
        )
    lines.extend(
        [
            "```json",
            json.dumps(
                [item.model_dump(mode="json") for item in record.acceptance_coverage],
                indent=2,
                sort_keys=True,
            ),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _frontier_review_markdown(delivery: PlanDelivery, plan: ArchitecturePlan) -> str:
    response_schema = {
        "summary": "string",
        "architecture_findings": [
            {
                "severity": "critical|high|medium|low",
                "path": "repository-relative path",
                "line": "integer or null",
                "finding": "string",
                "recommendation": "string",
            }
        ],
        "cross_slice_integration_risks": ["string"],
        "verification_gaps": ["string"],
        "release_readiness": "ready|needs_changes|blocked",
    }
    return (
        "# Whole-project frontier review handoff\n\n"
        "Upload this file together with the project ZIP named below. Review the "
        "entire resulting application, not one slice in isolation. Check architecture, "
        "cross-slice integration, security, correctness, operability, documentation, "
        "and verification gaps. Do not claim you ran commands. Return only JSON matching "
        "the response shape at the end.\n\n"
        f"- Plan: `{delivery.plan_id}` version `{delivery.plan_version}`\n"
        f"- Final commit: `{delivery.final_commit}`\n"
        f"- Project archive: `{Path(delivery.archive_path).name}`\n"
        f"- Archive SHA-256: `{delivery.archive_sha256}`\n"
        f"- Completed slices: `{', '.join(delivery.completed_slice_ids)}`\n\n"
        "## Original idea\n\n"
        + plan.idea_text
        + "\n\n## Architecture summary\n\n"
        + plan.architecture_summary
        + "\n\n## Per-slice objectives, work briefs, and acceptance criteria\n\n"
        + _slice_review_sections(plan)
        + "\n## Per-slice verification history\n\n"
        "Each entry below is one slice's *current* outcome and verification, "
        "projected from persisted task state, the append-only event history, "
        "and that stage's own operation artifact (ADR 0072) -- not from the "
        "one-time `report.json` snapshot written at the slice's first stop. "
        "`evidence_generation` names which artifact family the result came "
        "from, `evidence_sources` gives its repository-relative path, and "
        "`original_report_outcome` preserves what the first stop said when a "
        "later repair superseded it.\n\n"
        "Each slice was verified in isolation, against its own worktree, at "
        "the time it ran. This section is therefore per-slice history and is "
        "not evidence that the integrated project verifies as a whole; the "
        "next section is.\n\n"
        "```json\n"
        + json.dumps(delivery.verification_summary, indent=2, sort_keys=True)
        + "\n```\n\n"
        + _final_verification_section(delivery.final_project_verification)
        + "\n## Repository file inventory\n\n"
        + "\n".join(f"- `{path}`" for path in delivery.repository_files)
        + "\n\n## Complete approved architecture plan (raw)\n\n```json\n"
        + json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True)
        + "\n```\n\n## Cross-slice integration risks\n\n"
        "This project was built as multiple independently executed slices "
        "(see completed slices above), each verified in isolation. Explicitly "
        "assess how they interact: shared state, ordering assumptions, "
        "interface mismatches between slices, and anything one slice's "
        "verification could not have caught because it never ran against "
        "the others. Report these under `cross_slice_integration_risks` "
        "in the response shape below.\n\n"
        "## Required response shape\n\n```json\n"
        + json.dumps(response_schema, indent=2, sort_keys=True)
        + "\n```\n"
    )


def _operability_section(operability: DeliveredOperability) -> str:
    """The launch/install/test half of the usage guide (ADR 0076).

    Renders the plan's own structured instructions rather than guessing from
    filenames, and states plainly whether the launch path was exercised. The
    old guide's file-name heuristics are kept below it as a fallback for
    plans whose delivery contract says nothing, but they are labelled as
    inference rather than presented as the project's documented path.
    """

    lines: list[str] = ["## Install, launch, and test", ""]
    if operability.launch_measured:
        lines.append(
            f"- **Launch was exercised.** The command "
            f"`{operability.launch_command}` ran against this exact commit as "
            "part of whole-project verification and passed."
        )
    elif operability.launch_unmeasured_reason:
        lines.append(
            "- **Launch was NOT exercised.** The approved plan states why no "
            f"launch command could be run: {operability.launch_unmeasured_reason}"
        )
    else:
        lines.append(
            "- **Launch was NOT exercised**, and the plan recorded no reason. "
            "Treat the instructions below as unverified."
        )
    if operability.primary_documentation_path:
        state = (
            "is present in this archive"
            if operability.primary_documentation_present
            else "is MISSING from this archive"
        )
        lines.append(
            f"- The plan's primary documentation is "
            f"`{operability.primary_documentation_path}`, and it {state}."
        )
    lines.append("")
    for heading, body in (
        ("Install", operability.install_instructions),
        ("Launch or usage", operability.launch_or_usage_instructions),
        ("Tests", operability.test_instructions),
    ):
        if body:
            lines.extend([f"### {heading}", "", body, ""])
    if operability.readiness_checks:
        lines.extend(["### Readiness checks", ""])
        lines.extend(f"- {item}" for item in operability.readiness_checks)
        lines.append("")
    lines.append(
        "These instructions come from the approved plan's structured "
        "`delivery_contract`. Apoapsis reproduces them; it does not execute "
        "prose, and only the launch line above reflects a command that "
        "actually ran."
    )
    lines.append("")
    return "\n".join(lines)


def _usage_guide(
    plan_id: str,
    final_commit: str,
    files: list[str],
    final_verification: FinalProjectVerification,
    operability: DeliveredOperability,
) -> str:
    markers: list[str] = []
    file_set = set(files)
    if "README.md" in file_set:
        markers.append("1. Read `README.md`; it is the project's primary usage guide.")
    if "package.json" in file_set:
        markers.append(
            "2. This is a Node project: install its declared packages, then use the "
            "documented script in `package.json`/`README.md`."
        )
    if "pyproject.toml" in file_set or "requirements.txt" in file_set:
        markers.append(
            "3. This is a Python project: create an isolated environment, install the "
            "declared project dependencies, then follow `README.md` for its entry point."
        )
    if "Dockerfile" in file_set or "docker-compose.yml" in file_set or "compose.yml" in file_set:
        markers.append(
            "4. Container configuration is included; use the documented Docker/Compose "
            "path when that is the project's supported launch method."
        )
    if not markers:
        markers.append(
            "1. Inspect the top-level documentation and build manifests to identify the "
            "project's supported install and launch command."
        )
    unproven = final_verification.unproven_criterion_ids()
    exercised = (
        "- Whole-project verification ran "
        f"`{', '.join(final_verification.executed_command_names)}` against this "
        f"exact commit and passed.\n"
    )
    not_exercised = (
        "- **Not exercised:** these acceptance criteria were not proven by the "
        f"integrated run: `{', '.join(unproven)}`. Nothing here establishes them.\n"
        if unproven
        else "- Every acceptance criterion the plan mapped to a command was proven "
        "by that integrated run.\n"
    )
    return (
        "# Using the finished project\n\n"
        f"Apoapsis prepared this archive from plan `{plan_id}` at integrated commit "
        f"`{final_commit}` after every slice reached COMPLETE **and** the plan's own "
        "whole-project verification passed against the combined result.\n\n"
        + _operability_section(operability)
        + "\n## If you need more than the above\n\n"
        "These lines are inferred from filenames present in the archive, not "
        "from the plan. Use them only where the documented path above is "
        "silent.\n\n"
        + "\n".join(markers)
        + "\n\n## What was actually verified\n\n"
        + exercised
        + not_exercised
        + "- Each slice was also verified in isolation while it was being built. "
        "That per-slice history is separate evidence and is recorded separately in "
        "`delivery.json`; it never stood in for the integrated result.\n"
        "\n## Important\n\n"
        "- Extract the ZIP to a normal project folder before installing or running it.\n"
        "- This archive contains the final tracked project, not Apoapsis runtime databases, "
        "credentials, model logs, or `.git` metadata.\n"
        "- Verification passing proves the configured checks passed; it does not invent a "
        "deployment target or credentials that the project did not define.\n"
        "- `FRONTIER-WHOLE-PROJECT-REVIEW` is generated beside this ZIP when an additional "
        "whole-code review is wanted.\n"
    )


def prepare_plan_delivery(
    project_root: str | Path,
    plan_store: SQLitePlanStore,
    slice_store: PlanSliceExecutionStore,
    task_store: SQLiteTaskStore,
    plan_id: str,
    *,
    verification_config: VerificationConfig,
) -> PlanDelivery:
    """Checkpoint, verify, and export the integrated result of a finished plan.

    ``verification_config`` is required because delivery now runs the plan's
    own whole-project verification contract against the integrated commit
    before anything is archived or any plan status changes (ADR 0074). It is
    a parameter rather than something read from disk here so the caller's
    already-loaded configuration is the one that governs, and so a test can
    supply a narrowed one explicitly.
    """

    root = Path(project_root).resolve()
    existing = load_plan_delivery(root, plan_id)
    if existing is not None:
        return existing
    record = plan_store.get_plan(plan_id)
    if record.status not in {PlanStatus.APPROVED, PlanStatus.EXECUTED}:
        raise SlicePackagingError(f"plan {plan_id} must be approved before delivery")
    if not record.plan.slices:
        raise SlicePackagingError(f"plan {plan_id} has no slices to deliver")
    task_ids: list[str] = []
    slice_evidence: list[CurrentTaskEvidence] = []
    for slice_obj in record.plan.slices:
        try:
            execution = slice_store.get(plan_id, slice_obj.slice_id)
        except Exception as exc:
            raise SlicePackagingError(
                f"slice {slice_obj.slice_id} has not been completed"
            ) from exc
        if execution.task_id is None:
            raise SlicePackagingError(f"slice {slice_obj.slice_id} has no task")
        task = task_store.get_task(execution.task_id)
        if task.state != WorkflowState.COMPLETE:
            raise SlicePackagingError(
                f"slice {slice_obj.slice_id} is {task.state.value}, not COMPLETE"
            )
        # A COMPLETE workflow state authorizes delivery only when the
        # harness can still *show* what completed it (ADR 0072). If the
        # deciding stage's artifact is missing or malformed, the projection
        # fails closed and delivery refuses rather than shipping a ZIP
        # whose verification section silently falls back to a superseded
        # pass from `report.json`.
        evidence = project_current_task_evidence(
            root, task_store, execution.task_id, record=task
        )
        if not evidence.is_verified_complete:
            raise SlicePackagingError(
                f"slice {slice_obj.slice_id} (task {execution.task_id}) is "
                f"persisted COMPLETE, but its current verification evidence "
                f"cannot support delivery: generation="
                f"{evidence.evidence_generation.value}, integrity="
                f"{evidence.evidence_integrity.value}"
                + (
                    f" ({evidence.evidence_integrity_detail})"
                    if evidence.evidence_integrity_detail
                    else ""
                )
                + "; the original report is preserved but must not be "
                "substituted for the missing current evidence"
            )
        task_ids.append(execution.task_id)
        slice_evidence.append(evidence)

    final_commit, completed = checkpoint_completed_prior_slices(
        root,
        plan_id,
        record.plan,
        record.plan.slices[-1].slice_id,
        task_store,
        slice_store,
        include_current=True,
    )
    if final_commit is None:
        raise SlicePackagingError("finished plan has no integrated commit")
    repository = GitRepository(root)
    final_worktree = None
    final_branch = None
    for task_id in task_ids:
        managed = WorktreeManager(root).describe(task_id.removeprefix("TASK-").lower())
        tip = repository.run(["rev-parse", "HEAD"], cwd=managed.path).stdout.strip()
        if tip == final_commit:
            final_worktree = managed.path
            final_branch = managed.branch
            break
    if final_worktree is None or final_branch is None:
        raise SlicePackagingError(
            f"plan {plan_id}: none of the {len(task_ids)} completed slice "
            f"task worktree(s) ({', '.join(task_ids)}) has HEAD at the "
            f"integrated commit {final_commit}; could not locate the "
            "integrated final worktree to archive"
        )

    # The whole-project gate (ADR 0074). Runs before the archive is written
    # and before `mark_executed`, so a plan whose integrated result fails
    # stays APPROVED with no ZIP, no delivery record, and no EXECUTED
    # status to undo. The audit artifact is persisted by the call below
    # regardless of outcome, so a refusal leaves evidence of why.
    final_verification = ensure_final_project_verification(
        root,
        record.plan,
        verification_config,
        plan_id=plan_id,
        plan_version=record.version,
        final_commit=final_commit,
        final_branch=final_branch,
        final_worktree_path=final_worktree,
        measured_in_task_id=task_ids[-1],
    )
    if not final_verification.is_sufficient_for_delivery:
        raise SlicePackagingError(
            f"plan {plan_id}: every slice is COMPLETE, but the integrated "
            f"project does not satisfy the plan's own whole-project "
            f"verification contract "
            f"(status={final_verification.status.value}): "
            f"{final_verification.reason}. Per-slice verification history is "
            "not evidence about the combined result. The plan remains "
            "APPROVED and undelivered; the record is at "
            f".apoapsis/plans/{plan_id}/{FINAL_VERIFICATION_ARTIFACT}"
        )

    files = sorted(
        item
        for item in repository.run(
            ["ls-tree", "-r", "--name-only", final_commit], cwd=final_worktree
        ).stdout.splitlines()
        if item
    )
    forbidden = _forbidden_delivery_paths(files)
    if forbidden:
        raise SlicePackagingError(
            f"plan {plan_id}: the integrated commit {final_commit} tracks "
            f"path(s) that must never be delivered: {', '.join(forbidden)}; "
            "remove them from git tracking (Apoapsis runtime state, `.git` "
            "metadata, and credential-shaped files are never shipped)"
        )

    # The operability gate (ADR 0076). Plan validation established that some
    # slice was *responsible* for each required artifact; this establishes
    # that the artifact is actually in the delivered tree. Crisis Atlas
    # shipped a seed README under a delivery contract that named one, and
    # nothing compared the two.
    operability = assess_delivered_operability(record.plan, files)
    if operability.missing_artifacts:
        raise SlicePackagingError(
            f"plan {plan_id}: the integrated commit {final_commit} does not "
            "contain delivery artifact(s) the approved plan requires: "
            f"{', '.join(operability.missing_artifacts)}. The plan remains "
            "APPROVED and undelivered; produce the artifact(s) or correct the "
            "plan's delivery_contract"
        )
    verification_summary: list[dict[str, object]] = [
        _verification_summary_entry(slice_obj.slice_id, task_id, evidence)
        for slice_obj, task_id, evidence in zip(
            record.plan.slices, task_ids, slice_evidence, strict=True
        )
    ]

    audit = PlanAuditStore(root, plan_id)
    archive_name = f"{plan_id}-finished-project.zip"
    archive_path = audit.root / archive_name
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive_name}.", dir=archive_path.parent
    )
    os.close(descriptor)
    try:
        repository.run(
            ["archive", "--format=zip", f"--output={temporary_name}", final_commit],
            cwd=final_worktree,
        )
        with zipfile.ZipFile(temporary_name, mode="a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "APOAPSIS-USING-THE-FINISHED-PROJECT.md",
                _usage_guide(
                    plan_id, final_commit, files, final_verification, operability
                ),
            )
        os.replace(temporary_name, archive_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    archive_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    relative_archive = archive_path.relative_to(root).as_posix()
    handoff_name = f"FRONTIER-WHOLE-PROJECT-REVIEW-{plan_id}.md"
    provisional = PlanDelivery(
        plan_id=plan_id,
        plan_version=(
            record.version + 1 if record.status == PlanStatus.APPROVED else record.version
        ),
        final_commit=final_commit,
        final_branch=final_branch,
        final_worktree_path=final_worktree,
        completed_slice_ids=completed,
        task_ids=task_ids,
        repository_files=files,
        verification_summary=verification_summary,
        final_project_verification=final_verification,
        operability=operability,
        archive_path=relative_archive,
        archive_sha256=archive_sha,
        frontier_review_handoff_path=(audit.root / handoff_name).relative_to(root).as_posix(),
    )
    audit.write_text(
        handoff_name,
        _frontier_review_markdown(provisional, record.plan),
        kind="frontier_whole_project_review_handoff",
    )
    if record.status == PlanStatus.APPROVED:
        executed = plan_store.mark_executed(
            plan_id,
            expected_version=record.version,
            final_commit=final_commit,
            delivery_path=relative_archive,
        )
        delivery = provisional.model_copy(update={"plan_version": executed.version})
    else:
        delivery = provisional
    audit.write_json("delivery.json", delivery, kind="plan_delivery")
    return delivery


def load_plan_delivery(project_root: str | Path, plan_id: str) -> PlanDelivery | None:
    path = Path(project_root).resolve() / ".apoapsis" / "plans" / plan_id / "delivery.json"
    if not path.is_file():
        return None
    return PlanDelivery.model_validate_json(path.read_text(encoding="utf-8"))


__all__ = ["PlanDelivery", "load_plan_delivery", "prepare_plan_delivery"]
