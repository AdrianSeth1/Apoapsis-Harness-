"""A case package is registerable only when eight separate proofs passed.

Slice 7P.1a made a *digest* mean something: bytes on disk produce it, or it
does not resolve. This module makes a *package* mean something, which is a
different claim and needs different evidence. A package can consist entirely of
artifacts that resolve and still be worthless -- if the seed does not clone to
the commit it names, if the behaviour the task asks for already exists in the
seed, or if the candidate that historically slipped through still slips
through.

So validation is eight named proofs, and each one carries its own state:

* ``PASSED`` -- the proof ran and its property holds;
* ``FAILED`` -- the proof ran and its property does not hold;
* ``UNRUN`` -- the proof did not execute (no seed on this host, no probe);
* ``INCONCLUSIVE`` -- the proof executed and could not decide.

``UNRUN`` and ``INCONCLUSIVE`` both block registration. That is deliberate and
is the whole reason four states exist rather than a boolean. A boolean forces
"did not run" to be reported as either a pass, which is the false-readiness
defect again, or a failure, which is a lie about a package that may be fine on
a host that has the seed. Neither is true, so neither is offered.

Proofs are keyed by ``ProofId`` and every id must appear exactly once. Two
copies of the fresh-clone proof cannot stand in for the missing containment
proof, however many green results the total contains.

The probe is injected. Cloning a repository and running a suite are real
operations with real failure modes, and a validator that performed them
directly could only be tested by performing them, which would make the focused
suite depend on a seed existing on the host. ``PackageProbe`` is the seam:
``GitCommandProbe`` does the real work, and a deterministic fake covers every
branch without git, network, Docker or a model.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import Field

from apoapsis.qualification.artifacts import (
    ArtifactKind,
    ArtifactResolutionError,
    DeclaredArtifact,
    ResolvedArtifact,
    resolve_artifact,
)
from apoapsis.specification.schema import StrictModel

#: Every package declares exactly this many repetition identities. Fewer is an
#: under-powered pilot; more silently changes what a "matched pair" means.
REQUIRED_REPETITIONS = 3

_PACKAGE_DECLARATION = "package.json"


class ProofId(StrEnum):
    """The eight proofs. One may never substitute for another."""

    FRESH_CLONE_REPRODUCES_SEED = "fresh_clone_reproduces_seed"
    REQUESTED_BEHAVIOUR_ABSENT_FROM_SEED = "requested_behaviour_absent_from_seed"
    INHERITED_TEST_STATE_RECORDED = "inherited_test_state_recorded"
    REFERENCE_SATISFIES_EVERY_CRITERION = "reference_satisfies_every_criterion"
    REMOVAL_FAILS_ITS_MAPPED_CRITERION = "removal_fails_its_mapped_criterion"
    INCOMPLETE_CANDIDATE_CANNOT_COMPLETE = "incomplete_candidate_cannot_complete"
    WITNESSES_BOUND_TO_ADMITTED_SNAPSHOT = "witnesses_bound_to_admitted_snapshot"
    SECOND_FRESH_CLONE_IS_IDENTICAL = "second_fresh_clone_is_identical"


class ProofState(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNRUN = "unrun"
    INCONCLUSIVE = "inconclusive"

    @property
    def blocks_registration(self) -> bool:
        return self is not ProofState.PASSED


class EvidenceKind(StrEnum):
    """What a validation run's results are evidence *of*.

    7P.1b reported "eight proofs passed" and "registerable" from a run against
    an injected fake probe. Those results were real, and they proved something
    real -- that the orchestration branches, and that a failure in any one of
    them is reported as a failure. They did not prove anything about the
    package, because no clone was made, no suite was run, and no witness was
    emitted. The claim substituted orchestration coverage for qualification
    evidence, which is the same class of error as substituting a label hash for
    a measurement.

    So the distinction is now a value the probe must declare, `registerable`
    consults, and no caller can omit.
    """

    #: An injected probe. Proves the validator's branches, nothing about bytes.
    ORCHESTRATION_ONLY = "orchestration_only"
    #: Real clones, real commands, real witnesses, real filesystem.
    REAL_QUALIFICATION = "real_qualification"


class PackageStatus(StrEnum):
    NOT_YET_REGISTERABLE = "not_yet_registerable"
    REGISTERABLE = "registerable"


class ProofResult(StrictModel):
    proof_id: ProofId
    state: ProofState
    detail: str = Field(min_length=1)
    #: Identities the proof observed. Compared across the two fresh-clone
    #: validations, which is how proof 8 detects nondeterminism it would
    #: otherwise have to infer from a bare pass/fail.
    evidence: dict[str, str] = Field(default_factory=dict)


class CasePackageError(RuntimeError):
    pass


class GitObject(StrictModel):
    """An object id together with the type it is claimed to be.

    Both fields are required because a commit id and a tree id are both forty
    hex characters, and an unquoted ``HEAD^{tree}`` invocation on PowerShell
    emits the *parent commit* before failing. That value looks exactly like a
    tree. Recording the declared type is what turns "looks right" into
    something ``git cat-file -t`` can contradict.
    """

    object_id: str = Field(pattern=r"^[0-9a-f]{40}$")
    object_type: str = Field(min_length=1)


class SeedObservation(StrictModel):
    """What a fresh clone actually produced."""

    commit: GitObject
    tree: GitObject
    tracked_files: tuple[str, ...]
    working_tree_clean: bool


class CommandOutcome(StrictModel):
    name: str = Field(min_length=1)
    exit_code: int
    #: Files whose lines the hashed coverage artifact reported as executed.
    covered_paths: tuple[str, ...] = ()
    #: The admitted snapshot fingerprint the witness was emitted against.
    worktree_fingerprint: str | None = None


class CheckpointObservation(StrictModel):
    """The outcome of running the real checkpoint over a candidate tree."""

    outcome: str = Field(min_length=1)
    satisfied_criteria: tuple[str, ...] = ()
    readiness_blocks: tuple[str, ...] = ()
    repair_packet: str = ""
    commands: tuple[CommandOutcome, ...] = ()
    emitter_failed: bool = False


class PackageProbe(Protocol):
    """The operations validation needs but must not perform itself.

    `evidence_kind` is required rather than defaulted. A probe that forgot to
    declare it would otherwise default to something, and whichever value were
    chosen would be wrong half the time -- silently, in the direction of
    whichever caller forgot.
    """

    evidence_kind: EvidenceKind

    def clone_seed(self, *, destination: Path) -> SeedObservation: ...

    def read_seed_paths(self, *, destination: Path) -> tuple[str, ...]: ...

    def search_seed_symbols(
        self, *, destination: Path, symbols: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Return the subset of ``symbols`` actually found in the clone."""

    def run_inherited_suite(self, *, destination: Path) -> CommandOutcome: ...

    def run_checkpoint(
        self, *, destination: Path, candidate: str, omit_path: str | None = None
    ) -> CheckpointObservation:
        """Run the real checkpoint loop over a named candidate.

        ``candidate`` is ``"reference"`` or ``"incomplete"``. ``omit_path``
        deletes one file from the reference before the run, which is how the
        negative-control proof asks "does removing this actually fail the
        criterion it is mapped to" instead of assuming it would.
        """


