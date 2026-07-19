from __future__ import annotations

import unittest

from pydantic import ValidationError

from apoapsis.architect.schema import (
    ArchitecturePlan,
    PlanValidationFinding,
    PlanValidationResult,
    ValidationSeverity,
)
from apoapsis.architect.validation import validate_plan
from apoapsis.config import ArchitectPlanCeilings
from tests.architect_helpers import make_plan, make_slice

DEFAULT_CEILINGS = ArchitectPlanCeilings()


def _codes(findings: list[PlanValidationFinding]) -> set[str]:
    return {item.code for item in findings}


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
        self.assertEqual(findings, [])

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
