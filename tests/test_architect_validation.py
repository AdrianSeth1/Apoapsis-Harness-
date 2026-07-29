from __future__ import annotations

import unittest

from pydantic import ValidationError

from apoapsis.architect.schema import (
    ArchitectureComponent,
    ArchitecturePlan,
    EndToEndScenario,
    IntegrationContract,
    PlanDeliveryContract,
    PlanValidationFinding,
    PlanValidationResult,
    RuntimeBoundary,
    ValidationSeverity,
    VerificationStrategy,
)
from apoapsis.architect.validation import validate_plan
from apoapsis.config import ArchitectPlanCeilings
from apoapsis.specification.schema import (
    AcceptanceCriterion,
    HardConstraint,
    SourceKind,
)
from apoapsis.verification.runner import VerificationCommand
from tests.architect_helpers import make_plan, make_slice

# Mirrors the [architect.ceilings] block `apoapsis init` actually writes
# (DEFAULT_CONFIG in src/apoapsis/cli/app.py, ADR 0049) -- NOT the bare
# ArchitecturePlanCeilings() Pydantic defaults, which intentionally stay
# at the pre-ADR-0049 numbers (ADR 0049 Decision 5: no schema change).
DEFAULT_CEILINGS = ArchitectPlanCeilings(
    max_criteria_per_slice=20, max_work_brief_chars=3500
)


def _codes(findings: list[PlanValidationFinding]) -> set[str]:
    return {item.code for item in findings}


def _command(name: str, *argv: str, acceptance: bool = False) -> VerificationCommand:
    return VerificationCommand(
        name=name,
        category="tests",
        argv=list(argv) or ["python", "-m", "unittest"],
        acceptance=acceptance,
    )