class CasePackageDeclaration(StrictModel):
    """What ``package.json`` claims. Resolved before any of it is believed."""

    schema_version: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    package_id: str = Field(min_length=1)
    slice: str = Field(min_length=1)
    artifacts: tuple[DeclaredArtifact, ...] = Field(min_length=1)
    components: dict[str, str]


#: The twelve required components, by declaration key. Every one is mandatory:
#: an optional component is a component that silently defaults, and a package
#: whose oracle defaulted to empty would register while proving nothing.
REQUIRED_COMPONENT_KEYS: tuple[str, ...] = (
    "1_seed",
    "2_task_text",
    "3_plan_contract",
    "4_acceptance_criteria",
    "5_verification_commands",
    "6_evaluator_only_oracle",
    "7_expected_witnesses",
    "8_repetitions",
    "9_budgets",
    "10_capability_rationale",
    "11_reference_candidate",
    "12_incomplete_candidate",
)

#: Kinds a proposing agent may see. Derived from `evaluator_side_only` rather
#: than from a second hand-maintained list, and never from a path prefix: a
#: naming convention is a comment that the filesystem does not enforce.
MOUNTABLE_KINDS: frozenset[ArtifactKind] = frozenset(
    kind for kind in ArtifactKind if not kind.evaluator_side_only
)


