from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from sol.context.provenance import ContextEvidence, EvidenceKind
from sol.models.base import (
    ConstraintCoverage,
    ConstraintDisposition,
    ModelOperation,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from sol.verification.results import (
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


if __name__ == "__main__":
    unittest.main()