class PlanCrossConsistencyTests(unittest.TestCase):
    """ADR 0074 integration-obligation validation.

    Every check here reads structured fields. None of them reads
    `interface`, `data_flow`, `objective`, or any other prose -- that is the
    whole reason `IntegrationContract.runtime_boundary` exists, and ADR
    0073's keyword-based criterion warning is deliberately advisory and
    absent from these gates.
    """

    def _components(self) -> list[ArchitectureComponent]:
        return [
            ArchitectureComponent(
                component_id="COMP-API",
                name="Incident API",
                responsibility="Serve and persist incidents.",
            ),
            ArchitectureComponent(
                component_id="COMP-UI",
                name="Dashboard",
                responsibility="Render incidents in the browser.",
            ),
        ]

    def _contract(
        self, boundary: RuntimeBoundary = RuntimeBoundary.SAME_ORIGIN_HTTP
    ) -> IntegrationContract:
        return IntegrationContract(
            contract_id="INT-1",
            producer_component_id="COMP-API",
            consumer_component_ids=["COMP-UI"],
            interface="GET /incidents",
            data_flow="Dashboard reads incidents from the API.",
            error_behavior="Renders an error banner on a non-2xx response.",
            verification_obligation="A browser-to-API round trip is exercised.",
            runtime_boundary=boundary,
        )

    def test_a_plan_with_no_whole_project_command_is_an_error(self) -> None:
        plan = make_plan(verification_strategy=VerificationStrategy())
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("MISSING_WHOLE_PROJECT_VERIFICATION", _codes(findings))

    def test_an_integration_contract_no_slice_builds_is_an_error(self) -> None:
        plan = make_plan(
            components=self._components(),
            integration_contracts=[self._contract()],
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("UNASSIGNED_INTEGRATION_CONTRACT", _codes(findings))

    def test_a_contract_assigned_to_a_slice_is_accepted(self) -> None:
        plan = make_plan(
            slices=[make_slice("SLICE-1", integration_contract_ids=["INT-1"])],
            components=self._components(),
            integration_contracts=[self._contract()],
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertNotIn("UNASSIGNED_INTEGRATION_CONTRACT", _codes(findings))

    def test_a_required_artifact_no_slice_produces_is_an_error(self) -> None:
        plan = make_plan(
            delivery_contract=PlanDeliveryContract(
                primary_documentation_path="README.md",
                launch_not_runnable_reason="Library change; nothing to launch.",
                required_artifacts=["docs/OPERATIONS.md", "src/example.py"],
            )
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        # `src/example.py` and `README.md` are in the default slice's
        # suggested_paths; `docs/OPERATIONS.md` is assigned to nothing.
        artifact_findings = [
            item for item in findings if item.code == "UNASSIGNED_DELIVERY_ARTIFACT"
        ]
        self.assertEqual(len(artifact_findings), 1)
        self.assertIn("docs/OPERATIONS.md", artifact_findings[0].message)

    def test_an_end_to_end_scenario_with_no_command_is_an_error(self) -> None:
        plan = make_plan(
            verification_strategy=VerificationStrategy(
                whole_project_verification_commands=["unit-tests"],
                end_to_end_scenarios=[
                    EndToEndScenario(
                        scenario_id="E2E-1",
                        setup="Start the product.",
                        behavior="Create an incident in the browser.",
                        expected_result="It survives a reload.",
                    )
                ],
            )
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("UNVERIFIED_END_TO_END_SCENARIO", _codes(findings))

    def test_a_scenario_proven_only_by_a_slice_command_is_an_error(self) -> None:
        """A scenario spans slices by definition, so a command that only
        ever runs inside one slice's isolated worktree cannot prove it."""

        plan = make_plan(
            verification_strategy=VerificationStrategy(
                whole_project_verification_commands=["integration-check"],
                end_to_end_scenarios=[
                    EndToEndScenario(
                        scenario_id="E2E-1",
                        setup="Start the product.",
                        behavior="Create an incident in the browser.",
                        expected_result="It survives a reload.",
                        verification_commands=["unit-tests"],
                    )
                ],
            )
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests", "integration-check"},
            ceilings=DEFAULT_CEILINGS,
        )
        scenario_findings = [
            item for item in findings if item.code == "UNVERIFIED_END_TO_END_SCENARIO"
        ]
        self.assertEqual(len(scenario_findings), 1)
        self.assertIn("whole_project_verification_commands", scenario_findings[0].message)

    def test_a_scenario_mapped_to_a_whole_project_command_is_accepted(self) -> None:
        plan = make_plan(
            verification_strategy=VerificationStrategy(
                whole_project_verification_commands=["integration-check"],
                end_to_end_scenarios=[
                    EndToEndScenario(
                        scenario_id="E2E-1",
                        setup="Start the product.",
                        behavior="Create an incident in the browser.",
                        expected_result="It survives a reload.",
                        verification_commands=["integration-check"],
                    )
                ],
            )
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests", "integration-check"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertNotIn("UNVERIFIED_END_TO_END_SCENARIO", _codes(findings))


class OperabilityContractTests(PlanCrossConsistencyTests):
    """ADR 0076. A deliverable application must identify where an operator
    reads first, and must either have a launch path that can be exercised or
    an explicit owner-written reason it cannot.

    Every check reads structured fields. `launch_or_usage_instructions` and
    the other prose fields are never inspected -- Apoapsis reproduces them in
    the usage guide and never executes them.
    """

    def _contract_kwargs(self, **overrides):
        base = {
            "primary_documentation_path": "README.md",
            "launch_not_runnable_reason": "Library change; nothing to launch.",
        }
        base.update(overrides)
        return base

    def test_a_plan_with_no_primary_documentation_is_an_error(self) -> None:
        plan = make_plan(
            delivery_contract=PlanDeliveryContract(
                launch_not_runnable_reason="Nothing to launch."
            )
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("MISSING_PRIMARY_DOCUMENTATION", _codes(findings))

    def test_an_unsafe_documentation_path_is_an_error(self) -> None:
        plan = make_plan(
            delivery_contract=PlanDeliveryContract(
                **self._contract_kwargs(primary_documentation_path="../escape.md")
            )
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("UNSAFE_PRIMARY_DOCUMENTATION_PATH", _codes(findings))

    def test_documentation_no_slice_writes_is_an_error(self) -> None:
        """Naming a README nobody is responsible for updating is how a seed
        README survives to delivery."""

        plan = make_plan(
            delivery_contract=PlanDeliveryContract(
                **self._contract_kwargs(primary_documentation_path="docs/GUIDE.md")
            )
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        unassigned = [
            item for item in findings if item.code == "UNASSIGNED_DELIVERY_ARTIFACT"
        ]
        self.assertEqual(len(unassigned), 1)
        self.assertIn("docs/GUIDE.md", unassigned[0].message)

    def test_neither_launch_command_nor_reason_is_an_error(self) -> None:
        plan = make_plan(
            delivery_contract=PlanDeliveryContract(
                primary_documentation_path="README.md"
            )
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("MISSING_LAUNCH_CONTRACT", _codes(findings))

    def test_both_launch_command_and_reason_is_an_error(self) -> None:
        plan = make_plan(
            delivery_contract=PlanDeliveryContract(
                primary_documentation_path="README.md",
                launch_verification_command="unit-tests",
                launch_not_runnable_reason="Also cannot launch.",
            )
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("AMBIGUOUS_LAUNCH_CONTRACT", _codes(findings))

    def test_an_explicit_reason_alone_is_accepted(self) -> None:
        """The escape hatch is real. It requires the owner to write down why,
        which is the point: an unmeasured launch becomes a visible statement
        instead of silence."""

        findings = validate_plan(
            make_plan(),
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertEqual(findings, [])

    def test_a_launch_command_must_be_configured(self) -> None:
        plan = make_plan(
            delivery_contract=PlanDeliveryContract(
                primary_documentation_path="README.md",
                launch_verification_command="launch-smoke",
            )
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("UNKNOWN_VERIFICATION_COMMAND", _codes(findings))

    def test_a_launch_command_must_run_against_the_integrated_project(self) -> None:
        """A launch check confined to one slice's isolated worktree proves
        nothing about the delivered product."""

        plan = make_plan(
            delivery_contract=PlanDeliveryContract(
                primary_documentation_path="README.md",
                launch_verification_command="launch-smoke",
            ),
            verification_strategy=VerificationStrategy(
                whole_project_verification_commands=["unit-tests"],
            ),
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests", "launch-smoke"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("LAUNCH_COMMAND_NOT_WHOLE_PROJECT", _codes(findings))

    def test_a_whole_project_launch_command_is_accepted(self) -> None:
        plan = make_plan(
            delivery_contract=PlanDeliveryContract(
                primary_documentation_path="README.md",
                launch_verification_command="launch-smoke",
            ),
            verification_strategy=VerificationStrategy(
                whole_project_verification_commands=["launch-smoke"],
            ),
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests", "launch-smoke"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertEqual(findings, [])


class NetworkedIntegrationNeedsEndToEndProofTests(PlanCrossConsistencyTests):
    """ADR 0076's structural answer to "no offline-mode behaviour".

    The harness cannot detect seed data, a demo-only path, or an offline
    fallback generically -- that needs the prose inference barred from gates.
    What it can do is refuse to let a contract that crosses an origin at
    runtime exist with nothing but static evidence behind it.
    """

    def _plan(self, *, scenario_command: str | None):
        scenarios = []
        if scenario_command is not None:
            scenarios.append(
                EndToEndScenario(
                    scenario_id="E2E-1",
                    setup="Start the product.",
                    behavior="Create an incident in the browser.",
                    expected_result="It survives a reload.",
                    verification_commands=[scenario_command],
                )
            )
        return make_plan(
            slices=[make_slice("SLICE-1", integration_contract_ids=["INT-1"])],
            components=self._components(),
            integration_contracts=[self._contract(RuntimeBoundary.SAME_ORIGIN_HTTP)],
            verification_strategy=VerificationStrategy(
                whole_project_verification_commands=["integration-check"],
                end_to_end_scenarios=scenarios,
            ),
        )

    def _validate(self, plan, *, acceptance: bool):
        return validate_plan(
            plan,
            configured_verification_commands={"unit-tests", "integration-check"},
            ceilings=DEFAULT_CEILINGS,
            configured_commands=[
                _command("unit-tests"),
                _command("integration-check", acceptance=acceptance),
            ],
        )

    def test_a_networked_contract_without_any_scenario_is_an_error(self) -> None:
        findings = self._validate(
            self._plan(scenario_command=None), acceptance=True
        )
        self.assertIn("INTEGRATION_WITHOUT_END_TO_END_PROOF", _codes(findings))

    def test_a_scenario_proven_by_a_non_acceptance_command_is_not_enough(self) -> None:
        findings = self._validate(
            self._plan(scenario_command="integration-check"), acceptance=False
        )
        self.assertIn("INTEGRATION_WITHOUT_END_TO_END_PROOF", _codes(findings))

    def test_an_acceptance_designated_whole_project_scenario_satisfies_it(self) -> None:
        findings = self._validate(
            self._plan(scenario_command="integration-check"), acceptance=True
        )
        self.assertNotIn("INTEGRATION_WITHOUT_END_TO_END_PROOF", _codes(findings))

    def test_an_in_process_contract_needs_no_end_to_end_scenario(self) -> None:
        plan = make_plan(
            slices=[make_slice("SLICE-1", integration_contract_ids=["INT-1"])],
            components=self._components(),
            integration_contracts=[self._contract(RuntimeBoundary.IN_PROCESS)],
        )
        findings = self._validate(plan, acceptance=True)
        self.assertNotIn("INTEGRATION_WITHOUT_END_TO_END_PROOF", _codes(findings))

    def test_without_command_argv_the_check_is_silent(self) -> None:
        findings = validate_plan(
            self._plan(scenario_command=None),
            configured_verification_commands={"unit-tests", "integration-check"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertNotIn("INTEGRATION_WITHOUT_END_TO_END_PROOF", _codes(findings))


class IntegrationVersusVerificationContradictionTests(PlanCrossConsistencyTests):
    """The Crisis Atlas contradiction, detected structurally.

    The plan required a browser-to-local-API integration and configured a
    check that forbade the mechanism. Both statements were true, neither was
    machine-readable together, and nothing noticed until the model resolved
    the impossibility by deleting the integration.
    """

    def _plan(
        self, boundary: RuntimeBoundary, *, commands: list[str] | None = None
    ) -> ArchitecturePlan:
        return make_plan(
            slices=[
                make_slice(
                    "SLICE-1",
                    integration_contract_ids=["INT-1"],
                    verification_commands=commands or ["web-product"],
                )
            ],
            components=self._components(),
            integration_contracts=[self._contract(boundary)],
            verification_strategy=VerificationStrategy(
                whole_project_verification_commands=["integration-check"]
            ),
        )

    def test_same_origin_integration_versus_forbidden_runtime_networking(self) -> None:
        plan = self._plan(RuntimeBoundary.SAME_ORIGIN_HTTP)
        findings = validate_plan(
            plan,
            configured_verification_commands={"web-product", "integration-check"},
            ceilings=DEFAULT_CEILINGS,
            configured_commands=[
                _command(
                    "web-product",
                    "python",
                    "-m",
                    "apoapsis",
                    "verify-web-product",
                    "--forbid-runtime-network-apis",
                ),
                _command("integration-check"),
            ],
        )
        contradiction = [
            item
            for item in findings
            if item.code == "INTEGRATION_FORBIDDEN_BY_VERIFICATION"
        ]
        self.assertEqual(len(contradiction), 1)
        self.assertIn("INT-1", contradiction[0].message)
        self.assertIn("same_origin_http", contradiction[0].message)
        self.assertIn("--forbid-runtime-network-apis", contradiction[0].message)
        self.assertEqual(contradiction[0].severity, ValidationSeverity.ERROR)

    def test_same_origin_integration_is_fine_with_forbidden_external_resources(
        self,
    ) -> None:
        """ADR 0073's narrowed flag no longer forbids same-origin
        communication, so this combination is not a contradiction. If this
        test ever fails, the two ADRs have drifted apart."""

        plan = self._plan(RuntimeBoundary.SAME_ORIGIN_HTTP)
        findings = validate_plan(
            plan,
            configured_verification_commands={"web-product", "integration-check"},
            ceilings=DEFAULT_CEILINGS,
            configured_commands=[
                _command(
                    "web-product",
                    "python",
                    "-m",
                    "apoapsis",
                    "verify-web-product",
                    "--forbid-external-resources",
                ),
                _command("integration-check"),
            ],
        )
        self.assertNotIn("INTEGRATION_FORBIDDEN_BY_VERIFICATION", _codes(findings))

    def test_cross_origin_integration_versus_forbidden_external_resources(self) -> None:
        plan = self._plan(RuntimeBoundary.CROSS_ORIGIN_HTTP)
        findings = validate_plan(
            plan,
            configured_verification_commands={"web-product", "integration-check"},
            ceilings=DEFAULT_CEILINGS,
            configured_commands=[
                _command(
                    "web-product",
                    "python",
                    "-m",
                    "apoapsis",
                    "verify-web-product",
                    "--forbid-external-resources",
                ),
                _command("integration-check"),
            ],
        )
        self.assertIn("INTEGRATION_FORBIDDEN_BY_VERIFICATION", _codes(findings))

    def test_a_whole_project_command_also_governs_the_contradiction(self) -> None:
        """The forbidding flag need not be on the slice's own command: a
        whole-project command runs against the same integrated product."""

        plan = self._plan(
            RuntimeBoundary.SAME_ORIGIN_HTTP, commands=["unit-tests"]
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests", "integration-check"},
            ceilings=DEFAULT_CEILINGS,
            configured_commands=[
                _command("unit-tests"),
                _command(
                    "integration-check",
                    "python",
                    "-m",
                    "apoapsis",
                    "verify-web-product",
                    "--forbid-runtime-network-apis",
                ),
            ],
        )
        self.assertIn("INTEGRATION_FORBIDDEN_BY_VERIFICATION", _codes(findings))

    def test_an_unspecified_boundary_asserts_nothing(self) -> None:
        """A planner that did not fill the field in is not claiming a
        mechanism, and inventing one for it would be exactly the prose
        inference this field replaces."""

        plan = self._plan(RuntimeBoundary.UNSPECIFIED)
        findings = validate_plan(
            plan,
            configured_verification_commands={"web-product", "integration-check"},
            ceilings=DEFAULT_CEILINGS,
            configured_commands=[
                _command(
                    "web-product",
                    "python",
                    "-m",
                    "apoapsis",
                    "verify-web-product",
                    "--forbid-runtime-network-apis",
                ),
                _command("integration-check"),
            ],
        )
        self.assertNotIn("INTEGRATION_FORBIDDEN_BY_VERIFICATION", _codes(findings))

    def test_in_process_and_filesystem_boundaries_are_never_contradicted(self) -> None:
        for boundary in (
            RuntimeBoundary.IN_PROCESS,
            RuntimeBoundary.FILESYSTEM,
            RuntimeBoundary.SUBPROCESS,
        ):
            with self.subTest(boundary=boundary):
                findings = validate_plan(
                    self._plan(boundary),
                    configured_verification_commands={
                        "web-product",
                        "integration-check",
                    },
                    ceilings=DEFAULT_CEILINGS,
                    configured_commands=[
                        _command(
                            "web-product",
                            "python",
                            "-m",
                            "apoapsis",
                            "verify-web-product",
                            "--forbid-runtime-network-apis",
                        ),
                        _command("integration-check"),
                    ],
                )
                self.assertNotIn(
                    "INTEGRATION_FORBIDDEN_BY_VERIFICATION", _codes(findings)
                )

    def test_without_command_argv_the_contradiction_check_is_silent(self) -> None:
        """Absent information is not evidence of a contradiction: a caller
        that only has command names gets the other checks and not this one."""

        plan = self._plan(RuntimeBoundary.SAME_ORIGIN_HTTP)
        findings = validate_plan(
            plan,
            configured_verification_commands={"web-product", "integration-check"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertNotIn("INTEGRATION_FORBIDDEN_BY_VERIFICATION", _codes(findings))


class ValidatePlanTests(unittest.TestCase):
    def test_valid_plan_has_no_findings(self) -> None:
        plan = make_plan()
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertEqual(findings, [])

    def test_two_valid_tool_orderings_of_the_same_dependency_graph_both_validate(
        self,
    ) -> None:
        plan = make_plan(
            slices=[
                make_slice("SLICE-1", dependencies=[]),
                # Keeps its inherited criteria: this test is about
                # dependency ordering, and a criterion-less slice is
                # independently invalid (MISSING_ACCEPTANCE_CRITERIA),
                # which would mask what is being asserted here.
                make_slice(
                    "SLICE-2",
                    dependencies=["SLICE-1"],
                    inherited_constraint_ids=[],
                ),
            ]
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertEqual(findings, [])

    def test_slice_without_acceptance_criteria_is_an_error(self) -> None:
        """A slice nobody can judge must not reach approval.

        Regression: PLAN-ACB549CF6F2A/SLICE-001 shipped with
        acceptance_criterion_ids == [], so its coding agent was given a
        work brief and a test command but no statement of what that
        command had to prove.
        """

        plan = make_plan(slices=[make_slice("SLICE-1", acceptance_criterion_ids=[])])
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("MISSING_ACCEPTANCE_CRITERIA", _codes(findings))
        self.assertTrue(
            any(
                item.severity == ValidationSeverity.ERROR
                and item.code == "MISSING_ACCEPTANCE_CRITERIA"
                and item.slice_id == "SLICE-1"
                for item in findings
            )
        )

    def test_slice_without_test_obligations_is_an_error(self) -> None:
        plan = make_plan(slices=[make_slice("SLICE-1", test_obligations=[])])
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("MISSING_TEST_OBLIGATIONS", _codes(findings))

    def test_naming_a_verification_command_does_not_satisfy_either_rule(self) -> None:
        """Naming `unit-tests` is not a definition of done: an empty suite
        passes it. Both rules must still fire."""

        plan = make_plan(
            slices=[
                make_slice(
                    "SLICE-1",
                    acceptance_criterion_ids=[],
                    test_obligations=[],
                    verification_commands=["unit-tests"],
                )
            ]
        )
        codes = _codes(
            validate_plan(
                plan,
                configured_verification_commands={"unit-tests"},
                ceilings=DEFAULT_CEILINGS,
            )
        )
        self.assertIn("MISSING_ACCEPTANCE_CRITERIA", codes)
        self.assertIn("MISSING_TEST_OBLIGATIONS", codes)
        self.assertNotIn("MISSING_VERIFICATION_INTENT", codes)

    def test_dependency_cycle_detected(self) -> None:
        plan = make_plan(
            slices=[
                make_slice("SLICE-1", dependencies=["SLICE-2"]),
                make_slice(
                    "SLICE-2",
                    dependencies=["SLICE-1"],
                    inherited_constraint_ids=[],
                    acceptance_criterion_ids=[],
                ),
            ]
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("DEPENDENCY_CYCLE", _codes(findings))

    def test_missing_dependency_detected(self) -> None:
        plan = make_plan(slices=[make_slice("SLICE-1", dependencies=["SLICE-99"])])
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("MISSING_DEPENDENCY", _codes(findings))

    def test_duplicate_slice_ids_detected(self) -> None:
        plan = make_plan(
            slices=[
                make_slice("SLICE-1"),
                make_slice("SLICE-1", inherited_constraint_ids=[], acceptance_criterion_ids=[]),
            ]
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("DUPLICATE_ID", _codes(findings))

    def test_unknown_verification_command_rejected(self) -> None:
        plan = make_plan(
            slices=[make_slice("SLICE-1", verification_commands=["rm -rf /"])]
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("UNKNOWN_VERIFICATION_COMMAND", _codes(findings))

    def test_missing_verification_intent_detected(self) -> None:
        plan = make_plan(slices=[make_slice("SLICE-1", verification_commands=[])])
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("MISSING_VERIFICATION_INTENT", _codes(findings))

    def test_unknown_constraint_and_criterion_references_detected(self) -> None:
        plan = make_plan(
            slices=[
                make_slice(
                    "SLICE-1",
                    inherited_constraint_ids=["HC-999"],
                    acceptance_criterion_ids=["AC-999"],
                )
            ]
        )
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("UNKNOWN_CONSTRAINT_REFERENCE", _codes(findings))
        self.assertIn("UNKNOWN_CRITERION_REFERENCE", _codes(findings))

    def test_unrepresented_active_hard_constraint_detected(self) -> None:
        plan = make_plan(slices=[make_slice("SLICE-1", inherited_constraint_ids=[])])
        findings = validate_plan(
            plan,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn("UNREPRESENTED_HARD_CONSTRAINT", _codes(findings))

    def test_path_escape_rejected(self) -> None:
        for unsafe in ["../outside.py", "/etc/passwd", "C:/Windows/system.ini"]:
            with self.subTest(path=unsafe):
                plan = make_plan(
                    slices=[make_slice("SLICE-1", suggested_paths=[unsafe])]
                )
                findings = validate_plan(
                    plan,
                    configured_verification_commands={"unit-tests"},
                    ceilings=DEFAULT_CEILINGS,
                )
                self.assertIn("UNSAFE_SUGGESTED_PATH", _codes(findings))

    def test_excessive_slice_count_rejected(self) -> None:
        ceilings = ArchitectPlanCeilings(max_slices=1)
        plan = make_plan(
            slices=[
                make_slice("SLICE-1"),
                make_slice(
                    "SLICE-2", inherited_constraint_ids=[], acceptance_criterion_ids=[]
                ),
            ]
        )
        findings = validate_plan(
            plan, configured_verification_commands={"unit-tests"}, ceilings=ceilings
        )
        self.assertIn("TOO_MANY_SLICES", _codes(findings))

    def test_dependency_depth_ceiling_enforced(self) -> None:
        ceilings = ArchitectPlanCeilings(max_dependency_depth=1)
        plan = make_plan(
            slices=[
                make_slice("SLICE-1", dependencies=[]),
                make_slice(
                    "SLICE-2",
                    dependencies=["SLICE-1"],
                    inherited_constraint_ids=[],
                    acceptance_criterion_ids=[],
                ),
                make_slice(
                    "SLICE-3",
                    dependencies=["SLICE-2"],
                    inherited_constraint_ids=[],
                    acceptance_criterion_ids=[],
                ),
            ]
        )
        findings = validate_plan(
            plan, configured_verification_commands={"unit-tests"}, ceilings=ceilings
        )
        self.assertIn("DEPENDENCY_DEPTH_EXCEEDED", _codes(findings))

    def test_per_slice_ceilings_enforced(self) -> None:
        ceilings = ArchitectPlanCeilings(
            max_suggested_paths_per_slice=1,
            max_criteria_per_slice=1,
            max_work_brief_chars=100,
        )
        slice_ = make_slice(
            "SLICE-1",
            suggested_paths=["a.py", "b.py"],
            inherited_constraint_ids=["HC-1"],
            acceptance_criterion_ids=["AC-1"],
        ).model_copy(update={"work_brief": "x" * 200})
        plan = make_plan(slices=[slice_])
        findings = validate_plan(
            plan, configured_verification_commands={"unit-tests"}, ceilings=ceilings
        )
        codes = _codes(findings)
        self.assertIn("TOO_MANY_SUGGESTED_PATHS", codes)
        self.assertIn("TOO_MANY_CRITERIA", codes)
        self.assertIn("WORK_BRIEF_TOO_LONG", codes)

    # ADR 0049: the new default `max_criteria_per_slice = 20` (paired
    # with a 3,500-char work brief) lets a slice with up to 20 distinct
    # criteria and constraints validate cleanly using the *live*
    # `DEFAULT_CEILINGS` (whose value tracks `DEFAULT_CONFIG` from
    # `src/apoapsis/cli/app.py`), while a slice with 21 still fails
    # closed with `TOO_MANY_CRITERIA`. This pins the actual user-visible
    # behavior of `apoapsis init`, not an overridden ceiling.
    #
    # ``n`` is the *total* combined constraint+criterion count that
    # ``validate_plan``'s ``criteria_count`` check sees -- one fixed
    # inherited constraint (HC-1) plus ``n - 1`` acceptance criteria, so
    # ``n=20`` really does land exactly on the ceiling instead of one
    # past it.
    @staticmethod
    def _constrained_plan_with_n_criteria(
        n: int,
    ) -> tuple[ArchitecturePlan, list[str]]:
        criterion_ids = [f"AC-{index}" for index in range(1, n)]
        criteria = [
            AcceptanceCriterion(
                id=item,
                text=f"Criterion {item}",
                source=SourceKind.USER,
                source_reference="idea",
            )
            for item in criterion_ids
        ]
        constraints = [
            HardConstraint(
                id="HC-1",
                text="Preserve the current public API.",
                verbatim_source="Preserve the current public API.",
                interpreted_meaning="Do not change public signatures.",
                source=SourceKind.USER,
                source_reference="idea",
                verification_method="unit-tests",
            )
        ]
        plan = make_plan(
            hard_constraints=constraints,
            acceptance_criteria=criteria,
            slices=[
                make_slice(
                    "SLICE-1",
                    inherited_constraint_ids=["HC-1"],
                    acceptance_criterion_ids=criterion_ids,
                )
            ],
        )
        return plan, criterion_ids

    def test_default_criteria_ceiling_accepts_20_and_rejects_21(self) -> None:
        plan_at_ceiling, _ = self._constrained_plan_with_n_criteria(20)
        findings_at_ceiling = validate_plan(
            plan_at_ceiling,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertNotIn(
            "TOO_MANY_CRITERIA",
            _codes(findings_at_ceiling),
            "a slice with exactly 20 distinct criteria must validate cleanly "
            "against the ADR 0049 default ceiling",
        )

        plan_above_ceiling, _ = self._constrained_plan_with_n_criteria(21)
        findings_above_ceiling = validate_plan(
            plan_above_ceiling,
            configured_verification_commands={"unit-tests"},
            ceilings=DEFAULT_CEILINGS,
        )
        self.assertIn(
            "TOO_MANY_CRITERIA",
            _codes(findings_above_ceiling),
            "a slice with 21 criteria must still exceed the ADR 0049 default "
            "ceiling so the validation guard fails closed",
        )


class PlanValidationResultTests(unittest.TestCase):
    def test_valid_flag_must_match_findings(self) -> None:
        error = PlanValidationFinding(
            severity=ValidationSeverity.ERROR, code="X", message="bad"
        )
        with self.assertRaises(ValidationError):
            PlanValidationResult(
                plan_id="PLAN-1", plan_version=1, valid=True, findings=[error]
            )
        with self.assertRaises(ValidationError):
            PlanValidationResult(
                plan_id="PLAN-1", plan_version=1, valid=False, findings=[]
            )
        # Both directions correctly agreeing must construct without error.
        PlanValidationResult(
            plan_id="PLAN-1", plan_version=1, valid=False, findings=[error]
        )
        PlanValidationResult(plan_id="PLAN-1", plan_version=1, valid=True, findings=[])


class PlanAuthorityBoundaryTests(unittest.TestCase):
    def test_plan_cannot_smuggle_a_status_or_approval_field(self) -> None:
        payload = make_plan().model_dump(mode="json")
        payload["status"] = "approved"
        with self.assertRaises(ValidationError):
            ArchitecturePlan.model_validate(payload)

    def test_plan_cannot_smuggle_an_execution_field(self) -> None:
        payload = make_plan().model_dump(mode="json")
        payload["execute_now"] = True
        with self.assertRaises(ValidationError):
            ArchitecturePlan.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