class ResolvedCasePackage(StrictModel):
    """A package whose every declared artifact was read and hashed."""

    package_id: str
    case_id: str
    package_root: str
    artifacts: tuple[ResolvedArtifact, ...]
    #: Digest over the resolved artifact identities. A package edited after
    #: validation produces a different one, which is how `assert_unchanged`
    #: catches a mutation that would otherwise be invisible to a caller
    #: holding a stale result.
    package_digest: str

    @property
    def mountable(self) -> tuple[ResolvedArtifact, ...]:
        return tuple(item for item in self.artifacts if item.kind in MOUNTABLE_KINDS)

    @property
    def evaluator_only(self) -> tuple[ResolvedArtifact, ...]:
        return tuple(
            item for item in self.artifacts if item.kind.evaluator_side_only
        )

    def assert_arm_visible_set_is_contained(
        self, exposed_paths: tuple[str, ...]
    ) -> None:
        """Refuse a mount/task-information set that carries evaluator material.

        Compared by resolved absolute path, not by prefix or filename, so a
        copy of the oracle placed elsewhere and a symlink to it are both
        caught. 7P.2 calls this before building either arm's workcell.
        """

        forbidden = {item.absolute_path for item in self.evaluator_only}
        offenders = sorted(
            path
            for path in exposed_paths
            if str(Path(path).resolve()) in forbidden
        )
        if offenders:
            raise CasePackageError(
                "these evaluator-only artifacts would reach a proposing "
                f"agent: {offenders}. Evaluator material may not enter an "
                "arm's prompt, worktree, mounts, tool context, state capsule, "
                "repair packet or transcript."
            )

    def assert_unchanged(self) -> None:
        root = Path(self.package_root)
        for item in self.artifacts:
            path = root / item.relative_path
            if not path.is_file():
                raise CasePackageError(
                    f"{item.relative_path!r} disappeared after validation"
                )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != item.sha256:
                raise CasePackageError(
                    f"{item.relative_path!r} changed after validation: "
                    f"{digest} != {item.sha256}. The validation result "
                    "describes bytes that are no longer on disk."
                )


def _read_json(package: ResolvedCasePackage, relative: str):
    return json.loads(
        (Path(package.package_root) / relative).read_text(encoding="utf-8")
    )


def resolve_case_package(package_root: Path) -> ResolvedCasePackage:
    """Read ``package.json``, then prove every artifact it declares.

    Raises on the first failure. A partially resolved package has no use: the
    caller's next question is always "may I register this", and the answer is
    already no.
    """

    root = package_root.resolve()
    declaration_path = root / _PACKAGE_DECLARATION
    if not declaration_path.is_file():
        raise CasePackageError(f"no {_PACKAGE_DECLARATION} under {root}")

    declaration = CasePackageDeclaration.model_validate_json(
        declaration_path.read_text(encoding="utf-8")
    )

    missing = [
        key for key in REQUIRED_COMPONENT_KEYS if key not in declaration.components
    ]
    if missing:
        raise CasePackageError(
            f"package {declaration.package_id} omits required components "
            f"{missing}. All twelve are mandatory and none may default."
        )

    seen: set[str] = set()
    resolved: list[ResolvedArtifact] = []
    for declared in declaration.artifacts:
        if declared.relative_path in seen:
            raise CasePackageError(
                f"{declared.relative_path!r} is declared twice; two "
                "declarations of one file can disagree about its kind"
            )
        seen.add(declared.relative_path)
        try:
            resolved.append(
                resolve_artifact(
                    declared, package_root=root, expected_kind=declared.kind
                )
            )
        except ArtifactResolutionError as exc:
            raise CasePackageError(
                f"{declared.relative_path!r} did not resolve "
                f"({exc.rejection}): {exc}"
            ) from exc

    fingerprint = hashlib.sha256()
    for item in sorted(resolved, key=lambda value: value.relative_path):
        fingerprint.update(f"{item.relative_path}\0{item.sha256}\0".encode("utf-8"))

    return ResolvedCasePackage(
        package_id=declaration.package_id,
        case_id=declaration.case_id,
        package_root=str(root),
        artifacts=tuple(resolved),
        package_digest=fingerprint.hexdigest(),
    )


