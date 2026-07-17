from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from pydantic import ValidationError

from sol.execution.worktree import WorktreeError, WorktreeManager
from sol.repository.git import GitCommandError, GitRepository
from sol.specification.schema import (
    AcceptanceCriterion,
    HardConstraint,
    SourceKind,
    TaskSpecification,
    TraceableStatement,
)
from sol.verification.runner import VerificationConfig, VerificationRunner
from sol.workflow.engine import SQLiteTaskStore, TaskStoreError
from sol.workflow.events import WorkflowActor
from sol.workflow.states import WorkflowState


DEFAULT_CONFIG = """# SOL Harness project configuration
[project]
language = "python"

[verification]
stop_on_failure = false
output_limit_chars = 100000
environment_allowlist = [
  "PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP",
  "USERPROFILE", "HOME", "VIRTUAL_ENV"
]

[[verification.commands]]
name = "unit-tests"
category = "tests"
argv = ["python", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"]
timeout_seconds = 120
required = true
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sol",
        description="Local-first deterministic coding-task harness",
    )
    parser.add_argument(
        "--project-root", type=Path, default=Path.cwd(), help=argparse.SUPPRESS
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="initialize SOL metadata")

    task = subparsers.add_parser("task", help="draft a structured task")
    task.add_argument("request")
    task.add_argument(
        "--constraint", action="append", default=[], help="verbatim hard constraint"
    )
    task.add_argument(
        "--acceptance", action="append", default=[], help="acceptance criterion"
    )

    inspect = subparsers.add_parser("inspect", help="show a task and audit events")
    inspect.add_argument("task_id")

    approve = subparsers.add_parser("approve", help="approve a drafted task spec")
    approve.add_argument("task_id")
    approve.add_argument("--version", type=int)

    worktree = subparsers.add_parser(
        "worktree-create", help="create an isolated task worktree"
    )
    worktree.add_argument("task_id")
    worktree.add_argument("--base", default="HEAD")

    verify = subparsers.add_parser("verify", help="run configured checks")
    verify.add_argument("task_id")
    verify.add_argument("--path", type=Path)

    rollback = subparsers.add_parser(
        "rollback", help="remove a task worktree and mark it rolled back"
    )
    rollback.add_argument("task_id")
    rollback.add_argument(
        "--delete-branch", action="store_true", help="also delete the task branch"
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
    except (TaskStoreError, WorktreeError, GitCommandError, ValidationError) as exc:
        parser.exit(2, f"error: {exc}\n")
    if result is not None:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))


def _dispatch(args: argparse.Namespace) -> dict[str, object] | None:
    root = args.project_root.resolve()
    if args.command == "init":
        return _init(root)
    store = _store(root)
    if args.command == "task":
        return _task(store, args.request, args.constraint, args.acceptance)
    if args.command == "inspect":
        record = store.get_task(args.task_id)
        return {
            "task": record.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in store.events(args.task_id)],
        }
    if args.command == "approve":
        record = store.transition(
            args.task_id,
            WorkflowState.SPEC_APPROVED,
            actor=WorkflowActor.USER,
            event_type="specification_approved",
            expected_version=args.version,
        )
        return record.model_dump(mode="json")
    if args.command == "worktree-create":
        store.get_task(args.task_id)
        manager = WorktreeManager(root)
        worktree = manager.create(_task_slug(args.task_id), base_ref=args.base)
        return worktree.model_dump(mode="json")
    if args.command == "verify":
        return _verify(root, store, args.task_id, args.path)
    if args.command == "rollback":
        return _rollback(root, store, args.task_id, args.delete_branch)
    raise AssertionError(f"unhandled command: {args.command}")


def _init(root: Path) -> dict[str, object]:
    GitRepository(root)
    metadata = root / ".sol"
    metadata.mkdir(parents=True, exist_ok=True)
    config = metadata / "config.toml"
    created_config = False
    if not config.exists():
        config.write_text(DEFAULT_CONFIG, encoding="utf-8")
        created_config = True
    SQLiteTaskStore(metadata / "sol.db")
    return {
        "initialized": True,
        "metadata_directory": str(metadata),
        "config_created": created_config,
    }


def _store(root: Path) -> SQLiteTaskStore:
    metadata = root / ".sol"
    if not (metadata / "config.toml").is_file():
        raise TaskStoreError("SOL is not initialized; run 'sol init' first")
    return SQLiteTaskStore(metadata / "sol.db")


def _task(
    store: SQLiteTaskStore,
    request: str,
    constraints: list[str],
    acceptance: list[str],
) -> dict[str, object]:
    task_id = f"TASK-{uuid.uuid4().hex[:12].upper()}"
    specification = TaskSpecification(
        task_id=task_id,
        objective=TraceableStatement(
            text=request,
            source=SourceKind.USER,
            source_reference="cli-request",
        ),
        acceptance_criteria=[
            AcceptanceCriterion(
                id=f"AC-{index}",
                text=text,
                source=SourceKind.USER,
                source_reference=f"cli-acceptance-{index}",
            )
            for index, text in enumerate(acceptance, start=1)
        ],
        hard_constraints=[
            HardConstraint(
                id=f"HC-{index}",
                text=text,
                verbatim_source=text,
                interpreted_meaning=text,
                source=SourceKind.USER,
                source_reference=f"cli-constraint-{index}",
                verification_method="pending specification review",
            )
            for index, text in enumerate(constraints, start=1)
        ],
    )
    store.create_task(specification)
    record = store.transition(
        task_id,
        WorkflowState.SPEC_DRAFTED,
        actor=WorkflowActor.SYSTEM,
        event_type="deterministic_specification_drafted",
        payload={"natural_language_extraction_used": False},
    )
    return record.model_dump(mode="json")


def _verify(
    root: Path,
    store: SQLiteTaskStore,
    task_id: str,
    requested_path: Path | None,
) -> dict[str, object]:
    record = store.get_task(task_id)
    if record.state != WorkflowState.PATCH_READY:
        raise TaskStoreError(
            f"verification requires PATCH_READY, found {record.state.value}"
        )
    project_path = requested_path
    if project_path is None:
        manager = WorktreeManager(root)
        project_path = Path(manager.describe(_task_slug(task_id)).path)
    config = VerificationConfig.from_toml(root / ".sol" / "config.toml")
    store.transition(
        task_id,
        WorkflowState.VERIFYING,
        actor=WorkflowActor.VERIFICATION_ENGINE,
        event_type="verification_started",
        expected_version=record.version,
    )
    result = VerificationRunner(config).run(task_id, project_path)
    target = (
        WorkflowState.COMPLETE
        if result.status.value == "passed"
        else WorkflowState.LOCAL_REPAIR
    )
    store.transition(
        task_id,
        target,
        actor=WorkflowActor.VERIFICATION_ENGINE,
        event_type="verification_finished",
        payload=result.model_dump(mode="json"),
    )
    return result.model_dump(mode="json")


def _rollback(
    root: Path,
    store: SQLiteTaskStore,
    task_id: str,
    delete_branch: bool,
) -> dict[str, object]:
    record = store.get_task(task_id)
    manager = WorktreeManager(root)
    manager.cleanup(
        _task_slug(task_id), force=True, delete_branch=delete_branch
    )
    rolled_back = store.transition(
        task_id,
        WorkflowState.ROLLED_BACK,
        actor=WorkflowActor.USER,
        event_type="explicit_rollback",
        payload={"branch_deleted": delete_branch},
        expected_version=record.version,
    )
    return rolled_back.model_dump(mode="json")


def _task_slug(task_id: str) -> str:
    return task_id.removeprefix("TASK-").lower()


if __name__ == "__main__":
    main(sys.argv[1:])

