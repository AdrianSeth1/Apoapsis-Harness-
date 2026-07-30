"""Admit the candidate delta, or reject it — as one unit, outside the workcell.

This is ADR 0077's Layer 3, and it is where the useful half of ADR 0071
survives. That ADR made the model express a slice as an atomic JSON change set
so the harness could judge it whole; the Crisis Atlas control showed the
envelope cost more capability than the atomicity was worth. Admission keeps the
atomicity and drops the envelope: the agent edits files normally, and the
*controller* assembles those edits into one candidate that is accepted or
refused together.

Three properties are load-bearing.

**All findings at once.** A rejection returns every violation it found, not the
first. The handoff is explicit about this, and the reason is economic: a repair
context that fixes one forbidden path only to be told about the next one burns
a model call per finding.

**Atomic, and the source tree is never the workspace.** Admission writes to a
controller-owned snapshot directory and never to the owner's repository. A
rejected candidate leaves nothing behind; an accepted one is materialised
somewhere new. There is no code path here that mutates the base.

**Reconstruction is from the base plus the delta, not from the workcell.** The
verifier tree is built by copying the approved base and applying the admitted
entries. Anything in the workcell that is not in the admitted delta — a stray
build artifact, a leftover socket, a file the policy refused — cannot reach
the verifier, because the verifier tree is never copied from the workcell at
all.
"""

from __future__ import annotations

import shutil
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from typing import TYPE_CHECKING

from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.delta import (
    CandidateDelta,
    ChangeKind,
    DeltaEntry,
    PathClass,
    tree_fingerprint,
)

if TYPE_CHECKING:
    from apoapsis.workcell.spike import CapabilitySpikeReport


class AdmissionFinding(StrEnum):
    FORBIDDEN_PATH = "forbidden_path"
    #: A path that escapes the workspace root once normalised.
    UNSAFE_PATH = "unsafe_path"
    NON_REGULAR_FILE = "non_regular_file"
    TOO_MANY_FILES = "too_many_files"
    TOO_MANY_CHANGED_LINES = "too_many_changed_lines"
    FILE_TOO_LARGE = "file_too_large"
    TEST_CHANGE_NOT_PERMITTED = "test_change_not_permitted"
    DEPENDENCY_CHANGE_NOT_PERMITTED = "dependency_change_not_permitted"
    GENERATED_ARTIFACT_PRESENT = "generated_artifact_present"
    DELETION_NOT_PERMITTED = "deletion_not_permitted"
    EMPTY_DELTA = "empty_delta"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"


class AdmissionViolation(StrictModel):
    finding: AdmissionFinding
    path: str | None = None
    detail: str = Field(min_length=1)


class AdmissionPolicy(StrictModel):
    """Owner-set ceilings, applied to the delta as a whole.

    Defaults are deliberately permissive about *what* may change and strict
    about *where*: the forbidden classes are not configurable, because a
    configuration that could permit `.git/` or a credential would make the
    boundary advisory.
    """

    max_files: int = Field(default=100, ge=1, le=2_000)
    max_changed_lines: int = Field(default=10_000, ge=1, le=500_000)
    max_file_bytes: int = Field(default=2_097_152, ge=1_024)
    allow_test_changes: bool = True
    allow_dependency_changes: bool = True
    allow_deletions: bool = True
    #: Generated artifacts are refused by default: they are not work, they
    #: inflate the delta, and admitting one makes the verifier's environment
    #: depend on something the manifest does not describe.
    allow_generated_artifacts: bool = False


class AdmissionDecision(StrictModel):
    schema_version: str = "1.0"
    admitted: bool = False
    violations: list[AdmissionViolation] = Field(default_factory=list)
    #: Counts by class and kind, for the evidence record.
    file_count: int = Field(default=0, ge=0)
    changed_lines: int = Field(default=0, ge=0)
    counts_by_class: dict[str, int] = Field(default_factory=dict)
    counts_by_kind: dict[str, int] = Field(default_factory=dict)
    candidate_fingerprint: str | None = None
    #: Where the accepted candidate was materialised. `None` on rejection --
    #: a refused candidate is not written anywhere.
    snapshot_path: str | None = None
    detail: str = Field(min_length=1)


