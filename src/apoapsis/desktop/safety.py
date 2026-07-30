from __future__ import annotations

import fnmatch
import re
from pathlib import Path, PurePosixPath

from apoapsis.desktop.errors import ImportSafetyError

# Never copied, regardless of confirmation -- mirrors
# `ContextCompilerConfig.cloud_excluded_paths` (src/apoapsis/config.py) and
# `PatchValidator._safe_path`'s root-directory exclusion
# (src/apoapsis/patches/validator.py), extended for the import workflow's
# own additional categories (dependency caches, virtualenvs, build output,
# platform metadata).
EXCLUDED_ROOT_DIRECTORY_NAMES = frozenset({".git", ".apoapsis", ".sol"})

DEPENDENCY_CACHE_BUILD_DIRECTORY_NAMES = frozenset(
    {
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".env",  # as a directory name; the file `.env` is also secret-like below
        "env",
        "site-packages",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".cache",
        "target",
        ".gradle",
        ".tox",
        "vendor",
        ".terraform",
    }
)

SECRET_LIKE_FILENAME_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "id_dsa",
    "id_ecdsa",
    "credentials",
    "credentials.*",
    "*.credentials",
    ".npmrc",
    ".netrc",
    "secrets.*",
    "*.secret",
)

PLATFORM_METADATA_FILENAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})

# Windows reserved device names (case-insensitive, applies to the name
# before any extension). Rejected in destination paths even when the host
# platform running Apoapsis is not Windows, since a project may later be
# opened on Windows.
_RESERVED_WINDOWS_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def is_reserved_windows_name(segment: str) -> bool:
    stem = segment.split(".", 1)[0].upper()
    return stem in _RESERVED_WINDOWS_STEMS


def hard_exclusion_reason(relative_posix_path: str) -> str | None:
    """Returns a human-readable exclusion reason if `relative_posix_path`
    (already relative to whatever source root is being imported) must
    never be copied, or `None` if it is an ordinary candidate file. This
    check is independent of, and in addition to, symlink rejection."""

    parts = PurePosixPath(relative_posix_path).parts
    if not parts:
        return "empty path"
    for part in parts[:-1] if len(parts) > 1 else parts:
        if part in EXCLUDED_ROOT_DIRECTORY_NAMES:
            return f"excluded directory: {part}"
        if part in DEPENDENCY_CACHE_BUILD_DIRECTORY_NAMES:
            return f"excluded dependency/build/virtual-environment directory: {part}"
    name = parts[-1]
    if name in EXCLUDED_ROOT_DIRECTORY_NAMES:
        return f"excluded directory: {name}"
    if name in DEPENDENCY_CACHE_BUILD_DIRECTORY_NAMES:
        return f"excluded dependency/build/virtual-environment directory: {name}"
    if name in PLATFORM_METADATA_FILENAMES:
        return f"excluded platform metadata file: {name}"
    for pattern in SECRET_LIKE_FILENAME_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return f"excluded secret-like file: {name}"
    return None


def is_safe_destination_relative_path(relative_path: str) -> bool:
    """Rejects traversal, absolute paths, drive letters, backslashes, null
    bytes, and any path segment that is a reserved Windows device name.
    Mirrors `PatchValidator._safe_path`'s checks (src/apoapsis/patches/
    validator.py) plus the reserved-device-name rule that code does not
    have, since patches never write literal `CON`/`NUL`-named files but an
    operator-chosen import destination plausibly could."""

    if not relative_path or "\x00" in relative_path or "\\" in relative_path:
        return False
    if relative_path.startswith("/"):
        return False
    if re.match(r"^[A-Za-z]:", relative_path):
        return False
    pure = PurePosixPath(relative_path)
    if not pure.parts or ".." in pure.parts:
        return False
    return not any(is_reserved_windows_name(part) for part in pure.parts)


def resolve_within_root(root: Path, relative_path: str) -> Path:
    """Resolves `relative_path` against `root` and raises `ImportSafetyError`
    if the result would fall outside `root` -- the destination-containment
    guard every import target must pass, independent of the syntactic
    checks in `is_safe_destination_relative_path`."""

    if not is_safe_destination_relative_path(relative_path):
        raise ImportSafetyError(f"unsafe destination path: {relative_path!r}")
    root_resolved = root.resolve()
    candidate = (root_resolved / Path(*PurePosixPath(relative_path).parts)).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ImportSafetyError(
            f"destination escapes project root: {relative_path!r}"
        ) from exc
    return candidate


def looks_binary(sample: bytes) -> bool:
    """Null-byte sniff, matching `ContextCompiler._read_text`'s heuristic
    (src/apoapsis/context/compiler.py) rather than inventing a new one."""

    return b"\0" in sample


__all__ = [
    "EXCLUDED_ROOT_DIRECTORY_NAMES",
    "DEPENDENCY_CACHE_BUILD_DIRECTORY_NAMES",
    "SECRET_LIKE_FILENAME_PATTERNS",
    "PLATFORM_METADATA_FILENAMES",
    "is_reserved_windows_name",
    "hard_exclusion_reason",
    "is_safe_destination_relative_path",
    "resolve_within_root",
    "looks_binary",
]
