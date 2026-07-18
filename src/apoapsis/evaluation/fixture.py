from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def prepare_fixture_repository(source: Path, destination: Path) -> Path:
    """Copy a controlled fixture into a fresh, isolated, committed Git repository.

    Every evaluation lane gets its own copy so lanes never share worktree
    state, and mutating one copy never touches the checked-in fixture at
    `source`.
    """

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"{destination} already exists")
    shutil.copytree(source, destination)
    _git(destination, "init", "-b", "main")
    _git(destination, "config", "user.email", "apoapsis-eval@example.invalid")
    _git(destination, "config", "user.name", "Apoapsis Eval")
    _git(destination, "add", "-A")
    _git(destination, "commit", "-m", "controlled evaluation baseline")
    return destination


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
