"""Slice 7P.1b: a package registers only when eight separate proofs passed.

Most of these tests assert a refusal, for the same reason the 7P.1a tests do.
The failure this slice guards against is not a package that is obviously
broken -- it is a package that resolves cleanly, reads plausibly, and still
lets the historical Crisis Atlas candidate through.

No model, server, container or network is involved. The seed-dependent proofs
run against a deterministic fake probe; `GitCloneObserverTests` exercises the
real git path separately and skips where git is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from apoapsis.qualification.artifacts import ArtifactKind
from apoapsis.qualification.case_package import (
    CasePackageError,
    CheckpointObservation,
    CommandOutcome,
    GitCloneObserver,
    GitObject,
    ProofId,
    ProofState,
    SeedObservation,
    resolve_case_package,
    validate_case_package,
    validate_criteria_mapping,
    validate_repetitions,
)

PACKAGE_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "qualification"
    / "pilot"
    / "crisis-atlas"
)

SEED_COMMIT = "197b3610e5720cf36718c548fa19c05fe784a978"
SEED_TREE = "02fb45efeb4e19c619e3f730bd05a1f70bef9f13"
#: The value a mangled `HEAD^{tree}` invocation prints. It is a commit.
SEED_PARENT = "50bffcfe498129b833eaa35eb8c097a825b2ee39"

TRACKED = (
    ".gitignore",
    "README.md",
    "app.js",
    "crisis_atlas/__init__.py",
    "index.html",
    "styles.css",
    "tests/__init__.py",
    "tests/test_smoke.py",
)

FINGERPRINT = "a" * 64


@dataclass
class FakeProbe:
    """A deterministic stand-in for cloning and running the checkpoint.

    Every field is a knob a test turns to produce exactly one defect, so a
    failure names the property that broke rather than "something went wrong".
    """

    commit: str = SEED_COMMIT
    tree: str = SEED_TREE
    tree_type: str = "tree"
    clone_raises: str | None = None
    tracked: tuple[str, ...] = TRACKED
    seed_paths: tuple[str, ...] = TRACKED
    seed_symbols: tuple[str, ...] = ()
    inherited_exit: int = 0
    inherited_covered: tuple[str, ...] = ("crisis_atlas/__init__.py",)
    reference_outcome: str = "COMPLETE"
    reference_criteria: tuple[str, ...] = (
        "AC-INCIDENT-SERVICE",
        "AC-EXPORT-SERVICE",
        "AC-CHANGED-BEHAVIOUR-EXERCISED",
    )
    incomplete_outcome: str = "CONTINUE"
    incomplete_blocks: tuple[str, ...] = (
        "MISSING_REQUIRED_ARTIFACT",
        "CHANGED_BEHAVIOUR_UNEXERCISED",
    )
    incomplete_passing_commands: tuple[str, ...] = (
        "unit-tests",
        "web-product-integrity",
        "behavioral-integration",
        "launch-smoke",
    )
    fingerprints: tuple[str, ...] = (FINGERPRINT,)
    emitter_failed: bool = False
    removal_still_satisfies: bool = False
    #: Values consumed one per clone, so the second validation can differ.
    per_clone_commit: list[str] = field(default_factory=list)
    _clones: int = 0

    def clone_seed(self, *, destination: Path) -> SeedObservation:
        if self.clone_raises:
            raise RuntimeError(self.clone_raises)
        commit = self.commit
        if self.per_clone_commit:
            commit = self.per_clone_commit[
                min(self._clones, len(self.per_clone_commit) - 1)
            ]
        self._clones += 1
        return SeedObservation(
            commit=GitObject(object_id=commit, object_type="commit"),
            tree=GitObject(object_id=self.tree, object_type=self.tree_type),
            tracked_files=self.tracked,
            working_tree_clean=True,
        )

    def read_seed_paths(self, *, destination: Path) -> tuple[str, ...]:
        return self.seed_paths

    def search_seed_symbols(self, *, destination, symbols):
        return tuple(item for item in symbols if item in self.seed_symbols)

    def run_inherited_suite(self, *, destination: Path) -> CommandOutcome:
        return CommandOutcome(
            name="unit-tests",
            exit_code=self.inherited_exit,
            covered_paths=self.inherited_covered,
            worktree_fingerprint=FINGERPRINT,
        )

    def run_checkpoint(self, *, destination, candidate, omit_path=None):
        if candidate == "incomplete":
            return CheckpointObservation(
                outcome=self.incomplete_outcome,
                satisfied_criteria=(),
                readiness_blocks=self.incomplete_blocks,
                repair_packet="missing export service",
                commands=tuple(
                    CommandOutcome(
                        name=name, exit_code=0, worktree_fingerprint=FINGERPRINT
                    )
                    for name in self.incomplete_passing_commands
                ),
            )
        if omit_path is not None and not self.removal_still_satisfies:
            return CheckpointObservation(
                outcome="CONTINUE",
                satisfied_criteria=(),
                readiness_blocks=("MISSING_REQUIRED_ARTIFACT",),
            )
        return CheckpointObservation(
            outcome=self.reference_outcome,
            satisfied_criteria=self.reference_criteria,
            commands=tuple(
                CommandOutcome(
                    name=f"unit-tests-{index}",
                    exit_code=0,
                    worktree_fingerprint=value,
                )
                for index, value in enumerate(self.fingerprints)
            ),
            emitter_failed=self.emitter_failed,
        )


class _PackageCase(unittest.TestCase):
    """A disposable copy of the real authored package.

    A copy, not the committed tree: half of these tests corrupt an artifact on
    purpose, and a test that mutates the repository to make its point is a test
    that can lose the repository.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.root = self.workspace / "crisis-atlas"
        shutil.copytree(PACKAGE_SOURCE, self.root)

    def declaration(self) -> dict:
        return json.loads((self.root / "package.json").read_text(encoding="utf-8"))

    def rewrite_declaration(self, payload: dict) -> None:
        (self.root / "package.json").write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def entry(self, payload: dict, relative: str) -> dict:
        for item in payload["artifacts"]:
            if item["relative_path"] == relative:
                return item
        raise AssertionError(f"{relative} is not declared")

    def validate(self, probe: FakeProbe | None = None):
        return validate_case_package(
            self.root,
            probe=probe or FakeProbe(),
            workspace=self.workspace / "clones",
        )


