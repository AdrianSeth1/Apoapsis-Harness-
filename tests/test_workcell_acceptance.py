from __future__ import annotations

import unittest

from apoapsis.workcell.acceptance import (
    AcceptanceObligation,
    CheckpointOutcome,
    ObligationKind,
    ObligationStatus,
    ReadinessBlock,
    SliceAcceptanceContract,
    SliceNotReady,
    SliceReadinessReport,
    evaluate_checkpoint,
    evaluate_slice_readiness,
    new_production_components,
    readiness_packet,
    require_ready,
)
from apoapsis.workcell.delta import (
    CandidateDelta,
    ChangeKind,
    DeltaEntry,
    PathClass,
)
from apoapsis.workcell.witness import (
    CoverageObservation,
    EvidenceClass,
    HttpExchange,
    ProcessObservation,
    StructuredWitness,
    WitnessKind,
    WitnessProblem,
    WitnessRefused,
    require_witness,
    validate_witness,
)

_FP = "a" * 64
_OTHER_FP = "b" * 64


def _delta(*entries: DeltaEntry, fingerprint: str = _FP) -> CandidateDelta:
    return CandidateDelta(candidate_fingerprint=fingerprint, entries=list(entries))


def _added(path: str, path_class: PathClass = PathClass.PRODUCTION) -> DeltaEntry:
    return DeltaEntry(
        path=path,
        kind=ChangeKind.ADDED,
        path_class=path_class,
        candidate_sha256="c" * 64,
        added_lines=20,
    )


def _witness(**overrides) -> StructuredWitness:
    payload = {
        "witness_id": "w1",
        "kind": WitnessKind.TEST_SUITE,
        "evidence_class": EvidenceClass.INDEPENDENT,
        "command_name": "unit-tests",
        "command_version": "1",
        "command_argv": ["python3", "-m", "unittest"],
        "worktree_fingerprint": _FP,
        "passed": True,
        "coverage": CoverageObservation(
            executed_paths=["incident/services/incident_service.py"],
            collection_method="coverage.py 7.6",
        ),
        "criteria_proved": ["C1"],
    }
    payload.update(overrides)
    return StructuredWitness.model_validate(payload)


class WitnessValidationTests(unittest.TestCase):
    def test_a_substantive_witness_is_usable(self) -> None:
        self.assertEqual(validate_witness(_witness(), current_fingerprint=_FP), [])

    def test_a_command_name_alone_is_not_evidence(self) -> None:
        # The Crisis Atlas sentence: a command called `behavioral-integration`
        # exiting zero is an argument from vocabulary.
        bare = _witness(
            witness_id="w-name-only",
            command_name="behavioral-integration",
            coverage=None,
            criteria_proved=[],
        )
        problems = validate_witness(bare, current_fingerprint=_FP)
        self.assertIn(
            WitnessProblem.COMMAND_NAME_ONLY, {item.problem for item in problems}
        )

    def test_a_stale_witness_is_refused(self) -> None:
        problems = validate_witness(_witness(), current_fingerprint=_OTHER_FP)
        self.assertIn(
            WitnessProblem.STALE_FINGERPRINT, {item.problem for item in problems}
        )

    def test_coverage_must_say_how_it_was_collected(self) -> None:
        asserted = _witness(
            coverage=CoverageObservation(
                executed_paths=["a.py"], collection_method=None
            )
        )
        self.assertIn(
            WitnessProblem.NO_COVERAGE_METHOD,
            {item.problem for item in validate_witness(asserted, current_fingerprint=_FP)},
        )

    def test_a_failing_witness_may_not_claim_proof(self) -> None:
        self.assertIn(
            WitnessProblem.FAILED_WITNESS_CLAIMS_PROOF,
            {
                item.problem
                for item in validate_witness(
                    _witness(passed=False), current_fingerprint=_FP
                )
            },
        )

    def test_a_launch_witness_needs_a_process_and_routes(self) -> None:
        with self.assertRaises(ValueError):
            _witness(kind=WitnessKind.LAUNCH_HTTP, process=None)
        with self.assertRaises(ValueError):
            _witness(
                kind=WitnessKind.LAUNCH_HTTP,
                process=ProcessObservation(
                    command=["python3", "server.py"],
                    readiness_condition="port connectable",
                    bound_address="127.0.0.1:8000",
                    cleaned_up=True,
                ),
                exchanges=[],
            )

    def test_a_launched_process_must_be_cleaned_up(self) -> None:
        leaky = _witness(
            kind=WitnessKind.LAUNCH_HTTP,
            process=ProcessObservation(
                command=["python3", "server.py"],
                readiness_condition="port connectable",
                bound_address="127.0.0.1:8000",
                cleaned_up=False,
            ),
            exchanges=[HttpExchange(method="GET", route="/api/incidents", status=200)],
        )
        self.assertIn(
            WitnessProblem.LAUNCH_NOT_CLEANED_UP,
            {item.problem for item in validate_witness(leaky, current_fingerprint=_FP)},
        )

    def test_a_mutation_nobody_read_back_proves_nothing_persisted(self) -> None:
        # Crisis Atlas shipped exactly this shape.
        witness = _witness(
            kind=WitnessKind.LAUNCH_HTTP,
            process=ProcessObservation(
                command=["python3", "server.py"],
                readiness_condition="port connectable",
                bound_address="127.0.0.1:8000",
                cleaned_up=True,
            ),
            exchanges=[
                HttpExchange(
                    method="POST", route="/api/incidents", status=201, mutating=True
                )
            ],
        )
        self.assertIn(
            WitnessProblem.MUTATION_NEVER_RE_READ,
            {item.problem for item in validate_witness(witness, current_fingerprint=_FP)},
        )

    def test_a_mutation_followed_by_a_read_is_accepted(self) -> None:
        witness = _witness(
            kind=WitnessKind.LAUNCH_HTTP,
            process=ProcessObservation(
                command=["python3", "server.py"],
                readiness_condition="port connectable",
                bound_address="127.0.0.1:8000",
                cleaned_up=True,
            ),
            exchanges=[
                HttpExchange(
                    method="POST", route="/api/incidents", status=201, mutating=True
                ),
                HttpExchange(
                    method="GET",
                    route="/api/incidents",
                    status=200,
                    assertions=["body has 1 incident"],
                ),
            ],
        )
        self.assertEqual(validate_witness(witness, current_fingerprint=_FP), [])

    def test_require_witness_fails_closed(self) -> None:
        with self.assertRaises(WitnessRefused):
            require_witness(_witness(), current_fingerprint=_OTHER_FP)


