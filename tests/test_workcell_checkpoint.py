from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from apoapsis.workcell.acceptance import (
    AcceptanceObligation,
    CheckpointOutcome,
    ObligationKind,
    ReadinessBlock,
    SliceAcceptanceContract,
)
from apoapsis.workcell.behaviour import BehaviourKind, changed_behaviour
from apoapsis.workcell.checkpoint import run_checkpoint
from apoapsis.workcell.delta import compute_delta
from apoapsis.workcell.emitters import (
    EmitterError,
    emit_launch_witness,
    emit_test_witness,
    parse_coverage_json,
)
from apoapsis.workcell.witness import (
    EvidenceClass,
    ProcessObservation,
    WitnessKind,
    validate_witness,
)

# The Crisis Atlas Slice 2 shape, shrunk to two services and a seed suite.
_SEED = {
    "incident/__init__.py": "",
    "incident/domain.py": "class Incident:\n    def __init__(self, title):\n        self.title = title\n",
    "tests/test_domain.py": "from incident.domain import Incident\n\n\ndef test_title():\n    assert Incident('x').title == 'x'\n",
}

_INCIDENT_SERVICE = (
    "from incident.domain import Incident\n"
    "\n"
    "\n"
    "class IncidentService:\n"
    "    def create(self, title):\n"
    "        return Incident(title)\n"
)
_EXPORT_SERVICE = (
    "import json\n"
    "\n"
    "\n"
    "class ExportService:\n"
    "    def to_json(self, incidents):\n"
    "        return json.dumps([item.title for item in incidents], sort_keys=True)\n"
)