def validate_repetitions(package: ResolvedCasePackage) -> None:
    """Exactly three repetitions, distinct, varying only sampling identity."""

    payload = _read_json(package, "repetitions.json")
    repetitions = payload["repetitions"]
    if len(repetitions) != REQUIRED_REPETITIONS:
        raise CasePackageError(
            f"{len(repetitions)} repetitions declared; exactly "
            f"{REQUIRED_REPETITIONS} are required"
        )
    identities = [item["repetition_id"] for item in repetitions]
    if len(set(identities)) != len(identities):
        raise CasePackageError(
            f"duplicate repetition identities {sorted(identities)}; three "
            "runs that share an identity are one run recorded three times"
        )
    if "invariant_across_repetitions" not in payload:
        raise CasePackageError(
            "repetitions.json declares no invariant block, so a repetition "
            "could vary the seed tree, task, contract, criteria, commands, "
            "difficulty or budgets without any of it being visible"
        )
    controlled = {"sampling_seed", "session_identity", "repetition_id"}
    for item in repetitions:
        extra = set(item) - controlled
        if extra:
            raise CasePackageError(
                f"repetition {item.get('repetition_id')!r} carries "
                f"{sorted(extra)}, which is not sampling or session identity"
            )


def validate_criteria_mapping(package: ResolvedCasePackage) -> None:
    """Every criterion names a witness kind and what that witness must reach.

    A criterion whose success condition is a command exiting zero is refused
    outright. Four configured commands exited zero over the historical Slice 2
    candidate, which created neither declared service.
    """

    criteria = _read_json(package, "acceptance-criteria.json")["criteria"]
    contract = _read_json(package, "plan-contract.json")
    mapped = {item["criterion_id"] for item in criteria}
    for obligation in contract["obligations"]:
        unmapped = set(obligation["criteria"]) - mapped
        if unmapped:
            raise CasePackageError(
                f"obligation {obligation['obligation_id']!r} names criteria "
                f"{sorted(unmapped)} that no acceptance criterion defines"
            )
    for item in criteria:
        for field in (
            "criterion_id",
            "obligation",
            "required_witness_kind",
            "proves",
            "admitted_snapshot_binding",
            "success_condition",
            "insufficient_evidence",
        ):
            if not item.get(field):
                raise CasePackageError(
                    f"criterion {item.get('criterion_id')!r} omits {field!r}"
                )
        if not item["insufficient_evidence"]:
            raise CasePackageError(
                f"criterion {item['criterion_id']!r} lists nothing as "
                "insufficient, so nothing distinguishes proof from a green run"
            )


class CasePackageValidation(StrictModel):
    """Eight proofs and the package they were run against."""

    package_id: str
    package_digest: str
    evidence_kind: EvidenceKind
    results: tuple[ProofResult, ...]
    #: Fields excluded from the proof-8 comparison because they vary between
    #: two correct runs. Declared per run and reported, never inferred: a
    #: comparison that silently dropped whatever differed would pass by
    #: construction.
    volatile_evidence_fields: tuple[str, ...] = ()

    def result(self, proof_id: ProofId) -> ProofResult:
        for item in self.results:
            if item.proof_id is proof_id:
                return item
        raise CasePackageError(f"{proof_id} was never reported")

    @property
    def blocking(self) -> tuple[ProofResult, ...]:
        return tuple(item for item in self.results if item.state.blocks_registration)

    @property
    def all_proofs_passed(self) -> bool:
        """Eight distinct proofs, all ``PASSED``.

        Named for what it measures. Under a fake probe this is true and means
        only that the orchestration works, which is why it is no longer the
        thing called `registerable`.
        """

        reported = [item.proof_id for item in self.results]
        if sorted(reported) != sorted(ProofId):
            return False
        if len(set(reported)) != len(reported):
            return False
        return not self.blocking

    @property
    def registerable(self) -> bool:
        """Eight distinct real-qualification passes. Nothing weaker registers.

        An orchestration-only run can never return true here however green it
        is. That is the correction: 7P.1b called a fake-probe result
        registerable, and the type system had nothing to say about it.
        """

        return (
            self.evidence_kind is EvidenceKind.REAL_QUALIFICATION
            and self.all_proofs_passed
        )

    @property
    def status(self) -> PackageStatus:
        return (
            PackageStatus.REGISTERABLE
            if self.registerable
            else PackageStatus.NOT_YET_REGISTERABLE
        )

    def why_not_registerable(self) -> str:
        if self.registerable:
            return "registerable"
        if self.evidence_kind is not EvidenceKind.REAL_QUALIFICATION:
            return (
                "this run used an orchestration-only probe. It validates the "
                "validator, not the package: no clone was made, no command "
                "ran, and no witness was emitted."
            )
        blocking = {str(item.proof_id): str(item.state) for item in self.blocking}
        if blocking:
            return f"real qualification did not pass every proof: {blocking}"
        return "not every proof was reported exactly once"

    def summary(self) -> dict[str, str]:
        return {str(item.proof_id): str(item.state) for item in self.results}