def _crisis_atlas_slice2_contract() -> SliceAcceptanceContract:
    """The obligations Crisis Atlas Slice 2 actually had."""

    return SliceAcceptanceContract(
        slice_id="crisis-atlas-slice-2",
        criteria=["C-INCIDENT-SERVICE", "C-EXPORT-SERVICE"],
        obligations=[
            AcceptanceObligation(
                obligation_id="incident-service",
                kind=ObligationKind.PRODUCTION_ARTIFACT,
                description="IncidentService in its declared package",
                required_paths=["incident/services/incident_service.py"],
                must_be_exercised=["incident/services/incident_service.py"],
                criteria=["C-INCIDENT-SERVICE"],
            ),
            AcceptanceObligation(
                obligation_id="export-service",
                kind=ObligationKind.PRODUCTION_ARTIFACT,
                description="ExportService with deterministic JSON and Markdown",
                required_paths=["incident/services/export_service.py"],
                must_be_exercised=["incident/services/export_service.py"],
                criteria=["C-EXPORT-SERVICE"],
            ),
        ],
        required_commands=["unit-tests"],
    )


class CrisisAtlasSlice2Tests(unittest.TestCase):
    """The Slice 4 exit criterion, reconstructed exactly.

    Qwen proposed one partial file at `services/incident_service.py` -- the
    wrong package path -- created no export service, and wrote no tests. The
    inherited suite stayed green because it never imported the new file.
    Apoapsis read that green as completion.
    """

    def _inherited_green(self) -> StructuredWitness:
        return StructuredWitness(
            witness_id="inherited-suite",
            kind=WitnessKind.TEST_SUITE,
            evidence_class=EvidenceClass.INHERITED,
            command_name="unit-tests",
            command_version="1",
            command_argv=["python3", "-m", "unittest", "discover"],
            worktree_fingerprint=_FP,
            passed=True,
            coverage=CoverageObservation(
                # The seed's own modules, and nothing new. This is the whole
                # point: the suite is green *because* it never reached the
                # new file.
                executed_paths=["incident/domain.py", "incident/persistence.py"],
                collection_method="coverage.py 7.6",
            ),
            detail="46 tests passed",
        )

    def test_the_slice2_proposal_cannot_complete_despite_a_green_suite(self) -> None:
        delta = _delta(_added("services/incident_service.py"))
        report = evaluate_slice_readiness(
            _crisis_atlas_slice2_contract(),
            delta,
            [self._inherited_green()],
            candidate_paths={"services/incident_service.py"},
            passed_commands={"unit-tests"},
        )
        self.assertFalse(report.ready)

        blocks = {item.block for item in report.findings}
        # The declared package path is absent -- the wrong-path service.
        self.assertIn(ReadinessBlock.MISSING_REQUIRED_ARTIFACT, blocks)
        # The export service was never created at all.
        self.assertIn(ReadinessBlock.OBLIGATION_UNPROVED, blocks)
        # And the file that *was* written is reached by nothing.
        self.assertIn(ReadinessBlock.CHANGED_BEHAVIOUR_UNEXERCISED, blocks)
        self.assertEqual(
            report.unexercised_behaviour,
            ["services/incident_service.py::services/incident_service.py"],
        )

        with self.assertRaises(SliceNotReady):
            require_ready(report)

    def test_the_required_command_passing_does_not_rescue_it(self) -> None:
        # ADR 0069's rule was "all configured checks are green". It is green
        # here, and the slice is still not ready.
        delta = _delta(_added("services/incident_service.py"))
        report = evaluate_slice_readiness(
            _crisis_atlas_slice2_contract(),
            delta,
            [self._inherited_green()],
            candidate_paths={"services/incident_service.py"},
            passed_commands={"unit-tests"},
        )
        self.assertNotIn(
            ReadinessBlock.REQUIRED_COMMAND_NOT_PASSED,
            {item.block for item in report.findings},
        )
        self.assertFalse(report.ready)

    def test_the_repair_packet_names_every_missing_thing_at_once(self) -> None:
        delta = _delta(_added("services/incident_service.py"))
        packet = readiness_packet(
            evaluate_slice_readiness(
                _crisis_atlas_slice2_contract(),
                delta,
                [self._inherited_green()],
                candidate_paths={"services/incident_service.py"},
                passed_commands={"unit-tests"},
            )
        )
        self.assertIn("incident/services/incident_service.py", packet)
        self.assertIn("incident/services/export_service.py", packet)
        self.assertIn("services/incident_service.py", packet)

    def test_a_correct_slice2_with_real_witnesses_is_ready(self) -> None:
        # Both services in their declared packages, and a witness whose
        # coverage proves both are reached.
        delta = _delta(
            _added("incident/services/incident_service.py"),
            _added("incident/services/export_service.py"),
            _added("tests/test_services.py", PathClass.TEST),
        )
        witness = StructuredWitness(
            witness_id="focused-suite",
            kind=WitnessKind.TEST_SUITE,
            evidence_class=EvidenceClass.INDEPENDENT,
            command_name="unit-tests",
            command_version="1",
            command_argv=["python3", "-m", "unittest", "discover"],
            worktree_fingerprint=_FP,
            passed=True,
            coverage=CoverageObservation(
                executed_paths=[
                    "incident/services/incident_service.py",
                    "incident/services/export_service.py",
                ],
                collection_method="coverage.py 7.6",
            ),
            criteria_proved=["C-INCIDENT-SERVICE", "C-EXPORT-SERVICE"],
        )
        report = evaluate_slice_readiness(
            _crisis_atlas_slice2_contract(),
            delta,
            [witness],
            candidate_paths={
                "incident/services/incident_service.py",
                "incident/services/export_service.py",
                "tests/test_services.py",
            },
            passed_commands={"unit-tests"},
        )
        self.assertTrue(report.ready, report.detail)
        self.assertEqual(report.unexercised_behaviour, [])


