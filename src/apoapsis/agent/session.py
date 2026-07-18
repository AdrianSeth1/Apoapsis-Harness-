from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Callable

from pydantic import Field

from apoapsis.agent.actions import (
    AgentActionError,
    InspectDiffAction,
    ProposePatchAction,
    ReadFileAction,
    ReplaceTextAction,
    RequestEscalationAction,
    RunCheckAction,
    SearchRepositoryAction,
    SubmitForVerificationAction,
    agent_action_schema,
    parse_agent_action,
)
from apoapsis.agent.inspection import AgentInspectionError, RepositoryInspector
from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import AgentLoopConfig
from apoapsis.context.compiler import ContextCompiler, ContextPackage
from apoapsis.context.provenance import (
    ContextEvidence,
    EvidenceKind,
    TransmissionPolicy,
)
from apoapsis.models.base import ModelOperation, ModelResponse
from apoapsis.models.prompts import agent_step_prompt
from apoapsis.models.provider import ModelRole
from apoapsis.patches.apply import PatchApplicationError
from apoapsis.patches.parser import UnifiedDiffError
from apoapsis.patches.validator import PatchPolicyError
from apoapsis.repository.git import GitRepository
from apoapsis.specification.schema import StrictModel, TaskSpecification
from apoapsis.verification.failures import FailureNormalizer, NormalizedFailure
from apoapsis.verification.results import VerificationResult, VerificationStatus
from apoapsis.verification.runner import (
    VerificationCommand,
    VerificationConfig,
    VerificationRunner,
)


class AgentSessionOutcome(StrEnum):
    COMPLETE = "complete"
    ESCALATION_REQUIRED = "escalation_required"


class AgentTurnRecord(StrictModel):
    turn: int = Field(ge=1)
    action: str
    accepted: bool
    summary: str
    evidence_ids: list[str] = Field(default_factory=list)
    patch_attempt: int | None = Field(default=None, ge=1)
    verification_run: int | None = Field(default=None, ge=1)
    verification_status: VerificationStatus | None = None


class AgentSessionResult(StrictModel):
    outcome: AgentSessionOutcome
    stop_reason: str
    turns: int = Field(ge=0)
    patch_attempts: int = Field(ge=0)
    verification_runs: int = Field(ge=0)
    changed_files: list[str] = Field(default_factory=list)
    turn_records: list[AgentTurnRecord] = Field(default_factory=list)
    verification_results: list[VerificationResult] = Field(default_factory=list)


AgentModelCall = Callable[..., ModelResponse]
PatchApply = Callable[[str, int], None]


