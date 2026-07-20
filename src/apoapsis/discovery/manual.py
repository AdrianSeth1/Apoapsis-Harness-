from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from apoapsis.architect.store import SQLitePlanStore
from apoapsis.config import ApoapsisConfig
from apoapsis.discovery.audit import DiscoveryAuditStore, write_frontier_package_artifact
from apoapsis.discovery.errors import (
    MalformedResponseError,
    ResponseHashMismatchError,
    ResponseTooLargeError,
)
from apoapsis.discovery.frontier_package import load_package
from apoapsis.discovery.schema import (
    DiscoverySessionRecord,
    FrontierPlanningRequestPackage,
    FrontierPlanningResponseEnvelope,
)
from apoapsis.discovery.response import apply_frontier_planning_response
from apoapsis.discovery.store import SQLiteDiscoveryStore


def build_frontier_planning_markdown(package: FrontierPlanningRequestPackage) -> str:
    """A single, self-contained Markdown file to upload directly to a
    ChatGPT/Claude subscription session -- everything needed to produce
    one valid ``FrontierPlanningResponseEnvelope`` is embedded here."""

    lines: list[str] = []
    lines.append("# Frontier Planning Handoff")
    lines.append("")
    lines.append(
        "This is a manual, subscription-based Architect Mode planning "
        "handoff produced by Apoapsis Harness. Upload this whole file to "
        "your ChatGPT or Claude subscription session and ask it to design "
        "an architecture and implementation plan for the idea below, "
        "returning **only** the JSON response object described at the end "
        "-- nothing else."
    )
    lines.append("")
    lines.append(f"- Package ID: `{package.package_id}`")
    lines.append(f"- Package SHA-256: `{package.package_sha256}`")
    lines.append(f"- Session ID: `{package.session_id}`")
    lines.append(f"- Frontier round: `{package.frontier_round}` of at most `{package.max_clarification_rounds}` clarification rounds")
    lines.append("")
    lines.append("## Authority rules (binding)")
    lines.append("")
    for rule in package.authority_rules:
        lines.append(f"- {rule}")
    lines.append("")
    lines.append("## Idea")
    lines.append("")
    lines.append(package.idea_text)
    lines.append("")
    lines.append("## User-approved idea brief")
    lines.append("")
    lines.append(f"**Summary:** {package.idea_brief.summary}")
    if package.idea_brief.goals:
        lines.append("")
        lines.append("**Goals:**")
        for goal in package.idea_brief.goals:
            lines.append(f"- {goal}")
    if package.idea_brief.non_goals:
        lines.append("")
        lines.append("**Non-goals:**")
        for item in package.idea_brief.non_goals:
            lines.append(f"- {item}")
    if package.idea_brief.key_constraints:
        lines.append("")
        lines.append("**Key constraints:**")
        for constraint in package.idea_brief.key_constraints:
            lines.append(f"- **{constraint.id}**: {constraint.text}")
    lines.append("")
    if package.local_questions:
        lines.append("## Local clarification questions and verbatim answers")
        lines.append("")
        answers_by_id = {item.question_id: item.text for item in package.local_answers}
        for question in package.local_questions:
            lines.append(f"- Q: {question.text}")
            answer = answers_by_id.get(question.question_id)
            if answer:
                lines.append(f"  - A: {answer}")
        lines.append("")
    if package.frontier_prior_questions:
        lines.append("## Prior frontier clarification questions and verbatim answers")
        lines.append("")
        prior_answers_by_id = {
            item.question_id: item.text for item in package.frontier_prior_answers
        }
        for question in package.frontier_prior_questions:
            lines.append(f"- Q: {question.text}")
            answer = prior_answers_by_id.get(question.question_id)
            if answer:
                lines.append(f"  - A: {answer}")
        lines.append("")
    lines.append("## Configured verification commands (informational only)")
    lines.append("")
    lines.append("| Name | Category | Acceptance | Description |")
    lines.append("| --- | --- | --- | --- |")
    for entry in package.verification_catalog:
        lines.append(
            f"| {entry.name} | {entry.category} | {entry.acceptance_designated} | "
            f"{entry.description} |"
        )
    lines.append("")
    lines.append("## Architect Mode ceilings (informational only)")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(package.architect_ceilings, indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append("## Required response format")
    lines.append("")
    lines.append(
        "Return **one** JSON object matching this exact schema -- no "
        "markdown code fence, no extra text before or after it. Set "
        "`kind` to either `\"clarification_questions\"` (at most "
        f"{package.max_clarification_questions} questions) or `\"plan\"`, "
        "and include only the matching field:"
    )
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(package.response_json_schema, indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append(
        f"Echo back `\"package_id\": \"{package.package_id}\"`, "
        f"`\"package_sha256\": \"{package.package_sha256}\"`, and "
        f"`\"session_id\": \"{package.session_id}\"` exactly as shown above "
        "-- Apoapsis rejects any response that does not match this "
        "package exactly."
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def write_frontier_planning_artifacts(
    root: str | Path, package: FrontierPlanningRequestPackage
) -> tuple[str, str]:
    """Writes the canonical JSON package (via
    ``discovery.audit.write_frontier_package_artifact``) plus the
    self-contained ``FRONTIER-PLANNING-HANDOFF-<package_id>.md`` Markdown
    file the user uploads by hand."""

    json_path = write_frontier_package_artifact(root, package)
    markdown_path_artifact = DiscoveryAuditStore(root, package.session_id).write_text(
        f"FRONTIER-PLANNING-HANDOFF-{package.package_id}.md",
        build_frontier_planning_markdown(package),
        kind="frontier_planning_handoff_markdown",
    )
    return json_path, markdown_path_artifact.path


def import_manual_frontier_planning_response(
    root: str | Path,
    discovery_store: SQLiteDiscoveryStore,
    plan_store: SQLitePlanStore,
    config: ApoapsisConfig,
    *,
    session_id: str,
    package_id: str,
    response_bytes: bytes,
    declared_model_name: str,
) -> DiscoverySessionRecord:
    """Validate a pasted manual-subscription response and apply it
    (``discovery.response.apply_frontier_planning_response``). Tokens and
    cost are never recorded for this transport -- there is nothing to
    measure on a manual paste, and none is ever displayed as a fabricated
    ``0``. ``declared_model_name`` is required, non-empty, operator-typed
    provenance only; Apoapsis never verifies which model actually produced
    the response."""

    root_path = Path(root).resolve()
    declared_model_name = declared_model_name.strip()
    if not declared_model_name:
        raise MalformedResponseError(
            "a declared model name is required -- manual subscription "
            "model identity is operator-declared provenance and is never "
            "inferred or defaulted"
        )
    if len(response_bytes) > config.discovery.max_response_bytes:
        raise ResponseTooLargeError(
            f"response is {len(response_bytes)} bytes; maximum is "
            f"{config.discovery.max_response_bytes}"
        )
    try:
        raw_text = response_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedResponseError(f"response is not valid UTF-8: {exc}") from exc
    try:
        raw_payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise MalformedResponseError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(raw_payload, dict):
        raise MalformedResponseError("response must be a single JSON object")
    try:
        envelope = FrontierPlanningResponseEnvelope.model_validate(raw_payload)
    except ValidationError as exc:
        raise MalformedResponseError(f"response failed schema validation: {exc}") from exc

    package = load_package(root_path, package_id)
    if envelope.package_id != package_id:
        raise ResponseHashMismatchError(
            f"response package_id {envelope.package_id!r} does not match "
            f"the imported package {package_id!r}"
        )
    if envelope.package_sha256 != package.package_sha256:
        raise ResponseHashMismatchError(
            "response package_sha256 does not match the package's own "
            "hash -- the response was not produced for this exact package"
        )

    session = discovery_store.get_session(session_id)
    record = apply_frontier_planning_response(
        root_path, discovery_store, plan_store, config, session, package, envelope, raw_payload
    )
    audit = DiscoveryAuditStore(root_path, session_id)
    audit.write_json(
        f"frontier-response-{package_id}.json", raw_payload, kind="frontier_planning_response"
    )
    audit.write_text(
        f"frontier-response-{package_id}-declared-model.txt",
        declared_model_name,
        kind="frontier_planning_declared_model",
    )
    return record


__all__ = [
    "build_frontier_planning_markdown",
    "write_frontier_planning_artifacts",
    "import_manual_frontier_planning_response",
]