def _passed(proof: ProofId, detail: str, **evidence: str) -> ProofResult:
    return ProofResult(
        proof_id=proof, state=ProofState.PASSED, detail=detail, evidence=evidence
    )


def _failed(proof: ProofId, detail: str, **evidence: str) -> ProofResult:
    return ProofResult(
        proof_id=proof, state=ProofState.FAILED, detail=detail, evidence=evidence
    )


def _unrun(proof: ProofId, detail: str) -> ProofResult:
    return ProofResult(proof_id=proof, state=ProofState.UNRUN, detail=detail)


def _inconclusive(proof: ProofId, detail: str) -> ProofResult:
    return ProofResult(
        proof_id=proof, state=ProofState.INCONCLUSIVE, detail=detail
    )


def _proof_fresh_clone(package, probe, workspace) -> tuple[ProofResult, object]:
    declared = _read_json(package, "seed.json")
    try:
        observed = probe.clone_seed(destination=workspace)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return (
            _unrun(
                ProofId.FRESH_CLONE_REPRODUCES_SEED,
                f"the seed could not be cloned on this host: {exc}",
            ),
            None,
        )

    for label, expected in (
        ("commit", declared["seed_commit"]),
        ("tree", declared["seed_tree"]),
    ):
        actual: GitObject = getattr(observed, label)
        if actual.object_id != expected["object_id"]:
            return (
                _failed(
                    ProofId.FRESH_CLONE_REPRODUCES_SEED,
                    f"fresh clone {label} is {actual.object_id}, declared "
                    f"{expected['object_id']}",
                ),
                observed,
            )
        # The type check is the one that catches a parent commit id sitting in
        # the tree field. Both values are forty hex characters and only
        # `git cat-file -t` can tell them apart.
        if actual.object_type != expected["object_type"]:
            return (
                _failed(
                    ProofId.FRESH_CLONE_REPRODUCES_SEED,
                    f"{label} {actual.object_id} is a {actual.object_type}, "
                    f"declared a {expected['object_type']}",
                ),
                observed,
            )

    if tuple(declared["tracked_files"]) != observed.tracked_files:
        return (
            _failed(
                ProofId.FRESH_CLONE_REPRODUCES_SEED,
                "the fresh clone's tracked files differ from the declaration",
            ),
            observed,
        )

    return (
        _passed(
            ProofId.FRESH_CLONE_REPRODUCES_SEED,
            "a fresh clone reproduced the declared commit and tree, and both "
            "carry their declared Git object types",
            commit=observed.commit.object_id,
            tree=observed.tree.object_id,
        ),
        observed,
    )


def _proof_behaviour_absent(package, probe, workspace, cloned) -> ProofResult:
    proof = ProofId.REQUESTED_BEHAVIOUR_ABSENT_FROM_SEED
    if cloned is None:
        return _unrun(proof, "no clone to inspect")
    oracle = _read_json(package, "evaluator-only/oracle.json")
    present_paths = set(probe.read_seed_paths(destination=workspace))
    offending = sorted(set(oracle["seed_absent_paths"]) & present_paths)
    if offending:
        return _failed(
            proof,
            f"the seed already contains {offending}; the task would be "
            "satisfied by doing nothing",
        )
    found = probe.search_seed_symbols(
        destination=workspace, symbols=tuple(oracle["seed_absent_symbols"])
    )
    if found:
        return _failed(
            proof, f"symbols {sorted(found)} already exist in the seed"
        )
    return _passed(
        proof,
        "neither declared service path nor either declared symbol exists in "
        "the seed, so the task asks for behaviour that is genuinely absent",
    )


