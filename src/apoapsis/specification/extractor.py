from __future__ import annotations

import json
import re

from pydantic import ValidationError

from apoapsis.specification.schema import SourceKind, TaskSpecification


class SpecificationExtractionError(RuntimeError):
    """A model proposal could not become a valid, source-faithful specification."""


class SpecificationExtractor:
    def build_prompt(self, request: str, task_id: str) -> str:
        schema = json.dumps(TaskSpecification.model_json_schema(), sort_keys=True)
        return f"""You extract a coding request into the supplied JSON schema.

Rules:
- Return one JSON object only. Do not use Markdown fences or prose.
- Set schema_version to \"1.0\" and task_id to \"{task_id}\".
- Preserve each user hard constraint exactly in verbatim_source. It must be an
  exact, case-sensitive substring of USER_REQUEST.
- Use source \"user\" for direct user statements and \"derived\" only for
  conservative acceptance criteria or questions.
- Do not invent repository facts. Put uncertainties in open_questions.
- requested_output must be \"unified_diff\".

SCHEMA:
{schema}

USER_REQUEST_START
{request}
USER_REQUEST_END
"""

    def parse(self, content: str, request: str, task_id: str) -> TaskSpecification:
        candidate = content.strip()
        fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL)
        if fence:
            candidate = fence.group(1)
        try:
            raw = json.loads(candidate)
            specification = TaskSpecification.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise SpecificationExtractionError(
                f"frontier specification is invalid: {exc}"
            ) from exc
        if specification.task_id != task_id:
            raise SpecificationExtractionError(
                "frontier specification changed the deterministic task ID"
            )
        for constraint in specification.hard_constraints:
            if constraint.verbatim_source not in request:
                raise SpecificationExtractionError(
                    f"constraint {constraint.id} verbatim_source is not an exact "
                    "substring of the user request"
                )
            if constraint.source != SourceKind.USER:
                raise SpecificationExtractionError(
                    f"hard constraint {constraint.id} must retain user authority"
                )
        return specification