class NewComponentRuleTests(unittest.TestCase):
    def _contract(self) -> SliceAcceptanceContract:
        return SliceAcceptanceContract(
            slice_id="s1",
            criteria=["C1"],
            obligations=[
                AcceptanceObligation(
                    obligation_id="o1",
                    kind=ObligationKind.PRODUCTION_ARTIFACT,
                    description="the new service",
                    required_paths=["app/service.py"],
                    must_be_exercised=["app/service.py"],
                    criteria=["C1"],
                )
            ],
        )

    def test_only_added_production_files_are_new_components(self) -> None:
        delta = _delta(
            _added("app/service.py"),
            _added("tests/test_service.py", PathClass.TEST),
            DeltaEntry(
                path="app/existing.py",
                kind=ChangeKind.MODIFIED,
                path_class=PathClass.PRODUCTION,
                base_sha256="d" * 64,
                candidate_sha256="e" * 64,
            ),
        )
        self.assertEqual(new_production_components(delta), ["app/service.py"])

    def test_merely_adding_a_test_file_is_not_enough(self) -> None:
        # A test file that exists but never imports the component proves
        # nothing about the component.
        delta = _delta(
            _added("app/service.py"), _added("tests/test_service.py", PathClass.TEST)
        )
        witness = StructuredWitness(
            witness_id="w",
            kind=WitnessKind.TEST_SUITE,
            evidence_class=EvidenceClass.MODEL_AUTHORED,
            command_name="unit-tests",
            command_version="1",
            command_argv=["python3", "-m", "unittest"],
            worktree_fingerprint=_FP,
            passed=True,
            coverage=CoverageObservation(
                executed_paths=["tests/test_service.py"],
                collection_method="coverage.py",
            ),
            criteria_proved=["C1"],
        )
        report = evaluate_slice_readiness(
            self._contract(),
            delta,
            [witness],
            candidate_paths={"app/service.py", "tests/test_service.py"},
        )
        self.assertFalse(report.ready)
        self.assertIn(
            "app/service.py::app/service.py", report.unexercised_behaviour
        )

    def test_a_behavioural_witness_through_the_product_boundary_counts(self) -> None:
        delta = _delta(_added("app/service.py"))
        witness = StructuredWitness(
            witness_id="browser",
            kind=WitnessKind.BEHAVIOURAL,
            evidence_class=EvidenceClass.INDEPENDENT,
            command_name="browser-lifecycle",
            command_version="1",
            command_argv=["apoapsis", "verify-web-product"],
            worktree_fingerprint=_FP,
            passed=True,
            coverage=CoverageObservation(
                executed_paths=["app/service.py"], collection_method="import tracer"
            ),
            criteria_proved=["C1"],
        )
        report = evaluate_slice_readiness(
            self._contract(), delta, [witness], candidate_paths={"app/service.py"}
        )
        self.assertTrue(report.ready, report.detail)

    def test_a_stale_witness_cannot_exercise_a_component(self) -> None:
        delta = _delta(_added("app/service.py"))
        stale = StructuredWitness(
            witness_id="stale",
            kind=WitnessKind.TEST_SUITE,
            evidence_class=EvidenceClass.INDEPENDENT,
            command_name="unit-tests",
            command_version="1",
            command_argv=["python3", "-m", "unittest"],
            worktree_fingerprint=_OTHER_FP,
            passed=True,
            coverage=CoverageObservation(
                executed_paths=["app/service.py"], collection_method="coverage.py"
            ),
            criteria_proved=["C1"],
        )
        report = evaluate_slice_readiness(
            self._contract(), delta, [stale], candidate_paths={"app/service.py"}
        )
        self.assertFalse(report.ready)
        self.assertTrue(report.rejected_witnesses)

    def test_model_authored_evidence_cannot_discharge_an_independent_obligation(
        self,
    ) -> None:
        contract = SliceAcceptanceContract(
            slice_id="s1",
            criteria=["C1"],
            obligations=[
                AcceptanceObligation(
                    obligation_id="o1",
                    kind=ObligationKind.TEST_OR_WITNESS,
                    description="independently verified",
                    criteria=["C1"],
                    requires_independent_evidence=True,
                )
            ],
        )
        model_authored = StructuredWitness(
            witness_id="self-test",
            kind=WitnessKind.TEST_SUITE,
            evidence_class=EvidenceClass.MODEL_AUTHORED,
            command_name="unit-tests",
            command_version="1",
            command_argv=["python3", "-m", "unittest"],
            worktree_fingerprint=_FP,
            passed=True,
            coverage=CoverageObservation(
                executed_paths=["app/service.py"], collection_method="coverage.py"
            ),
            criteria_proved=["C1"],
        )
        report = evaluate_slice_readiness(contract, _delta(), [model_authored])
        self.assertFalse(report.ready)