def _proof_inherited_state(package, probe, workspace, cloned) -> ProofResult:
    proof = ProofId.INHERITED_TEST_STATE_RECORDED
    if cloned is None:
        return _unrun(proof, "no clone to run the inherited suite in")
    outcome = probe.run_inherited_suite(destination=workspace)
    if outcome.exit_code != 0:
        return _inconclusive(
            proof,
            f"the inherited suite exited {outcome.exit_code}. A seed whose "
            "own suite is red cannot demonstrate that inherited green is "
            "insufficient, because there is no inherited green.",
        )
    oracle = _read_json(package, "evaluator-only/oracle.json")
    reached = sorted(set(outcome.covered_paths) & set(oracle["seed_absent_paths"]))
    if reached:
        return _failed(
            proof, f"the inherited suite already reaches {reached}"
        )
    return _passed(
        proof,
        "the inherited suite passes and its coverage reaches none of the "
        "declared service paths, which is the false-green shape this case "
        "exists to detect",
        exit_code=str(outcome.exit_code),
        covered=",".join(sorted(outcome.covered_paths)),
    )


def _proof_reference_satisfies(package, probe, workspace, cloned):
    proof = ProofId.REFERENCE_SATISFIES_EVERY_CRITERION
    if cloned is None:
        return _unrun(proof, "no clone to apply the reference to"), None
    observation = probe.run_checkpoint(destination=workspace, candidate="reference")
    criteria = _read_json(package, "acceptance-criteria.json")["criteria"]
    required = {
        item["criterion_id"]
        for item in criteria
        if item["required_witness_kind"] != "negative_control"
    }
    unsatisfied = sorted(required - set(observation.satisfied_criteria))
    if unsatisfied:
        return (
            _failed(
                proof,
                f"the known-good reference leaves {unsatisfied} unsatisfied",
            ),
            observation,
        )
    if observation.outcome != "COMPLETE":
        return (
            _failed(
                proof,
                f"the reference reached {observation.outcome}, not COMPLETE, "
                f"blocked by {sorted(observation.readiness_blocks)}",
            ),
            observation,
        )
    return (
        _passed(
            proof,
            "the known-good reference satisfies every mapped criterion and "
            "reaches COMPLETE. It is evaluator reference material, not a "
            "model achievement.",
            outcome=observation.outcome,
        ),
        observation,
    )


def _proof_removal_fails(package, probe, workspace, cloned) -> ProofResult:
    proof = ProofId.REMOVAL_FAILS_ITS_MAPPED_CRITERION
    if cloned is None:
        return _unrun(proof, "no clone to remove artifacts from")
    removals = _read_json(package, "evaluator-only/oracle.json")[
        "negative_control_removals"
    ]
    if not removals:
        return _inconclusive(
            proof, "the oracle declares no removals, so nothing was tested"
        )
    for removal in removals:
        observation = probe.run_checkpoint(
            destination=workspace,
            candidate="reference",
            omit_path=removal["remove_path"],
        )
        criterion = removal["must_fail_criterion"]
        if criterion in observation.satisfied_criteria:
            return _failed(
                proof,
                f"removing {removal['remove_path']!r} still satisfied "
                f"{criterion}, so that criterion is not actually mapped to "
                "the artifact it names",
            )
        if observation.outcome == "COMPLETE":
            return _failed(
                proof,
                f"removing {removal['remove_path']!r} still reached COMPLETE",
            )
    return _passed(
        proof,
        f"each of the {len(removals)} declared removals failed exactly the "
        "criterion it is mapped to",
    )


