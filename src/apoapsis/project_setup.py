"""Friendly, fail-closed setup for a folder selected by the Windows launcher."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from apoapsis.cli.app import _init
from apoapsis.repository.git import GitCommandError, GitRepository


class ProjectSetupError(RuntimeError):
    """The selected folder cannot be prepared without guessing at user files."""


def _run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ProjectSetupError(completed.stderr.strip() or "Git could not prepare the folder.")
    return completed


def _find_existing_git_root(root: Path) -> Path | None:
    """Return the containing Git root without creating anything."""

    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=30,
    )
    if completed.returncode != 0:
        return None
    return Path(completed.stdout.strip()).resolve()


def _visible_entries(root: Path) -> list[Path]:
    try:
        return [item for item in root.iterdir() if item.name.casefold() != ".git"]
    except OSError as exc:
        raise ProjectSetupError("Apoapsis could not read the selected folder.") from exc


def prepare_selected_project(project_root: str | Path) -> dict[str, object]:
    """Prepare an empty folder or initialize an existing Git repository.

    Non-empty non-Git folders and repositories with files but no first commit
    are refused. Automatically committing those files could capture secrets or
    unrelated work, so that decision remains with the operator.
    """

    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ProjectSetupError("The selected folder is unavailable.")

    created_git_repository = False
    existing_git_root = _find_existing_git_root(root)
    if existing_git_root is not None and existing_git_root != root:
        raise ProjectSetupError(
            f"This folder is inside a Git project. Choose its top folder instead: "
            f"{existing_git_root}"
        )
    if existing_git_root is None:
        if _visible_entries(root):
            raise ProjectSetupError(
                "This folder already contains files but is not a Git project. "
                "Choose an empty folder or an existing Git project."
            )
        _run_git(root, ["init", "-b", "main"])
        created_git_repository = True

    try:
        repository = GitRepository(root)
    except GitCommandError as exc:
        raise ProjectSetupError(
            "This folder is not the top level of a usable Git project."
        ) from exc
    if repository.root.resolve() != root:
        raise ProjectSetupError(
            f"Choose the project's top folder instead: {repository.root}"
        )

    had_commit = repository.has_commits()
    if not had_commit and _visible_entries(root):
        raise ProjectSetupError(
            "This Git project contains files but has no saved starting point. "
            "Create its first commit in your Git app, then select it again."
        )

    created_initial_commit = False
    if not had_commit:
        _run_git(
            root,
            [
                "-c",
                "user.name=Apoapsis Setup",
                "-c",
                "user.email=apoapsis@local.invalid",
                "commit",
                "--no-verify",
                "--allow-empty",
                "-m",
                "Initialize project",
            ],
        )
        created_initial_commit = True

    init_result = _init(root, local_git_exclude=True)

    return {
        "ready": True,
        "project_root": str(root),
        "created_git_repository": created_git_repository,
        "created_initial_commit": created_initial_commit,
        "apoapsis_initialized": bool(init_result["initialized"]),
        "config_created": bool(init_result["config_created"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a selected Apoapsis project")
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare_selected_project(args.project_root)
    except ProjectSetupError as exc:
        print(f"Apoapsis could not prepare this folder.\n{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ProjectSetupError", "prepare_selected_project"]