class ContractTests(unittest.TestCase):
    def test_a_criterion_with_no_obligation_is_refused(self) -> None:
        # It could never be proved, and nobody would notice until delivery.
        with self.assertRaises(ValueError):
            SliceAcceptanceContract(
                slice_id="s1",
                criteria=["C1", "C-ORPHAN"],
                obligations=[
                    AcceptanceObligation(
                        obligation_id="o1",
                        kind=ObligationKind.PRODUCTION_ARTIFACT,
                        description="x",
                        required_paths=["a.py"],
                        criteria=["C1"],
                    )
                ],
            )

    def test_an_obligation_that_nothing_could_discharge_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            AcceptanceObligation(
                obligation_id="o1",
                kind=ObligationKind.OPERABILITY,
                description="it should be good",
            )

    def test_an_intentionally_unmeasured_obligation_blocks_completion(self) -> None:
        contract = SliceAcceptanceContract(
            slice_id="s1",
            criteria=["C1"],
            obligations=[
                AcceptanceObligation(
                    obligation_id="o1",
                    kind=ObligationKind.OPERABILITY,
                    description="hardware-dependent launch",
                    criteria=["C1"],
                    unmeasured_reason="no CI machine has the required GPU",
                )
            ],
        )
        report = evaluate_slice_readiness(contract, _delta(), [])
        self.assertFalse(report.ready)
        self.assertEqual(
            report.obligations[0].status, ObligationStatus.INTENTIONALLY_UNMEASURED
        )
        self.assertIn(
            ReadinessBlock.INTENTIONALLY_UNMEASURED,
            {item.block for item in report.findings},
        )

    def test_a_required_command_that_never_passed_blocks(self) -> None:
        contract = SliceAcceptanceContract(
            slice_id="s1",
            criteria=["C1"],
            obligations=[
                AcceptanceObligation(
                    obligation_id="o1",
                    kind=ObligationKind.PRODUCTION_ARTIFACT,
                    description="x",
                    required_paths=["a.py"],
                    criteria=["C1"],
                )
            ],
            required_commands=["unit-tests"],
        )
        witness = _witness(
            criteria_proved=["C1"],
            coverage=CoverageObservation(
                executed_paths=["a.py"], collection_method="coverage.py"
            ),
        )
        report = evaluate_slice_readiness(
            contract, _delta(), [witness], candidate_paths={"a.py"}
        )
        self.assertIn(
            ReadinessBlock.REQUIRED_COMMAND_NOT_PASSED,
            {item.block for item in report.findings},
        )

    def test_the_contract_digest_is_stable_and_content_sensitive(self) -> None:
        first = _crisis_atlas_slice2_contract()
        self.assertEqual(first.digest(), _crisis_atlas_slice2_contract().digest())
        changed = _crisis_atlas_slice2_contract()
        changed.obligations[0].required_paths = ["elsewhere.py"]
        self.assertNotEqual(first.digest(), changed.digest())


