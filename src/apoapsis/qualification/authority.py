"""Authority is what a Git commit contains, not what the checkout happens to have.

Slice 7P.3 stopped at this gate. The lock named
`evaluator_framework_commit = 22cd8af` and `qualification/pilot.py` -- the
module defining `PilotLock`, `authorize_rehearsal` and the stop conditions --
does not exist at that commit. Every test still passed, because every test
did `from apoapsis.qualification.pilot import ...`, which reads the *working
tree*. The import succeeded, so nothing asked which commit the bytes came
from, and a lock that did not bind its own validator looked healthy for two
commits.

So this module never imports anything it is checking. It reads Git blobs:

    git cat-file -e <commit>:<path>     does it exist there
    git rev-parse <commit>:<path>       what blob is it
    git cat-file blob <blob>            what bytes are those

A working tree that contains newer, better or entirely different files cannot
make a missing object present, which is the property the previous check
lacked. `verify_authority` is therefore correct when run from a dirty
checkout, from a different branch, or from a checkout where the module has
since been deleted.
"""

from __future__ import annotations

import hashlib
import subprocess
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel

_SHA256 = r"^[0-9a-f]{64}$"
_GIT40 = r"^[0-9a-f]{40}$"

#: Modules without which a pilot authority cannot decide anything. Absence of
#: any one is a refusal, not a warning: a lock whose schema module is missing
#: cannot be validated, and a rehearsal whose scheduler is missing cannot run.
REQUIRED_AUTHORITY_MODULES: tuple[str, ...] = (
    "src/apoapsis/qualification/pilot.py",
    "src/apoapsis/qualification/authority.py",
    "src/apoapsis/qualification/rehearsal.py",
    "src/apoapsis/qualification/fake_pilot_provider.py",
)


class AuthorityRejection(StrEnum):
    COMMIT_MISSING = "commit_missing"
    MODULE_MISSING_AT_COMMIT = "module_missing_at_commit"
    BLOB_DIGEST_MISMATCH = "blob_digest_mismatch"
    NOT_A_GIT_CHECKOUT = "not_a_git_checkout"


class AuthorityError(RuntimeError):
    def __init__(self, rejection: AuthorityRejection, detail: str) -> None:
        self.rejection = rejection
        super().__init__(detail)


