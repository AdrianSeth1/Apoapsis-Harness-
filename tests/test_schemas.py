from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from apoapsis.context.provenance import ContextEvidence, EvidenceKind
from apoapsis.models.base import (
    ConstraintCoverage,
    ConstraintDisposition,
    ModelOperation,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from apoapsis.verification.results import (
    VerificationCommandResult,
    VerificationResult,
    VerificationStatus,
)
from tests.helpers import make_constraint, make_specification


class TaskSpecificationTests(unittest.TestCase):
    def test_hard_constraint_preserves_verbatim_source(self) -> None:
        exact = "  Preserve the current public API -- including aliases.\t"
        constraint = make_constraint(text=exact)
        specification = make_specification(constraints=[constraint])

        restored = type(specification).model_validate_json(
            specification.model_dump_json()
        )

        self.assertEqual(restored.hard_constraints[0].verbatim_source, exact)
        self.assertEqual(restored.active_hard_constraints, [constraint])

    def test_duplicate_constraint_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "must be unique"):
            make_specification(
                constraints=[make_constraint(), make_constraint()]
            )


class ContextEvidenceTests(unittest.TestCase):
    def test_digest_is_derived_and_location_is_validated(self) -> None:
        evidence = ContextEvidence(
            evidence_id="EV-1",
            kind=EvidenceKind.FILE_EXCERPT,
            path="src/jobs.py",
            start_line=8,
            end_line=12,
            commit="abc123",
            reason_included="Owns persisted download state.",
            content="state = 'running'\n",
        )
        expected = hashlib.sha256(evidence.content.encode()).hexdigest()
        self.assertEqual(evidence.content_sha256, expected)

        with self.assertRaisesRegex(ValidationError, "provided together"):
            ContextEvidence(
                evidence_id="EV-2",
                kind=EvidenceKind.FILE_EXCERPT,
                path="src/jobs.py",
                start_line=8,
                commit="abc123",
                reason_included="Relevant state.",
                content="x",
            )

    def test_incorrect_digest_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "does not match"):
            ContextEvidence(
                evidence_id="EV-1",
                kind=EvidenceKind.FILE_EXCERPT,
                path="src/jobs.py",
                commit="abc123",
                reason_included="Relevant state.",
                content="x",
                content_sha256="0" * 64,
            )


class ModelSchemaTests(unittest.TestCase):
    def test_model_request_fails_closed_without_constraint_coverage(self) -> None:
        constraint = make_constraint()
        specification = make_specification(constraints=[constraint])

        with self.assertRaisesRegex(ValidationError, "coverage disposition"):
            ModelRequest(
                request_id="MRQ-1",
                task_id=specification.task_id,
                operation=ModelOperation.REVIEW_PATCH,
                provider="frontier",
                model="example",
                specification=specification,
                active_constraints=[constraint],
                requested_output="review",
            )

    def test_model_request_and_response_round_trip(self) -> None:
        constraint = make_constraint()
        specification = make_specification(constraints=[constraint])
        request = ModelRequest(
            request_id="MRQ-1",
            task_id=specification.task_id,
            operation=ModelOperation.REVIEW_PATCH,
            provider="frontier",
            model="example",
            specification=specification,
            active_constraints=[constraint],
            constraint_coverage=[
                ConstraintCoverage(
                    constraint_id=constraint.id,
                    disposition=ConstraintDisposition.INCLUDED,
                    reason="Included verbatim in the request.",
                )
            ],
            requested_output="review",
        )
        response = ModelResponse(
            response_id="MRS-1",
            request_id=request.request_id,
            provider=request.provider,
            model=request.model,
            operation=request.operation,
            content="No violations found.",
            usage=TokenUsage(input_tokens=10, output_tokens=4),
            finish_reason="stop",
        )

        restored = ModelResponse.model_validate_json(response.model_dump_json())
        self.assertEqual(restored.usage.input_tokens, 10)
        self.assertEqual(restored.operation, ModelOperation.REVIEW_PATCH)