if __name__ == "__main__":
    unittest.main()


class CheckpointTests(unittest.TestCase):
    """The replacement for ADR 0069's green-test termination."""

    def _ready(self, ready: bool, *, unmeasured: bool = False):
        from apoapsis.workcell.acceptance import ReadinessFinding

        findings = []
        if unmeasured:
            findings.append(
                ReadinessFinding(
                    block=ReadinessBlock.INTENTIONALLY_UNMEASURED,
                    detail="no CI machine has the required GPU",
                )
            )
        elif not ready:
            findings.append(
                ReadinessFinding(
                    block=ReadinessBlock.CHANGED_BEHAVIOUR_UNEXERCISED,
                    path="app/service.py",
                    detail="nothing reaches app/service.py",
                )
            )
        return SliceReadinessReport(
            slice_id="s1",
            contract_digest="a" * 64,
            ready=ready,
            findings=findings,
            detail="ready" if ready else "not ready",
        )

    def test_admitted_and_ready_completes(self) -> None:
        decision = evaluate_checkpoint(True, "admitted", self._ready(True))
        self.assertEqual(decision.outcome, CheckpointOutcome.COMPLETE)
        self.assertEqual(decision.repair_packet, "")

    def test_admitted_but_not_ready_continues_rather_than_completing(self) -> None:
        # The outcome Crisis Atlas Slice 2 never got. The agent is given
        # another turn to finish its own stated plan instead of the harness
        # declaring the slice done on its behalf.
        decision = evaluate_checkpoint(True, "admitted", self._ready(False))
        self.assertEqual(decision.outcome, CheckpointOutcome.CONTINUE)
        self.assertIn("app/service.py", decision.repair_packet)

    def test_a_refused_candidate_never_completes(self) -> None:
        decision = evaluate_checkpoint(False, "forbidden path", self._ready(True))
        self.assertEqual(decision.outcome, CheckpointOutcome.CANDIDATE_REFUSED)

    def test_an_intentionally_unmeasured_obligation_goes_to_a_human(self) -> None:
        # Not something the agent can fix by trying harder; sending it back
        # for repair would loop.
        decision = evaluate_checkpoint(
            True, "admitted", self._ready(False, unmeasured=True)
        )
        self.assertEqual(decision.outcome, CheckpointOutcome.HUMAN_REVIEW_REQUIRED)

    def test_there_is_no_path_to_complete_from_green_commands_alone(self) -> None:
        # `evaluate_checkpoint` takes no command results at all: greenness is
        # an input to readiness, several layers down, and cannot reach here.
        import inspect

        parameters = set(inspect.signature(evaluate_checkpoint).parameters)
        self.assertEqual(
            parameters, {"admission_admitted", "admission_detail", "readiness"}
        )
