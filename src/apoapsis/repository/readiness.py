from __future__ import annotations

import ast
import json
import re
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


_PLAN_RESPONSE_TRANSFER_NAME = re.compile(
    r"^apoapsis-plan-response(?:[-_ .][A-Za-z0-9._ -]+)?\.json$",
    re.IGNORECASE,
)
_MAX_PLAN_RESPONSE_TRANSFER_BYTES = 16 * 1024 * 1024


def _registered_plan_response_payloads(root: Path) -> list[object]:
    """Load only canonical responses already accepted into discovery audit."""

    from pydantic import ValidationError

    from apoapsis.discovery.schema import FrontierPlanningResponseEnvelope

    discovery_root = root / ".apoapsis" / "discovery"
    if not discovery_root.is_dir():
        return []
    payloads: list[object] = []
    for artifact in sorted(discovery_root.glob("*/frontier-response-FPKG-*.json")):
        try:
            if artifact.stat().st_size > _MAX_PLAN_RESPONSE_TRANSFER_BYTES:
                continue
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            FrontierPlanningResponseEnvelope.model_validate(payload)
            payloads.append(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError):
            # An unreadable audit artifact proves nothing. The ordinary dirty
            # repository refusal remains in force for the transfer file.
            continue
    return payloads


def _plan_response_identity(payload: object) -> tuple[object, ...] | None:
    if not isinstance(payload, dict):
        return None
    return tuple(
        payload.get(key)
        for key in (
            "schema_version",
            "package_id",
            "package_sha256",
            "session_id",
            "kind",
        )
    )


def exclude_registered_plan_response_transfers(
    project_root: str | Path,
) -> list[str]:
    """Locally exclude exact, already-imported manual planning responses.

    A manual frontier response is transport material, not project source. A
    user can naturally save it in the selected project folder before uploading
    it through the browser, which used to make the next execution fail its
    dirty-parent guard. This recovery is deliberately narrow: the candidate
    must be an untracked top-level JSON file using Apoapsis's transfer filename
    convention, validate as a response envelope, and carry the same
    cryptographic package/session identity as a canonical response already
    retained in the discovery audit. The user file is never moved, rewritten,
    or deleted; only its exact root-relative name is appended to
    ``.git/info/exclude``.
    """

    from pydantic import ValidationError

    from apoapsis.discovery.schema import FrontierPlanningResponseEnvelope
    from apoapsis.specification.pasted_json import PastedJsonError, parse_pasted_json

    root = Path(project_root).resolve()
    repository = GitRepository(root)
    registered = _registered_plan_response_payloads(root)
    if not registered:
        return []
    raw_untracked = repository.run(
        ["ls-files", "-z", "--others", "--exclude-standard"]
    ).stdout
    matched: list[str] = []
    for relative in sorted(item for item in raw_untracked.split("\0") if item):
        candidate_relative = Path(relative)
        if len(candidate_relative.parts) != 1:
            continue
        if not _PLAN_RESPONSE_TRANSFER_NAME.fullmatch(candidate_relative.name):
            continue
        candidate = root / candidate_relative
        try:
            if candidate.stat().st_size > _MAX_PLAN_RESPONSE_TRANSFER_BYTES:
                continue
            payload = parse_pasted_json(
                candidate.read_text(encoding="utf-8"), what="plan response transfer"
            )
            FrontierPlanningResponseEnvelope.model_validate(payload)
        except (OSError, UnicodeError, PastedJsonError, ValidationError):
            continue
        identity = _plan_response_identity(payload)
        if identity is not None and any(
            identity == _plan_response_identity(accepted) for accepted in registered
        ):
            matched.append(candidate_relative.as_posix())
    if not matched:
        return []

    raw_exclude = repository.run(
        ["rev-parse", "--git-path", "info/exclude"]
    ).stdout.strip()
    exclude_path = Path(raw_exclude)
    if not exclude_path.is_absolute():
        exclude_path = root / exclude_path
    exclude_path = exclude_path.resolve()
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        exclude_path.read_text(encoding="utf-8") if exclude_path.is_file() else ""
    )
    existing_lines = set(existing.splitlines())
    additions = [
        f"/{relative}"
        for relative in matched
        if f"/{relative}" not in existing_lines
    ]
    if additions:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        heading = "# Imported Apoapsis planning response transfer files"
        heading_line = "" if heading in existing_lines else f"{heading}\n"
        exclude_path.write_text(
            existing
            + separator
            + heading_line
            + "".join(f"{line}\n" for line in additions),
            encoding="utf-8",
        )
    return matched