class BoundModule(StrictModel):
    """One module, bound by the digest of its bytes at a named commit."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256)


def _git(*argv: str, repo: Path, binary: bool = False):
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *argv],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=120,
    )
    return result


def commit_exists(commit: str, *, repo: Path) -> bool:
    return _git("cat-file", "-e", f"{commit}^{{commit}}", repo=repo).returncode == 0


def blob_at(commit: str, path: str, *, repo: Path) -> bytes | None:
    """The exact bytes of `path` at `commit`, or `None` if it is not there.

    Deliberately not `Path(path).read_bytes()`. The whole defect this closes is
    that reading the checkout answers a different question.
    """

    result = _git("cat-file", "blob", f"{commit}:{path}", repo=repo)
    if result.returncode != 0:
        return None
    return result.stdout


def digest_at(commit: str, path: str, *, repo: Path) -> str | None:
    body = blob_at(commit, path, repo=repo)
    return None if body is None else hashlib.sha256(body).hexdigest()


def bind_modules(
    commit: str, paths: tuple[str, ...], *, repo: Path
) -> tuple[BoundModule, ...]:
    """Digest each module *as committed*, for writing into a manifest."""

    if not (repo / ".git").exists():
        raise AuthorityError(
            AuthorityRejection.NOT_A_GIT_CHECKOUT,
            f"{repo} is not a Git checkout, so no authority can be established",
        )
    if not commit_exists(commit, repo=repo):
        raise AuthorityError(
            AuthorityRejection.COMMIT_MISSING, f"commit {commit} does not exist"
        )
    bound: list[BoundModule] = []
    for path in paths:
        digest = digest_at(commit, path, repo=repo)
        if digest is None:
            raise AuthorityError(
                AuthorityRejection.MODULE_MISSING_AT_COMMIT,
                f"{path!r} does not exist at {commit}. This is exactly the "
                "condition that made the 7P.2 lock invalid: the module was "
                "present in the working tree and absent from the commit the "
                "lock named.",
            )
        bound.append(BoundModule(path=path, sha256=digest))
    return tuple(bound)


class AuthorityFinding(StrictModel):
    path: str
    rejection: AuthorityRejection
    detail: str = Field(min_length=1)


class AuthorityVerification(StrictModel):
    """Whether a commit really contains the executables it is said to."""

    #: Abbreviations are accepted because callers legitimately use them and
    #: Git resolves them; what must be exact is the *blob* digest below, which
    #: is the thing a wrong value would let through.
    commit: str = Field(pattern=r"^[0-9a-f]{7,40}$")
    verified: tuple[BoundModule, ...] = ()
    findings: tuple[AuthorityFinding, ...] = ()

    @property
    def satisfied(self) -> bool:
        return not self.findings

    def require(self) -> None:
        if self.findings:
            detail = "; ".join(
                f"{item.path}: {item.rejection}" for item in self.findings
            )
            raise AuthorityError(
                self.findings[0].rejection,
                f"authority {self.commit} is not satisfied -- {detail}",
            )


def verify_authority(
    commit: str,
    declared: tuple[BoundModule, ...],
    *,
    repo: Path,
    required: tuple[str, ...] = REQUIRED_AUTHORITY_MODULES,
) -> AuthorityVerification:
    """Prove, from Git objects only, that `commit` holds these exact bytes.

    Three separate questions, because they have three different repairs:
    does the commit exist, is each declared module present *there*, and do its
    committed bytes hash to the declared value. A checkout containing newer
    files cannot satisfy any of them.
    """

    if not (repo / ".git").exists():
        return AuthorityVerification(
            commit=commit,
            findings=(
                AuthorityFinding(
                    path="(repository)",
                    rejection=AuthorityRejection.NOT_A_GIT_CHECKOUT,
                    detail=f"{repo} is not a Git checkout",
                ),
            ),
        )

    if not commit_exists(commit, repo=repo):
        return AuthorityVerification(
            commit=commit,
            findings=(
                AuthorityFinding(
                    path="(commit)",
                    rejection=AuthorityRejection.COMMIT_MISSING,
                    detail=f"commit {commit} does not exist in {repo}",
                ),
            ),
        )

    findings: list[AuthorityFinding] = []
    verified: list[BoundModule] = []
    declared_by_path = {item.path: item for item in declared}

    for path in required:
        if path not in declared_by_path:
            findings.append(
                AuthorityFinding(
                    path=path,
                    rejection=AuthorityRejection.MODULE_MISSING_AT_COMMIT,
                    detail=(
                        f"{path!r} is required for a pilot authority and the "
                        "manifest binds no digest for it"
                    ),
                )
            )

    for item in declared:
        observed = digest_at(commit, item.path, repo=repo)
        if observed is None:
            findings.append(
                AuthorityFinding(
                    path=item.path,
                    rejection=AuthorityRejection.MODULE_MISSING_AT_COMMIT,
                    detail=(
                        f"{item.path!r} does not exist at {commit}. A working "
                        "tree containing it does not make it present there."
                    ),
                )
            )
            continue
        if observed != item.sha256:
            findings.append(
                AuthorityFinding(
                    path=item.path,
                    rejection=AuthorityRejection.BLOB_DIGEST_MISMATCH,
                    detail=(
                        f"{item.path!r} at {commit} hashes to {observed}, not "
                        f"the declared {item.sha256}"
                    ),
                )
            )
            continue
        verified.append(item)

    return AuthorityVerification(
        commit=commit, verified=tuple(verified), findings=tuple(findings)
    )


def package_authority_modules_unchanged(
    original: str, current: str, paths: tuple[str, ...], *, repo: Path
) -> tuple[bool, tuple[str, ...]]:
    """Are the modules that produced the package evidence byte-identical?

    The eight real Crisis Atlas proofs may be reused only if the code that
    produced them has not moved. Comparing commits would be too strict --
    unrelated commits change constantly -- and comparing behaviour would be
    too weak. Comparing the blobs is exactly the question: same bytes, same
    evidence.

    Returns the verdict and the paths that differ, so a caller can say *which*
    module forced requalification rather than only that something did.
    """

    changed: list[str] = []
    for path in paths:
        before = digest_at(original, path, repo=repo)
        after = digest_at(current, path, repo=repo)
        if before is None or after is None or before != after:
            changed.append(path)
    return (not changed, tuple(changed))
