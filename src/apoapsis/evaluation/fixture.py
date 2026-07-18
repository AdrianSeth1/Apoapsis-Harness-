from __future__ import annotations

import shutil
import subprocess
from pathlib import Path, PurePosixPath


def prepare_fixture_repository(
    source: Path,
    destination: Path,
    *,
    excluded_relative_files: list[str] | None = None,
) -> Path:
    """Copy a controlled fixture into a fresh, isolated, committed Git repository.

    Every evaluation lane gets its own copy so lanes never share worktree
    state, and mutating one copy never touches the checked-in fixture at
    `source`.
    """

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"{destination} already exists")
    validated_exclusions: list[PurePosixPath] = []
    for relative in sorted(excluded_relative_files or []):
        pure = PurePosixPath(relative.replace("\\", "/"))
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            raise ValueError(f"unsafe fixture exclusion path: {relative}")
        source_target = (Path(source).resolve() / Path(*pure.parts)).resolve()
        try:
            source_target.relative_to(Path(source).resolve())
        except ValueError as exc:
            raise ValueError(f"fixture exclusion escapes source: {relative}") from exc
        if not source_target.is_file():
            raise FileNotFoundError(
                f"fixture exclusion is not a regular file: {relative}"
            )
        validated_exclusions.append(pure)
    shutil.copytree(source, destination)
    for pure in validated_exclusions:
        target = destination / Path(*pure.parts)
        target.unlink()
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