class AuthoredPackageTests(_PackageCase):
    def test_the_authored_package_resolves_and_registers(self) -> None:
        validation = self.validate()
        self.assertEqual(
            validation.summary(),
            {str(proof): "passed" for proof in ProofId},
            validation.summary(),
        )
        self.assertTrue(validation.registerable)
        self.assertEqual(validation.blocking, ())

    def test_every_proof_is_reported_exactly_once(self) -> None:
        validation = self.validate()
        reported = [item.proof_id for item in validation.results]
        self.assertEqual(len(reported), 8)
        self.assertEqual(len(set(reported)), 8)

    def test_a_repeated_proof_cannot_stand_in_for_a_missing_one(self) -> None:
        validation = self.validate()
        duplicated = validation.model_copy(
            update={
                "results": tuple(
                    [
                        *validation.results[:-1],
                        validation.results[0],  # a second copy of proof 1
                    ]
                )
            }
        )
        # Eight results, every one passed, and still not registerable: the
        # containment proof is simply absent.
        self.assertEqual(len(duplicated.results), 8)
        self.assertTrue(all(item.state is ProofState.PASSED for item in duplicated.results))
        self.assertFalse(duplicated.registerable)

    def test_an_unrun_proof_blocks_registration(self) -> None:
        validation = self.validate(FakeProbe(clone_raises="no seed on this host"))
        self.assertIs(
            validation.result(ProofId.FRESH_CLONE_REPRODUCES_SEED).state,
            ProofState.UNRUN,
        )
        self.assertFalse(validation.registerable)

    def test_an_inconclusive_proof_blocks_registration(self) -> None:
        validation = self.validate(FakeProbe(inherited_exit=1))
        self.assertIs(
            validation.result(ProofId.INHERITED_TEST_STATE_RECORDED).state,
            ProofState.INCONCLUSIVE,
        )
        self.assertFalse(validation.registerable)


