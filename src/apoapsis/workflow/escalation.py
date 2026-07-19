from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from apoapsis.agent.session import AgentSessionResult
from apoapsis.config import AgentLoopConfig
from apoapsis.context.compiler import ContextCompiler, ContextPackage
from apoapsis.context.provenance import (
    ContextEvidence,
    EvidenceKind,
    TransmissionPolicy,
)
from apoapsis.repository.git import GitRepository
from apoapsis.specification.schema import HardConstraint, StrictModel, TaskSpecification
from apoapsis.verification.failures import FailureNormalizer, NormalizedFailure
from apoapsis.verification.results import VerificationStatus


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
    frontier_budget: AgentLoopConfig

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


def build_local_to_frontier_escalation(
    *,
    task_id: str,
    specification: TaskSpecification,
    worktree_path: str | Path,
    local_result: AgentSessionResult,
    context_compiler: ContextCompiler,
    files_changed: list[str],
    local_provider_name: str,
    local_model_name: str,
    frontier_provider_name: str,
    frontier_model_name: str,
    frontier_budget: AgentLoopConfig,
    external_research_brief: str | None = None,
    research_evidence_ids: list[str] | None = None,
) -> tuple[ContextPackage, EscalationPackage]:
    """Deterministically build the frontier-facing context and the
    immutable ``EscalationPackage`` audit record for a local-to-frontier
    escalation. Shared by the automatic escalation path
    (``VerticalSliceRunner._run_frontier_escalation``) and the explicit,
    human-authorized ``AUTHORIZE_FRONTIER_STAGE`` review action (ADR 0022),
    so both produce byte-for-byte the same deterministic package shape from
    the same inputs -- no separate, divergent construction logic.
    """

    failure_normalizer = FailureNormalizer()
    failures: list[NormalizedFailure] = []
    for result in local_result.verification_results:
        if result.status == VerificationStatus.PASSED:
            continue
        _, failure = failure_normalizer.extract(result, worktree_path)
        failures.append(failure)
    queries = [
        text
        for failure in failures
        for text in (failure.root_error, failure.relevant_error)
    ]
    frontier_context = context_compiler.compile(
        specification,
        worktree_path,
        extra_queries=queries,
        preferred_paths=files_changed,
        preferred_line_anchors={
            location.path: location.line
            for failure in failures
            for location in failure.locations
        },
        external_research_brief=external_research_brief,
        research_evidence_ids=research_evidence_ids or [],
    )
    frontier_context = add_escalation_evidence(frontier_context, local_result, failures)
    current_diff = GitRepository(worktree_path).run(
        ["diff", "--no-ext-diff", "--unified=5", "HEAD"]
    ).stdout
    package = EscalationPackage(
        task_id=task_id,
        trigger=local_result.stop_reason,
        local_provider=local_provider_name,
        local_model=local_model_name,
        frontier_provider=frontier_provider_name,
        frontier_model=frontier_model_name,
        specification=specification,
        active_constraints=specification.active_hard_constraints,
        current_diff=current_diff,
        local_session=local_result,
        normalized_failures=failures,
        frontier_context_sha256=frontier_context.context_sha256 or "0" * 64,
        frontier_budget=frontier_budget,
    )
    return frontier_context, package