class VerificationSchemaTests(unittest.TestCase):
    def test_passing_aggregate_rejects_failed_required_command(self) -> None:
        now = datetime.now(timezone.utc)
        command = VerificationCommandResult(
            name="tests",
            category="tests",
            argv=["python", "-m", "unittest"],
            cwd=".",
            status=VerificationStatus.FAILED,
            exit_code=1,
            started_at=now,
            finished_at=now,
            duration_seconds=0,
        )
        with self.assertRaisesRegex(ValidationError, "aggregate cannot pass"):
            VerificationResult(
                task_id="TASK-TEST-001",
                status=VerificationStatus.PASSED,
                commands=[command],
                started_at=now,
                finished_at=now,
                duration_seconds=0,
            )


class PastedJsonTests(unittest.TestCase):
    """ADR 0067: a chat interface reliably wraps JSON in a code fence, which
    puts a backtick at character 0 and defeats parsing entirely."""

    def test_a_backtick_fence_is_removed(self) -> None:
        from apoapsis.specification.pasted_json import parse_pasted_json

        # The exact shape observed live on 2026-07-26: a single backtick and
        # a language tag, which produced
        # "Expecting value: line 1 column 1 (char 0)".
        self.assertEqual(
            parse_pasted_json('`json\n{"kind": "plan"}\n`'), {"kind": "plan"}
        )
        self.assertEqual(
            parse_pasted_json('```json\n{"kind": "plan"}\n```'), {"kind": "plan"}
        )
        self.assertEqual(
            parse_pasted_json('```\n{"kind": "plan"}\n```'), {"kind": "plan"}
        )

    def test_a_byte_order_mark_is_removed(self) -> None:
        from apoapsis.specification.pasted_json import parse_pasted_json

        self.assertEqual(parse_pasted_json('﻿{"kind": "plan"}'), {"kind": "plan"})

    def test_plain_json_is_unchanged(self) -> None:
        from apoapsis.specification.pasted_json import parse_pasted_json

        self.assertEqual(parse_pasted_json('  {"kind": "plan"}  '), {"kind": "plan"})

    def test_prose_is_not_guessed_at_and_the_error_shows_the_text(self) -> None:
        from apoapsis.specification.pasted_json import (
            PastedJsonError,
            parse_pasted_json,
        )

        with self.assertRaises(PastedJsonError) as raised:
            parse_pasted_json('Here is the plan:\n{"kind": "plan"}')
        message = str(raised.exception)
        # Never scan forward for the first brace -- that is guessing at intent.
        self.assertIn("not valid JSON", message)
        self.assertIn("it starts:", message)
        self.assertIn("Here is the plan:", message)

    def test_a_fence_containing_nothing_is_reported_as_empty(self) -> None:
        from apoapsis.specification.pasted_json import (
            PastedJsonError,
            parse_pasted_json,
        )

        with self.assertRaisesRegex(PastedJsonError, "empty after removing"):
            parse_pasted_json("```json\n\n```")