class BoundedAgentSession:
    """A deterministic controller for untrusted model-requested coding actions."""

    def __init__(
        self,
        *,
        specification: TaskSpecification,
        worktree: str | Path,
        initial_context: ContextPackage,
        context_compiler: ContextCompiler,
        config: AgentLoopConfig,
        verification_config: VerificationConfig,
        audit: TaskAuditStore,
        model_call: AgentModelCall,
        apply_patch: PatchApply,
        model_role: ModelRole = ModelRole.CODING_AGENT,
        audit_prefix: str = "",
    ) -> None:
        self.specification = specification
        self.worktree = Path(worktree).resolve()
        self.base_context = initial_context
        self.context_compiler = context_compiler
        self.config = config
        self.verification_config = verification_config
        self.audit = audit
        self.model_call = model_call
        self.apply_patch = apply_patch
        self.model_role = model_role
        self.audit_prefix = audit_prefix
        self.inspector = RepositoryInspector(
            self.worktree,
            max_search_results=config.max_search_results,
            max_read_lines=config.max_read_lines,
            max_chars=config.max_observation_chars,
        )
        self.failure_normalizer = FailureNormalizer()
        self.observations: list[ContextEvidence] = []
        self.observation_chars = 0
        self.records: list[AgentTurnRecord] = []
        self.verification_results: list[VerificationResult] = []
        self.patch_attempts = 0
        self.verification_runs = 0
        self.verification_cache: dict[str, VerificationResult] = {}
        self.passed_checks: dict[str, set[str]] = {}

    def run(self) -> AgentSessionResult:
        for turn in range(1, self.config.max_turns + 1):
            context = self._context_for_turn(turn)
            prompt = agent_step_prompt(
                context,
                turn=turn,
                remaining_budgets=self._remaining_budgets(turn),
                verification_commands=[
                    item.name for item in self.verification_config.commands
                ],
                history=[item.model_dump(mode="json") for item in self.records],
            )
            response = self.model_call(
                ModelOperation.AGENT_STEP,
                prompt,
                context,
                requested_output="one_model_action_json",
                response_schema=agent_action_schema(),
                role=self.model_role,
            )
            try:
                action = parse_agent_action(response.content)
            except AgentActionError as exc:
                self._record(
                    AgentTurnRecord(
                        turn=turn,
                        action="invalid_action",
                        accepted=False,
                        summary=str(exc)[:2_000],
                    )
                )
                continue

            if isinstance(action, RequestEscalationAction):
                self._record(
                    AgentTurnRecord(
                        turn=turn,
                        action=action.action,
                        accepted=True,
                        summary=action.reason,
                    )
                )
                return self._result(
                    AgentSessionOutcome.ESCALATION_REQUIRED,
                    f"model requested escalation: {action.reason}",
                )

            try:
                completed = self._execute(turn, action)
            except AgentInspectionError as exc:
                self._record(
                    AgentTurnRecord(
                        turn=turn,
                        action=action.action,
                        accepted=False,
                        summary=str(exc)[:2_000],
                    )
                )
                continue
            if completed:
                return self._result(
                    AgentSessionOutcome.COMPLETE,
                    "full deterministic verification passed",
                )

        return self._result(
            AgentSessionOutcome.ESCALATION_REQUIRED,
            f"agent turn budget exhausted after {self.config.max_turns} turns",
        )

    def interrupted(self, reason: str) -> AgentSessionResult:
        """Persist a deterministic stop when the selected provider fails."""

        return self._result(AgentSessionOutcome.ESCALATION_REQUIRED, reason)

    def _execute(self, turn: int, action: object) -> bool:
        if isinstance(action, SearchRepositoryAction):
            evidence = self.inspector.search(action.query, action.path_glob)
            added = self._add_evidence(evidence)
            self._record(
                AgentTurnRecord(
                    turn=turn,
                    action=action.action,
                    accepted=True,
                    summary=(
                        f"literal search returned {len(evidence)} bounded matches; "
                        f"{len(added)} added to the context ledger"
                    ),
                    evidence_ids=added,
                )
            )
            return False

        if isinstance(action, ReadFileAction):
            evidence = self.inspector.read(
                action.path, action.start_line, action.end_line
            )
            added = self._add_evidence([evidence])
            self._record(
                AgentTurnRecord(
                    turn=turn,
                    action=action.action,
                    accepted=True,
                    summary=(
                        f"read {evidence.path}:{evidence.start_line}-"
                        f"{evidence.end_line}"
                    ),
                    evidence_ids=added,
                )
            )
            return False

        if isinstance(action, InspectDiffAction):
            evidence = self.inspector.diff()
            added = self._add_evidence([evidence] if evidence else [])
            self._record(
                AgentTurnRecord(
                    turn=turn,
                    action=action.action,
                    accepted=True,
                    summary=(
                        "current worktree diff added to context"
                        if evidence
                        else "current worktree has no diff"
                    ),
                    evidence_ids=added,
                )
            )
            return False

        if isinstance(action, ProposePatchAction):
            return self._apply_patch_action(
                turn, action.action, action.unified_diff
            )

        if isinstance(action, ReplaceTextAction):
            patch = self.inspector.replacement_patch(
                action.path, action.old_text, action.new_text
            )
            return self._apply_patch_action(turn, action.action, patch)

        if isinstance(action, RunCheckAction):
            command = next(
                (
                    item
                    for item in self.verification_config.commands
                    if item.name == action.command_name
                ),
                None,
            )
            if command is None:
                allowed = [item.name for item in self.verification_config.commands]
                raise AgentInspectionError(
                    f"unknown verification command {action.command_name!r}; "
                    f"configured names are {allowed}"
                )
            result = self._verify([command])
            self._record_verification(turn, action.action, result)
            return (
                result.status == VerificationStatus.PASSED
                and self._all_required_checks_passed()
            )

        if isinstance(action, SubmitForVerificationAction):
            if not self.inspector.has_changes():
                raise AgentInspectionError(
                    "full verification submission requires a non-empty worktree diff"
                )
            result = self._verify(self.verification_config.commands)
            self._record_verification(turn, action.action, result)
            return result.status == VerificationStatus.PASSED

        raise TypeError(f"unsupported agent action: {type(action).__name__}")

    def _apply_patch_action(
        self, turn: int, action: str, patch: str
    ) -> bool:
        if self.patch_attempts >= self.config.max_patch_attempts:
            raise AgentInspectionError("patch-attempt budget is exhausted")
        self.patch_attempts += 1
        try:
            self.apply_patch(patch, self.patch_attempts)
        except (UnifiedDiffError, PatchPolicyError, PatchApplicationError) as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.audit.write_json(
                (
                    f"{self.audit_prefix}agent-patch-failure-"
                    f"{self.patch_attempts:03d}.json"
                ),
                {
                    "patch_attempt": self.patch_attempts,
                    "root_error": message,
                    "worktree_changed_paths": self.inspector.changed_paths(),
                },
                kind="normalized_patch_failure",
            )
            self._record(
                AgentTurnRecord(
                    turn=turn,
                    action=action,
                    accepted=False,
                    summary=message[:4_000],
                    patch_attempt=self.patch_attempts,
                )
            )
            return False
        current_diff = self.inspector.diff()
        added = self._add_evidence([current_diff] if current_diff else [])
        self._record(
            AgentTurnRecord(
                turn=turn,
                action=action,
                accepted=True,
                summary="edit passed policy and was applied in the task worktree",
                evidence_ids=added,
                patch_attempt=self.patch_attempts,
            )
        )
        return False

    def _verify(self, commands: list[VerificationCommand]) -> VerificationResult:
        state_digest = self._verification_state_digest()
        command_key = ",".join(sorted(item.name for item in commands))
        cache_key = f"{state_digest}:{command_key}"
        if cache_key in self.verification_cache:
            raise AgentInspectionError(
                "identical verification already ran for the current diff; "
                "change the code or inspect the recorded failure"
            )
        if self.verification_runs >= self.config.max_verification_runs:
            raise AgentInspectionError("verification-run budget is exhausted")
        self.verification_runs += 1
        selected = self.verification_config.model_copy(
            update={"commands": commands}
        )
        result = VerificationRunner(selected).run(
            self.specification.task_id, self.worktree
        )
        self.verification_results.append(result)
        self.verification_cache[cache_key] = result
        passed = self.passed_checks.setdefault(state_digest, set())
        passed.update(
            item.name
            for item in result.commands
            if item.status == VerificationStatus.PASSED
        )
        self.audit.write_json(
            (
                f"{self.audit_prefix}verification-"
                f"{self.verification_runs:03d}.json"
            ),
            result,
            kind="verification_result",
        )
        if result.status != VerificationStatus.PASSED:
            _, failure = self.failure_normalizer.extract(result, self.worktree)
            self.audit.write_json(
                (
                    f"{self.audit_prefix}verification-failure-"
                    f"{self.verification_runs:03d}.json"
                ),
                failure,
                kind="normalized_failure",
            )
            self._add_failure_evidence(failure)
            self.base_context = self.context_compiler.compile(
                self.specification,
                self.worktree,
                extra_queries=[failure.root_error, failure.relevant_error],
                preferred_paths=self.inspector.changed_paths(),
                external_research_brief=self.base_context.external_research_brief,
                research_evidence_ids=self.base_context.research_evidence_ids,
            )
        return result

    def _verification_state_digest(self) -> str:
        content = GitRepository(self.worktree).run(
            ["diff", "--no-ext-diff", "HEAD"]
        ).stdout
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _all_required_checks_passed(self) -> bool:
        required = {
            item.name
            for item in self.verification_config.commands
            if item.required
        }
        return required.issubset(
            self.passed_checks.get(self._verification_state_digest(), set())
        )

    def _record_verification(
        self, turn: int, action: str, result: VerificationResult
    ) -> None:
        failed = next(
            (
                item
                for item in result.commands
                if item.required and item.status != VerificationStatus.PASSED
            ),
            None,
        )
        summary = (
            "deterministic verification passed"
            if failed is None
            else f"{failed.name} {failed.status.value} with exit code {failed.exit_code}"
        )
        self._record(
            AgentTurnRecord(
                turn=turn,
                action=action,
                accepted=True,
                summary=summary,
                verification_run=self.verification_runs,
                verification_status=result.status,
            )
        )

    def _add_failure_evidence(self, failure: NormalizedFailure) -> None:
        self._add_evidence(
            [
                ContextEvidence(
                    evidence_id=f"EV-AGENT-FAILURE-{self.verification_runs:03d}",
                    kind=EvidenceKind.FAILURE,
                    path=f"<verification:{failure.command_name}>",
                    commit=f"{self.base_context.head_commit}+working-tree",
                    reason_included="normalized deterministic verification failure",
                    content=failure.relevant_error,
                    transmission_policy=TransmissionPolicy.CLOUD_ALLOWED,
                )
            ]
        )

    def _add_evidence(
        self, evidence: list[ContextEvidence]
    ) -> list[str]:
        existing = {
            (item.path, item.start_line, item.end_line, item.content_sha256)
            for item in [*self.base_context.evidence, *self.observations]
        }
        added: list[str] = []
        for item in evidence:
            key = (item.path, item.start_line, item.end_line, item.content_sha256)
            if key in existing:
                continue
            remaining = self.config.max_observation_chars - self.observation_chars
            if remaining <= 0:
                break
            selected = item
            if len(item.content) > remaining:
                content = item.content[:remaining]
                payload = item.model_dump(mode="python")
                payload["content"] = content
                payload["content_sha256"] = None
                if item.start_line is not None:
                    payload["end_line"] = item.start_line + content.count("\n")
                selected = ContextEvidence.model_validate(payload)
            self.observations.append(selected)
            self.observation_chars += len(selected.content)
            added.append(selected.evidence_id)
            existing.add(
                (
                    selected.path,
                    selected.start_line,
                    selected.end_line,
                    selected.content_sha256,
                )
            )
        return added

    def _context_for_turn(self, turn: int) -> ContextPackage:
        unique: list[ContextEvidence] = []
        seen: set[tuple[str, int | None, int | None, str | None]] = set()
        for item in [*self.base_context.evidence, *self.observations]:
            key = (item.path, item.start_line, item.end_line, item.content_sha256)
            if key in seen:
                continue
            seen.add(key)
            payload = item.model_dump(mode="python")
            payload["evidence_id"] = f"EV-{len(unique) + 1:03d}"
            unique.append(ContextEvidence.model_validate(payload))
        parameters = dict(self.base_context.compiler_parameters)
        parameters["agent_turn"] = turn
        parameters["agent_loop"] = self.config.model_dump(mode="json")
        return ContextPackage(
            task_id=self.base_context.task_id,
            specification=self.base_context.specification,
            head_commit=self.base_context.head_commit,
            query_terms=self.base_context.query_terms,
            retrieval_tools=sorted(
                set(
                    [
                        *self.base_context.retrieval_tools,
                        "agent_literal_search",
                        "agent_bounded_read",
                        "git_worktree_diff",
                    ]
                )
            ),
            compiler_parameters=parameters,
            external_research_brief=self.base_context.external_research_brief,
            research_evidence_ids=self.base_context.research_evidence_ids,
            evidence=unique,
        )

    def _remaining_budgets(self, turn: int) -> dict[str, int]:
        return {
            "turns": self.config.max_turns - turn + 1,
            "patch_attempts": self.config.max_patch_attempts - self.patch_attempts,
            "verification_runs": (
                self.config.max_verification_runs - self.verification_runs
            ),
            "observation_characters": (
                self.config.max_observation_chars - self.observation_chars
            ),
        }

    def _record(self, record: AgentTurnRecord) -> None:
        self.records.append(record)
        self.audit.write_json(
            f"{self.audit_prefix}agent-turn-{record.turn:03d}.json",
            record,
            kind="agent_turn",
        )

    def _result(
        self, outcome: AgentSessionOutcome, stop_reason: str
    ) -> AgentSessionResult:
        result = AgentSessionResult(
            outcome=outcome,
            stop_reason=stop_reason,
            turns=len(self.records),
            patch_attempts=self.patch_attempts,
            verification_runs=self.verification_runs,
            changed_files=self.inspector.changed_paths(),
            turn_records=self.records,
            verification_results=self.verification_results,
        )
        self.audit.write_json(
            f"{self.audit_prefix}agent-session.json",
            result,
            kind="agent_session",
        )
        return result