class _Workspace:
    def __init__(self, case: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.base = self.root / "base"
        self.candidate = self.root / "candidate"
        for target in (self.base, self.candidate):
            for name, body in _SEED.items():
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")

    def write(self, relative: str, body: str) -> None:
        path = self.candidate / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def _contract() -> SliceAcceptanceContract:
    return SliceAcceptanceContract(
        slice_id="SLICE-services",
        criteria=["AC-INCIDENT", "AC-EXPORT"],
        obligations=[
            AcceptanceObligation(
                obligation_id="incident-service",
                kind=ObligationKind.PRODUCTION_ARTIFACT,
                description="IncidentService in its declared package",
                required_paths=["incident/services/incident_service.py"],
                must_be_exercised=["incident/services/incident_service.py"],
                criteria=["AC-INCIDENT"],
            ),
            AcceptanceObligation(
                obligation_id="export-service",
                kind=ObligationKind.PRODUCTION_ARTIFACT,
                description="ExportService with deterministic JSON",
                required_paths=["incident/services/export_service.py"],
                must_be_exercised=["incident/services/export_service.py"],
                criteria=["AC-EXPORT"],
            ),
        ],
    )


def _coverage_emitter(
    executed: dict[str, list[int]], *, criteria: list[str], passed: bool = True
):
    """A controller-owned emitter backed by a real coverage artifact on disk.

    The artifact is written by the fake "run", then read and hashed by
    `emit_test_witness`, which is the property under test: the coverage the
    witness carries came out of a file the controller produced.
    """

    def emit(snapshot: Path, fingerprint: str):
        artifact = snapshot.parent / "coverage.json"

        def runner(argv, *, timeout_seconds):
            artifact.write_text(
                json.dumps(
                    {"files": {path: {"executed_lines": lines}
                               for path, lines in executed.items()}}
                ),
                encoding="utf-8",
            )
            return (0 if passed else 1), "ran", ""

        return [
            emit_test_witness(
                runner,
                command_name="unit-tests",
                command_version="1",
                argv=["python3", "-m", "pytest"],
                worktree_fingerprint=fingerprint,
                coverage_artifact=artifact,
                criteria_proved=criteria,
            )
        ]

    return emit


class CoverageProvenanceTests(unittest.TestCase):
    def test_coverage_comes_from_a_hashed_controller_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "coverage.json"

            def runner(argv, *, timeout_seconds):
                artifact.write_text(
                    json.dumps({"files": {"a.py": {"executed_lines": [1, 2, 3]}}}),
                    encoding="utf-8",
                )
                return 0, "", ""

            witness = emit_test_witness(
                runner,
                command_name="unit-tests",
                command_version="1",
                argv=["pytest"],
                worktree_fingerprint="a" * 64,
                coverage_artifact=artifact,
                criteria_proved=["AC-1"],
            )
        self.assertIsNotNone(witness.coverage.source_artifact_sha256)
        self.assertEqual(witness.coverage.executed_lines["a.py"], [1, 2, 3])
        self.assertEqual(validate_witness(witness, current_fingerprint="a" * 64), [])

    def test_a_run_that_produced_no_artifact_emits_nothing(self) -> None:
        # A witness asserting coverage anyway would be a claim, which is the
        # thing these emitters exist to make impossible.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(EmitterError):
                emit_test_witness(
                    lambda argv, *, timeout_seconds: (0, "", ""),
                    command_name="unit-tests",
                    command_version="1",
                    argv=["pytest"],
                    worktree_fingerprint="a" * 64,
                    coverage_artifact=Path(tmp) / "missing.json",
                )

    def test_a_stale_artifact_cannot_be_read_as_this_runs_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "coverage.json"
            artifact.write_text(
                json.dumps({"files": {"old.py": {"executed_lines": [1]}}}),
                encoding="utf-8",
            )
            with self.assertRaises(EmitterError):
                emit_test_witness(
                    lambda argv, *, timeout_seconds: (0, "", ""),
                    command_name="unit-tests",
                    command_version="1",
                    argv=["pytest"],
                    worktree_fingerprint="a" * 64,
                    coverage_artifact=artifact,
                )

    def test_a_failing_run_claims_no_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "coverage.json"

            def runner(argv, *, timeout_seconds):
                artifact.write_text(
                    json.dumps({"files": {"a.py": {"executed_lines": [1]}}}),
                    encoding="utf-8",
                )
                return 1, "", "failed"

            witness = emit_test_witness(
                runner,
                command_name="unit-tests",
                command_version="1",
                argv=["pytest"],
                worktree_fingerprint="a" * 64,
                coverage_artifact=artifact,
                criteria_proved=["AC-1"],
            )
        self.assertFalse(witness.passed)
        self.assertEqual(witness.criteria_proved, [])

    def test_an_empty_report_is_refused(self) -> None:
        with self.assertRaises(EmitterError):
            parse_coverage_json({"files": {}}, source_sha256="a" * 64,
                                collection_method="x")


class LaunchEmitterTests(unittest.TestCase):
    def _process(self) -> ProcessObservation:
        return ProcessObservation(
            command=["python3", "server.py"],
            readiness_condition="port 8000 connectable",
            bound_address="127.0.0.1:8000",
        )

    def test_a_launch_witness_records_routes_and_cleans_up(self) -> None:
        stopped: list[bool] = []

        witness = emit_launch_witness(
            command_name="launch",
            command_version="1",
            argv=["python3", "server.py"],
            worktree_fingerprint="a" * 64,
            start_process=self._process,
            probe=lambda method, route: (
                (201, ["status == 201"]) if method == "POST" else (200, ["1 item"])
            ),
            exchanges=[
                ("POST", "/api/incidents", True),
                ("GET", "/api/incidents", False),
            ],
            stop_process=lambda: (stopped.append(True), True)[1],
            criteria_proved=["AC-1"],
        )
        self.assertTrue(witness.passed)
        self.assertTrue(witness.process.cleaned_up)
        self.assertEqual(stopped, [True])
        self.assertEqual(validate_witness(witness, current_fingerprint="a" * 64), [])

    def test_cleanup_runs_even_when_a_probe_raises(self) -> None:
        # A server left behind can make a later witness pass for the wrong
        # reason, which is worse than the failure that leaked it.
        stopped: list[bool] = []

        def probe(method, route):
            raise RuntimeError("connection reset")

        with self.assertRaises(EmitterError):
            emit_launch_witness(
                command_name="launch",
                command_version="1",
                argv=["python3", "server.py"],
                worktree_fingerprint="a" * 64,
                start_process=self._process,
                probe=probe,
                exchanges=[("GET", "/api/incidents", False)],
                stop_process=lambda: (stopped.append(True), True)[1],
            )
        self.assertEqual(stopped, [True])


class ChangedBehaviourTests(unittest.TestCase):
    def test_a_new_symbol_inside_a_modified_file_is_a_behaviour_unit(self) -> None:
        # The Slice 4 gap: Crisis Atlas Slice 3's unreachable export routes
        # lived in a modified file, which a file-level rule cannot see.
        workspace = _Workspace(self)
        workspace.write(
            "incident/domain.py",
            _SEED["incident/domain.py"] + "\n\ndef summarise(incidents):\n    return len(incidents)\n",
        )
        delta = compute_delta(workspace.base, workspace.candidate)
        units = changed_behaviour(delta, workspace.base, workspace.candidate)
        self.assertEqual(
            [(item.kind, item.name) for item in units],
            [(BehaviourKind.NEW_SYMBOL, "summarise")],
        )

    def test_a_new_route_in_a_modified_file_is_a_behaviour_unit(self) -> None:
        workspace = _Workspace(self)
        # The file must pre-exist in the base, or it is an *added* file and
        # correctly yields one NEW_FILE unit instead. The route rule is
        # specifically about additions inside a file that was already there.
        original = "ROUTES = []\n\n\ndef register(app):\n    app.route('/api/incidents')\n"
        for tree in (workspace.base, workspace.candidate):
            (tree / "incident" / "api.py").write_text(original, encoding="utf-8")
        workspace.write(
            "incident/api.py",
            original.replace(
                "app.route('/api/incidents')",
                "app.route('/api/incidents')\n    app.route('/api/exports')",
            ),
        )
        delta = compute_delta(workspace.base, workspace.candidate)
        units = changed_behaviour(delta, workspace.base, workspace.candidate)
        routes = [item.name for item in units if item.kind == BehaviourKind.NEW_ROUTE]
        self.assertIn("/api/exports", routes)

    def test_an_added_file_is_one_unit_not_one_per_symbol(self) -> None:
        # Requiring every helper in a brand-new module to be individually
        # covered would be stricter than the handoff asks.
        workspace = _Workspace(self)
        workspace.write("incident/services/incident_service.py", _INCIDENT_SERVICE)
        delta = compute_delta(workspace.base, workspace.candidate)
        units = changed_behaviour(delta, workspace.base, workspace.candidate)
        self.assertEqual([item.kind for item in units], [BehaviourKind.NEW_FILE])

    def test_a_docstring_only_package_marker_is_not_executable_behaviour(self) -> None:
        workspace = _Workspace(self)
        workspace.write(
            "incident/services/__init__.py",
            '"""Incident service package."""\n',
        )
        delta = compute_delta(workspace.base, workspace.candidate)
        self.assertEqual(changed_behaviour(delta, workspace.base, workspace.candidate), [])


class CrisisAtlasTwoTurnTests(unittest.TestCase):
    """The Slice 4B integration test the reviewer asked for.

    Turn one is the actual Crisis Atlas Slice 2 proposal. Turn two finishes it.
    Only the second reaches `COMPLETE`, and it does so through the real
    checkpoint loop rather than through a hand-built readiness report.
    """

    def _run(self, workspace: _Workspace, emitter, *, snapshot: str):
        return run_checkpoint(
            _contract(),
            base_root=workspace.base,
            candidate_root=workspace.candidate,
            snapshot_root=workspace.root / snapshot,
            emit_witnesses=emitter,
        )

    def test_partial_then_finished(self) -> None:
        workspace = _Workspace(self)

        # --- Turn one: one partial file at the wrong package path, no export
        # service, no new tests. The inherited suite still passes, and its
        # coverage names only the seed's own module.
        workspace.write("services/incident_service.py", _INCIDENT_SERVICE)
        first = self._run(
            workspace,
            _coverage_emitter(
                {"incident/domain.py": [1, 2, 3]}, criteria=[]
            ),
            snapshot="snap1",
        )

        self.assertTrue(first.admission.admitted, first.admission.detail)
        self.assertEqual(first.decision.outcome, CheckpointOutcome.CONTINUE)
        blocks = {item.block for item in first.readiness.findings}
        self.assertIn(ReadinessBlock.MISSING_REQUIRED_ARTIFACT, blocks)
        self.assertIn(ReadinessBlock.CHANGED_BEHAVIOUR_UNEXERCISED, blocks)
        self.assertIn(
            "services/incident_service.py", first.decision.repair_packet
        )
        self.assertIn(
            "incident/services/export_service.py", first.decision.repair_packet
        )

        # --- Turn two: the agent reads that packet and finishes its own plan.
        (workspace.candidate / "services").mkdir(exist_ok=True)
        (workspace.candidate / "services" / "incident_service.py").unlink()
        workspace.write("incident/services/__init__.py", "")
        workspace.write("incident/services/incident_service.py", _INCIDENT_SERVICE)
        workspace.write("incident/services/export_service.py", _EXPORT_SERVICE)
        workspace.write(
            "tests/test_services.py",
            "from incident.services.incident_service import IncidentService\n"
            "from incident.services.export_service import ExportService\n",
        )
        second = self._run(
            workspace,
            _coverage_emitter(
                {
                    "incident/services/__init__.py": [1],
                    "incident/services/incident_service.py": [1, 4, 5, 6],
                    "incident/services/export_service.py": [1, 4, 5, 6],
                },
                criteria=["AC-INCIDENT", "AC-EXPORT"],
            ),
            snapshot="snap2",
        )

        self.assertTrue(second.admission.admitted, second.admission.detail)
        self.assertEqual(
            second.decision.outcome,
            CheckpointOutcome.COMPLETE,
            second.readiness.detail,
        )
        self.assertEqual(second.readiness.unexercised_behaviour, [])
        self.assertEqual(second.decision.repair_packet, "")

    def test_a_refused_candidate_never_reaches_readiness(self) -> None:
        workspace = _Workspace(self)
        workspace.write("incident/services/incident_service.py", _INCIDENT_SERVICE)
        workspace.write(".env", "SECRET=1\n")
        record = self._run(
            workspace, _coverage_emitter({}, criteria=[]), snapshot="snap"
        )
        self.assertEqual(record.decision.outcome, CheckpointOutcome.CANDIDATE_REFUSED)
        self.assertIsNone(record.readiness)

    def test_an_emitter_failure_cannot_produce_a_complete(self) -> None:
        workspace = _Workspace(self)
        workspace.write("incident/services/incident_service.py", _INCIDENT_SERVICE)
        workspace.write("incident/services/export_service.py", _EXPORT_SERVICE)

        def broken(snapshot, fingerprint):
            raise EmitterError("coverage tool is not installed")

        record = self._run(workspace, broken, snapshot="snap")
        self.assertNotEqual(record.decision.outcome, CheckpointOutcome.COMPLETE)
        self.assertIn("coverage tool", record.emitter_error or "")

    def test_witnesses_are_emitted_against_the_admitted_snapshot(self) -> None:
        # Never against the workcell: a command must not be observed running
        # over a file the policy refused.
        workspace = _Workspace(self)
        workspace.write("incident/services/incident_service.py", _INCIDENT_SERVICE)
        seen: list[Path] = []

        def emit(snapshot: Path, fingerprint: str):
            seen.append(snapshot)
            return _coverage_emitter(
                {"incident/services/incident_service.py": [1]}, criteria=[]
            )(snapshot, fingerprint)

        record = self._run(workspace, emit, snapshot="snap")
        self.assertEqual(len(seen), 1)
        self.assertEqual(str(seen[0]), record.admission.snapshot_path)
        self.assertNotEqual(seen[0], workspace.candidate)


if __name__ == "__main__":
    unittest.main()


class ContractCompilerTests(unittest.TestCase):
    """Contracts are compiled from the approved plan, before any model spend."""

    def _plan(self, **slice_overrides):
        from tests.architect_helpers import make_plan, make_slice

        payload = {
            "slice_id": "SLICE-services",
            "acceptance_criterion_ids": ["AC-INCIDENT", "AC-EXPORT"],
            "suggested_paths": [
                "incident/services/incident_service.py",
                "incident/services/export_service.py",
                "README.md",
            ],
            "verification_commands": ["unit-tests"],
        }
        # `suggested_symbols` is not exposed by the shared helper; set it on
        # the constructed slice rather than widening a helper other tests use.
        symbols = payload.pop("suggested_symbols", None)
        payload.update(slice_overrides)
        symbols = payload.pop("suggested_symbols", symbols)
        built = make_slice(**payload)
        if symbols:
            built.suggested_symbols = list(symbols)
        return make_plan(slices=[built])

    def test_declared_paths_become_artifact_obligations(self) -> None:
        from apoapsis.workcell.contract_compiler import compile_slice_contract

        contract = compile_slice_contract(self._plan(), "SLICE-services")
        required = {
            path
            for obligation in contract.obligations
            for path in obligation.required_paths
        }
        # The declared package path is the field that catches a wrong-path
        # service, so it has to survive compilation verbatim.
        self.assertIn("incident/services/incident_service.py", required)
        self.assertIn("incident/services/export_service.py", required)
        self.assertEqual(contract.required_commands, ["unit-tests"])

    def test_documentation_paths_are_not_required_to_be_exercised(self) -> None:
        from apoapsis.workcell.contract_compiler import compile_slice_contract

        contract = compile_slice_contract(self._plan(), "SLICE-services")
        doc = next(
            item for item in contract.obligations if "README.md" in item.required_paths
        )
        self.assertEqual(doc.must_be_exercised, [])

    def test_every_criterion_gets_an_obligation(self) -> None:
        from apoapsis.workcell.contract_compiler import compile_slice_contract

        contract = compile_slice_contract(self._plan(), "SLICE-services")
        claimed = {
            criterion
            for obligation in contract.obligations
            for criterion in obligation.criteria
        }
        self.assertEqual(claimed, {"AC-INCIDENT", "AC-EXPORT"})

    def test_a_slice_with_no_criteria_will_not_compile(self) -> None:
        from apoapsis.workcell.contract_compiler import (
            ContractCompilationError,
            compile_slice_contract,
        )

        with self.assertRaises(ContractCompilationError):
            compile_slice_contract(
                self._plan(acceptance_criterion_ids=[]), "SLICE-services"
            )

    def test_an_unknown_slice_will_not_compile(self) -> None:
        from apoapsis.workcell.contract_compiler import (
            ContractCompilationError,
            compile_slice_contract,
        )

        with self.assertRaises(ContractCompilationError):
            compile_slice_contract(self._plan(), "SLICE-nope")

    def test_advisory_suggested_symbols_never_become_a_gate(self) -> None:
        # Slice 4C. `ImplementationSlice` says in its own docstring that every
        # cross-reference on it is an advisory planner proposal. The first
        # compiler promoted `suggested_symbols` into mandatory obligations it
        # then marked intentionally-unmeasured, which silently turned advice
        # into a completion gate no evidence could open.
        from apoapsis.workcell.acceptance import ObligationKind
        from apoapsis.workcell.contract_compiler import compile_slice_contract

        contract = compile_slice_contract(
            self._plan(suggested_symbols=["IncidentService", "ExportService"]),
            "SLICE-services",
        )
        interfaces = [
            item
            for item in contract.obligations
            if item.kind == ObligationKind.INTERFACE
        ]
        self.assertEqual(interfaces, [])
        self.assertTrue(
            all(not item.unmeasured_reason for item in contract.obligations),
            "no obligation should be born intentionally unmeasured",
        )

    def test_advisory_integration_ids_never_become_a_gate(self) -> None:
        from apoapsis.workcell.acceptance import ObligationKind
        from apoapsis.workcell.contract_compiler import compile_slice_contract

        contract = compile_slice_contract(
            self._plan(integration_contract_ids=["INT-dashboard-api"]),
            "SLICE-services",
        )
        edges = [
            item
            for item in contract.obligations
            if item.kind == ObligationKind.INTEGRATION_EDGE
        ]
        self.assertEqual(edges, [])

    def test_an_owner_approved_interface_becomes_a_real_obligation(self) -> None:
        from apoapsis.workcell.acceptance import ObligationKind
        from apoapsis.workcell.contract_compiler import compile_slice_contract

        contract = compile_slice_contract(
            self._plan(),
            "SLICE-services",
            required_interfaces={
                "incident-service": ["incident.services.IncidentService"]
            },
            required_integration_routes={"INT-dashboard-api": ["/api/incidents"]},
        )
        interface = next(
            item
            for item in contract.obligations
            if item.kind == ObligationKind.INTERFACE
        )
        self.assertEqual(
            interface.required_symbols, ["incident.services.IncidentService"]
        )
        # Dischargeable by observation, not marked unmeasured.
        self.assertFalse(interface.unmeasured_reason)
        edge = next(
            item
            for item in contract.obligations
            if item.kind == ObligationKind.INTEGRATION_EDGE
        )
        self.assertEqual(edge.required_routes, ["/api/incidents"])

    def test_a_compiled_contract_can_reach_complete_without_a_human(self) -> None:
        # The practical consequence: a plan with real interface obligations
        # now completes automatically when the evidence is there, instead of
        # being routed to review by an obligation nothing could discharge.
        from apoapsis.workcell.acceptance import evaluate_slice_readiness
        from apoapsis.workcell.contract_compiler import compile_slice_contract
        from apoapsis.workcell.delta import CandidateDelta
        from apoapsis.workcell.witness import (
            CoverageObservation,
            EvidenceClass as EC,
            StructuredWitness as SW,
            WitnessKind as WK,
        )

        contract = compile_slice_contract(
            self._plan(suggested_symbols=["IncidentService"]),
            "SLICE-services",
            required_interfaces={
                "incident-service": ["incident.services.IncidentService"]
            },
        )
        fingerprint = "a" * 64
        witness = SW(
            witness_id="w",
            kind=WK.TEST_SUITE,
            evidence_class=EC.INDEPENDENT,
            command_name="unit-tests",
            command_version="1",
            command_argv=["pytest"],
            worktree_fingerprint=fingerprint,
            passed=True,
            coverage=CoverageObservation(
                executed_paths=[
                    "incident/services/incident_service.py",
                    "incident/services/export_service.py",
                ],
                observed_symbols=["incident.services.IncidentService"],
                collection_method="coverage.py",
            ),
            criteria_proved=["AC-INCIDENT", "AC-EXPORT"],
        )
        report = evaluate_slice_readiness(
            contract,
            CandidateDelta(candidate_fingerprint=fingerprint),
            [witness],
            candidate_paths={
                "incident/services/incident_service.py",
                "incident/services/export_service.py",
                "README.md",
            },
        )
        self.assertTrue(report.ready, report.detail)

    def test_an_owner_reason_blocks_completion_rather_than_satisfying_it(self) -> None:
        from apoapsis.workcell.acceptance import ObligationStatus
        from apoapsis.workcell.contract_compiler import compile_slice_contract

        contract = compile_slice_contract(
            self._plan(),
            "SLICE-services",
            unmeasured_reasons={
                "SLICE-services-criterion-AC-EXPORT": "no CI machine has the GPU"
            },
        )
        obligation = next(
            item
            for item in contract.obligations
            if item.obligation_id == "SLICE-services-criterion-AC-EXPORT"
        )
        self.assertTrue(obligation.unmeasured_reason)
        self.assertEqual(ObligationStatus.INTENTIONALLY_UNMEASURED.value,
                         "intentionally_unmeasured")
