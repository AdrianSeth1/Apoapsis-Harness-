from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import Field

from apoapsis.repository.git import GitRepository
from apoapsis.specification.schema import StrictModel

_FORBIDDEN_TOP_LEVEL = {".git", ".apoapsis", ".sol"}


def normalize_relative_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_safe_relative_path(path: str) -> bool:
    """Deterministic path-safety check shared with `agent/inspection.py` and
    `patches/validator.py`'s equivalent (independently implemented there,
    same policy): no null bytes, no absolute/drive paths, no `..` escape,
    and never inside the harness's own `.git`/`.apoapsis`/`.sol` trees."""

    if (
        not path
        or "\0" in path
        or path.startswith("/")
        or re.match(r"^[A-Za-z]:", path)
    ):
        return False
    parts = PurePosixPath(path).parts
    return bool(parts) and ".." not in parts and parts[0] not in _FORBIDDEN_TOP_LEVEL


def list_permitted_untracked_paths(repository: GitRepository) -> list[str]:
    """Sorted, safe, non-ignored untracked file paths in `repository`.

    Uses `git ls-files --others --exclude-standard` (gitignore-aware) plus
    the same forbidden-top-level-directory policy as tracked-file access, so
    the harness's own bookkeeping directories can never enter a fingerprint
    or be presented as evidence, regardless of whether the target project's
    own `.gitignore` happens to exclude them too.
    """

    raw = repository.run(
        ["ls-files", "-z", "--others", "--exclude-standard"]
    ).stdout
    paths = (normalize_relative_path(item) for item in raw.split("\0") if item)
    return sorted(path for path in paths if is_safe_relative_path(path))


class UntrackedEntryKind(StrEnum):
    FILE = "file"
    SYMLINK = "symlink"


class UntrackedFileFingerprint(StrictModel):
    path: str
    kind: UntrackedEntryKind
    mode: str
    content_sha256: str


class WorktreeFingerprint(StrictModel):
    head_commit: str
    tracked_diff_sha256: str
    untracked_files: list[UntrackedFileFingerprint] = Field(default_factory=list)
    digest: str


def _tracked_diff_sha256(repository: GitRepository) -> str:
    # `--unified=0` keeps the fingerprint sensitive only to changed lines
    # themselves, not to how much surrounding context git happens to print;
    # evidence-facing diffs (inspection/repair prompts) intentionally use a
    # larger context elsewhere for human/model readability and are
    # unaffected by this choice.
    content = repository.run(
        ["diff", "--no-ext-diff", "--unified=0", "HEAD"]
    ).stdout
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _untracked_fingerprint(
    repository: GitRepository, relative_path: str
) -> UntrackedFileFingerprint | None:
    full_path = repository.root / Path(*PurePosixPath(relative_path).parts)
    try:
        entry_stat = full_path.lstat()
    except OSError:
        # Vanished between listing and stat (e.g. a concurrent edit); a
        # fingerprint computed moments later will simply omit or re-add it.
        return None
    if stat.S_ISLNK(entry_stat.st_mode):
        # Deterministic handling, never rejection: hash the literal link
        # target text (never dereferenced/followed), so a symlink's mere
        # presence or retargeting always perturbs the fingerprint even
        # though its content is never read as if it were safe text.
        target = os.readlink(full_path)
        content_hash = hashlib.sha256(
            target.encode("utf-8", errors="surrogateescape")
        ).hexdigest()
        return UntrackedFileFingerprint(
            path=relative_path,
            kind=UntrackedEntryKind.SYMLINK,
            mode="120000",
            content_sha256=content_hash,
        )
    if stat.S_ISREG(entry_stat.st_mode):
        raw = full_path.read_bytes()
        mode = "100755" if (entry_stat.st_mode & 0o111) else "100644"
        return UntrackedFileFingerprint(
            path=relative_path,
            kind=UntrackedEntryKind.FILE,
            mode=mode,
            content_sha256=hashlib.sha256(raw).hexdigest(),
        )
    # Directories, sockets, FIFOs, etc. are not fingerprintable file content.
    return None


def compute_worktree_fingerprint(worktree: str | Path) -> WorktreeFingerprint:
    """The single, shared, deterministic notion of "current code" used to
    scope verification caching, command results, and acceptance proof
    (ADR 0017).

    Captures: HEAD identity, the canonical (zero-context) tracked diff
    against HEAD, and every permitted untracked path with an exact content
    hash and type/mode -- so a brand-new untracked file (a common byproduct
    of an applied patch that was never `git add`ed) changes the fingerprint
    exactly as a tracked edit would. Untracked symlinks and binary files are
    hashed deterministically (never dereferenced, never decoded as text) so
    their presence can never be a blind spot, even though neither is ever
    rendered as content elsewhere (`RepositoryInspector`).
    """

    repository = GitRepository(worktree)
    head_commit = repository.run(["rev-parse", "HEAD"]).stdout.strip()
    tracked_diff_sha256 = _tracked_diff_sha256(repository)
    untracked_files: list[UntrackedFileFingerprint] = []
    for relative_path in list_permitted_untracked_paths(repository):
        entry = _untracked_fingerprint(repository, relative_path)
        if entry is not None:
            untracked_files.append(entry)
    untracked_files.sort(key=lambda item: item.path)
    canonical = json.dumps(
        {
            "head_commit": head_commit,
            "tracked_diff_sha256": tracked_diff_sha256,
            "untracked_files": [
                item.model_dump(mode="json") for item in untracked_files
            ],
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return WorktreeFingerprint(
        head_commit=head_commit,
        tracked_diff_sha256=tracked_diff_sha256,
        untracked_files=untracked_files,
        digest=digest,
    )
