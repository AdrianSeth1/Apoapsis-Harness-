"""Slice 7P.2: the pilot manifest and its lock, frozen before any result.

These tests are about what the frozen artifacts refuse. A manifest that can be
edited into claiming a corpus result, a lock that still authorises a run after
the manifest moved, or a repetition label that reads as a determinism control
are all failures of the same kind: an artifact that looks authoritative and
says something untrue.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from pydantic import ValidationError

from apoapsis.qualification.pilot import (
    ArmKind,
    ExecutionRecordRefused,
    MountVisibility,
    PilotLock,
    PilotManifest,
    StopCondition,
    accept_execution_record,
    authorize_rehearsal,
)

REPO = Path(__file__).resolve().parents[1]
#: The *current* manifest. The v1 pair is preserved unedited as decision
#: history and is deliberately stale: it records the pre-requalification
#: evidence digest, and rewriting it to match would destroy the record of what
#: was actually locked when.
MANIFEST_PATH = (
    REPO / "docs" / "qualification" / "slice7-crisis-atlas-pilot-manifest-v5.json"
)
LOCK_PATH = REPO / "docs" / "qualification" / "slice7-crisis-atlas-pilot-lock-v5.json"
#: The pair v5 supersedes. Preserved unedited: v4 bound a relay that reported
#: truncated responses as successes, and its rehearsal halted in Stage 3 without
#: producing a verdict.
SUPERSEDED_MANIFEST_PATH = (
    REPO / "docs" / "qualification" / "slice7-crisis-atlas-pilot-manifest-v4.json"
)
SUPERSEDED_LOCK_PATH = (
    REPO / "docs" / "qualification" / "slice7-crisis-atlas-pilot-lock-v4.json"
)
#: v3, superseded by v4: it bound a runner whose stage 6 raised TypeError the
#: first time it executed.
V3_MANIFEST_PATH = (
    REPO / "docs" / "qualification" / "slice7-crisis-atlas-pilot-manifest-v3.json"
)
V3_LOCK_PATH = (
    REPO / "docs" / "qualification" / "slice7-crisis-atlas-pilot-lock-v3.json"
)
#: v2, superseded by v3: it expected a 13-tool surface the image never had,
#: bound none of the modules that sequence a rehearsal, and could no longer
#: verify its own manifest once the schema gained a field.
V2_MANIFEST_PATH = (
    REPO / "docs" / "qualification" / "slice7-crisis-atlas-pilot-manifest-v2.json"
)
V2_LOCK_PATH = (
    REPO / "docs" / "qualification" / "slice7-crisis-atlas-pilot-lock-v2.json"
)
#: v1, superseded by v2 and still preserved. Three supersessions deep is the
#: record; collapsing it would delete the reason anyone can say what was locked
#: when.
V1_MANIFEST_PATH = (
    REPO / "docs" / "qualification" / "slice7-crisis-atlas-pilot-manifest.json"
)
V1_LOCK_PATH = (
    REPO / "docs" / "qualification" / "slice7-crisis-atlas-pilot-lock.json"
)
PACKAGE = REPO / "docs" / "qualification" / "pilot" / "crisis-atlas"
EVIDENCE = REPO / "docs" / "evaluation" / "slice-7p1c-evidence"
DRAFT = REPO / "docs" / "qualification" / "slice7-qualification-manifest.json"


def load_manifest() -> PilotManifest:
    return PilotManifest.model_validate_json(
        MANIFEST_PATH.read_text(encoding="utf-8")
    )


def load_lock() -> PilotLock | None:
    if not LOCK_PATH.is_file():
        return None
    return PilotLock.model_validate_json(LOCK_PATH.read_text(encoding="utf-8"))


class ManifestResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest()

    def test_every_controlled_variable_resolves(self) -> None:
        self.assertEqual(self.manifest.unresolved_hashes(), ())
        self.assertTrue(self.manifest.ready_for_inference())

    def test_no_placeholder_or_label_derived_identity_remains(self) -> None:
        """The `sha256("slice7::<case>::seed")` defect, as a regression."""

        from apoapsis.qualification.artifacts import is_label_derived

        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        found: list[str] = []

        def walk(node, path=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, f"{path}/{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")
            elif isinstance(node, str):
                if is_label_derived(node, case_ids=("crisis-atlas",)):
                    found.append(path)
                if node in {"0" * 64, "f" * 64} or node.startswith("PENDING"):
                    found.append(path)

        walk(payload)
        self.assertEqual(found, [], f"placeholder identities at {found}")

    def test_package_and_evidence_digests_match_the_bytes_on_disk(self) -> None:
        declaration = json.loads((PACKAGE / "package.json").read_text("utf-8"))
        fingerprint = hashlib.sha256()
        for item in sorted(declaration["artifacts"], key=lambda v: v["relative_path"]):
            body = (PACKAGE / item["relative_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(body).hexdigest(), item["sha256"])
            fingerprint.update(
                f"{item['relative_path']}\0{item['sha256']}\0".encode("utf-8")
            )
        self.assertEqual(
            fingerprint.hexdigest(), self.manifest.crisis_atlas.package_digest
        )

        # The evidence root is whatever the manifest says it is, and the two
        # shapes are digested differently on purpose. v1 and v2 named a
        # directory tree; v3 names the single R3 proofs document, whose digest
        # is over its own bytes. Recomputing a tree fingerprint for a file, or
        # the reverse, would produce a mismatch that looks like tampering.
        root = REPO / self.manifest.crisis_atlas.qualification_evidence_root
        self.assertTrue(root.exists(), f"the evidence root is missing at {root}")
        if root.is_file():
            observed = hashlib.sha256(root.read_bytes()).hexdigest()
        else:
            evidence = hashlib.sha256()
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(root).as_posix()
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    evidence.update(f"{relative}\0{digest}\0".encode("utf-8"))
            observed = evidence.hexdigest()
        self.assertEqual(
            observed, self.manifest.crisis_atlas.qualification_evidence_sha256
        )

    def test_the_bound_evidence_is_a_real_qualification_run(self) -> None:
        """A digest match is not enough: orchestration-only evidence would
        also digest cleanly. What is bound must claim real qualification and
        must have passed all eight proofs."""

        root = REPO / self.manifest.crisis_atlas.qualification_evidence_root
        if not root.is_file():
            self.skipTest("the bound evidence is a directory tree, not a document")
        payload = json.loads(root.read_text(encoding="utf-8"))
        self.assertEqual(payload["evidence_kind"], "real_qualification")
        self.assertEqual(len(payload["results"]), 8)
        self.assertTrue(all(item["state"] == "passed" for item in payload["results"]))
        self.assertEqual(
            payload["package_digest"], self.manifest.crisis_atlas.package_digest
        )

    def test_the_package_is_still_registerable(self) -> None:
        self.assertTrue(self.manifest.crisis_atlas.package_registerable)

    def test_the_historical_draft_manifest_is_untouched(self) -> None:
        payload = json.loads(DRAFT.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["manifest_digest"],
            "8c374827aa4ace9576ed9d2d2f0db04747f3b4fb05d425b10e6fc770454f3762",
        )


class ScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest()

    def test_scope_is_exactly_one_crisis_atlas_case(self) -> None:
        self.assertEqual(self.manifest.scope.case_ids, ("crisis-atlas",))
        self.assertEqual(len(self.manifest.paired_executions), 3)

    def test_broad_and_rollout_claims_are_prohibited(self) -> None:
        scope = self.manifest.scope
        self.assertFalse(scope.broad_non_inferiority_claimed)
        self.assertFalse(scope.held_out_qualification_claimed)
        self.assertTrue(scope.default_rollout_prohibited)
        self.assertEqual(len(scope.deferred_corpus_cases), 7)

    def test_a_second_case_will_not_construct(self) -> None:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        payload["scope"]["case_ids"] = ["crisis-atlas", "focus-orbit"]
        with self.assertRaises(ValidationError):
            PilotManifest.model_validate(payload)

    def test_no_combined_score_exists(self) -> None:
        self.assertFalse(self.manifest.combined_score_defined)

    def test_the_manifest_alone_authorises_nothing(self) -> None:
        self.assertFalse(self.manifest.live_execution_authorised_by_manifest)


class PairAndContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest()

    def test_all_three_pairs_are_fully_bound(self) -> None:
        identities = []
        for pair in self.manifest.paired_executions:
            identities.append(pair.repetition.repetition_id)
            self.assertEqual(len(pair.executions), 2)
            arms = {item.arm for item in pair.executions}
            self.assertEqual(
                arms, {ArmKind.DEFAULT_QWEN_CONTROL, ArmKind.APOAPSIS_SANDBOX}
            )
        self.assertEqual(len(set(identities)), 3)

    def test_the_schedule_is_balanced_and_predetermined(self) -> None:
        first_arms = [
            next(
                item.arm
                for item in pair.executions
                if item.order_within_repetition == 1
            )
            for pair in self.manifest.paired_executions
        ]
        self.assertEqual(
            first_arms,
            [
                ArmKind.DEFAULT_QWEN_CONTROL,
                ArmKind.APOAPSIS_SANDBOX,
                ArmKind.DEFAULT_QWEN_CONTROL,
            ],
        )

    def test_arm_visible_task_inputs_match(self) -> None:
        visible = self.manifest.network_and_mounts.arm_visible_mounts()
        self.assertTrue(visible)
        for spec in visible:
            self.assertIs(spec.visibility, MountVisibility.BOTH_ARMS)
        # Both arms clone the same commit and read the same task bytes.
        for pair in self.manifest.paired_executions:
            commits = {item.fresh_clone_of_seed_commit for item in pair.executions}
            self.assertEqual(len(commits), 1)

    def test_evaluator_only_assets_are_absent_from_both_arm_mount_sets(self) -> None:
        for spec in self.manifest.network_and_mounts.arm_visible_mounts():
            self.assertNotIn("evaluator-only", spec.source_identity)
        hidden = [
            spec
            for spec in self.manifest.network_and_mounts.mounts
            if "evaluator-only" in spec.source_identity
        ]
        self.assertTrue(hidden)
        for spec in hidden:
            self.assertIs(spec.visibility, MountVisibility.CONTROLLER_ONLY)

    def test_a_pair_sharing_a_qwen_home_is_refused(self) -> None:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        pair = payload["paired_executions"][0]
        pair["executions"][1]["qwen_home"] = pair["executions"][0]["qwen_home"]
        with self.assertRaises(ValidationError):
            PilotManifest.model_validate(payload)

    def test_mounting_evaluator_material_into_both_arms_is_refused(self) -> None:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        for spec in payload["network_and_mounts"]["mounts"]:
            if "evaluator-only" in spec["source_identity"]:
                spec["visibility"] = "both_arms"
        with self.assertRaises(ValidationError):
            PilotManifest.model_validate(payload)

    def test_containment_is_frozen_identically(self) -> None:
        policy = self.manifest.network_and_mounts
        self.assertEqual(policy.network, "none")
        self.assertFalse(policy.direct_upstream_route)
        self.assertTrue(policy.hardened_workcell)
        self.assertTrue(policy.separate_worktrees)
        self.assertTrue(policy.separate_qwen_homes)
        self.assertTrue(policy.durable_evaluator_side_evidence)
        self.assertTrue(policy.supervisor_internals_carry_no_solution_information)


class IdentityInvalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_server_argv_reordering_invalidates_comparability(self) -> None:
        """Reordering flags changes the digest even though the set is equal."""

        argv = list(self.payload["server"]["argv"])
        index = argv.index("--jinja")
        argv.insert(1, argv.pop(index))
        self.payload["server"]["argv"] = argv
        with self.assertRaises(ValidationError) as caught:
            PilotManifest.model_validate(self.payload)
        self.assertIn("argv_sha256", str(caught.exception))

    def test_changing_an_argv_value_invalidates_comparability(self) -> None:
        argv = list(self.payload["server"]["argv"])
        argv[argv.index("65536")] = "32768"
        self.payload["server"]["argv"] = argv
        with self.assertRaises(ValidationError):
            PilotManifest.model_validate(self.payload)

    def test_an_image_claiming_unproven_provenance_must_say_why(self) -> None:
        self.payload["workcell_image"]["note"] = None
        with self.assertRaises(ValidationError):
            PilotManifest.model_validate(self.payload)

    def test_a_source_commit_disagreeing_with_the_image_label_is_refused(self) -> None:
        self.payload["controller_image"]["source_commit"] = "0" * 40
        with self.assertRaises(ValidationError) as caught:
            PilotManifest.model_validate(self.payload)
        self.assertIn("label", str(caught.exception))

    def test_the_controller_image_is_built_from_the_pinned_subject_commit(self) -> None:
        manifest = load_manifest()
        self.assertTrue(manifest.controller_image.provenance_proven)
        self.assertEqual(
            manifest.controller_image.source_commit,
            manifest.subject_implementation_commit,
        )

    def test_the_dependency_closure_names_the_real_implementation(self) -> None:
        manifest = load_manifest()
        names = {
            item.absolute_path.rsplit("/", 1)[-1]
            for item in manifest.server_dependency_closure.hashed_libraries
        }
        self.assertIn("libllama-server-impl.so", names)
        self.assertTrue(any(name.startswith("libggml-cuda") for name in names))
        # The launcher alone identifies almost nothing.
        self.assertLess(manifest.server_dependency_closure.launcher.size_bytes, 100_000)

    def test_a_closure_without_the_server_implementation_is_refused(self) -> None:
        libraries = self.payload["server_dependency_closure"]["hashed_libraries"]
        self.payload["server_dependency_closure"]["hashed_libraries"] = [
            item
            for item in libraries
            if not item["absolute_path"].endswith("libllama-server-impl.so")
        ]
        with self.assertRaises(ValidationError):
            PilotManifest.model_validate(self.payload)


class SamplingHonestyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest()
        self.payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_the_manifest_records_that_no_seed_reaches_the_request(self) -> None:
        sampling = self.manifest.sampling
        self.assertFalse(sampling.seed_reaches_provider_request)
        self.assertIsNone(sampling.provider_request_field)
        self.assertEqual(sampling.model_sampling, "stochastic")
        self.assertEqual(len(sampling.audited_paths), 3)

    def test_seeded_sampling_is_refused_without_a_named_request_field(self) -> None:
        self.payload["sampling"]["model_sampling"] = "seeded"
        with self.assertRaises(ValidationError):
            PilotManifest.model_validate(self.payload)

    def test_claiming_propagation_without_a_field_is_refused(self) -> None:
        self.payload["sampling"]["seed_reaches_provider_request"] = True
        with self.assertRaises(ValidationError):
            PilotManifest.model_validate(self.payload)

    def test_temperature_absence_is_not_translated_into_a_number(self) -> None:
        """An unset temperature is null, never 0.0.

        The resolved `samplingParams` is `{"max_tokens": 16384}`. Writing a
        number here would turn an absence into a setting nobody made, and the
        run would look configured when it is defaulted.
        """

        self.assertIsNone(self.manifest.sampling.temperature)
        self.assertEqual(
            self.manifest.sampling.temperature_state, "unset_provider_default"
        )
        self.assertEqual(
            self.manifest.sampling.sampling_params_observed, {"max_tokens": 16384}
        )

    def test_an_unset_temperature_carrying_a_value_is_refused(self) -> None:
        self.payload["sampling"]["temperature"] = 0.0
        with self.assertRaises(ValidationError):
            PilotManifest.model_validate(self.payload)

    def test_repetitions_are_identities_not_seeds(self) -> None:
        for pair in self.manifest.paired_executions:
            self.assertGreaterEqual(pair.repetition.repetition_identity, 1)
            self.assertNotIn(
                "sampling_seed", pair.repetition.model_dump(mode="json")
            )

    def test_comparison_is_paired_within_repetition_only(self) -> None:
        self.assertEqual(
            self.manifest.sampling.comparability_policy,
            "paired_within_repetition_only",
        )


class RepairAndBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest()

    def test_repairs_cannot_enter_proposal_scoring(self) -> None:
        repair = self.manifest.repair
        self.assertTrue(repair.first_proposal_scored_before_repair)
        self.assertFalse(repair.repair_may_improve_proposal_score)
        self.assertFalse(repair.frontier_repair_allowed_during_scored_phase)
        self.assertFalse(repair.human_repair_allowed_during_scored_phase)
        self.assertTrue(repair.post_score_repair_recorded_as_separate_checkpoint)
        self.assertTrue(repair.proposal_quality_separate_from_delivered_quality)

    def test_no_unrecorded_repair_path_is_permitted(self) -> None:
        self.assertFalse(self.manifest.repair.unrecorded_repair_path_permitted)
        self.assertTrue(self.manifest.repair.enumerated_repair_routes)

    def test_both_arms_get_equivalent_model_spend(self) -> None:
        budgets = self.manifest.budgets
        self.assertTrue(budgets.equivalent_model_spend_opportunity)
        self.assertTrue(budgets.harness_verification_cost_recorded_separately)
        self.assertLessEqual(budgets.max_output_tokens, budgets.context_limit_tokens)

    def test_the_ladder_is_measured_not_derived(self) -> None:
        ladder = self.manifest.threshold_ladder
        self.assertEqual(ladder.auto_tokens, 32_536)
        self.assertEqual(ladder.governing_term, "absolute_ceiling")
        self.assertAlmostEqual(ladder.effective_auto_ratio, 0.4965, places=4)
        # The naive prediction, and why it is not used.
        self.assertNotEqual(ladder.auto_tokens, int(ladder.builtin_pct * 65_536))

    def test_a_ladder_contradicting_its_own_numbers_is_refused(self) -> None:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        payload["threshold_ladder"]["auto_tokens"] = 55_706
        with self.assertRaises(ValidationError):
            PilotManifest.model_validate(payload)


class StopConditionTests(unittest.TestCase):
    def test_every_stop_condition_is_declared(self) -> None:
        manifest = load_manifest()
        self.assertEqual(set(manifest.stop_conditions), set(StopCondition))

    def test_omitting_a_stop_condition_is_refused(self) -> None:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        payload["stop_conditions"] = payload["stop_conditions"][:-1]
        with self.assertRaises(ValidationError):
            PilotManifest.model_validate(payload)


class ColdWarmTests(unittest.TestCase):
    def test_the_readiness_request_was_not_executed_in_this_phase(self) -> None:
        cold_warm = load_manifest().cold_warm
        self.assertTrue(cold_warm.readiness_request_is_inference)
        self.assertFalse(cold_warm.readiness_request_executed_in_this_phase)
        self.assertGreaterEqual(len(cold_warm.steps), 6)


class LockGateTests(unittest.TestCase):
    """These pass in both commits: before the lock exists, and after.

    The manifest commit must leave the rehearsal unauthorised, and the lock
    commit must authorise it. A test that only worked in one of the two would
    have to be edited between them, which is how a gate stops being one.
    """

    def setUp(self) -> None:
        self.manifest = load_manifest()
        self.lock = load_lock()

    def test_a_complete_manifest_without_a_lock_authorises_nothing(self) -> None:
        decision = authorize_rehearsal(self.manifest, None)
        self.assertFalse(decision.authorized)
        self.assertIn("no lock exists", decision.reason)

    def test_the_lock_when_present_authorises_only_the_rehearsal(self) -> None:
        if self.lock is None:
            self.skipTest("no lock yet; this is the manifest commit")
        decision = authorize_rehearsal(self.manifest, self.lock)
        self.assertTrue(decision.authorized, decision.reason)
        self.assertTrue(self.lock.authorises_zero_token_rehearsal)
        self.assertFalse(self.lock.authorises_live_inference)

    def test_manifest_mutation_invalidates_the_lock(self) -> None:
        if self.lock is None:
            self.skipTest("no lock yet; this is the manifest commit")
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        payload["budgets"]["max_output_tokens"] = 8_192
        mutated = PilotManifest.model_validate(payload)
        decision = authorize_rehearsal(mutated, self.lock)
        self.assertFalse(decision.authorized)
        self.assertIn("changed since it was locked", decision.reason)

    def test_the_lock_binds_this_manifest_and_package(self) -> None:
        if self.lock is None:
            self.skipTest("no lock yet; this is the manifest commit")
        self.lock.verify_against(self.manifest)
        self.assertEqual(
            self.lock.crisis_atlas_package_digest,
            self.manifest.crisis_atlas.package_digest,
        )
        self.assertEqual(
            self.lock.subject_implementation_commit,
            self.manifest.subject_implementation_commit,
        )

    def test_a_lock_naming_another_package_is_refused(self) -> None:
        if self.lock is None:
            self.skipTest("no lock yet; this is the manifest commit")
        other = self.lock.model_copy(
            update={"crisis_atlas_package_digest": "a" * 64}
        )
        with self.assertRaises(ValueError):
            other.verify_against(self.manifest)

    def test_the_lock_commit_is_not_the_manifest_commit(self) -> None:
        """Avoids the self-reference a single commit would require.

        A lock naming the hash of the commit that contains it could never be
        written truthfully, so the two are separate commits by construction.
        """

        if self.lock is None:
            self.skipTest("no lock yet; this is the manifest commit")
        head = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if head:
            self.assertNotEqual(self.lock.manifest_commit, head)


class SupersessionTests(unittest.TestCase):
    """The v3 pair is preserved, marked, and never rehearsed."""

    def setUp(self) -> None:
        self.current = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_the_old_manifest_and_lock_still_exist_unedited(self) -> None:
        self.assertTrue(SUPERSEDED_MANIFEST_PATH.is_file())
        self.assertTrue(SUPERSEDED_LOCK_PATH.is_file())
        old = json.loads(SUPERSEDED_MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(old["manifest_id"], "slice7-crisis-atlas-pilot-v4")
        self.assertEqual(old["schema_version"], "2.0")
        # v3, v2 and v1 are still there behind v4. Supersession is a chain, not
        # a swap: each link records a defect that was real when it was locked.
        self.assertTrue(V3_MANIFEST_PATH.is_file())
        self.assertTrue(V3_LOCK_PATH.is_file())
        self.assertEqual(
            json.loads(V3_MANIFEST_PATH.read_text(encoding="utf-8"))["manifest_id"],
            "slice7-crisis-atlas-pilot-v3",
        )
        self.assertTrue(V2_MANIFEST_PATH.is_file())
        self.assertTrue(V2_LOCK_PATH.is_file())
        self.assertEqual(
            json.loads(V2_MANIFEST_PATH.read_text(encoding="utf-8"))["manifest_id"],
            "slice7-crisis-atlas-pilot-v2",
        )
        self.assertTrue(V1_MANIFEST_PATH.is_file())
        self.assertTrue(V1_LOCK_PATH.is_file())
        v1 = json.loads(V1_MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(v1["manifest_id"], "slice7-crisis-atlas-pilot")
        self.assertEqual(v1["schema_version"], "1.0")

    def test_the_new_manifest_records_why_the_old_one_was_invalid(self) -> None:
        block = self.current["supersedes"]
        self.assertEqual(block["status"], "superseded")
        self.assertFalse(block["ever_rehearsed"])
        self.assertFalse(block["ever_authorized_for_live_inference"])
        self.assertTrue(block["preserved_not_edited"])
        self.assertEqual(
            block["manifest_path"],
            "docs/qualification/slice7-crisis-atlas-pilot-manifest-v4.json",
        )
        reasons = " ".join(block["invalid_because"])
        self.assertIn("relay", reasons)

    def test_the_old_lock_cannot_authorise_the_new_manifest(self) -> None:
        old_lock = PilotLock.model_validate_json(
            SUPERSEDED_LOCK_PATH.read_text(encoding="utf-8")
        )
        decision = authorize_rehearsal(load_manifest(), old_lock)
        self.assertFalse(decision.authorized)
        self.assertIn("changed since it was locked", decision.reason)

    def test_the_new_authority_binds_every_verdict_deciding_module(self) -> None:
        authority = self.current["pilot_authority"]
        bound = {item["path"] for item in authority["bound_modules"]}
        for path in (
            "src/apoapsis/qualification/pilot.py",
            "src/apoapsis/qualification/authority.py",
            "src/apoapsis/qualification/rehearsal.py",
            "src/apoapsis/qualification/fake_pilot_provider.py",
        ):
            self.assertIn(path, bound)
        self.assertTrue(authority["fake_provider_script_sha256"])

    def test_the_bound_digests_match_the_bytes_at_the_authority_commit(self) -> None:
        if not (REPO / ".git").exists() or shutil.which("git") is None:
            self.skipTest("not a git checkout")
        from apoapsis.qualification.authority import BoundModule, verify_authority

        authority = self.current["pilot_authority"]
        declared = tuple(
            BoundModule(path=item["path"], sha256=item["sha256"])
            for item in authority["bound_modules"]
        )
        result = verify_authority(
            authority["authority_commit"], declared, repo=REPO
        )
        self.assertTrue(result.satisfied, [f.detail for f in result.findings])

    def test_package_evidence_was_regenerated_not_reused(self) -> None:
        reuse = self.current["package_evidence_reuse"]
        self.assertFalse(reuse["reused"])
        self.assertIn(
            "src/apoapsis/qualification/case_package.py", reuse["changed_modules"]
        )
        self.assertTrue(reuse["all_eight_proofs_passed"])


class SupersededV2CannotAuthoriseAnythingTests(unittest.TestCase):
    """v2 is history. History must not be able to authorise a run.

    Every assertion here is about the *superseded* pair. They exist because the
    v2 documents are still on disk, still parse, and would still look like a
    valid manifest and lock to anything that did not check which pair it was
    holding.
    """

    def setUp(self) -> None:
        self.v2_manifest = PilotManifest.model_validate_json(
            V2_MANIFEST_PATH.read_text(encoding="utf-8")
        )
        self.v2_lock = PilotLock.model_validate_json(
            V2_LOCK_PATH.read_text(encoding="utf-8")
        )
        self.v3_manifest = PilotManifest.model_validate_json(
            V3_MANIFEST_PATH.read_text(encoding="utf-8")
        )
        self.v3_lock = PilotLock.model_validate_json(
            V3_LOCK_PATH.read_text(encoding="utf-8")
        )
        self.v4_manifest = PilotManifest.model_validate_json(
            SUPERSEDED_MANIFEST_PATH.read_text(encoding="utf-8")
        )
        self.v4_lock = PilotLock.model_validate_json(
            SUPERSEDED_LOCK_PATH.read_text(encoding="utf-8")
        )
        self.manifest = load_manifest()

    def test_the_superseded_pair_is_recorded_at_its_real_digests(self) -> None:
        block = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["supersedes"]
        self.assertEqual(block["manifest_digest"], self.v4_manifest.digest())
        self.assertEqual(block["lock_digest"], self.v4_lock.digest())

    def test_no_superseded_lock_can_authorise_the_current_manifest(self) -> None:
        for name, lock in (
            ("v2", self.v2_lock),
            ("v3", self.v3_lock),
            ("v4", self.v4_lock),
        ):
            with self.subTest(lock=name):
                decision = authorize_rehearsal(self.manifest, lock)
                self.assertFalse(decision.authorized)
                self.assertIn("changed since it was locked", decision.reason)

    def test_the_v2_lock_could_not_even_verify_its_own_manifest(self) -> None:
        """Not a v3 problem: the v2 pair never agreed with itself.

        `expected_tool_schema_sha256` was added to `QwenIdentity` after the v2
        lock was written, so the manifest's digest moved underneath a lock that
        had already recorded the old value. A lock that cannot verify the
        document it names never authorised anything.
        """

        with self.assertRaises(ValueError):
            self.v2_lock.verify_against(self.v2_manifest)

    def test_the_v2_thirteen_tool_expectation_is_rejected(self) -> None:
        self.assertEqual(self.v2_manifest.qwen.expected_native_tool_count, 13)
        self.assertIsNone(self.v2_manifest.qwen.expected_tool_schema_sha256)

        self.assertEqual(self.manifest.qwen.expected_native_tool_count, 26)
        self.assertEqual(len(self.manifest.qwen.expected_tool_names), 26)
        self.assertIsNotNone(self.manifest.qwen.expected_tool_schema_sha256)
        # Same image, different surface: the count was wrong, the image was not.
        self.assertEqual(
            self.v2_manifest.qwen.image_digest, self.manifest.qwen.image_digest
        )
        # `tool_search` is disabled in settings and absent from the wire.
        self.assertNotIn("tool_search", self.manifest.qwen.expected_tool_names)
        self.assertFalse(self.manifest.qwen.tool_search_enabled)
        # `web_fetch` is present, and its presence is not the containment claim.
        self.assertIn("web_fetch", self.manifest.qwen.expected_tool_names)

    def _bound(self, manifest, path: str) -> str:
        return {
            item["path"]: item["sha256"]
            for item in manifest.pilot_authority.bound_modules
        }[path]

    def test_each_supersession_changed_the_module_it_blames(self) -> None:
        """Why each pair was superseded, asserted against its own bound bytes.

        v3 bound a `runner.py` whose control 14 raised `TypeError` on its first
        execution; v4 bound a `relay.py` that reported truncated responses as
        successes. Both manifests record that. This checks the records are about
        modules that actually changed, rather than stories told afterwards.
        """

        self.assertNotEqual(
            self._bound(self.v3_manifest, "src/apoapsis/qualification/runner.py"),
            self._bound(self.v4_manifest, "src/apoapsis/qualification/runner.py"),
        )
        self.assertNotEqual(
            self._bound(self.v4_manifest, "src/apoapsis/workcell/relay.py"),
            self._bound(self.manifest, "src/apoapsis/workcell/relay.py"),
        )
        reasons = " ".join(
            json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["supersedes"][
                "invalid_because"
            ]
        )
        self.assertIn("terminal", reasons)

    def test_only_the_modules_that_changed_were_rebound(self) -> None:
        """v5 moves three modules and leaves the other seventeen alone.

        A revision that rebound everything would make "what changed" unreadable
        from the manifests, which is most of what the supersession chain is for.
        """

        v4 = {
            item["path"]: item["sha256"]
            for item in self.v4_manifest.pilot_authority.bound_modules
        }
        v5 = {
            item["path"]: item["sha256"]
            for item in self.manifest.pilot_authority.bound_modules
        }
        self.assertEqual(set(v4), set(v5))
        changed = sorted(path for path in v5 if v4[path] != v5[path])
        self.assertEqual(
            changed,
            [
                "src/apoapsis/qualification/relay_faults.py",
                "src/apoapsis/qualification/slot_driver.py",
                "src/apoapsis/workcell/relay.py",
            ],
        )

    def test_v3_binds_the_modules_v2_left_unbound(self) -> None:
        v2_paths = {item["path"] for item in self.v2_manifest.pilot_authority.bound_modules}
        v3_paths = {item["path"] for item in self.manifest.pilot_authority.bound_modules}
        for path in (
            "src/apoapsis/qualification/runner.py",
            "src/apoapsis/qualification/session_factory.py",
            "src/apoapsis/qualification/slot_driver.py",
            "src/apoapsis/qualification/observation.py",
            "src/apoapsis/qualification/relay_faults.py",
            "src/apoapsis/qualification/fake_provider_server.py",
        ):
            self.assertNotIn(path, v2_paths)
            self.assertIn(path, v3_paths)


class LockV3BindsExactlyWhatItSaysTests(unittest.TestCase):
    """One byte anywhere in the bound set invalidates the authorisation."""

    def setUp(self) -> None:
        self.manifest = load_manifest()
        self.lock = load_lock()
        if self.lock is None:
            self.skipTest("no lock yet; this is the manifest commit")
        if not (REPO / ".git").exists() or shutil.which("git") is None:
            self.skipTest("not a git checkout")

    def test_v3_binds_the_current_fake_provider_bytes(self) -> None:
        from apoapsis.qualification.fake_pilot_provider import script_digest

        authority = self.manifest.pilot_authority
        self.assertIsNotNone(authority)
        self.assertEqual(authority.fake_provider_script_sha256, script_digest())

    def test_changing_one_bound_provider_byte_invalidates_v3(self) -> None:
        from apoapsis.qualification.fake_pilot_provider import (
            SCRIPTS,
            ScriptId,
            script_digest,
        )

        authority = self.manifest.pilot_authority
        original = SCRIPTS[ScriptId.COMPLETE_PROPOSAL]
        SCRIPTS[ScriptId.COMPLETE_PROPOSAL] = (
            original[0].model_copy(update={"finish_reason": "length"}),
        )
        try:
            self.assertNotEqual(authority.fake_provider_script_sha256, script_digest())
        finally:
            SCRIPTS[ScriptId.COMPLETE_PROPOSAL] = original

    def test_changing_one_runner_module_byte_invalidates_v3(self) -> None:
        from apoapsis.qualification.authority import BoundModule, verify_authority

        authority = self.manifest.pilot_authority
        tampered = tuple(
            BoundModule(
                path=item["path"],
                sha256=("0" * 64 if item["path"].endswith("runner.py") else item["sha256"]),
            )
            for item in authority.bound_modules
        )
        result = verify_authority(authority.authority_commit, tampered, repo=REPO)
        self.assertFalse(result.satisfied)
        self.assertIn(
            "src/apoapsis/qualification/runner.py",
            {item.path for item in result.findings},
        )

    def test_manifest_mutation_invalidates_lock_v3(self) -> None:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        payload["qwen"]["qwen_home"] = "/tmp/somewhere-else"
        mutated = PilotManifest.model_validate(payload)
        decision = authorize_rehearsal(mutated, self.lock)
        self.assertFalse(decision.authorized)
        self.assertIn("changed since it was locked", decision.reason)

    def test_lock_v3_authorises_the_rehearsal_only(self) -> None:
        decision = authorize_rehearsal(self.manifest, self.lock)
        self.assertTrue(decision.authorized, decision.reason)
        self.assertTrue(self.lock.authorises_zero_token_rehearsal)
        self.assertFalse(self.lock.authorises_live_inference)
        self.assertIn("live inference is not authorised", decision.reason)
        # There is no field that can turn live inference on.
        with self.assertRaises(ValueError):
            self.lock.model_copy(update={"authorises_live_inference": True}).model_validate(
                {
                    **self.lock.model_dump(mode="json"),
                    "authorises_live_inference": True,
                }
            )


class ExecutableProvenanceTests(unittest.TestCase):
    """The lock must bind the code that decides whether the lock is valid.

    Slice 7P.3's gate found that it did not: `pilot.py` defines `PilotLock`,
    `authorize_rehearsal` and the stop conditions, and was introduced in the
    manifest commit, while the lock named an *earlier* evaluator commit that
    does not contain it. Nothing noticed, because every test imports the module
    from the working tree where it is present and never asks which commit it
    came from.
    """

    def setUp(self) -> None:
        self.lock = load_lock()
        if self.lock is None:
            self.skipTest("no lock yet")
        if not (REPO / ".git").exists():
            self.skipTest("not a git checkout")

    def _contains(self, commit: str, path: str) -> bool:
        return (
            subprocess.run(  # noqa: S603
                ["git", "cat-file", "-e", f"{commit}:{path}"],
                cwd=REPO,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )

    def test_the_evaluator_commit_contains_the_lock_schema_module(self) -> None:
        """Was `expectedFailure` under lock `6eb267d`, and is not any more.

        7P.3 marked this expected-failure rather than deleting or weakening it,
        precisely so it would start passing by itself once the binding was
        corrected. Under the v2 lock it does: `evaluator_framework_commit` now
        names a commit that contains `pilot.py`. The marker is removed rather
        than left in place, because a passing test under `expectedFailure` is
        an *unexpected success* -- a failure with a friendlier name.
        """

        self.assertTrue(
            self._contains(
                self.lock.evaluator_framework_commit,
                "src/apoapsis/qualification/pilot.py",
            ),
            "the lock names an evaluator commit that does not contain the "
            "module defining PilotLock and authorize_rehearsal",
        )

    def test_a_rehearsal_runner_is_now_bound_by_committed_bytes(self) -> None:
        """The rewrite the 7P.3 placeholder asked for.

        That test asserted the runner was *absent*, so it would fail the moment
        a binding appeared and force this rewrite. Note it inspected the lock,
        where the authority does not live; had it stayed, it would have kept
        passing vacuously after the binding landed. Asserting an absence is
        only safe when it is checked where the presence would actually be.
        """

        from apoapsis.qualification.authority import BoundModule, verify_authority

        manifest = load_manifest()
        authority = manifest.pilot_authority
        self.assertIsNotNone(authority, "no pilot authority is bound")
        declared = tuple(
            BoundModule(path=item["path"], sha256=item["sha256"])
            for item in authority.bound_modules
        )
        paths = {item.path for item in declared}
        self.assertIn("src/apoapsis/qualification/rehearsal.py", paths)
        self.assertIn("src/apoapsis/qualification/fake_pilot_provider.py", paths)

        result = verify_authority(
            authority.authority_commit, declared, repo=REPO
        )
        self.assertTrue(result.satisfied, [f.detail for f in result.findings])

    def test_changing_one_runner_byte_invalidates_the_authority(self) -> None:
        from apoapsis.qualification.authority import BoundModule, verify_authority

        authority = load_manifest().pilot_authority
        assert authority is not None
        tampered = tuple(
            BoundModule(
                path=item["path"],
                sha256=("0" * 64 if "rehearsal.py" in item["path"] else item["sha256"]),
            )
            for item in authority.bound_modules
        )
        result = verify_authority(
            authority.authority_commit, tampered, repo=REPO
        )
        self.assertFalse(result.satisfied)
        self.assertIn(
            "src/apoapsis/qualification/rehearsal.py",
            {item.path for item in result.findings},
        )

    def test_changing_the_fake_provider_script_invalidates_authorization(self) -> None:
        from apoapsis.qualification.fake_pilot_provider import (
            SCRIPTS,
            ScriptId,
            script_digest,
        )

        authority = load_manifest().pilot_authority
        assert authority is not None
        self.assertEqual(authority.fake_provider_script_sha256, script_digest())

        original = SCRIPTS[ScriptId.INCOMPLETE_PROPOSAL]
        SCRIPTS[ScriptId.INCOMPLETE_PROPOSAL] = (
            original[0].model_copy(update={"finish_reason": "length"}),
        )
        try:
            self.assertNotEqual(
                authority.fake_provider_script_sha256, script_digest()
            )
        finally:
            SCRIPTS[ScriptId.INCOMPLETE_PROPOSAL] = original


class ExecutionRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest()
        self.lock = load_lock()
        if self.lock is None:
            self.skipTest("no lock yet; this is the manifest commit")

    def test_a_record_naming_this_pilot_is_accepted(self) -> None:
        accept_execution_record(
            {
                "manifest_digest": self.manifest.digest(),
                "lock_digest": self.lock.digest(),
            },
            manifest=self.manifest,
            lock=self.lock,
        )

    def test_a_record_using_another_digest_is_refused(self) -> None:
        with self.assertRaises(ExecutionRecordRefused) as caught:
            accept_execution_record(
                {"manifest_digest": "b" * 64, "lock_digest": self.lock.digest()},
                manifest=self.manifest,
                lock=self.lock,
            )
        self.assertIn("different experiment", str(caught.exception))

    def test_a_record_with_no_digest_is_refused(self) -> None:
        with self.assertRaises(ExecutionRecordRefused):
            accept_execution_record(
                {"lock_digest": self.lock.digest()},
                manifest=self.manifest,
                lock=self.lock,
            )


if __name__ == "__main__":
    unittest.main()
