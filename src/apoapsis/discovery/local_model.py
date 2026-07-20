from __future__ import annotations

import json
import re
import uuid
from typing import Sequence

from pydantic import ValidationError

from apoapsis.config import ApoapsisConfig, FrontierProviderConfig
from apoapsis.discovery.audit import DiscoveryAuditStore
from apoapsis.discovery.schema import (
    ClarificationAnswer,
    ClarificationQuestion,
    IdeaBrief,
    LocalQuestionsProposal,
)
from apoapsis.models.base import ModelOperation
from apoapsis.models.frontier import OpenAICompatibleFrontierProvider
from apoapsis.models.local import OllamaProvider
from apoapsis.models.provider import ModelRole, ProviderInvocation
from apoapsis.models.telemetry import InstrumentedModelProvider, InstrumentedProviderError


class DiscoveryModelError(RuntimeError):
    """A local-model proposal could not become a valid, source-faithful
    result, including after the one bounded correction attempt (ADR 0018's
    established precedent, applied here)."""


def build_local_provider(provider_config: FrontierProviderConfig) -> InstrumentedModelProvider:
    if provider_config.provider == "ollama":
        adapter = OllamaProvider(provider_config)
    elif provider_config.provider == "openai_compatible":
        adapter = OpenAICompatibleFrontierProvider(provider_config)
    else:
        raise DiscoveryModelError(f"unsupported provider: {provider_config.provider}")
    return InstrumentedModelProvider(adapter, provider_config.pricing)