def _proof_incomplete_cannot_complete(package, probe, workspace, cloned):
    proof = ProofId.INCOMPLETE_CANDIDATE_CANNOT_COMPLETE
    if cloned is None:
        return _unrun(proof, "no clone to apply the historical candidate to")
    oracle = _read_json(package, "evaluator-only/oracle.json")
    observation = probe.run_checkpoint(destination=workspace, candidate="incomplete")

    if observation.outcome == oracle["forbidden_outcome_for_incomplete_candidate"]:
        return _failed(
            proof,
            "the historical candidate reached COMPLETE. This is the exact "
            "regression the case exists to detect.",
        )
    if observation.outcome not in oracle["required_outcome_for_incomplete_candidate"]:
        return _inconclusive(
            proof,
            f"the candidate produced {observation.outcome!r}, which the "
            "oracle neither requires nor forbids",
        )

    expected_blocks = {
        item["readiness_block"] for item in oracle["incomplete_candidate_blocks"]
    }
    missing = sorted(expected_blocks - set(observation.readiness_blocks))
    if missing:
        return _failed(
            proof,
            f"the candidate was refused, but not for {missing}. A refusal for "
            "the wrong reason does not prove the mapped blocks work.",
        )

    # The passing command is not incidental. It is the property under test:
    # the historical arm terminated COMPLETE *because* commands were green.
    green = [item.name for item in observation.commands if item.exit_code == 0]
    if not green:
        return _inconclusive(
            proof,
            "no configured command passed, so this run does not demonstrate "
            "that a green command can coexist with a non-complete result",
        )
    return _passed(
        proof,
        f"the historical candidate reached {observation.outcome} with "
        f"{len(green)} configured command(s) passing, blocked independently "
        f"by {sorted(expected_blocks)}",
        outcome=observation.outcome,
        passing_commands=",".join(sorted(green)),
    )


def _proof_witness_binding(package, reference_observation) -> ProofResult:
    proof = ProofId.WITNESSES_BOUND_TO_ADMITTED_SNAPSHOT
    if reference_observation is None:
        return _unrun(proof, "no checkpoint observation to inspect")
    if reference_observation.emitter_failed:
        return _failed(proof, "witness emission failed")
    commands = reference_observation.commands
    if not commands:
        return _inconclusive(proof, "the run emitted no witnesses to inspect")
    unbound = [item.name for item in commands if not item.worktree_fingerprint]
    if unbound:
        return _failed(
            proof,
            f"witnesses {sorted(unbound)} carry no admitted-snapshot "
            "fingerprint, so nothing ties them to the tree that was admitted",
        )
    fingerprints = {item.worktree_fingerprint for item in commands}
    if len(fingerprints) != 1:
        return _failed(
            proof,
            f"witnesses claim {len(fingerprints)} different snapshots; at "
            "most one of them can describe the admitted tree",
        )
    return _passed(
        proof,
        "every witness is bound to one admitted-snapshot fingerprint",
        fingerprint=next(iter(fingerprints)),
    )


def _run_seven(
    package: ResolvedCasePackage, probe: PackageProbe, workspace: Path
) -> list[ProofResult]:
    """Proofs 1-7 over one fresh clone. Proof 8 runs this a second time."""

    results: list[ProofResult] = []
    clone_result, cloned = _proof_fresh_clone(package, probe, workspace)
    results.append(clone_result)
    if clone_result.state is not ProofState.PASSED:
        cloned = None
    results.append(_proof_behaviour_absent(package, probe, workspace, cloned))
    results.append(_proof_inherited_state(package, probe, workspace, cloned))
    reference_result, reference = _proof_reference_satisfies(
        package, probe, workspace, cloned
    )
    results.append(reference_result)
    results.append(_proof_removal_fails(package, probe, workspace, cloned))
    results.append(
        _proof_incomplete_cannot_complete(package, probe, workspace, cloned)
    )
    results.append(_proof_witness_binding(package, reference))
    return results


