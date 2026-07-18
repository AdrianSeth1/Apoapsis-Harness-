from __future__ import annotations

import json
import re
from typing import Sequence

from pydantic import ValidationError

from apoapsis.specification.schema import SourceKind, TaskSpecification
from apoapsis.verification.runner import VerificationCommand


class SpecificationExtractionError(RuntimeError):
    """A model proposal could not become a valid, source-faithful specification."""


def _acceptance_catalog_json(commands: Sequence[VerificationCommand]) -> str:
    """Render the deterministic acceptance-command catalog (ADR 0016).

    This is the *only* vocabulary a model may draw an
    `AcceptanceCriterion.verification_method` from -- it names configured
    commands, never grants shell authority, and is rebuilt fresh from the
    real `[verification.commands]` configuration on every extraction call.
    """

    catalog = [
        {
            "name": command.name,
            "category": command.category,
            "description": command.description,
            "acceptance_designated": command.acceptance,
        }
        for command in sorted(commands, key=lambda item: item.name)
    ]
    return json.dumps(catalog, sort_keys=True)


class SpecificationExtractor:
    def build_prompt(
        self,
        request: str,
        task_id: str,
        acceptance_catalog: Sequence[VerificationCommand] = (),
    ) -> str:
        schema = json.dumps(TaskSpecification.model_json_schema(), sort_keys=True)
        catalog = _acceptance_catalog_json(acceptance_catalog)
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
- An acceptance_criteria item's verification_method may be set only to a
  "name" value that appears in ACCEPTANCE_COMMAND_CATALOG below, or left
  null if no configured command proves it. Never invent a command name and
  never propose a shell command directly -- the catalog is the complete,
  closed vocabulary; only commands with "acceptance_designated": true can
  ever prove a criterion, but you may name any catalog entry if you are
  unsure. The user reviews and approves this mapping together with the
  rest of the specification before it takes effect.

SCHEMA:
{schema}

ACCEPTANCE_COMMAND_CATALOG:
{catalog}

USER_REQUEST_START
{request}
USER_REQUEST_END
"""

    def parse(
        self,
        content: str,
        request: str,
        task_id: str,
        acceptance_catalog: Sequence[VerificationCommand] = (),
    ) -> TaskSpecification:
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
        catalog_names = {command.name for command in acceptance_catalog}
        for criterion in specification.acceptance_criteria:
            method = criterion.verification_method
            if method is not None and method not in catalog_names:
                raise SpecificationExtractionError(
                    f"acceptance criterion {criterion.id} verification_method "
                    f"{method!r} is not in the configured acceptance-command "
                    "catalog"
                )
        return specification