def _parse_json_object(content: str) -> dict[str, object]:
    candidate = content.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    try:
        raw = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise DiscoveryModelError(f"local model response is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise DiscoveryModelError("local model response must be a single JSON object")
    return raw


_QUESTIONS_RULES = """Rules:
- Return one JSON object only. Do not use Markdown fences or prose.
- Set schema_version to "1.0".
- Propose at most {max_questions} clarification questions -- fewer is fine
  if the idea is already clear; never more than {max_questions}.
- Each question must be a single, specific, answerable question about the
  idea below, not a restatement of it.
- Do not answer your own questions. Do not propose an idea brief here."""


def build_questions_prompt(idea_text: str, max_questions: int) -> str:
    schema = json.dumps(LocalQuestionsProposal.model_json_schema(), sort_keys=True)
    rules = _QUESTIONS_RULES.format(max_questions=max_questions)
    return f"""You propose a small, bounded set of clarification questions for a coding-project idea.

{rules}

SCHEMA:
{schema}

IDEA_START
{idea_text}
IDEA_END
"""


def build_questions_correction_prompt(
    idea_text: str, max_questions: int, previous_response: str, validation_errors: str
) -> str:
    schema = json.dumps(LocalQuestionsProposal.model_json_schema(), sort_keys=True)
    rules = _QUESTIONS_RULES.format(max_questions=max_questions)
    return f"""Your previous response below failed validation and could not be used. This is your one bounded correction attempt: return a single, complete, fully valid object addressing every validation error listed below.

VALIDATION_ERRORS:
{validation_errors}

{rules}

SCHEMA:
{schema}

YOUR_PREVIOUS_RESPONSE_START
{previous_response}
YOUR_PREVIOUS_RESPONSE_END

IDEA_START
{idea_text}
IDEA_END
"""


def parse_questions(content: str, *, max_questions: int) -> list[ClarificationQuestion]:
    raw = _parse_json_object(content)
    try:
        proposal = LocalQuestionsProposal.model_validate(raw)
    except ValidationError as exc:
        raise DiscoveryModelError(f"local questions proposal is invalid: {exc}") from exc
    # The harness alone enforces the ceiling, regardless of how many the
    # model proposed -- never trusted from the model's own output count.
    return proposal.questions[:max_questions]


_BRIEF_RULES = """Rules:
- Return one JSON object only. Do not use Markdown fences or prose.
- Set schema_version to "1.0".
- Every key_constraints item's verbatim_source must be an exact,
  case-sensitive substring of SOURCE_TEXT below (the idea plus the user's
  own answers) -- never paraphrased or invented.
- Every key_constraints item's source must be "user".
- Base the brief only on SOURCE_TEXT; put anything you are not confident
  about in open_questions instead of guessing."""


def build_brief_prompt(idea_text: str, answers: Sequence[ClarificationAnswer]) -> str:
    schema = json.dumps(IdeaBrief.model_json_schema(), sort_keys=True)
    answers_text = "\n".join(f"- {item.text}" for item in answers) or "(no answers given)"
    return f"""You draft a concise idea brief for a coding-project idea, informed by the user's own clarifying answers.

{_BRIEF_RULES}

SCHEMA:
{schema}

SOURCE_TEXT_START
{idea_text}

{answers_text}
SOURCE_TEXT_END
"""


def build_brief_correction_prompt(
    idea_text: str,
    answers: Sequence[ClarificationAnswer],
    previous_response: str,
    validation_errors: str,
) -> str:
    schema = json.dumps(IdeaBrief.model_json_schema(), sort_keys=True)
    answers_text = "\n".join(f"- {item.text}" for item in answers) or "(no answers given)"
    return f"""Your previous response below failed validation and could not be used. This is your one bounded correction attempt: return a single, complete, fully valid object addressing every validation error listed below.

VALIDATION_ERRORS:
{validation_errors}

{_BRIEF_RULES}

SCHEMA:
{schema}

YOUR_PREVIOUS_RESPONSE_START
{previous_response}
YOUR_PREVIOUS_RESPONSE_END

SOURCE_TEXT_START
{idea_text}

{answers_text}
SOURCE_TEXT_END
"""


def _source_text(idea_text: str, answers: Sequence[ClarificationAnswer]) -> str:
    return idea_text + "\n" + "\n".join(item.text for item in answers)


def parse_brief(
    content: str, idea_text: str, answers: Sequence[ClarificationAnswer]
) -> IdeaBrief:
    raw = _parse_json_object(content)
    try:
        brief = IdeaBrief.model_validate(raw)
    except ValidationError as exc:
        raise DiscoveryModelError(f"idea brief proposal is invalid: {exc}") from exc
    source = _source_text(idea_text, answers)
    for constraint in brief.key_constraints:
        if constraint.verbatim_source not in source:
            raise DiscoveryModelError(
                f"key constraint {constraint.id} verbatim_source is not an "
                "exact substring of the idea text and answers"
            )
    return brief


def _call(
    provider: InstrumentedModelProvider,
    provider_config: FrontierProviderConfig,
    audit: DiscoveryAuditStore,
    *,
    operation: ModelOperation,
    prompt: str,
    call_label: str,
) -> str:
    invocation = ProviderInvocation(
        request_id=f"MRQ-{uuid.uuid4().hex}",
        operation=operation,
        prompt=prompt,
        role=ModelRole.LOCAL_DISCOVERY_MODEL,
        max_output_tokens=provider_config.max_output_tokens,
        timeout_seconds=provider_config.timeout_seconds,
    )
    audit.write_text(f"{call_label}-prompt.txt", prompt, kind="discovery_prompt")
    try:
        call = provider.complete(invocation)
    except InstrumentedProviderError as exc:
        audit.write_json(f"{call_label}-telemetry.json", exc.telemetry, kind="provider_telemetry")
        raise DiscoveryModelError(f"local model call failed: {exc}") from exc
    audit.write_text(f"{call_label}-response.txt", call.output.content, kind="discovery_response")
    audit.write_json(f"{call_label}-telemetry.json", call.telemetry, kind="provider_telemetry")
    return call.output.content


def propose_clarification_questions(
    provider: InstrumentedModelProvider,
    provider_config: FrontierProviderConfig,
    audit: DiscoveryAuditStore,
    idea_text: str,
    *,
    max_questions: int,
) -> list[ClarificationQuestion]:
    """One local-model call proposing up to ``max_questions`` clarification
    questions, with exactly one bounded correction attempt on schema
    failure (mirrors ADR 0018's precedent). Raises ``DiscoveryModelError``
    if both attempts fail -- never a second correction."""

    prompt = build_questions_prompt(idea_text, max_questions)
    content = _call(
        provider,
        provider_config,
        audit,
        operation=ModelOperation.PROPOSE_DISCOVERY_QUESTIONS,
        prompt=prompt,
        call_label="questions-001",
    )
    try:
        return parse_questions(content, max_questions=max_questions)
    except DiscoveryModelError as first_error:
        correction_prompt = build_questions_correction_prompt(
            idea_text, max_questions, content, str(first_error)
        )
        corrected_content = _call(
            provider,
            provider_config,
            audit,
            operation=ModelOperation.PROPOSE_DISCOVERY_QUESTIONS,
            prompt=correction_prompt,
            call_label="questions-002-correction",
        )
        return parse_questions(corrected_content, max_questions=max_questions)


def propose_idea_brief(
    provider: InstrumentedModelProvider,
    provider_config: FrontierProviderConfig,
    audit: DiscoveryAuditStore,
    idea_text: str,
    answers: Sequence[ClarificationAnswer],
) -> IdeaBrief:
    """One local-model call proposing an ``IdeaBrief``, with exactly one
    bounded correction attempt on schema/verbatim-constraint failure."""

    prompt = build_brief_prompt(idea_text, answers)
    content = _call(
        provider,
        provider_config,
        audit,
        operation=ModelOperation.DRAFT_IDEA_BRIEF,
        prompt=prompt,
        call_label="brief-001",
    )
    try:
        return parse_brief(content, idea_text, answers)
    except DiscoveryModelError as first_error:
        correction_prompt = build_brief_correction_prompt(
            idea_text, answers, content, str(first_error)
        )
        corrected_content = _call(
            provider,
            provider_config,
            audit,
            operation=ModelOperation.DRAFT_IDEA_BRIEF,
            prompt=correction_prompt,
            call_label="brief-002-correction",
        )
        return parse_brief(corrected_content, idea_text, answers)


__all__ = [
    "DiscoveryModelError",
    "build_local_provider",
    "propose_clarification_questions",
    "propose_idea_brief",
]
