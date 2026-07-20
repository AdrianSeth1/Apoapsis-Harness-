from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from apoapsis.audit.store import AuditArtifact, TaskAuditStore
from apoapsis.manual_frontier.schema import (
    ManualFrontierHandoffPackage,
    ManualFrontierResponseEnvelope,
    VerificationCatalogEntry,
)
from apoapsis.review.schema import ReviewCase
from apoapsis.specification.schema import TaskSpecification
from apoapsis.verification.runner import VerificationCommand

# Fixed, non-negotiable statements included verbatim in every exported
# package and rendered in the Markdown file, so the boundary is explicit to
# a human pasting this into a chat session, not merely enforced silently by
# the response schema (ADR 0031).
MANUAL_FRONTIER_AUTHORITY_RULES: tuple[str, ...] = (
    "Return exactly one complete, bounded unified-diff patch that solves "
    "the task -- this is not an interactive shell or tool-call loop, and "
    "you cannot request more turns, more files, or another round.",
    "You cannot mark this task complete. Only Apoapsis's own verification "
    "runner, after your patch is applied in the real worktree by Apoapsis, "
    "decides whether the task is done.",
    "You cannot select, invoke, or substitute a verification command. The "
    "commands listed below are informational only -- Apoapsis runs them, "
    "never you.",
    "You cannot expand your own budget, request another round, or change "
    "any workflow state. If verification fails, Apoapsis alone decides "
    "whether a further bounded repair round is offered.",
    "Return only the fields defined in the response schema below, wrapped "
    "in nothing else -- no markdown fences, no commentary outside the "
    "JSON object. Any additional field is rejected outright.",
    "You have no shell, filesystem, git, network, or workflow access. "
    "Everything you know about this repository is in this document.",
)


