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
from apoapsis.architect.schema import PlanStatus
from apoapsis.architect.slice_package import checkpoint_completed_prior_slices
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.execution.worktree import WorktreeManager
from apoapsis.reporting.report import FinalTaskReport
from apoapsis.repository.git import GitRepository
from apoapsis.specification.schema import StrictModel
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.states import WorkflowState


class PlanDelivery(StrictModel):
    schema_version: str = "1.0"
    plan_id: str
    plan_version: int
    final_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    final_branch: str
    final_worktree_path: str
    completed_slice_ids: list[str]
    task_ids: list[str]
    repository_files: list[str]
    verification_summary: list[dict[str, object]]
    archive_path: str
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frontier_review_handoff_path: str


def _report(root: Path, task_id: str) -> FinalTaskReport | None:
    path = root / ".apoapsis" / "tasks" / task_id / "report.json"
    if not path.is_file():
        return None
    return FinalTaskReport.model_validate_json(path.read_text(encoding="utf-8"))


def _frontier_review_markdown(delivery: PlanDelivery, plan_payload: dict) -> str:
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
        "## Approved architecture plan\n\n```json\n"
        + json.dumps(plan_payload, indent=2, sort_keys=True)
        + "\n```\n\n## Harness verification summary\n\n```json\n"
        + json.dumps(delivery.verification_summary, indent=2, sort_keys=True)
        + "\n```\n\n## Repository file inventory\n\n"
        + "\n".join(f"- `{path}`" for path in delivery.repository_files)
        + "\n\n## Required response shape\n\n```json\n"
        + json.dumps(response_schema, indent=2, sort_keys=True)
        + "\n```\n"
    )


def _usage_guide(plan_id: str, final_commit: str, files: list[str]) -> str:
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
    return (
        "# Using the finished project\n\n"
        f"Apoapsis prepared this archive from plan `{plan_id}` at integrated commit "
        f"`{final_commit}` after every slice reached COMPLETE.\n\n"
        "## Start here\n\n"
        + "\n".join(markers)
        + "\n\n## Important\n\n"
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
) -> PlanDelivery:
    """Checkpoint and export the exact integrated result of a finished plan."""

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
        task_ids.append(execution.task_id)

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
        raise SlicePackagingError("could not locate the integrated final worktree")

    files = sorted(
        item
        for item in repository.run(
            ["ls-tree", "-r", "--name-only", final_commit], cwd=final_worktree
        ).stdout.splitlines()
        if item
    )
    verification_summary: list[dict[str, object]] = []
    for slice_obj, task_id in zip(record.plan.slices, task_ids, strict=True):
        report = _report(root, task_id)
        verification_summary.append(
            {
                "slice_id": slice_obj.slice_id,
                "task_id": task_id,
                "outcome": report.outcome.value if report else "complete_without_report",
                "verification": [
                    {
                        "name": item.command_name,
                        "status": item.status.value,
                        "exit_code": item.exit_code,
                    }
                    for item in (report.verification_results if report else [])
                ],
            }
        )

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
                _usage_guide(plan_id, final_commit, files),
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
        archive_path=relative_archive,
        archive_sha256=archive_sha,
        frontier_review_handoff_path=(audit.root / handoff_name).relative_to(root).as_posix(),
    )
    audit.write_text(
        handoff_name,
        _frontier_review_markdown(provisional, record.plan.model_dump(mode="json")),
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
