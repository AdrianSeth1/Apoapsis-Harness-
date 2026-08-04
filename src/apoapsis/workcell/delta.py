"""What the workcell actually changed, computed outside its trust domain.

ADR 0077 permits the agent a real shell and real Git inside a sacrificial
clone. That is only safe because nothing Apoapsis relies on is read back out of
that clone: the agent may commit, amend, rebase, reset, or delete `.git`
entirely, and the admitted delta must be identical either way.

So the delta here is **not** `git diff`. It is a content comparison between two
trees the controller materialises itself — the approved base, and the frozen
workcell — walked and hashed by this process. An agent that rewrites its own
history changes nothing about what this reports.

The output is deliberately whole. `CandidateDelta` describes every path that
differs, in one object, because the admission step must be able to judge and
reject the change *as a unit*. That is the useful half of ADR 0071's atomicity,
kept without making the model express its work as a JSON payload.
"""

from __future__ import annotations

import hashlib
import os
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel

#: Never walked into on either side, because the contents are noise rather than
#: work: the clone's history is the agent's to rewrite and is not evidence, and
#: the caches are large, uninteresting, and regenerated on demand.
#:
#: Excluded **by name, whatever the entry is** -- see `_walk`. Treating these
#: as directory names only was a real defect: a managed Git worktree's `.git`
#: is a *file* (`gitdir: ...`), not a directory, so the directory filter never
#: saw it, `.git` was collected as an ordinary file, and it appeared in
#: `changed_files` on three of four live sandbox tasks. ADR 0063 requires the
#: reviewer-facing change surface to contain only model-authored work, and a
#: `.git` entry in it is the harness's own metadata presented as the model's.
#:
#: `.apoapsis` and `.sol` are deliberately **not** here. They are controller
#: state, and an agent writing into them is a boundary violation that must be
#: *seen and refused*, not silently dropped. Excluding them would make the
#: delta report "clean" for exactly the change admission exists to catch --
#: `classify_path` marks them `FORBIDDEN` and admission rejects the candidate.
EXCLUDED_METADATA_NAMES: frozenset[str] = frozenset(
    {".git", "__pycache__", ".pytest_cache", "node_modules"}
)

#: Former name, kept because it is imported elsewhere and because the rename is
#: the point: these were never only directories.
EXCLUDED_DIRECTORY_NAMES = EXCLUDED_METADATA_NAMES