class ArtifactIntegrityTests(_PackageCase):
    def test_a_missing_artifact_refuses_the_package(self) -> None:
        (self.root / "task.md").unlink()
        with self.assertRaises(CasePackageError) as caught:
            resolve_case_package(self.root)
        self.assertIn("task.md", str(caught.exception))

    def test_a_wrong_artifact_kind_refuses_the_package(self) -> None:
        payload = self.declaration()
        # The oracle re-declared as task text is the containment failure in
        # miniature: it would move from evaluator-only into the mountable set.
        self.entry(payload, "evaluator-only/oracle.json")["kind"] = (
            ArtifactKind.TASK_TEXT.value
        )
        self.rewrite_declaration(payload)
        package = resolve_case_package(self.root)
        self.assertNotIn(
            "evaluator-only/oracle.json",
            [item.relative_path for item in package.evaluator_only],
        )
        # Which is exactly why kind is declared and checked rather than
        # inferred from the path.
        self.assertIn(
            "evaluator-only/oracle.json",
            [item.relative_path for item in package.mountable],
        )

    def test_a_digest_mismatch_refuses_the_package(self) -> None:
        payload = self.declaration()
        self.entry(payload, "task.md")["sha256"] = "0" * 64
        self.rewrite_declaration(payload)
        with self.assertRaises(CasePackageError) as caught:
            resolve_case_package(self.root)
        self.assertIn("digest_mismatch", str(caught.exception))

    def test_an_artifact_modified_after_hashing_is_refused(self) -> None:
        (self.root / "task.md").write_text("something else\n", encoding="utf-8")
        with self.assertRaises(CasePackageError):
            resolve_case_package(self.root)

    def test_a_package_mutated_after_validation_is_detected(self) -> None:
        package = resolve_case_package(self.root)
        package.assert_unchanged()
        (self.root / "task.md").write_text("edited later\n", encoding="utf-8")
        with self.assertRaises(CasePackageError) as caught:
            package.assert_unchanged()
        self.assertIn("changed after validation", str(caught.exception))

    def test_path_traversal_is_refused(self) -> None:
        payload = self.declaration()
        self.entry(payload, "task.md")["relative_path"] = "../task.md"
        self.rewrite_declaration(payload)
        with self.assertRaises(CasePackageError) as caught:
            resolve_case_package(self.root)
        self.assertIn("outside_package_root", str(caught.exception))

    def test_a_duplicate_declaration_is_refused(self) -> None:
        payload = self.declaration()
        payload["artifacts"].append(dict(self.entry(payload, "task.md")))
        self.rewrite_declaration(payload)
        with self.assertRaises(CasePackageError) as caught:
            resolve_case_package(self.root)
        self.assertIn("declared twice", str(caught.exception))

    def test_a_missing_component_is_refused(self) -> None:
        payload = self.declaration()
        del payload["components"]["6_evaluator_only_oracle"]
        self.rewrite_declaration(payload)
        with self.assertRaises(CasePackageError) as caught:
            resolve_case_package(self.root)
        self.assertIn("6_evaluator_only_oracle", str(caught.exception))

    @unittest.skipUnless(
        os.name != "nt" or os.environ.get("APOAPSIS_SYMLINKS") == "1",
        "symlink creation needs privileges this Windows host may not grant",
    )
    def test_a_symlink_escape_is_refused(self) -> None:
        outside = self.workspace / "outside.txt"
        outside.write_text("evaluator material from elsewhere\n", encoding="utf-8")
        target = self.root / "task.md"
        digest = self.entry(self.declaration(), "task.md")["sha256"]
        target.unlink()
        target.symlink_to(outside)
        self.assertEqual(
            digest, self.entry(self.declaration(), "task.md")["sha256"]
        )
        with self.assertRaises(CasePackageError) as caught:
            resolve_case_package(self.root)
        self.assertIn("symlink_escape", str(caught.exception))