class JsonSkeletonTests(unittest.TestCase):
    """ADR 0066: the handoff's literal shape is derived from the models, so it
    cannot drift away from what validation actually enforces."""

    def test_skeleton_keys_match_the_model_exactly_at_every_level(self) -> None:
        from apoapsis.architect.schema import ArchitecturePlan
        from apoapsis.discovery.schema import FrontierPlanningResponseEnvelope
        from apoapsis.specification.skeleton import json_skeleton

        skeleton = json_skeleton(FrontierPlanningResponseEnvelope)
        self.assertEqual(
            set(skeleton),
            set(FrontierPlanningResponseEnvelope.model_fields),
        )
        self.assertEqual(set(skeleton["plan"]), set(ArchitecturePlan.model_fields))

    def test_nested_objects_are_expanded_rather_than_referenced(self) -> None:
        from apoapsis.architect.schema import (
            PlanDeliveryContract,
            RuntimeDesign,
            VerificationStrategy,
        )
        from apoapsis.discovery.schema import FrontierPlanningResponseEnvelope
        from apoapsis.specification.skeleton import json_skeleton

        plan = json_skeleton(FrontierPlanningResponseEnvelope)["plan"]
        for field, model in (
            ("delivery_contract", PlanDeliveryContract),
            ("runtime_design", RuntimeDesign),
            ("verification_strategy", VerificationStrategy),
        ):
            self.assertIsInstance(plan[field], dict)
            self.assertEqual(set(plan[field]), set(model.model_fields))

        # Lists of nested models show one fully expanded element, not a $ref.
        obligations = plan["verification_strategy"]["acceptance_proof_obligations"]
        self.assertEqual(len(obligations), 1)
        self.assertEqual(
            set(obligations[0]), {"criterion_id", "proof", "verification_commands"}
        )

    def test_enum_placeholders_list_every_permitted_value(self) -> None:
        """ADR 0075: an enum placeholder showing only the first member looked
        like a real answer, so a reader copying the shape kept it. For
        `runtime_boundary` the first member is `unspecified`, which asserts
        nothing and disables ADR 0074's contradiction check for that
        contract."""

        from apoapsis.architect.schema import RuntimeBoundary
        from apoapsis.discovery.schema import FrontierPlanningResponseEnvelope
        from apoapsis.specification.skeleton import json_skeleton

        plan = json_skeleton(FrontierPlanningResponseEnvelope)["plan"]
        contract = plan["integration_contracts"][0]
        self.assertIn("runtime_boundary", contract)
        placeholder = contract["runtime_boundary"]
        self.assertTrue(placeholder.startswith("<one of: "))
        for member in RuntimeBoundary:
            self.assertIn(member.value, placeholder)
        # Obviously a placeholder, never mistakable for a chosen value.
        self.assertNotEqual(placeholder, RuntimeBoundary.UNSPECIFIED.value)

    def test_enum_placeholders_are_derived_from_the_model(self) -> None:
        """Rendered from the enum itself, so a new member cannot be missing
        from the handoff without someone adding it there by hand."""

        from apoapsis.architect.schema import RiskLevel
        from apoapsis.discovery.schema import FrontierPlanningResponseEnvelope
        from apoapsis.specification.skeleton import json_skeleton

        plan = json_skeleton(FrontierPlanningResponseEnvelope)["plan"]
        placeholder = plan["slices"][0]["risk_level"]
        self.assertEqual(
            placeholder,
            "<one of: " + "|".join(item.value for item in RiskLevel) + ">",
        )

    def test_a_true_literal_field_still_shows_its_single_value(self) -> None:
        """`schema_version` is a `Literal`, not an enum, and there is exactly
        one permitted value -- so it must render as that value, not as a
        choice. The two branches are separate on purpose: a Literal pins a
        constant, an enum offers alternatives."""

        from apoapsis.discovery.schema import FrontierPlanningResponseEnvelope
        from apoapsis.specification.skeleton import json_skeleton

        skeleton = json_skeleton(FrontierPlanningResponseEnvelope)
        self.assertEqual(skeleton["schema_version"], "1.0")

    def test_the_response_variant_field_shows_both_variants(self) -> None:
        """`kind` is an enum with two members and the response must carry
        exactly one. Showing both is what the surrounding prose already
        says, so the shape and the prose now agree."""

        from apoapsis.discovery.schema import (
            FrontierPlanningResponseEnvelope,
            FrontierPlanningResponseKind,
        )
        from apoapsis.specification.skeleton import json_skeleton

        skeleton = json_skeleton(FrontierPlanningResponseEnvelope)
        for member in FrontierPlanningResponseKind:
            self.assertIn(member.value, skeleton["kind"])

    def test_the_keys_the_live_frontier_invented_are_absent(self) -> None:
        """The exact failure this exists to prevent: a plan rejected by
        `extra_forbidden` after the model guessed these key names."""

        from apoapsis.discovery.schema import FrontierPlanningResponseEnvelope
        from apoapsis.specification.skeleton import json_skeleton

        plan = json_skeleton(FrontierPlanningResponseEnvelope)["plan"]
        for invented in ("artifacts", "definition_of_done", "deployment_requirements"):
            self.assertNotIn(invented, plan["delivery_contract"])
        for invented in ("levels", "acceptance_validation", "exit_criteria"):
            self.assertNotIn(invented, plan["verification_strategy"])


if __name__ == "__main__":
    unittest.main()