class ChangeKind(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


class PathClass(StrEnum):
    """What kind of file this is, for policy purposes.

    Classification is by path shape alone. It is used to *report* and to apply
    per-class ceilings, never to infer that a change is safe: a production file
    and a test file are both just files, and the distinction exists so an owner
    can say "tests may not change" and have that mean something.
    """

    PRODUCTION = "production"
    TEST = "test"
    DEPENDENCY = "dependency"
    #: Build output, caches, lockfile-adjacent artifacts. Reported separately
    #: because they inflate a delta without being work.
    GENERATED = "generated"
    DOCUMENTATION = "documentation"
    #: Controller state, credentials, or the task artifact. Presence of one of
    #: these is disqualifying, not merely notable.
    FORBIDDEN = "forbidden"


#: Ordered most-specific-first; the first match wins.
_TEST_MARKERS: tuple[str, ...] = ("tests/", "test/", "spec/")
_TEST_FILENAME_MARKERS: tuple[str, ...] = ("test_", "_test.", ".test.", ".spec.")
_DEPENDENCY_FILENAMES: frozenset[str] = frozenset(
    {
        "requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py",
        "setup.cfg", "poetry.lock", "package.json", "package-lock.json",
        "yarn.lock", "pnpm-lock.yaml", "Pipfile", "Pipfile.lock", "go.mod",
        "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock",
    }
)
_GENERATED_MARKERS: tuple[str, ...] = (
    "dist/", "build/", ".venv/", "venv/", "coverage/", ".mypy_cache/",
    "site-packages/", ".tox/",
)
_GENERATED_SUFFIXES: tuple[str, ...] = (
    ".pyc", ".pyo", ".so", ".o", ".class", ".log", ".sqlite", ".db",
)
_DOCUMENTATION_SUFFIXES: tuple[str, ...] = (".md", ".rst", ".txt", ".adoc")
#: Anything under these, anywhere in the tree, is disqualifying.
_FORBIDDEN_MARKERS: tuple[str, ...] = (
    ".git/", ".apoapsis/", ".sol/", ".ssh/", ".aws/", "task/",
)
_FORBIDDEN_FILENAMES: frozenset[str] = frozenset(
    {".env", ".netrc", ".npmrc", "id_rsa", "id_ed25519", "credentials"}
)
_FORBIDDEN_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx")


def classify_path(relative_path: str) -> PathClass:
    """Classify one repository-relative POSIX path.

    Forbidden is checked first and unconditionally: a credential inside a
    directory called `tests/` is still a credential.
    """

    # `removeprefix`, not `lstrip("./")`: `lstrip` strips *characters*, so it
    # turns `.env` into `env` and `.apoapsis/state.json` into
    # `apoapsis/state.json`, quietly declassifying both. That is how a
    # credential gets admitted as ordinary production source.
    path = relative_path.replace("\\", "/").removeprefix("./")
    lowered = path.lower()
    name = path.rsplit("/", 1)[-1]

    if (
        any(marker in f"{lowered}/" for marker in _FORBIDDEN_MARKERS)
        or name in _FORBIDDEN_FILENAMES
        or lowered.startswith(".env")
        or "/.env" in lowered
        or lowered.endswith(_FORBIDDEN_SUFFIXES)
    ):
        return PathClass.FORBIDDEN
    if any(marker in f"{lowered}/" for marker in _GENERATED_MARKERS) or lowered.endswith(
        _GENERATED_SUFFIXES
    ):
        return PathClass.GENERATED
    if name in _DEPENDENCY_FILENAMES:
        return PathClass.DEPENDENCY
    if any(marker in f"{lowered}/" for marker in _TEST_MARKERS) or any(
        marker in name.lower() for marker in _TEST_FILENAME_MARKERS
    ):
        return PathClass.TEST
    if lowered.endswith(_DOCUMENTATION_SUFFIXES):
        return PathClass.DOCUMENTATION
    return PathClass.PRODUCTION


class DeltaEntry(StrictModel):
    path: str = Field(min_length=1)
    kind: ChangeKind
    path_class: PathClass
    #: `None` for an added file; `None` on the candidate side for a deletion.
    base_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    added_lines: int = Field(default=0, ge=0)
    removed_lines: int = Field(default=0, ge=0)
    #: Line counts are meaningless for binary content, and reporting zero would
    #: let a large binary slip past a changed-line ceiling.
    binary: bool = False
    size_bytes: int = Field(default=0, ge=0)

    @property
    def changed_lines(self) -> int:
        return self.added_lines + self.removed_lines


class CandidateDelta(StrictModel):
    """Everything that differs between the approved base and the workcell."""

    schema_version: str = "1.0"
    base_commit: str | None = None
    #: Content fingerprint of the frozen candidate tree, computed here.
    candidate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: list[DeltaEntry] = Field(default_factory=list)
    #: Paths skipped because they are not ordinary files -- symlinks, sockets,
    #: devices. Counted rather than ignored: a symlink pointing out of the
    #: workspace is exactly the sort of thing that must not travel silently.
    skipped_non_regular: list[str] = Field(default_factory=list)
    #: Repository metadata observed on either side and deliberately left out of
    #: `entries` -- `.git`, caches, `node_modules`. Recorded rather than merely
    #: dropped: "the candidate's `.git` differs from the base's" is true and
    #: worth having in the audit trail, it is simply not *model-authored work*
    #: and so must not reach a reviewer's change surface. An exclusion nobody
    #: can see is indistinguishable from a walk that never encountered it.
    excluded_metadata: list[str] = Field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return [item.path for item in self.entries]

    @property
    def changed_lines(self) -> int:
        return sum(item.changed_lines for item in self.entries)

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def by_class(self, path_class: PathClass) -> list[DeltaEntry]:
        return [item for item in self.entries if item.path_class == path_class]

    def by_kind(self, kind: ChangeKind) -> list[DeltaEntry]:
        return [item for item in self.entries if item.kind == kind]


def _walk(root: Path) -> tuple[dict[str, Path], list[str], list[str]]:
    """Map relative POSIX path -> absolute path, for ordinary files only.

    Returns the files, the non-regular entries skipped, and the repository
    metadata excluded. Exclusion is by *name*, at both the directory and the
    file level, because `.git` is a directory in a clone and a file in a
    managed worktree and both are metadata either way.
    """

    files: dict[str, Path] = {}
    skipped: list[str] = []
    excluded: list[str] = []
    if not root.is_dir():
        return files, skipped, excluded
    for current, directories, names in os.walk(root, followlinks=False):
        for item in sorted(directories):
            if item in EXCLUDED_METADATA_NAMES:
                excluded.append(
                    (Path(current) / item).relative_to(root).as_posix()
                )
        directories[:] = sorted(
            item for item in directories if item not in EXCLUDED_METADATA_NAMES
        )
        for name in sorted(names):
            absolute = Path(current) / name
            relative = absolute.relative_to(root).as_posix()
            if name in EXCLUDED_METADATA_NAMES:
                # The managed-worktree `.git` pointer file lands here.
                excluded.append(relative)
                continue
            if absolute.is_symlink() or not absolute.is_file():
                # Never followed. A symlink is not content, and following one
                # is how a delta acquires a file from outside the workspace.
                skipped.append(relative)
                continue
            files[relative] = absolute
    return files, skipped, excluded


def _digest(path: Path) -> tuple[str, bytes]:
    payload = path.read_bytes()
    return hashlib.sha256(payload).hexdigest(), payload


def _is_binary(payload: bytes) -> bool:
    return b"\0" in payload[:8192]


def _line_delta(base: bytes | None, candidate: bytes | None) -> tuple[int, int]:
    """Added and removed line counts.

    A deliberately cheap measure: it counts lines present on one side and not
    the other, not a minimal edit script. Admission uses it for size ceilings,
    where over-counting a reordered file is the safe direction to err.
    """

    def lines(payload: bytes | None) -> list[str]:
        if payload is None:
            return []
        return payload.decode("utf-8", errors="replace").splitlines()

    before = lines(base)
    after = lines(candidate)
    from collections import Counter

    before_counts = Counter(before)
    after_counts = Counter(after)
    added = sum((after_counts - before_counts).values())
    removed = sum((before_counts - after_counts).values())
    return added, removed


def tree_fingerprint(root: Path) -> str:
    """SHA-256 over sorted (path, content-digest) pairs.

    The same construction the controller uses at freeze time, so a delta and a
    freeze record can be compared without either trusting the workcell.
    """

    files, _skipped, _excluded = _walk(Path(root))
    digest = hashlib.sha256()
    for relative in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_digest(files[relative])[0].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def compute_delta(
    base_root: str | Path,
    candidate_root: str | Path,
    *,
    base_commit: str | None = None,
) -> CandidateDelta:
    """Compare two trees the controller owns. Never reads the workcell's Git.

    Both roots are walked and hashed by this process. Nothing the agent wrote
    into `.git` — or anywhere else excluded above — can influence the result.
    """

    base_path = Path(base_root)
    candidate_path = Path(candidate_root)
    base_files, base_skipped, base_excluded = _walk(base_path)
    candidate_files, candidate_skipped, candidate_excluded = _walk(candidate_path)

    entries: list[DeltaEntry] = []
    for relative in sorted(set(base_files) | set(candidate_files)):
        in_base = relative in base_files
        in_candidate = relative in candidate_files
        base_digest = base_payload = None
        candidate_digest = candidate_payload = None
        if in_base:
            base_digest, base_payload = _digest(base_files[relative])
        if in_candidate:
            candidate_digest, candidate_payload = _digest(candidate_files[relative])
        if in_base and in_candidate and base_digest == candidate_digest:
            continue

        kind = (
            ChangeKind.ADDED
            if not in_base
            else ChangeKind.DELETED
            if not in_candidate
            else ChangeKind.MODIFIED
        )
        binary = _is_binary(base_payload or b"") or _is_binary(candidate_payload or b"")
        added, removed = (0, 0) if binary else _line_delta(base_payload, candidate_payload)
        entries.append(
            DeltaEntry(
                path=relative,
                kind=kind,
                path_class=classify_path(relative),
                base_sha256=base_digest,
                candidate_sha256=candidate_digest,
                added_lines=added,
                removed_lines=removed,
                binary=binary,
                size_bytes=len(candidate_payload or b""),
            )
        )

    return CandidateDelta(
        base_commit=base_commit,
        candidate_fingerprint=tree_fingerprint(candidate_path),
        entries=entries,
        skipped_non_regular=sorted(set(base_skipped) | set(candidate_skipped)),
        excluded_metadata=sorted(set(base_excluded) | set(candidate_excluded)),
    )
