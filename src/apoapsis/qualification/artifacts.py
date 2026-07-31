"""A digest is evidence only when bytes on disk produce it.

The draft manifest (`cfe7df7`) accepted `sha256("slice7::crisis-atlas::seed")`
as a seed-tree hash. It is a perfectly well-formed SHA-256. It refers to
nothing. Every per-case identity in that manifest was built this way, and the
consequence was not cosmetic: `ready_for_inference()` would have become true
once eight unrelated placeholders were captured, while twenty-one of
twenty-four pairs still had no repository to clone.

The defect is not that the wrong string was used. It is that the type system
could not tell a measurement from a name, because both are 64 hex characters.

So resolution here is a *procedure over bytes*, and every step can fail:

1. the declared path exists;
2. it is a regular file -- not a directory, device, socket or dangling link;
3. it resolves inside the declared package root, after following symlinks;
4. the validator reads the bytes itself;
5. the recomputed digest equals the declared one;
6. the artifact's kind matches the use it was declared for.

Step 3 is separate from step 1 on purpose. A symlink inside the package
pointing at `/etc/passwd` exists and is a regular file, and a package that
could reach outside itself would let an evaluator-only oracle be smuggled in
from anywhere on the host.

`ResolvedArtifact` can only be constructed by `resolve_artifact`. There is no
path that produces one from a declared digest alone, which is what makes
"resolved" mean something a caller cannot fake by assembling the dataclass.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel

_SHA256 = r"^[0-9a-f]{64}$"

#: Chunked so a large seed tarball does not have to be held in memory.
_READ_CHUNK = 1 << 20


class ArtifactKind(StrEnum):
    """What an artifact is *for*.

    Declared and checked, because a task text and an evaluator-only oracle are
    both UTF-8 files and only one of them may be mounted into an agent
    workcell. A kind mismatch is a containment failure, not a typo.
    """

    SEED_TREE = "seed_tree"
    TASK_TEXT = "task_text"
    PLAN_CONTRACT = "plan_contract"
    ACCEPTANCE_CRITERIA = "acceptance_criteria"
    VERIFICATION_COMMANDS = "verification_commands"
    #: Never mounted into either arm's workcell.
    EVALUATOR_ONLY = "evaluator_only"
    EXPECTED_WITNESS = "expected_witness"
    REFERENCE_IMPLEMENTATION = "reference_implementation"
    INCOMPLETE_CANDIDATE = "incomplete_candidate"

    @property
    def evaluator_side_only(self) -> bool:
        """Kinds that must never reach a proposing agent."""

        return self in (
            ArtifactKind.EVALUATOR_ONLY,
            ArtifactKind.EXPECTED_WITNESS,
            ArtifactKind.REFERENCE_IMPLEMENTATION,
            ArtifactKind.INCOMPLETE_CANDIDATE,
        )


class ArtifactRejection(StrEnum):
    """Why a declared artifact did not resolve.

    Distinct values because the repairs differ: a missing file is authored, a
    digest mismatch is investigated, and an escape attempt is a containment
    finding.
    """

    MISSING = "missing"
    NOT_A_REGULAR_FILE = "not_a_regular_file"
    OUTSIDE_PACKAGE_ROOT = "outside_package_root"
    SYMLINK_ESCAPE = "symlink_escape"
    DIGEST_MISMATCH = "digest_mismatch"
    KIND_MISMATCH = "kind_mismatch"
    UNREADABLE = "unreadable"


class ArtifactResolutionError(RuntimeError):
    def __init__(self, rejection: ArtifactRejection, detail: str) -> None:
        self.rejection = rejection
        super().__init__(detail)


class DeclaredArtifact(StrictModel):
    """What a package *claims*. Never evidence on its own."""

    #: Relative to the package root. Absolute paths are refused: a package that
    #: names a host path is not portable and cannot be revalidated from a fresh
    #: clone, which is one of the eight required proofs.
    relative_path: str = Field(min_length=1)
    kind: ArtifactKind
    sha256: str = Field(pattern=_SHA256)
    purpose: str = Field(min_length=1)


class ResolvedArtifact(StrictModel):
    """Bytes that were read and hashed. Only `resolve_artifact` builds one."""

    relative_path: str
    kind: ArtifactKind
    sha256: str = Field(pattern=_SHA256)
    size_bytes: int = Field(ge=0)
    absolute_path: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_artifact(
    declared: DeclaredArtifact,
    *,
    package_root: Path,
    expected_kind: ArtifactKind | None = None,
) -> ResolvedArtifact:
    """Read the bytes and prove the declared digest, or raise saying why.

    Raises rather than returning a status, because every caller in this package
    treats a failure as fatal to registration. A `None` return would be one
    `if` away from a package registering with an unresolved artifact, which is
    precisely the shape of the defect being closed.
    """

    root = package_root.resolve()
    if Path(declared.relative_path).is_absolute() or ".." in Path(
        declared.relative_path
    ).parts:
        # Refused before touching the filesystem: `..` that happens to stay
        # inside the root is still a package that cannot be relocated, and a
        # traversal check performed after resolution has already followed the
        # link it was meant to catch.
        raise ArtifactResolutionError(
            ArtifactRejection.OUTSIDE_PACKAGE_ROOT,
            f"{declared.relative_path!r} must be a relative path inside the "
            "package root, with no parent traversal",
        )

    candidate = root / declared.relative_path
    if not candidate.exists():
        raise ArtifactResolutionError(
            ArtifactRejection.MISSING,
            f"{declared.relative_path!r} does not exist under {root}. A "
            "well-formed digest is not evidence that an artifact exists.",
        )

    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        # `exists()` and `is_file()` both pass for a symlink pointing anywhere
        # on the host. This is the check that stops an evaluator-only oracle
        # being reached from outside the package.
        raise ArtifactResolutionError(
            ArtifactRejection.SYMLINK_ESCAPE,
            f"{declared.relative_path!r} resolves to {resolved}, outside the "
            f"package root {root}",
        ) from exc

    if not resolved.is_file():
        raise ArtifactResolutionError(
            ArtifactRejection.NOT_A_REGULAR_FILE,
            f"{declared.relative_path!r} is not a regular file",
        )

    if expected_kind is not None and declared.kind is not expected_kind:
        raise ArtifactResolutionError(
            ArtifactRejection.KIND_MISMATCH,
            f"{declared.relative_path!r} is declared {declared.kind} but is "
            f"being used as {expected_kind}; a task text and an "
            "evaluator-only oracle are both UTF-8 files and only one of them "
            "may reach an agent workcell",
        )

    try:
        observed = sha256_file(resolved)
        size = resolved.stat().st_size
    except OSError as exc:
        raise ArtifactResolutionError(
            ArtifactRejection.UNREADABLE, f"{declared.relative_path!r}: {exc}"
        ) from exc

    if observed != declared.sha256:
        raise ArtifactResolutionError(
            ArtifactRejection.DIGEST_MISMATCH,
            f"{declared.relative_path!r} hashes to {observed}, not the "
            f"declared {declared.sha256}. The artifact changed after it was "
            "declared, or the declaration was never taken from these bytes.",
        )

    return ResolvedArtifact(
        relative_path=declared.relative_path,
        kind=declared.kind,
        sha256=observed,
        size_bytes=size,
        absolute_path=str(resolved),
    )


#: Label-shaped strings whose digests were used as identities in the draft.
#: Matched on the *plaintext*, then hashed, because a hash cannot be reversed:
#: the only way to recognise `sha256("slice7::crisis-atlas::seed")` is to
#: recompute it from the label that produced it.
_LABEL_TEMPLATES: tuple[str, ...] = (
    "slice7::{case_id}::tree",
    "slice7::{case_id}::task",
    "slice7::{case_id}::ac",
    "slice7::{case_id}::cmd",
    "slice7::{case_id}::seed",
    "slice7::{case_id}::plan",
    "PENDING_CAPTURE::{case_id}",
)


def label_derived_digests(case_id: str) -> frozenset[str]:
    """Every digest the draft's label scheme would produce for a case id."""

    return frozenset(
        hashlib.sha256(template.format(case_id=case_id).encode("utf-8")).hexdigest()
        for template in _LABEL_TEMPLATES
    )


def is_label_derived(digest: str, *, case_ids: tuple[str, ...]) -> bool:
    """Whether a digest is one the label scheme would have produced.

    A backstop, not the defence. The real defence is that nothing is resolved
    without reading bytes -- a label hash fails `resolve_artifact` at the
    missing-file step regardless of whether it is recognised here. This exists
    so a package carrying one is rejected with an accurate reason rather than a
    generic "file not found", because the two have very different repairs.
    """

    if not re.match(_SHA256, digest):
        return False
    return any(digest in label_derived_digests(case_id) for case_id in case_ids)


def assert_no_label_derived_digests(
    declared: tuple[DeclaredArtifact, ...], *, case_ids: tuple[str, ...]
) -> None:
    offenders = [
        item.relative_path
        for item in declared
        if is_label_derived(item.sha256, case_ids=case_ids)
    ]
    if offenders:
        raise ArtifactResolutionError(
            ArtifactRejection.DIGEST_MISMATCH,
            "these declarations carry digests derived from case labels rather "
            f"than from artifacts: {sorted(offenders)}. This is the defect "
            "found in draft manifest cfe7df7.",
        )