def validate_case_package(
    package_root: Path,
    *,
    probe: PackageProbe,
    workspace: Path,
    volatile_evidence_fields: tuple[str, ...] = (),
) -> CasePackageValidation:
    """Resolve the package, then run all eight proofs.

    The structural checks -- three repetitions, mapped criteria -- raise rather
    than producing a failed proof. They describe a package that is malformed,
    not one whose properties were measured and found wanting, and reporting
    them as a failed proof would put a package that cannot be run in the same
    bucket as one that was run and lost.
    """

    package = resolve_case_package(package_root)
    validate_repetitions(package)
    validate_criteria_mapping(package)

    first_workspace = workspace / "clone-1"
    second_workspace = workspace / "clone-2"
    first = _run_seven(package, probe, first_workspace)
    second = _run_seven(package, probe, second_workspace)

    # Proof 8 compares the two runs rather than asserting determinism from one.
    # A validator that ran once and declared the result reproducible would be
    # making the claim it was asked to check.
    def comparable(evidence: dict[str, str]) -> dict[str, str]:
        # Volatile fields are dropped only when the caller *declared* them. A
        # comparison that discarded whatever happened to differ would agree
        # with itself by construction.
        return {
            key: value
            for key, value in evidence.items()
            if key not in volatile_evidence_fields
        }

    divergent = [
        str(left.proof_id)
        for left, right in zip(first, second, strict=True)
        if left.state is not right.state
        or comparable(left.evidence) != comparable(right.evidence)
    ]
    if divergent:
        second_pass = _failed(
            ProofId.SECOND_FRESH_CLONE_IS_IDENTICAL,
            f"a second fresh clone produced different outcomes or evidence "
            f"identities for {sorted(divergent)}",
        )
    elif any(item.state is ProofState.UNRUN for item in first):
        second_pass = _unrun(
            ProofId.SECOND_FRESH_CLONE_IS_IDENTICAL,
            "the first validation did not run every proof, so agreement "
            "between two incomplete runs proves nothing",
        )
    else:
        second_pass = _passed(
            ProofId.SECOND_FRESH_CLONE_IS_IDENTICAL,
            "a second fresh clone produced identical outcomes and identical "
            "evidence identities for all seven preceding proofs",
        )

    return CasePackageValidation(
        package_id=package.package_id,
        package_digest=package.package_digest,
        evidence_kind=probe.evidence_kind,
        volatile_evidence_fields=volatile_evidence_fields,
        results=tuple([*first, second_pass]),
    )


def _git(*argv: str, cwd: Path) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    return completed.stdout.strip()


class GitCloneObserver:
    """The real clone half of a probe. Deliberately not a full ``PackageProbe``.

    Cloning and inspecting a repository is decidable here. Running a checkpoint
    is not: it needs a workcell, a contract compiler and an emitter, all of
    which belong to 7P.2's wiring. This class exists so the part that *can* be
    done for real is done for real, rather than the whole probe living only as
    a fake.
    """

    def __init__(self, seed_repository: Path) -> None:
        self.seed_repository = seed_repository.resolve()

    def clone_seed(self, *, destination: Path) -> SeedObservation:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _git(
            "clone",
            "--no-hardlinks",
            str(self.seed_repository),
            str(destination),
            cwd=destination.parent,
        )
        commit = _git("rev-parse", "HEAD", cwd=destination)
        # `git cat-file -p HEAD` rather than `HEAD^{tree}`: the braces are
        # shell metacharacters on PowerShell and an unquoted invocation emits
        # the parent commit id before failing. Parsing the commit object cannot
        # be misread that way.
        header = _git("cat-file", "-p", "HEAD", cwd=destination)
        tree = ""
        for line in header.splitlines():
            if line.startswith("tree "):
                tree = line.split(" ", 1)[1].strip()
                break
        if not tree:
            raise CasePackageError("commit object declared no tree")
        return SeedObservation(
            commit=GitObject(
                object_id=commit,
                object_type=_git("cat-file", "-t", commit, cwd=destination),
            ),
            tree=GitObject(
                object_id=tree,
                object_type=_git("cat-file", "-t", tree, cwd=destination),
            ),
            tracked_files=tuple(
                sorted(_git("ls-files", cwd=destination).splitlines())
            ),
            working_tree_clean=not _git("status", "--porcelain", cwd=destination),
        )

    def read_seed_paths(self, *, destination: Path) -> tuple[str, ...]:
        return tuple(sorted(_git("ls-files", cwd=destination).splitlines()))

    def search_seed_symbols(
        self, *, destination: Path, symbols: tuple[str, ...]
    ) -> tuple[str, ...]:
        found = []
        for symbol in symbols:
            for path in destination.rglob("*.py"):
                if symbol in path.read_text(encoding="utf-8", errors="replace"):
                    found.append(symbol)
                    break
        return tuple(found)
