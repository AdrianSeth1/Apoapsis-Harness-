from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

from apoapsis.config import ApoapsisConfig
from apoapsis.repository.git import GitRepository
from apoapsis.verification.runner import VerificationConfig


class DirtyParentRepositoryError(RuntimeError):
    """Raised when the parent repository has uncommitted tracked changes
    or untracked files at the moment execution is about to start (ADR
    0026).

    ``VerticalSliceRunner._run_from_approved`` compiles the agent's
    initial context by reading directly from the parent checkout, but
    ``WorktreeManager.create()`` creates the task's isolated worktree from
    clean HEAD, carrying none of that uncommitted state. If the parent
    checkout is dirty, the two disagree about what the repository
    contains: the context could describe file content the worktree the
    agent actually edits does not have. Failing closed here is the only
    safe response -- Apoapsis never stashes, resets, deletes, or commits
    a user's uncommitted work automatically."""


class VerificationContractError(RuntimeError):
    """Raised when configured verification cannot possibly run as written."""


def required_verification_scaffolding(
    project_root: str | Path,
    verification: VerificationConfig,
    *,
    allow_test_changes: bool,
    allow_dependency_changes: bool = False,
) -> list[str]:
    """Return live, deterministic obligations needed to run required checks."""

    if not allow_test_changes and not allow_dependency_changes:
        return []
    root = Path(project_root).resolve()
    obligations: list[str] = []
    for command in verification.commands if allow_test_changes else []:
        argv = list(command.argv)
        if not command.required or "unittest" not in argv or "discover" not in argv:
            continue
        try:
            start_directory = Path(argv[argv.index("-s") + 1])
        except (ValueError, IndexError):
            continue
        resolved = start_directory if start_directory.is_absolute() else root / start_directory
        if resolved.is_dir():
            continue
        obligations.append(
            f"Required check {command.name!r} discovers from missing directory "
            f"{start_directory.as_posix()!r}. Because test changes are allowed, "
            "create that importable directory and meaningful task-focused tests "
            "before verification. This repair is part of implementation; the "
            "missing scaffold alone is not a reason to request escalation."
        )
    requirement_manifests = [
        path
        for path in root.glob("requirements*.txt")
        if any(
            line.strip() and not line.lstrip().startswith("#")
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    ]
    pyproject = root / "pyproject.toml"
    pyproject_dependencies = False
    if pyproject.is_file():
        try:
            with pyproject.open("rb") as handle:
                project_table = tomllib.load(handle).get("project", {})
            pyproject_dependencies = bool(
                isinstance(project_table, dict)
                and (
                    project_table.get("dependencies")
                    or project_table.get("optional-dependencies")
                )
            )
        except (OSError, tomllib.TOMLDecodeError):
            pyproject_dependencies = False
    has_manifest = bool(requirement_manifests or pyproject_dependencies)
    if allow_dependency_changes and not has_manifest:
        local_modules = {
            path.stem for path in root.glob("*.py")
        } | {
            path.name for path in root.iterdir() if path.is_dir()
        } | {
            path.name for path in root.rglob("*") if path.is_dir() and (path / "__init__.py").is_file()
        }
        third_party: set[str] = set()
        for path in sorted(root.rglob("*.py"))[:500]:
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeError):
                continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for name in names:
                    root_name = name.split(".", 1)[0]
                    if root_name not in sys.stdlib_module_names and root_name not in local_modules:
                        third_party.add(root_name)
        if third_party:
            obligations.append(
                "Third-party Python imports are present but no requirements*.txt "
                "or pyproject.toml exists. Declare installable distributions for: "
                + ", ".join(sorted(third_party))
                + ". Apoapsis will install the manifest before verification."
            )
    return obligations


def require_viable_verification_contract(
    project_root: str | Path, config: ApoapsisConfig
) -> None:
    """Reject a known-impossible test contract before spending model calls.

    ``apoapsis init`` historically supplied Python unittest discovery as an
    example. In a blank or non-Python repository, ``-s tests`` cannot run; if
    test changes are also forbidden, no model patch can make it runnable.
    """

    root = Path(project_root).resolve()
    if config.patch.allow_test_changes:
        return
    for command in config.verification.commands:
        argv = list(command.argv)
        if not command.required or "unittest" not in argv or "discover" not in argv:
            continue
        try:
            start_directory = Path(argv[argv.index("-s") + 1])
        except (ValueError, IndexError):
            continue
        resolved = (
            start_directory
            if start_directory.is_absolute()
            else root / start_directory
        )
        if resolved.is_dir():
            continue
        raise VerificationContractError(
            f"required verification command {command.name!r} discovers tests "
            f"from missing directory {start_directory.as_posix()!r}, while "
            "patch.allow_test_changes is false; no permitted model patch can "
            "make this command runnable. Configure a real project check, add "
            "the test directory yourself, or explicitly allow test changes "
            "before starting execution"
        )


def require_clean_parent_repository(project_root: str | Path) -> None:
    snapshot = GitRepository(project_root).snapshot()
    if snapshot.is_clean:
        return
    changed = ", ".join(snapshot.changed_files[:20])
    more = (
        f" (and {len(snapshot.changed_files) - 20} more)"
        if len(snapshot.changed_files) > 20
        else ""
    )
    raise DirtyParentRepositoryError(
        "the parent repository has uncommitted tracked changes or "
        "untracked files, so the context the agent would see (compiled "
        "from the parent checkout) would not match the isolated worktree "
        "it will actually edit (created fresh from clean HEAD). Commit or "
        "stash these changes yourself first -- Apoapsis will not modify "
        f"them automatically. Changed: {changed}{more}"
    )


__all__ = [
    "DirtyParentRepositoryError",
    "VerificationContractError",
    "required_verification_scaffolding",
    "require_clean_parent_repository",
    "require_viable_verification_contract",
]