def _sha256_canonical(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verification_catalog(
    commands: list[VerificationCommand],
) -> list[VerificationCatalogEntry]:
    return [
        VerificationCatalogEntry(
            name=command.name,
            category=command.category,
            description=command.description,
            required=command.required,
            acceptance_designated=command.acceptance,
        )
        for command in commands
    ]


def build_manual_frontier_handoff_package(
    review_case: ReviewCase,
    specification: TaskSpecification,
    verification_commands: list[VerificationCommand],
    *,
    package_id: str | None = None,
    repair_round: int = 0,
) -> ManualFrontierHandoffPackage:
    """Deterministically build the immutable manual-frontier handoff
    package (ADR 0031) from the exact same ``ReviewCase`` projection every
    other review action uses -- no separate, parallel evidence format.
    Requires a worktree (diff, fingerprint, repository HEAD); manual
    handoff is only ever eligible once one exists.

    Contains no secrets (``VerificationCommand.environment`` values are
    never included, only the command's name/category/description),
    no unrelated files (only the current diff, active constraints, and
    normalized failures -- the same bounded evidence the automated
    escalation package already includes), and no held-out oracle or
    audit-only content.
    """

    assert review_case.current_diff is not None
    assert review_case.worktree_fingerprint is not None
    assert review_case.repository_head_commit is not None
    resolved_id = package_id or f"MFH-{uuid.uuid4().hex}"
    response_schema = ManualFrontierResponseEnvelope.model_json_schema()

    base = ManualFrontierHandoffPackage(
        package_id=resolved_id,
        task_id=review_case.task_id,
        task_version=review_case.task_version,
        repair_round=repair_round,
        worktree_fingerprint=review_case.worktree_fingerprint,
        repository_head_commit=review_case.repository_head_commit,
        specification=specification,
        active_constraints=review_case.active_hard_constraints,
        current_diff=review_case.current_diff,
        stop_reason_kind=review_case.stop_reason_kind,
        stop_reason_text=review_case.stop_reason_text,
        normalized_failures=review_case.normalized_failures,
        verification_catalog=_verification_catalog(verification_commands),
        response_schema=response_schema,
        authority_rules=list(MANUAL_FRONTIER_AUTHORITY_RULES),
        package_sha256="0" * 64,
    )
    payload = base.model_dump(mode="json", exclude={"package_id", "generated_at", "package_sha256"})
    digest = _sha256_canonical(payload)
    return base.model_copy(update={"package_sha256": digest})


def verify_package_integrity(package: ManualFrontierHandoffPackage) -> bool:
    """Recompute ``package_sha256`` from a package's own content and
    compare -- used before trusting any package reloaded from disk."""

    payload = package.model_dump(
        mode="json", exclude={"package_id", "generated_at", "package_sha256"}
    )
    return _sha256_canonical(payload) == package.package_sha256


def build_handoff_markdown(package: ManualFrontierHandoffPackage) -> str:
    """A single, self-contained Markdown file a user uploads directly to a
    ChatGPT/Claude subscription session -- everything needed to produce one
    valid ``ManualFrontierResponseEnvelope`` is embedded here; nothing
    external is referenced."""

    lines: list[str] = []
    lines.append("# Frontier Coding Handoff")
    lines.append("")
    lines.append(
        "This is a manual, subscription-based handoff package produced by "
        "Apoapsis Harness. Upload this whole file to your ChatGPT or Claude "
        "subscription session and ask it to solve the task described below, "
        "returning **only** the JSON response object described at the end "
        "-- nothing else."
    )
    lines.append("")
    lines.append(f"- Package ID: `{package.package_id}`")
    lines.append(f"- Package SHA-256: `{package.package_sha256}`")
    lines.append(f"- Task ID: `{package.task_id}`")
    lines.append(f"- Task version: `{package.task_version}`")
    lines.append(f"- Repair round: `{package.repair_round}`")
    lines.append(f"- Worktree fingerprint: `{package.worktree_fingerprint}`")
    lines.append(f"- Repository HEAD: `{package.repository_head_commit}`")
    lines.append("")
    lines.append("## Authority rules (binding)")
    lines.append("")
    for rule in package.authority_rules:
        lines.append(f"- {rule}")
    lines.append("")
    lines.append("## Task objective")
    lines.append("")
    lines.append(package.specification.objective.text)
    lines.append("")
    lines.append("## Active hard constraints")
    lines.append("")
    if package.active_constraints:
        for constraint in package.active_constraints:
            lines.append(f"- **{constraint.id}**: {constraint.text}")
            if constraint.interpreted_meaning:
                lines.append(f"  - Interpreted meaning: {constraint.interpreted_meaning}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Stop reason")
    lines.append("")
    lines.append(f"- Kind: `{package.stop_reason_kind.value}`")
    lines.append(f"- Detail: {package.stop_reason_text}")
    lines.append("")
    if package.normalized_failures:
        lines.append("## Relevant failure evidence")
        lines.append("")
        for failure in package.normalized_failures:
            lines.append(f"### `{failure.command_name}`")
            lines.append("")
            lines.append("```")
            lines.append(failure.relevant_error)
            lines.append("```")
            lines.append("")
    lines.append("## Current diff (already applied to the worktree)")
    lines.append("")
    lines.append("```diff")
    lines.append(package.current_diff or "(no diff -- worktree matches HEAD)")
    lines.append("```")
    lines.append("")
    lines.append("## Configured verification commands (informational only)")
    lines.append("")
    lines.append("| Name | Category | Required | Acceptance | Description |")
    lines.append("| --- | --- | --- | --- | --- |")
    for entry in package.verification_catalog:
        lines.append(
            f"| {entry.name} | {entry.category} | {entry.required} | "
            f"{entry.acceptance_designated} | {entry.description} |"
        )
    lines.append("")
    lines.append("## Required response format")
    lines.append("")
    lines.append(
        "Return **one** JSON object matching this exact schema -- no "
        "markdown code fence, no extra text before or after it, and no "
        "additional fields beyond what is listed here:"
    )
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(package.response_schema, indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append(
        f"Echo back `\"package_id\": \"{package.package_id}\"` and "
        f"`\"package_sha256\": \"{package.package_sha256}\"` exactly as shown "
        f"above, `\"task_id\": \"{package.task_id}\"`, and "
        f"`\"task_version\": {package.task_version}` -- Apoapsis rejects any "
        "response that does not match this package exactly."
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def write_handoff_artifacts(
    audit: TaskAuditStore, package: ManualFrontierHandoffPackage
) -> tuple[AuditArtifact, AuditArtifact]:
    """Writes the canonical JSON package plus the self-contained Markdown
    handoff file, both under the task's own audit directory."""

    json_artifact = audit.write_json(
        f"manual-frontier-handoff-{package.package_id}.json",
        package,
        kind="manual_frontier_handoff_package",
    )
    markdown_artifact = audit.write_text(
        f"FRONTIER-CODING-HANDOFF-{package.package_id}.md",
        build_handoff_markdown(package),
        kind="manual_frontier_handoff_markdown",
    )
    return json_artifact, markdown_artifact


def package_path(root: str | Path, task_id: str, package_id: str) -> Path:
    return (
        Path(root).resolve()
        / ".apoapsis"
        / "tasks"
        / task_id
        / f"manual-frontier-handoff-{package_id}.json"
    )


def load_package(root: str | Path, task_id: str, package_id: str) -> ManualFrontierHandoffPackage:
    from apoapsis.manual_frontier.errors import PackageIntegrityError, PackageNotFoundError

    path = package_path(root, task_id, package_id)
    if not path.is_file():
        raise PackageNotFoundError(f"manual-frontier package not found: {package_id}")
    package = ManualFrontierHandoffPackage.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    if not verify_package_integrity(package):
        raise PackageIntegrityError(
            f"manual-frontier package {package_id} failed its own integrity check "
            "-- the file on disk does not match its recorded package_sha256"
        )
    return package


__all__ = [
    "MANUAL_FRONTIER_AUTHORITY_RULES",
    "build_manual_frontier_handoff_package",
    "verify_package_integrity",
    "build_handoff_markdown",
    "write_handoff_artifacts",
    "package_path",
    "load_package",
]