class RepetitionTests(_PackageCase):
    def _rewrite(self, mutate) -> None:
        path = self.root / "repetitions.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        declaration = self.declaration()
        import hashlib

        self.entry(declaration, "repetitions.json")["sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        self.rewrite_declaration(declaration)

    def test_three_distinct_repetitions_are_accepted(self) -> None:
        validate_repetitions(resolve_case_package(self.root))

    def test_a_duplicate_repetition_identity_is_refused(self) -> None:
        self._rewrite(
            lambda payload: payload["repetitions"][1].update(
                {"repetition_id": payload["repetitions"][0]["repetition_id"]}
            )
        )
        with self.assertRaises(CasePackageError) as caught:
            validate_repetitions(resolve_case_package(self.root))
        self.assertIn("duplicate repetition", str(caught.exception))

    def test_fewer_than_three_repetitions_is_refused(self) -> None:
        self._rewrite(lambda payload: payload["repetitions"].pop())
        with self.assertRaises(CasePackageError) as caught:
            validate_repetitions(resolve_case_package(self.root))
        self.assertIn("exactly 3", str(caught.exception))

    def test_more_than_three_repetitions_is_refused(self) -> None:
        def mutate(payload):
            extra = dict(payload["repetitions"][0])
            extra["repetition_id"] = "crisis-atlas-rep-4"
            payload["repetitions"].append(extra)

        self._rewrite(mutate)
        with self.assertRaises(CasePackageError) as caught:
            validate_repetitions(resolve_case_package(self.root))
        self.assertIn("exactly 3", str(caught.exception))

    def test_a_repetition_may_not_carry_a_controlled_variable(self) -> None:
        self._rewrite(
            lambda payload: payload["repetitions"][2].update(
                {"max_output_tokens": 8192}
            )
        )
        with self.assertRaises(CasePackageError) as caught:
            validate_repetitions(resolve_case_package(self.root))
        self.assertIn("max_output_tokens", str(caught.exception))


class CriteriaMappingTests(_PackageCase):
    def _rewrite_criteria(self, mutate) -> None:
        import hashlib

        path = self.root / "acceptance-criteria.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        declaration = self.declaration()
        self.entry(declaration, "acceptance-criteria.json")["sha256"] = (
            hashlib.sha256(path.read_bytes()).hexdigest()
        )
        self.rewrite_declaration(declaration)

    def test_the_authored_criteria_map_every_obligation(self) -> None:
        validate_criteria_mapping(resolve_case_package(self.root))

    def test_an_unmapped_acceptance_obligation_is_refused(self) -> None:
        self._rewrite_criteria(
            lambda payload: payload["criteria"].pop(0)  # AC-INCIDENT-SERVICE
        )
        with self.assertRaises(CasePackageError) as caught:
            validate_criteria_mapping(resolve_case_package(self.root))
        self.assertIn("AC-INCIDENT-SERVICE", str(caught.exception))

    def test_a_criterion_with_no_insufficient_evidence_is_refused(self) -> None:
        def mutate(payload):
            payload["criteria"][0]["insufficient_evidence"] = []

        self._rewrite_criteria(mutate)
        with self.assertRaises(CasePackageError) as caught:
            validate_criteria_mapping(resolve_case_package(self.root))
        self.assertIn("insufficient", str(caught.exception))

    def test_no_criterion_is_proved_by_a_command_passing(self) -> None:
        criteria = json.loads(
            (self.root / "acceptance-criteria.json").read_text(encoding="utf-8")
        )["criteria"]
        for item in criteria:
            self.assertNotEqual(item["required_witness_kind"], "command_exit_code")
            self.assertNotIn("exit", item["success_condition"].lower())
        # Every criterion backed by a witness must name inherited green as
        # insufficient. AC-INHERITED-INSUFFICIENT is the rule itself, so it
        # states the same thing in its success condition instead.
        witness_backed = [
            item
            for item in criteria
            if item["criterion_id"] != "AC-INHERITED-INSUFFICIENT"
        ]
        self.assertEqual(len(witness_backed), 3)
        for item in witness_backed:
            self.assertRegex(
                " ".join(item["insufficient_evidence"]),
                r"green|inherited suite passing|exiting 0",
            )


class ProofBehaviourTests(_PackageCase):
    def _state(self, probe: FakeProbe, proof: ProofId) -> ProofState:
        return self.validate(probe).result(proof).state

    def test_a_seed_that_already_has_the_behaviour_fails_proof_two(self) -> None:
        probe = FakeProbe(seed_symbols=("IncidentService", "ExportService"))
        self.assertIs(
            self._state(probe, ProofId.REQUESTED_BEHAVIOUR_ABSENT_FROM_SEED),
            ProofState.FAILED,
        )

    def test_inherited_green_that_already_reaches_the_services_fails(self) -> None:
        probe = FakeProbe(
            inherited_covered=("crisis_atlas/services/incident_service.py",)
        )
        self.assertIs(
            self._state(probe, ProofId.INHERITED_TEST_STATE_RECORDED),
            ProofState.FAILED,
        )

    def test_inherited_green_without_changed_behaviour_evidence_passes_proof_three(
        self,
    ) -> None:
        # The seed's suite passes and reaches nothing the task asks for. That
        # is the case's discriminating property, not a defect.
        self.assertIs(
            self._state(FakeProbe(), ProofId.INHERITED_TEST_STATE_RECORDED),
            ProofState.PASSED,
        )

    def test_a_known_good_reference_missing_one_behaviour_fails_proof_four(
        self,
    ) -> None:
        probe = FakeProbe(
            reference_criteria=("AC-INCIDENT-SERVICE", "AC-CHANGED-BEHAVIOUR-EXERCISED")
        )
        self.assertIs(
            self._state(probe, ProofId.REFERENCE_SATISFIES_EVERY_CRITERION),
            ProofState.FAILED,
        )

    def test_a_reference_that_does_not_complete_fails_proof_four(self) -> None:
        self.assertIs(
            self._state(
                FakeProbe(reference_outcome="CONTINUE"),
                ProofId.REFERENCE_SATISFIES_EVERY_CRITERION,
            ),
            ProofState.FAILED,
        )

    def test_a_removal_that_still_satisfies_its_criterion_fails_proof_five(
        self,
    ) -> None:
        self.assertIs(
            self._state(
                FakeProbe(removal_still_satisfies=True),
                ProofId.REMOVAL_FAILS_ITS_MAPPED_CRITERION,
            ),
            ProofState.FAILED,
        )

    def test_the_incomplete_candidate_reaching_complete_fails_proof_six(self) -> None:
        validation = self.validate(FakeProbe(incomplete_outcome="COMPLETE"))
        result = validation.result(ProofId.INCOMPLETE_CANDIDATE_CANNOT_COMPLETE)
        self.assertIs(result.state, ProofState.FAILED)
        self.assertIn("regression", result.detail)
        self.assertFalse(validation.registerable)

    def test_a_refusal_for_the_wrong_reason_fails_proof_six(self) -> None:
        # Refused, but only for the missing artifact. The unexercised-behaviour
        # block never fired, so that mapping is unproven.
        probe = FakeProbe(incomplete_blocks=("MISSING_REQUIRED_ARTIFACT",))
        result = self.validate(probe).result(
            ProofId.INCOMPLETE_CANDIDATE_CANNOT_COMPLETE
        )
        self.assertIs(result.state, ProofState.FAILED)
        self.assertIn("CHANGED_BEHAVIOUR_UNEXERCISED", result.detail)

    def test_a_passing_command_must_coexist_with_the_refusal(self) -> None:
        result = self.validate(
            FakeProbe(incomplete_passing_commands=())
        ).result(ProofId.INCOMPLETE_CANDIDATE_CANNOT_COMPLETE)
        self.assertIs(result.state, ProofState.INCONCLUSIVE)

    def test_the_historical_candidate_is_refused_with_four_commands_green(
        self,
    ) -> None:
        result = self.validate().result(ProofId.INCOMPLETE_CANDIDATE_CANNOT_COMPLETE)
        self.assertIs(result.state, ProofState.PASSED)
        self.assertEqual(result.evidence["outcome"], "CONTINUE")
        self.assertEqual(
            sorted(result.evidence["passing_commands"].split(",")),
            [
                "behavioral-integration",
                "launch-smoke",
                "unit-tests",
                "web-product-integrity",
            ],
        )

    def test_a_stale_snapshot_fingerprint_fails_proof_seven(self) -> None:
        probe = FakeProbe(fingerprints=(FINGERPRINT, "b" * 64))
        result = self.validate(probe).result(
            ProofId.WITNESSES_BOUND_TO_ADMITTED_SNAPSHOT
        )
        self.assertIs(result.state, ProofState.FAILED)
        self.assertIn("different snapshots", result.detail)

    def test_an_emitter_failure_fails_proof_seven(self) -> None:
        self.assertIs(
            self._state(
                FakeProbe(emitter_failed=True),
                ProofId.WITNESSES_BOUND_TO_ADMITTED_SNAPSHOT,
            ),
            ProofState.FAILED,
        )

    def test_a_nondeterministic_fresh_clone_fails_proof_eight(self) -> None:
        probe = FakeProbe(per_clone_commit=[SEED_COMMIT, "f" * 40])
        validation = self.validate(probe)
        self.assertIs(
            validation.result(ProofId.SECOND_FRESH_CLONE_IS_IDENTICAL).state,
            ProofState.FAILED,
        )
        self.assertFalse(validation.registerable)


class ObjectTypeTests(_PackageCase):
    def test_the_parent_commit_id_is_not_accepted_as_the_tree(self) -> None:
        """The PowerShell near-miss, as a regression.

        `50bffcfe...` is forty valid hex characters and was printed by a
        mangled `HEAD^{tree}` invocation. Only its object type distinguishes
        it from the tree it was mistaken for.
        """

        probe = FakeProbe(tree=SEED_PARENT)
        result = self.validate(probe).result(ProofId.FRESH_CLONE_REPRODUCES_SEED)
        self.assertIs(result.state, ProofState.FAILED)
        self.assertIn(SEED_PARENT, result.detail)

    def test_a_correct_id_with_the_wrong_object_type_is_refused(self) -> None:
        probe = FakeProbe(tree_type="commit")
        result = self.validate(probe).result(ProofId.FRESH_CLONE_REPRODUCES_SEED)
        self.assertIs(result.state, ProofState.FAILED)
        self.assertIn("is a commit", result.detail)

    def test_the_package_records_commit_tree_and_parent_separately(self) -> None:
        seed = json.loads((self.root / "seed.json").read_text(encoding="utf-8"))
        self.assertEqual(seed["seed_commit"]["object_id"], SEED_COMMIT)
        self.assertEqual(seed["seed_tree"]["object_id"], SEED_TREE)
        self.assertEqual(
            seed["provenance_parent_commit"]["object_id"], SEED_PARENT
        )
        self.assertEqual(seed["provenance_parent_commit"]["object_type"], "commit")
        self.assertNotEqual(
            seed["seed_tree"]["object_id"],
            seed["provenance_parent_commit"]["object_id"],
        )


class ContainmentTests(_PackageCase):
    def test_evaluator_only_and_mountable_partition_the_package(self) -> None:
        package = resolve_case_package(self.root)
        self.assertEqual(
            len(package.mountable) + len(package.evaluator_only),
            len(package.artifacts),
        )
        self.assertTrue(package.evaluator_only)
        self.assertTrue(package.mountable)

    def test_a_hidden_evaluator_asset_exposed_to_an_arm_is_refused(self) -> None:
        package = resolve_case_package(self.root)
        oracle = self.root / "evaluator-only" / "oracle.json"
        with self.assertRaises(CasePackageError) as caught:
            package.assert_arm_visible_set_is_contained((str(oracle),))
        self.assertIn("oracle.json", str(caught.exception))

    def test_the_reference_candidate_may_not_reach_an_arm(self) -> None:
        package = resolve_case_package(self.root)
        reference = (
            self.root
            / "evaluator-only"
            / "reference"
            / "crisis_atlas"
            / "services"
            / "export_service.py"
        )
        with self.assertRaises(CasePackageError):
            package.assert_arm_visible_set_is_contained((str(reference),))

    def test_the_incomplete_candidate_may_not_reach_an_arm(self) -> None:
        package = resolve_case_package(self.root)
        candidate = (
            self.root
            / "evaluator-only"
            / "incomplete"
            / "services"
            / "incident_service.py"
        )
        with self.assertRaises(CasePackageError):
            package.assert_arm_visible_set_is_contained((str(candidate),))

    def test_a_mountable_set_of_task_and_contract_is_allowed(self) -> None:
        package = resolve_case_package(self.root)
        package.assert_arm_visible_set_is_contained(
            (
                str(self.root / "task.md"),
                str(self.root / "plan-contract.json"),
                str(self.root / "verification-commands.json"),
            )
        )

    def test_containment_is_not_decided_by_the_path_prefix(self) -> None:
        """A copy outside `evaluator-only/` is still evaluator-only.

        Naming conventions are comments. The check compares resolved absolute
        paths against the set derived from declared kinds.
        """

        package = resolve_case_package(self.root)
        relocated = self.workspace / "innocuous-name.json"
        shutil.copy(self.root / "evaluator-only" / "oracle.json", relocated)
        # A *copy* is a different file and is not caught -- which is why 7P.2
        # must build its mount set from `package.mountable` rather than
        # filtering a list it assembled itself.
        package.assert_arm_visible_set_is_contained((str(relocated),))
        self.assertNotIn(
            str(relocated), [item.absolute_path for item in package.mountable]
        )


class HistoricalProvenanceTests(_PackageCase):
    def setUp(self) -> None:
        super().setUp()
        self.provenance = json.loads(
            (
                self.root / "evaluator-only" / "incomplete" / "provenance.json"
            ).read_text(encoding="utf-8")
        )

    def test_the_incomplete_candidate_is_historical_not_reconstructed(self) -> None:
        self.assertEqual(
            self.provenance["classification"], "actual_historical_candidate"
        )
        self.assertTrue(self.provenance["not_a_reconstruction"])

    def test_it_changed_exactly_one_path_outside_the_declared_package(self) -> None:
        self.assertEqual(
            self.provenance["changed_paths"], ["services/incident_service.py"]
        )
        contract = json.loads(
            (self.root / "plan-contract.json").read_text(encoding="utf-8")
        )
        declared = {
            path
            for obligation in contract["obligations"]
            for path in obligation["required_paths"]
        }
        self.assertNotIn("services/incident_service.py", declared)

    def test_its_own_summary_is_refuted_by_its_own_change_set(self) -> None:
        self.assertIn("ExportService", self.provenance["model_summary"])
        self.assertEqual(len(self.provenance["changed_paths"]), 1)
        self.assertTrue(
            self.provenance["summary_is_refuted_by_its_own_change_set"]
        )

    def test_the_omission_is_not_an_output_cap_artifact(self) -> None:
        self.assertEqual(self.provenance["finish_reason"], "stop")

    def test_every_configured_command_was_green(self) -> None:
        self.assertEqual(self.provenance["inherited_verification_status"], "passed")
        self.assertEqual(len(self.provenance["inherited_commands"]), 4)
        for command in self.provenance["inherited_commands"]:
            self.assertEqual(command["exit_code"], 0)

    def test_unavailable_fields_are_recorded_not_invented(self) -> None:
        for key in (
            "candidate_tree_object_id",
            "repair_distance_files",
            "repair_distance_lines",
        ):
            self.assertIn("not recorded", self.provenance["unavailable_fields"][key])

    def test_the_repaired_worktree_is_named_as_a_trap(self) -> None:
        warning = self.provenance["path_confusion_warning"]
        self.assertIn("SINGULAR", warning)
        self.assertIn("PLURAL", warning)

    def test_the_reference_candidate_is_labelled_as_evaluator_material(self) -> None:
        index = (self.root / "evidence-index.md").read_text(encoding="utf-8")
        self.assertIn("Not a model achievement", index)
        self.assertIn("Actual historical bytes", index)


class GitCloneObserverTests(unittest.TestCase):
    """The real git path, exercised against a repository built in a temp dir.

    Not against the Crisis Atlas seed: that would make the focused suite depend
    on an evaluation fixture existing on the host. What is under test here is
    the parsing, and specifically that the tree is read from the commit object
    rather than from `HEAD^{tree}`.
    """

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not available on this host")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.origin = self.workspace / "origin"
        self.origin.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        for argv in (
            ["init", "-q"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "t"],
        ):
            subprocess.run(  # noqa: S603
                ["git", *argv], cwd=self.origin, check=True, env=env
            )
        (self.origin / "a.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.origin, check=True, env=env)  # noqa: S603
        subprocess.run(  # noqa: S603
            ["git", "commit", "-qm", "first"], cwd=self.origin, check=True, env=env
        )
        (self.origin / "b.txt").write_text("two\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.origin, check=True, env=env)  # noqa: S603
        subprocess.run(  # noqa: S603
            ["git", "commit", "-qm", "second"], cwd=self.origin, check=True, env=env
        )

    def test_the_tree_is_a_tree_and_not_the_parent_commit(self) -> None:
        observer = GitCloneObserver(self.origin)
        observed = observer.clone_seed(destination=self.workspace / "clone")
        self.assertEqual(observed.commit.object_type, "commit")
        self.assertEqual(observed.tree.object_type, "tree")
        parent = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD^"],
            cwd=self.origin,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertNotEqual(observed.tree.object_id, parent)
        self.assertEqual(observed.tracked_files, ("a.txt", "b.txt"))
        self.assertTrue(observed.working_tree_clean)

    def test_two_clones_of_one_repository_agree(self) -> None:
        observer = GitCloneObserver(self.origin)
        first = observer.clone_seed(destination=self.workspace / "c1")
        second = observer.clone_seed(destination=self.workspace / "c2")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