def _has_testcase_class(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            base_name = ""
            if isinstance(base, ast.Attribute):
                base_name = base.attr
            elif isinstance(base, ast.Name):
                base_name = base.id
            if "TestCase" in base_name:
                return True
    return False


def _imports_pytest(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            item.name.split(".", 1)[0] == "pytest" for item in node.names
        ):
            return True
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".", 1)[0] == "pytest"
        ):
            return True
    return False


def _unittest_discovery_pitfalls(
    root: Path, start_directory: Path, pattern: str, command_name: str
) -> list[str]:
    """Deterministic, live-worktree checks for the two ways a
    ``python -m unittest discover`` command silently collects zero tests
    from files that do exist: a package directory in the discovered tree
    missing its own ``__init__.py`` (discover skips non-package
    subdirectories without raising), and a test file written for pytest
    (bare ``assert``, ``pytest.raises``, plain classes) instead of a
    ``unittest.TestCase`` subclass unittest's loader can actually find.
    Both were observed together in a live local-coder run that repeatedly
    failed verification with ``NO TESTS RAN`` and never diagnosed why."""

    findings: list[str] = []
    missing_init_dirs: set[Path] = set()
    pytest_style_files: list[Path] = []
    for match in start_directory.rglob(pattern):
        if not match.is_file():
            continue
        directory = match.parent
        while True:
            if not (directory / "__init__.py").is_file():
                missing_init_dirs.add(directory)
            if directory == start_directory:
                break
            directory = directory.parent
        try:
            tree = ast.parse(match.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        if _imports_pytest(tree) and not _has_testcase_class(tree):
            pytest_style_files.append(match)
    if missing_init_dirs:
        relative = sorted(
            directory.relative_to(root).as_posix() for directory in missing_init_dirs
        )
        findings.append(
            f"Required check {command_name!r} uses unittest discovery, which "
            "silently collects zero tests from a package directory missing its "
            "own __init__.py rather than raising an error. Add __init__.py to: "
            + ", ".join(relative)
            + "."
        )
    if pytest_style_files:
        relative = sorted(
            path.relative_to(root).as_posix() for path in pytest_style_files
        )
        findings.append(
            f"Required check {command_name!r} runs stdlib unittest discovery, "
            "which only collects unittest.TestCase subclasses. The following "
            "test file(s) import pytest and use plain classes/bare assert/"
            "pytest.raises instead, so unittest discovers zero tests from them "
            "even though they exist: "
            + ", ".join(relative)
            + ". Rewrite them as unittest.TestCase subclasses using "
            "self.assertEqual/self.assertRaises, or add a configured "
            "pytest-based verification command instead."
        )
    return findings


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
        if not resolved.is_dir():
            obligations.append(
                f"Required check {command.name!r} discovers from missing directory "
                f"{start_directory.as_posix()!r}. Because test changes are allowed, "
                "create that importable directory and meaningful task-focused tests "
                "before verification. This repair is part of implementation; the "
                "missing scaffold alone is not a reason to request escalation."
            )
            continue
        try:
            pattern = argv[argv.index("-p") + 1]
        except (ValueError, IndexError):
            pattern = "test*.py"
        obligations.extend(
            _unittest_discovery_pitfalls(root, resolved, pattern, command.name)
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
    exclude_registered_plan_response_transfers(project_root)
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
    "exclude_registered_plan_response_transfers",
    "required_verification_scaffolding",
    "require_clean_parent_repository",
    "require_viable_verification_contract",
]