class AdmissionRefused(RuntimeError):
    """The candidate delta may not be promoted."""


def _is_safe_relative(path: str) -> bool:
    normalised = path.replace("\\", "/")
    if normalised.startswith("/") or ":" in normalised.split("/")[0]:
        return False
    parts = [item for item in normalised.split("/") if item]
    depth = 0
    for part in parts:
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        elif part != ".":
            depth += 1
    return True


def evaluate_admission(
    delta: CandidateDelta,
    policy: AdmissionPolicy | None = None,
    *,
    expected_fingerprint: str | None = None,
) -> AdmissionDecision:
    """Judge the whole delta and return *every* violation it has.

    Never raises on a policy problem. The caller decides whether to route the
    findings back for repair or to stop; this function's job is to be complete
    and boring.
    """

    policy = policy or AdmissionPolicy()
    violations: list[AdmissionViolation] = []

    if expected_fingerprint is not None and (
        delta.candidate_fingerprint != expected_fingerprint
    ):
        # The tree moved between freeze and admission. Everything below would
        # describe a candidate that no longer exists.
        violations.append(
            AdmissionViolation(
                finding=AdmissionFinding.FINGERPRINT_MISMATCH,
                detail=(
                    f"the candidate fingerprint {delta.candidate_fingerprint[:12]} "
                    f"does not match the frozen {expected_fingerprint[:12]}; the "
                    "workcell changed after it was frozen"
                ),
            )
        )

    for path in delta.skipped_non_regular:
        violations.append(
            AdmissionViolation(
                finding=AdmissionFinding.NON_REGULAR_FILE,
                path=path,
                detail=(
                    f"{path} is a symlink or other non-regular file; it is never "
                    "followed and never admitted"
                ),
            )
        )

    for entry in delta.entries:
        if not _is_safe_relative(entry.path):
            violations.append(
                AdmissionViolation(
                    finding=AdmissionFinding.UNSAFE_PATH,
                    path=entry.path,
                    detail=f"{entry.path} escapes the workspace root",
                )
            )
        if entry.path_class == PathClass.FORBIDDEN:
            violations.append(
                AdmissionViolation(
                    finding=AdmissionFinding.FORBIDDEN_PATH,
                    path=entry.path,
                    detail=(
                        f"{entry.path} is controller state, a credential, or the "
                        "task artifact, and may never reach the owner's branch"
                    ),
                )
            )
        if entry.size_bytes > policy.max_file_bytes:
            violations.append(
                AdmissionViolation(
                    finding=AdmissionFinding.FILE_TOO_LARGE,
                    path=entry.path,
                    detail=(
                        f"{entry.path} is {entry.size_bytes:,} bytes, above the "
                        f"{policy.max_file_bytes:,}-byte ceiling"
                    ),
                )
            )
        if entry.path_class == PathClass.TEST and not policy.allow_test_changes:
            violations.append(
                AdmissionViolation(
                    finding=AdmissionFinding.TEST_CHANGE_NOT_PERMITTED,
                    path=entry.path,
                    detail=f"{entry.path} is a test and this repository protects tests",
                )
            )
        if (
            entry.path_class == PathClass.DEPENDENCY
            and not policy.allow_dependency_changes
        ):
            violations.append(
                AdmissionViolation(
                    finding=AdmissionFinding.DEPENDENCY_CHANGE_NOT_PERMITTED,
                    path=entry.path,
                    detail=(
                        f"{entry.path} is a dependency manifest and this repository "
                        "keeps dependencies owner-managed"
                    ),
                )
            )
        if (
            entry.path_class == PathClass.GENERATED
            and not policy.allow_generated_artifacts
        ):
            violations.append(
                AdmissionViolation(
                    finding=AdmissionFinding.GENERATED_ARTIFACT_PRESENT,
                    path=entry.path,
                    detail=(
                        f"{entry.path} is a generated artifact; admitting it would "
                        "make the verifier depend on state the manifest does not "
                        "describe"
                    ),
                )
            )
        if entry.kind == ChangeKind.DELETED and not policy.allow_deletions:
            violations.append(
                AdmissionViolation(
                    finding=AdmissionFinding.DELETION_NOT_PERMITTED,
                    path=entry.path,
                    detail=f"{entry.path} would be deleted and deletions are refused",
                )
            )

    if len(delta.entries) > policy.max_files:
        violations.append(
            AdmissionViolation(
                finding=AdmissionFinding.TOO_MANY_FILES,
                detail=(
                    f"the candidate changes {len(delta.entries)} files, above the "
                    f"{policy.max_files} ceiling"
                ),
            )
        )
    if delta.changed_lines > policy.max_changed_lines:
        violations.append(
            AdmissionViolation(
                finding=AdmissionFinding.TOO_MANY_CHANGED_LINES,
                detail=(
                    f"the candidate changes {delta.changed_lines:,} lines, above "
                    f"the {policy.max_changed_lines:,} ceiling"
                ),
            )
        )
    if delta.is_empty:
        # Not a policy breach so much as an empty result. Reported as a finding
        # so a session that produced nothing cannot be promoted as if it had.
        violations.append(
            AdmissionViolation(
                finding=AdmissionFinding.EMPTY_DELTA,
                detail="the candidate changes nothing, so there is nothing to admit",
            )
        )

    counts_by_class: dict[str, int] = {}
    counts_by_kind: dict[str, int] = {}
    for entry in delta.entries:
        counts_by_class[entry.path_class.value] = (
            counts_by_class.get(entry.path_class.value, 0) + 1
        )
        counts_by_kind[entry.kind.value] = counts_by_kind.get(entry.kind.value, 0) + 1

    if violations:
        return AdmissionDecision(
            admitted=False,
            violations=violations,
            file_count=len(delta.entries),
            changed_lines=delta.changed_lines,
            counts_by_class=counts_by_class,
            counts_by_kind=counts_by_kind,
            candidate_fingerprint=delta.candidate_fingerprint,
            detail=(
                f"the candidate is refused with {len(violations)} violation(s): "
                + "; ".join(item.detail for item in violations[:8])
                + ("; ..." if len(violations) > 8 else "")
            ),
        )
    return AdmissionDecision(
        admitted=True,
        file_count=len(delta.entries),
        changed_lines=delta.changed_lines,
        counts_by_class=counts_by_class,
        counts_by_kind=counts_by_kind,
        candidate_fingerprint=delta.candidate_fingerprint,
        detail=(
            f"the candidate is admitted: {len(delta.entries)} file(s), "
            f"{delta.changed_lines:,} changed line(s), no policy violations"
        ),
    )


