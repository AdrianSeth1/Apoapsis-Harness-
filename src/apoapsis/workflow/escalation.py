from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from apoapsis.agent.session import AgentSessionResult
from apoapsis.context.compiler import ContextPackage
from apoapsis.context.provenance import (
    ContextEvidence,
    EvidenceKind,
    TransmissionPolicy,
)
from apoapsis.specification.schema import HardConstraint, StrictModel, TaskSpecification
from apoapsis.verification.failures import NormalizedFailure


class EscalationPackage(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    trigger: str = Field(min_length=1)
    local_provider: str = Field(min_length=1)
    local_model: str = Field(min_length=1)
    frontier_provider: str = Field(min_length=1)
    frontier_model: str = Field(min_length=1)
    specification: TaskSpecification
    active_constraints: list[HardConstraint] = Field(default_factory=list)
    current_diff: str
    current_diff_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    local_session: AgentSessionResult
    normalized_failures: list[NormalizedFailure] = Field(default_factory=list)
    frontier_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_package(self) -> EscalationPackage:
        if self.task_id != self.specification.task_id:
            raise ValueError("escalation task_id must match specification")
        expected_constraints = {
            item.id for item in self.specification.active_hard_constraints
        }
        if {item.id for item in self.active_constraints} != expected_constraints:
            raise ValueError("escalation package must contain every active constraint")
        digest = hashlib.sha256(self.current_diff.encode("utf-8")).hexdigest()
        if self.current_diff_sha256 is None:
            self.current_diff_sha256 = digest
        elif self.current_diff_sha256 != digest:
            raise ValueError("current_diff_sha256 does not match current_diff")
        return self


def add_escalation_evidence(
    context: ContextPackage,
    local_session: AgentSessionResult,
    failures: list[NormalizedFailure],
) -> ContextPackage:
    evidence = list(context.evidence)
    history = json.dumps(
        [item.model_dump(mode="json") for item in local_session.turn_records],
        indent=2,
        sort_keys=True,
    )
    evidence.append(
        ContextEvidence(
            evidence_id="EV-ESCALATION-HISTORY",
            kind=EvidenceKind.FAILURE,
            path="<local-agent-attempt-history>",
            commit=f"{context.head_commit}+working-tree",
            reason_included="bounded local-agent actions and deterministic outcomes",
            content=history,
            transmission_policy=TransmissionPolicy.CLOUD_ALLOWED,
        )
    )
    for index, failure in enumerate(failures, start=1):
        evidence.append(
            ContextEvidence(
                evidence_id=f"EV-ESCALATION-FAILURE-{index:03d}",
                kind=EvidenceKind.FAILURE,
                path=f"<local-verification:{failure.command_name}>",
                commit=f"{context.head_commit}+working-tree",
                reason_included="exact normalized local verification failure",
                content=(
                    f"COMMAND_JSON\n{json.dumps(failure.argv)}\n\n"
                    f"ROOT_ERROR\n{failure.root_error}\n\n"
                    f"RELEVANT_ERROR\n{failure.relevant_error}"
                ),
                transmission_policy=TransmissionPolicy.CLOUD_ALLOWED,
            )
        )
    normalized: list[ContextEvidence] = []
    seen: set[tuple[str, int | None, int | None, str | None]] = set()
    for item in evidence:
        key = (item.path, item.start_line, item.end_line, item.content_sha256)
        if key in seen:
            continue
        seen.add(key)
        payload = item.model_dump(mode="python")
        payload["evidence_id"] = f"EV-{len(normalized) + 1:03d}"
        normalized.append(ContextEvidence.model_validate(payload))
    parameters = dict(context.compiler_parameters)
    parameters["escalation_package"] = "local_to_frontier_v1"
    return ContextPackage(
        task_id=context.task_id,
        specification=context.specification,
        head_commit=context.head_commit,
        query_terms=context.query_terms,
        retrieval_tools=sorted(
            set([*context.retrieval_tools, "local_attempt_history"])
        ),
        compiler_parameters=parameters,
        external_research_brief=context.external_research_brief,
        research_evidence_ids=context.research_evidence_ids,
        evidence=normalized,
    )
