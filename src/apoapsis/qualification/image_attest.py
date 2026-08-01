"""Image provenance must be read off the image, not asserted beside it.

`ImageProvenance` already refused a manifest whose `labels` block disagreed
with its `source_commit`. That check was written for a real defect -- a cached
`LABEL` layer retaining an earlier build's arguments -- and it is genuinely
useful. It is also defeatable by the author, and manifest v2 defeated it:

    source_commit                       = b30079a...
    labels["org.apoapsis.source-commit"] = b30079a...
    the image itself said                  0a6defb...

Both manifest fields came from one variable in the generating script, so they
agreed with each other while both disagreed with the artefact. A validator
comparing two fields of the same document can only catch a document that
contradicts itself; it cannot catch a document that is uniformly wrong.

So this module never takes the manifest's word for what an image says. It runs
`docker image inspect` and compares the *observed* labels against the declared
ones. The shape of the fix is the same one this project keeps arriving at: ask
the artefact, not the record of the artefact.

It also re-derives the build context. `git archive` at the declared commit,
restricted to the declared pathspec, digests to a value the build wrote into
the image as a label. Re-deriving it here means a manifest cannot bind an image
built from any other tree, even one whose commit label happens to be right.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel

_SHA256 = r"^[0-9a-f]{64}$"

#: Written by `docker/pilot-controller/Dockerfile`. Named here so a rename is a
#: code change rather than a silent mismatch.
SOURCE_COMMIT_LABEL = "org.apoapsis.source-commit"
SOURCE_TREE_LABEL = "org.apoapsis.source-tree"
BUILD_CONTEXT_LABEL = "org.apoapsis.build-context-sha256"

#: The paths the pilot controller build puts in its context. Kept beside the
#: attestation because re-deriving the digest requires the identical pathspec;
#: a different one produces a different tar and a false mismatch.
CONTROLLER_CONTEXT_PATHS: tuple[str, ...] = (
    "src",
    "pyproject.toml",
    "README.md",
    "LICENSE.txt",
)


class AttestationRejection(StrEnum):
    IMAGE_ABSENT = "image_absent"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    LABEL_MISSING = "label_missing"
    COMMIT_MISMATCH = "commit_mismatch"
    TREE_MISMATCH = "tree_mismatch"
    CONTEXT_MISMATCH = "context_mismatch"


class AttestationFinding(StrictModel):
    rejection: AttestationRejection
    detail: str = Field(min_length=1)


class ImageAttestation(StrictModel):
    """What the image itself reports, and whether it matches the declaration."""

    image_id: str = Field(min_length=1)
    observed_labels: dict[str, str] = Field(default_factory=dict)
    findings: tuple[AttestationFinding, ...] = ()

    @property
    def attested(self) -> bool:
        return not self.findings


def read_image_labels(
    image_id: str, *, runtime: str = "docker"
) -> dict[str, str] | None:
    """Ask the daemon. Returns `None` when the image or runtime is unavailable.

    `None` rather than `{}`: an image with no labels and an image that does not
    exist are different facts, and collapsing them would let a missing image
    read as an unlabelled one.
    """

    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [runtime, "image", "inspect", image_id, "--format", "{{json .Config.Labels}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    payload = json.loads(result.stdout.strip() or "null")
    return dict(payload) if isinstance(payload, dict) else {}


def rederive_build_context_digest(
    commit: str,
    *,
    repo: Path,
    paths: tuple[str, ...] | None = None,
) -> str | None:
    """Recompute the build-context digest from committed bytes.

    The build produced its context with `git archive <commit> -- <paths>` and
    labelled the tar's digest into the image. Recomputing it here is what makes
    "this image was built from this commit" checkable rather than asserted: a
    correct commit label on an image built from a different tree still fails.
    """

    # Resolved here rather than as a default argument: a default binds the
    # module constant at definition time, so a caller (or a test) overriding
    # the constant would be silently ignored.
    selected = CONTROLLER_CONTEXT_PATHS if paths is None else paths
    result = subprocess.run(  # noqa: S603
        ["git", "archive", "--format=tar", commit, "--", *selected],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if result.returncode != 0:
        return None
    return hashlib.sha256(result.stdout).hexdigest()


def attest_image(
    *,
    image_id: str,
    declared_source_commit: str,
    declared_source_tree: str | None = None,
    declared_context_sha256: str | None = None,
    repo: Path | None = None,
    runtime: str = "docker",
    rederive_context: bool = True,
) -> ImageAttestation:
    """Compare an image's own labels with what a manifest declares about it.

    Every comparison here has the artefact on one side. The manifest's `labels`
    block is deliberately ignored: it is a transcription, and a transcription
    is what was wrong in v2.
    """

    observed = read_image_labels(image_id, runtime=runtime)
    if observed is None:
        return ImageAttestation(
            image_id=image_id,
            findings=(
                AttestationFinding(
                    rejection=AttestationRejection.IMAGE_ABSENT,
                    detail=(
                        f"{image_id} is not present to this {runtime} daemon, so "
                        "nothing about its provenance can be observed. A "
                        "manifest that binds an absent image is binding a "
                        "record, not an artefact."
                    ),
                ),
            ),
        )

    findings: list[AttestationFinding] = []

    actual_commit = observed.get(SOURCE_COMMIT_LABEL)
    if actual_commit is None:
        findings.append(
            AttestationFinding(
                rejection=AttestationRejection.LABEL_MISSING,
                detail=f"the image carries no {SOURCE_COMMIT_LABEL} label",
            )
        )
    elif actual_commit != declared_source_commit:
        findings.append(
            AttestationFinding(
                rejection=AttestationRejection.COMMIT_MISMATCH,
                detail=(
                    f"the image says it was built from {actual_commit}; the "
                    f"manifest declares {declared_source_commit}. This is the "
                    "manifest-v2 defect: a hand-written labels block agreeing "
                    "with its own source_commit and with nothing else."
                ),
            )
        )

    if declared_source_tree is not None:
        actual_tree = observed.get(SOURCE_TREE_LABEL)
        if actual_tree != declared_source_tree:
            findings.append(
                AttestationFinding(
                    rejection=AttestationRejection.TREE_MISMATCH,
                    detail=(
                        f"the image says tree {actual_tree}; the manifest "
                        f"declares {declared_source_tree}"
                    ),
                )
            )

    actual_context = observed.get(BUILD_CONTEXT_LABEL)
    if declared_context_sha256 is not None and actual_context != declared_context_sha256:
        findings.append(
            AttestationFinding(
                rejection=AttestationRejection.CONTEXT_MISMATCH,
                detail=(
                    f"the image says context {actual_context}; the manifest "
                    f"declares {declared_context_sha256}"
                ),
            )
        )

    # The strongest check: rebuild the context digest from committed bytes and
    # compare it with what the image recorded. A right commit label on an image
    # built from a different tree fails here and nowhere else.
    if rederive_context and repo is not None and actual_commit and actual_context:
        rederived = rederive_build_context_digest(actual_commit, repo=repo)
        if rederived is not None and rederived != actual_context:
            findings.append(
                AttestationFinding(
                    rejection=AttestationRejection.CONTEXT_MISMATCH,
                    detail=(
                        f"re-deriving `git archive {actual_commit[:7]}` gives "
                        f"{rederived}, but the image recorded {actual_context}; "
                        "the image was not built from that commit's bytes"
                    ),
                )
            )

    return ImageAttestation(
        image_id=image_id, observed_labels=observed, findings=tuple(findings)
    )