def reconstruct_candidate(
    base_root: str | Path,
    candidate_root: str | Path,
    delta: CandidateDelta,
    destination: str | Path,
) -> str:
    """Build a clean verifier tree from the base plus the admitted delta.

    Deliberately *not* a copy of the workcell. Only paths named in `delta` are
    taken from the candidate; everything else comes from the approved base. A
    stray artifact the policy refused, or anything the agent left lying around,
    cannot reach the verifier because it is never consulted.

    Returns the reconstructed tree's fingerprint, which the caller should
    compare against `delta.candidate_fingerprint`.
    """

    base_path = Path(base_root)
    candidate_path = Path(candidate_root)
    target = Path(destination)
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        base_path,
        target,
        symlinks=False,
        ignore=shutil.ignore_patterns(
            ".git", ".apoapsis", ".sol", "__pycache__", ".pytest_cache", "node_modules"
        ),
    )

    for entry in delta.entries:
        destination_file = target / entry.path
        if entry.kind == ChangeKind.DELETED:
            destination_file.unlink(missing_ok=True)
            continue
        source_file = candidate_path / entry.path
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, destination_file)
    return tree_fingerprint(target)


def admit_candidate(
    base_root: str | Path,
    candidate_root: str | Path,
    delta: CandidateDelta,
    *,
    snapshot_root: str | Path,
    policy: AdmissionPolicy | None = None,
    expected_fingerprint: str | None = None,
    slice2_spike: "CapabilitySpikeReport | None" = None,
) -> AdmissionDecision:
    """Evaluate, and materialise only on acceptance.

    The atomicity that matters: a refused candidate is never written anywhere,
    so there is no partially promoted state to clean up and no snapshot for a
    later step to mistake for an approved one.

    `slice2_spike` is the Slice 2 gate. Passing `None` is permitted only for
    the deterministic tests, which exercise admission's own logic; a live
    promotion supplies the spike report, and `require_slice3_unblocked` raises
    unless it says `CAPABILITY_PRESERVED` with containment, provider-protocol
    conformance, agent execution-profile identity, and capability readiness all
    holding. Admission is the first thing Slice 3 does, so this is where that
    gate has to bite.
    """

    if slice2_spike is not None:
        from apoapsis.workcell.gate import require_slice3_unblocked

        require_slice3_unblocked(slice2_spike)

    decision = evaluate_admission(
        delta, policy, expected_fingerprint=expected_fingerprint
    )
    if not decision.admitted:
        return decision

    snapshot = Path(snapshot_root)
    fingerprint = reconstruct_candidate(base_root, candidate_root, delta, snapshot)
    if fingerprint != delta.candidate_fingerprint:
        # The reconstruction does not match what was measured. Something in the
        # base differs from the candidate outside the delta, which means the
        # delta is not a complete description of the change. Refuse, and remove
        # the tree rather than leave a wrong candidate on disk.
        shutil.rmtree(snapshot, ignore_errors=True)
        return AdmissionDecision(
            admitted=False,
            violations=[
                AdmissionViolation(
                    finding=AdmissionFinding.FINGERPRINT_MISMATCH,
                    detail=(
                        f"the reconstructed tree fingerprints to {fingerprint[:12]} "
                        f"but the candidate measured {delta.candidate_fingerprint[:12]}; "
                        "the delta does not fully describe the change"
                    ),
                )
            ],
            file_count=decision.file_count,
            changed_lines=decision.changed_lines,
            counts_by_class=decision.counts_by_class,
            counts_by_kind=decision.counts_by_kind,
            candidate_fingerprint=delta.candidate_fingerprint,
            detail=(
                "the candidate was refused after reconstruction disagreed with the "
                "measured delta"
            ),
        )
    return decision.model_copy(update={"snapshot_path": str(snapshot)})


def require_admitted(decision: AdmissionDecision) -> AdmissionDecision:
    """The raising form, for callers that must not proceed on a refusal."""

    if not decision.admitted:
        raise AdmissionRefused(decision.detail)
    return decision


def repair_packet(decision: AdmissionDecision) -> str:
    """A compact, complete list of what to fix, for a repair context.

    Compact because a repair context pays for every token; complete because
    the handoff requires a rejection to return *all* violations at once.
    """

    if decision.admitted:
        return "The candidate was admitted; there is nothing to repair."
    lines = [
        f"The candidate was refused with {len(decision.violations)} violation(s). "
        "Fix all of them before requesting evaluation again.",
        "",
    ]
    for violation in decision.violations:
        location = f" [{violation.path}]" if violation.path else ""
        lines.append(f"- {violation.finding.value}{location}: {violation.detail}")
    return "\n".join(lines)


def entries_for_class(delta: CandidateDelta, path_class: PathClass) -> list[DeltaEntry]:
    return delta.by_class(path_class)
