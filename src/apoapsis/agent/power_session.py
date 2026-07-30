"""The Local Power Sandbox execution path (ADR 0059).

This is an *opt-in experiment*, not a replacement for `BoundedAgentSession`.
The strict one-action loop remains the default and is unchanged by this file.

The hypothesis being tested is narrow and falsifiable: a small local model
(Laguna S 2.1 in particular) produces usable code but fails the strict loop on
protocol mechanics -- tool-call wrapper residue, cross-action fields, very long
retries, hand-authored diff syntax. If that is really a protocol problem rather
than a capability problem, then letting the model write whole files inside a
disposable sandbox, and having the harness compute the diff, should move the
success rate materially. If it does not, the hypothesis is wrong and this mode
should be deleted rather than expanded.

What deliberately does NOT change:

* The sandbox is the disposable per-task worktree, never the project checkout
  and never the Apoapsis repository.
* Apoapsis internals, `.git`, credentials, and the user's system stay out of
  reach; `apoapsis.agent.sandbox` enforces this on every single action.
* The final change is represented as an ordinary diff computed by the harness.
* Configured verification -- not the model's `finish` summary -- decides the
  outcome, and only the harness or the user may accept, apply, or complete.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from pydantic import Field

from apoapsis.agent.inspection import AgentInspectionError, RepositoryInspector
from apoapsis.agent.power_actions import (
    ChangeSetOperationKind,
    PowerActionError,
    PowerChangeSetOperation,
    PowerDeleteFileAction,
    PowerFinishAction,
    PowerProposeChangeSetAction,
    PowerReadFileAction,
    PowerRunShellAction,
    PowerRunVerificationAction,
    PowerSearchAction,
    PowerWriteFileAction,
    parse_power_action,
    power_action_schema,
)
from apoapsis.agent.sandbox import (
    ChangeBudget,
    SandboxGuard,
    SandboxShell,
    SandboxViolation,
    ShellOutcome,
    ShellPolicy,
)
from apoapsis.agent.session import AgentSessionOutcome, AgentSessionResult, AgentTurnRecord
from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import CompletionPolicy, LocalPowerConfig
from apoapsis.context.compiler import ContextPackage
from apoapsis.context.provenance import (
    ContextEvidence,
    EvidenceKind,
    TransmissionPolicy,
)
from apoapsis.models.base import ModelOperation, ModelResponse
from apoapsis.models.prompts import local_power_step_prompt
from apoapsis.models.provider import ModelRole
from apoapsis.repository.fingerprint import compute_worktree_fingerprint
from apoapsis.specification.schema import StrictModel, TaskSpecification
from apoapsis.verification.contract import (
    VerificationContractAssessment,
    assess_verification_contract,
)
from apoapsis.verification.failures import FailureNormalizer, NormalizedFailure
from apoapsis.verification.results import VerificationResult, VerificationStatus
from apoapsis.verification.runner import (
    VerificationCommand,
    VerificationConfig,
    VerificationRunner,
)
from apoapsis.workflow.acceptance import (
    AcceptanceCoverage,
    acceptance_coverage_satisfied,
    compute_acceptance_coverage,
)


PowerModelCall = Callable[..., ModelResponse]


_NOT_PASSED_STATUSES = frozenset(
    {
        VerificationStatus.FAILED,
        VerificationStatus.TIMED_OUT,
        VerificationStatus.ERROR,
    }
)

# How many times the harness will refuse a premature `finish` before letting
# the session end anyway. Two is enough to correct a model that simply had
# not noticed an outstanding check, and few enough that a model determined to
# stop cannot be made to spend its whole remaining budget being told no.
_MAX_FINISH_REFUSALS = 2


class SandboxCommandRecord(StrictModel):
    """One mediated shell execution, exactly as it was run."""

    turn: int = Field(ge=1)
    command: str
    argv: list[str] = Field(default_factory=list)
    cwd: str
    exit_code: int | None = None
    timed_out: bool = False
    duration_seconds: float = Field(default=0.0, ge=0)
    stdout: str = ""
    stderr: str = ""


class SandboxRejectionRecord(StrictModel):
    """One request the boundary refused, kept so probing is visible in review."""

    turn: int = Field(ge=1)
    action: str
    detail: str
    reason: str


class ChangeSetOperationRecord(StrictModel):
    """What the harness actually did with one operation in a proposal."""

    operation: str
    path: str
    # "created", "replaced", "deleted", or "rejected". Recorded per operation
    # because a reviewer asking "what did this turn change" should not have to
    # reconstruct it from a diff, and because an atomic rejection needs to name
    # the operation that caused it without implying the others were applied.
    outcome: str
    characters: int = Field(default=0, ge=0)
    detail: str = ""


class ChangeSetProposalRecord(StrictModel):
    """One atomic multi-file proposal, applied or refused in full (ADR 0071)."""

    turn: int = Field(ge=1)
    summary: str
    applied: bool
    operations: list[ChangeSetOperationRecord] = Field(default_factory=list)
    requested_verification_commands: list[str] = Field(default_factory=list)
    # The model's optimistic-concurrency claim, if it made one, next to the
    # digest the harness actually observed. Kept even when they match, so a
    # reviewer can see that the claim was checked rather than assumed.
    claimed_base_digest: str | None = None
    observed_base_digest: str = ""
    resulting_digest: str | None = None
    rejection_reason: str | None = None


class LocalPowerReviewPackage(StrictModel):
    """Everything a human needs to decide whether to accept this work.

    Assembled by the harness from observed facts only. The model contributes
    exactly one field -- `model_summary` -- and it is explicitly labelled as a
    claim rather than a finding.
    """

    task_id: str
    experimental: bool = True
    sandbox_root: str
    base_commit: str
    stop_reason: str
    model_summary: str | None = None
    contract_assessment: VerificationContractAssessment | None = None
    final_diff: str = ""
    changed_files: list[str] = Field(default_factory=list)
    generated_byproducts: list[str] = Field(default_factory=list)
    commands_run: list[SandboxCommandRecord] = Field(default_factory=list)
    rejected_requests: list[SandboxRejectionRecord] = Field(default_factory=list)
    change_sets: list[ChangeSetProposalRecord] = Field(default_factory=list)
    transcript: list[AgentTurnRecord] = Field(default_factory=list)
    verification_results: list[VerificationResult] = Field(default_factory=list)
    verification_passed: bool = False
    acceptance_coverage: list[AcceptanceCoverage] = Field(default_factory=list)
    requires_human_review: bool = True


class LocalPowerSession:
    """A mediated, budgeted, disposable-sandbox loop for a local coding model."""

    def __init__(
        self,
        *,
        specification: TaskSpecification,
        worktree: str | Path,
        initial_context: ContextPackage,
        config: LocalPowerConfig,
        verification_config: VerificationConfig,
        audit: TaskAuditStore,
        model_call: PowerModelCall,
        model_role: ModelRole = ModelRole.LOCAL_CODING_AGENT,
        audit_prefix: str = "local-power-",
        completion_policy: CompletionPolicy = CompletionPolicy.BASELINE,
        shell: SandboxShell | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.specification = specification
        self.worktree = Path(worktree).resolve()
        self.base_context = initial_context
        self.config = config
        self.verification_config = verification_config
        self.audit = audit
        self.model_call = model_call
        self.model_role = model_role
        self.audit_prefix = audit_prefix
        self.completion_policy = completion_policy
        self.clock = clock

        self.guard = SandboxGuard(
            self.worktree,
            forbidden_paths=config.forbidden_paths,
            max_file_chars=config.max_file_chars,
        )
        self.shell = shell or SandboxShell(
            self.worktree,
            policy=ShellPolicy(
                allow_shell=config.allow_shell,
                allow_network=config.allow_network,
                timeout_seconds=config.max_shell_seconds,
                max_output_chars=config.max_shell_output_chars,
            ),
            guard=self.guard,
        )
        self.budget = ChangeBudget(
            max_changed_files=config.max_changed_files,
            max_changed_lines=config.max_changed_lines,
            max_shell_commands=config.max_shell_commands if config.allow_shell else 0,
            max_seconds=config.max_seconds,
            started_monotonic=clock(),
        )
        # Reused verbatim from the strict loop: same bounded search/read/diff
        # implementation, same untracked-file handling, same Git-aware diff.
        # The sandbox mode changes what the model may *write*, not how the
        # harness observes the result.
        self.inspector = RepositoryInspector(
            self.worktree,
            max_search_results=config.max_search_results,
            max_read_lines=config.max_read_lines,
            max_chars=config.max_observation_chars,
        )

        self.records: list[AgentTurnRecord] = []
        self.observations: list[ContextEvidence] = []
        self.observation_chars = 0
        self.commands_run: list[SandboxCommandRecord] = []
        self.rejections: list[SandboxRejectionRecord] = []
        self.change_sets: list[ChangeSetProposalRecord] = []
        self.verification_results: list[VerificationResult] = []
        # Keyed by worktree fingerprint, exactly as `BoundedAgentSession`
        # keys it (ADR 0017). A flat name->status map -- what this session
        # used before ADR 0069 -- cannot express "passed, but for code that
        # has since changed", which is the difference between reusing a
        # result and fabricating one.
        self.command_results: dict[str, dict[str, VerificationStatus]] = {}
        self.verification_cache: dict[str, VerificationResult] = {}
        self.acceptance_coverage: list[AcceptanceCoverage] = []
        self.failure_normalizer = FailureNormalizer()
        # Edits made since the most recent verification run. The finish gate
        # (ADR 0070) reads this to tell "the model tried something and it may
        # or may not have worked" apart from "the model declared victory
        # having changed nothing and run nothing".
        self.edits_since_verification = 0
        self.finish_refusals = 0
        self.model_summary: str | None = None
        self.review_package: LocalPowerReviewPackage | None = None
        self.contract_assessment: VerificationContractAssessment = (
            assess_verification_contract(
                specification,
                list(verification_config.commands),
                completion_policy,
            )
        )

    # -- public entry point -------------------------------------------------

    @classmethod
    def resume(
        cls,
        *,
        specification: TaskSpecification,
        worktree: str | Path,
        initial_context: ContextPackage,
        config: LocalPowerConfig,
        verification_config: VerificationConfig,
        audit: TaskAuditStore,
        model_call: PowerModelCall,
        prior_result: AgentSessionResult,
        prior_review_package: LocalPowerReviewPackage | None = None,
        model_role: ModelRole = ModelRole.LOCAL_CODING_AGENT,
        audit_prefix: str = "local-power-",
        completion_policy: CompletionPolicy = CompletionPolicy.BASELINE,
        shell: SandboxShell | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> "LocalPowerSession":
        """Reconstruct a sandbox session continuing from ``prior_result``.

        The local-power analogue of ``BoundedAgentSession.resume`` (ADR
        0020), and deliberately the same shape: prior turn records, the
        observation ledger, verification results, and acceptance coverage
        are seeded rather than reset, so ``config`` must already express
        the harness-authorized *cumulative* ceiling -- counters here only
        increase, and ``run(start_turn=...)`` indexes into the same budget
        the prior stage was spending from.

        Two seams differ from the strict loop and are deliberate:

        * The shell-command budget is restored from
          ``prior_review_package`` when one is supplied. Without it the
          sandbox would hand a resumed model a fresh allowance of shell
          commands, which is a real authority boundary rather than a
          bookkeeping detail -- so callers that can supply the package
          should, and this signature makes its absence explicit rather
          than silent.
        * The wall clock restarts. ``max_seconds`` bounds one authorized
          stage, not the task's lifetime; a human authorizing a
          continuation is authorizing another stage.

        The prior run's rejection records are restored for prompt context
        so a resumed model still sees what it was previously refused, and
        does not simply re-request it.
        """

        session = cls(
            specification=specification,
            worktree=worktree,
            initial_context=initial_context,
            config=config,
            verification_config=verification_config,
            audit=audit,
            model_call=model_call,
            model_role=model_role,
            audit_prefix=audit_prefix,
            completion_policy=completion_policy,
            shell=shell,
            clock=clock,
        )
        session.records = list(prior_result.turn_records)
        if prior_result.turn_records:
            last = prior_result.turn_records[-1]
            session.observations = list(last.observation_ledger)
            session.observation_chars = last.observation_ledger_chars
        session.verification_results = list(prior_result.verification_results)
        session.acceptance_coverage = list(prior_result.acceptance_coverage)
        if prior_review_package is not None:
            session.commands_run = list(prior_review_package.commands_run)
            session.rejections = list(prior_review_package.rejected_requests)
            session.change_sets = list(prior_review_package.change_sets)
            session.budget.shell_commands_used = min(
                len(prior_review_package.commands_run),
                session.budget.max_shell_commands,
            )
        session._seed_prior_failure_evidence(prior_result.verification_results)
        return session

    def _seed_prior_failure_evidence(
        self, prior_results: list[VerificationResult]
    ) -> None:
        """Carry the last unresolved failure into the resumed prompt.

        ADR 0070. A continuation exists *because* something failed, so
        starting one with no record of what failed is the one thing it must
        not do. On `TASK-E01762481075` the continuation package held the
        `web-product-integrity` failure and the resumed prompt did not, so
        the model reasoned from a history in which everything had passed.

        Only the most recent non-passing result is seeded, and only for a
        command that is not already passing in a later result: older
        failures for since-fixed commands are noise, and noise in a repair
        prompt is worse than silence.
        """

        if not prior_results:
            return
        settled: set[str] = set()
        for result in reversed(prior_results):
            for item in result.commands:
                if item.status == VerificationStatus.PASSED:
                    settled.add(item.name)
                    continue
                if item.name in settled:
                    continue
                settled.add(item.name)
                try:
                    _, failure = self.failure_normalizer.extract(result, self.worktree)
                except ValueError:
                    continue
                if failure.command_name != item.name:
                    continue
                self._add_failure_evidence(failure, len(prior_results))

    def run(self, *, start_turn: int = 1) -> AgentSessionResult:
        stop_reason = f"local power turn budget exhausted after {self.config.max_turns} turns"
        for turn in range(start_turn, self.config.max_turns + 1):
            if self.budget.wall_clock_exhausted(now=self.clock()):
                stop_reason = (
                    "local power wall-clock budget exhausted after "
                    f"{self.config.max_seconds:.0f} seconds"
                )
                break
            response = self.model_call(
                ModelOperation.AGENT_STEP,
                self._prompt(turn),
                self._context_for_turn(turn),
                requested_output="one_local_power_action_json",
                response_schema=power_action_schema(
                    include_change_sets=self.config.atomic_change_sets
                ),
                role=self.model_role,
            )
            try:
                action = parse_power_action(response.content)
            except PowerActionError as exc:
                self._record(turn, "invalid_action", accepted=False, summary=str(exc)[:2_000])
                continue
            if isinstance(action, PowerFinishAction):
                blocked = self._finish_blocked_reason()
                if blocked is not None:
                    self._reject(turn, action, blocked)
                    self.finish_refusals += 1
                    continue
                self.model_summary = action.summary
                self._record(
                    turn,
                    action.action,
                    accepted=True,
                    summary=(
                        "model reported it was finished; harness-owned "
                        "verification still decides the outcome"
                    ),
                )
                stop_reason = "model finished; awaiting harness verification"
                break
            try:
                self._execute(turn, action)
            except (SandboxViolation, AgentInspectionError) as exc:
                self._reject(turn, action, str(exc))
                continue
            # Only a verification can newly satisfy the contract, so the
            # question is asked only after one. Asking after every action
            # would compute a worktree fingerprint on every read, search,
            # and shell turn for an answer that cannot have changed. An
            # applied change set counts because the harness runs the required
            # commands itself as part of applying it (ADR 0071).
            if isinstance(
                action, (PowerRunVerificationAction, PowerProposeChangeSetAction)
            ) and self._verification_sufficient():
                # ADR 0069. The model is not asked whether a passing check is
                # enough, because on the live trial it answered that question
                # by re-requesting the same passing check until its turn
                # budget ran out. Sufficiency is a deterministic property of
                # the configured contract and the current sandbox state, so
                # the harness decides it and stops.
                stop_reason = (
                    "configured verification passed for the current sandbox "
                    "state; harness ended the session"
                )
                break
        return self._finalize(stop_reason)

    def interrupted(self, reason: str) -> AgentSessionResult:
        """Persist a deterministic stop when the selected provider fails."""

        return self._finalize(reason, run_verification=False)

    # -- action execution ---------------------------------------------------

    def _execute(self, turn: int, action: object) -> None:
        if isinstance(action, PowerReadFileAction):
            # Routed through the guard, not the inspector, so the forbidden
            # globs apply to reads exactly as they do to writes. A model must
            # not be able to read `.env` merely because reading is harmless
            # to the filesystem -- reading a secret is the harm.
            content = self.guard.read_text(action.path)
            relative = self.guard.relative(action.path).as_posix()
            self._add_evidence(
                ContextEvidence(
                    evidence_id=f"EV-POWER-READ-{turn:03d}",
                    kind=EvidenceKind.FILE_EXCERPT,
                    path=relative,
                    commit=f"{self.base_context.head_commit}+sandbox",
                    reason_included="model-requested sandbox file read",
                    content=content,
                    transmission_policy=TransmissionPolicy.LOCAL_ONLY,
                )
            )
            self._record(
                turn,
                action.action,
                accepted=True,
                summary=f"read {relative} ({len(content)} characters)",
            )
            return

        if isinstance(action, PowerSearchAction):
            evidence = self.inspector.search(action.query, action.path_glob)
            permitted = [
                item
                for item in evidence
                if self.guard.forbidden_reason(self.guard.relative(item.path)) is None
            ]
            for item in permitted:
                self._add_evidence(item)
            self._record(
                turn,
                action.action,
                accepted=True,
                summary=(
                    f"literal search returned {len(permitted)} permitted matches "
                    f"({len(evidence) - len(permitted)} suppressed as forbidden)"
                ),
            )
            return

        if isinstance(action, PowerWriteFileAction):
            self._write_file(turn, action)
            return

        if isinstance(action, PowerDeleteFileAction):
            self._delete_file(turn, action)
            return

        if isinstance(action, PowerProposeChangeSetAction):
            self._apply_change_set(turn, action)
            return

        if isinstance(action, PowerRunShellAction):
            self._run_shell(turn, action)
            return

        if isinstance(action, PowerRunVerificationAction):
            self._run_verification(turn, action)
            return

        raise TypeError(f"unsupported local power action: {type(action).__name__}")

    def _write_file(self, turn: int, action: PowerWriteFileAction) -> None:
        target = self.guard.resolve(action.path)
        self.guard.validate_content(action.path, action.content)
        existed = target.is_file()
        previous = target.read_bytes() if existed else None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(action.content, encoding="utf-8", newline="")
        try:
            self._assert_within_change_budget()
        except SandboxViolation:
            # Restore byte-for-byte rather than leaving a half-applied change
            # the harness would then have to diff and review. The budget is a
            # boundary, so crossing it must leave no trace in the sandbox.
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(previous)
            raise
        relative = self.guard.relative(action.path).as_posix()
        self.edits_since_verification += 1
        self._record(
            turn,
            action.action,
            accepted=True,
            summary=(
                f"{'replaced' if existed else 'created'} {relative} "
                f"({len(action.content)} characters)"
            ),
        )

    def _delete_file(self, turn: int, action: PowerDeleteFileAction) -> None:
        target = self.guard.resolve(action.path)
        if not target.is_file():
            raise SandboxViolation(f"file does not exist in the sandbox: {action.path}")
        previous = target.read_bytes()
        target.unlink()
        try:
            self._assert_within_change_budget()
        except SandboxViolation:
            target.write_bytes(previous)
            raise
        relative = self.guard.relative(action.path).as_posix()
        self.edits_since_verification += 1
        self._record(turn, action.action, accepted=True, summary=f"deleted {relative}")

    # -- atomic multi-file proposals (ADR 0071) -----------------------------

    def _max_change_set_files(self) -> int:
        """The per-proposal file ceiling, never above the session ceiling.

        `min` rather than a validator, so lowering `max_changed_files` always
        lowers the per-proposal bound too and the two ceilings cannot be
        configured into disagreeing with each other.
        """

        return min(self.config.max_change_set_files, self.config.max_changed_files)

    def _apply_change_set(
        self, turn: int, action: PowerProposeChangeSetAction
    ) -> None:
        """Validate a whole proposal, then apply all of it or none of it.

        Every check runs before a single byte is written. That ordering is the
        feature: a proposal that is invalid anywhere leaves the sandbox exactly
        as it was, so the next turn repairs one coherent slice rather than
        reasoning about a half-applied one.
        """

        if not self.config.atomic_change_sets:
            raise SandboxViolation(
                "atomic change sets are disabled for this session; use "
                "write_file or delete_file for one file at a time"
            )
        observed = self._verification_state_digest()
        operations, reason = self._validate_change_set(action, observed)
        if reason is not None:
            self._record_change_set(
                turn,
                action,
                applied=False,
                operations=operations,
                observed=observed,
                resulting=None,
                reason=reason,
            )
            raise SandboxViolation(reason)
        applied, restore = self._write_change_set(action)
        try:
            self._assert_within_change_budget()
        except SandboxViolation as exc:
            self._restore_change_set(restore)
            rejection = (
                f"change set refused and rolled back in full: {exc}. The "
                "sandbox is byte-for-byte as it was before this proposal; "
                "propose a smaller slice."
            )
            self._record_change_set(
                turn,
                action,
                applied=False,
                operations=[
                    ChangeSetOperationRecord(
                        operation=item.operation,
                        path=item.path,
                        outcome="rejected",
                        characters=item.characters,
                        detail="rolled back: change set exceeded a session ceiling",
                    )
                    for item in applied
                ],
                observed=observed,
                resulting=None,
                reason=rejection,
            )
            raise SandboxViolation(rejection) from exc
        self.edits_since_verification += len(applied)
        resulting = self._verification_state_digest()
        self._record_change_set(
            turn,
            action,
            applied=True,
            operations=applied,
            observed=observed,
            resulting=resulting,
            reason=None,
        )
        created = sum(1 for item in applied if item.outcome == "created")
        replaced = sum(1 for item in applied if item.outcome == "replaced")
        deleted = sum(1 for item in applied if item.outcome == "deleted")
        self._record(
            turn,
            action.action,
            accepted=True,
            summary=(
                f"applied an atomic change set of {len(applied)} operations "
                f"({created} created, {replaced} replaced, {deleted} deleted)"
            ),
        )
        self._verify_change_set(turn, action)

    def _validate_change_set(
        self, action: PowerProposeChangeSetAction, observed: str
    ) -> tuple[list[ChangeSetOperationRecord], str | None]:
        """Check every operation and report all of the problems, not the first.

        A model told only about its first bad path fixes that one and
        rediscovers the next on the following turn. Since the whole proposal
        is rejected anyway, the complete list costs nothing and is the
        difference between one repair turn and four.
        """

        problems: list[str] = []
        records: list[ChangeSetOperationRecord] = []
        claimed = action.base_worktree_digest
        if claimed is not None and claimed != observed:
            problems.append(
                f"base_worktree_digest {claimed!r} does not match the current "
                f"sandbox state {observed!r}; the files changed since you read "
                "them, so re-read what you are about to overwrite"
            )
        ceiling = self._max_change_set_files()
        if len(action.changes) > ceiling:
            problems.append(
                f"change set touches {len(action.changes)} files, above the "
                f"per-proposal ceiling of {ceiling}"
            )
        configured = {item.name for item in self.verification_config.commands}
        unknown = [
            name for name in action.verification_commands if name not in configured
        ]
        if unknown:
            problems.append(
                f"requested verification commands {sorted(unknown)} are not "
                f"configured; configured names are {sorted(configured)}"
            )
        seen: set[str] = set()
        for change in action.changes:
            detail = self._validate_change(change, seen)
            records.append(
                ChangeSetOperationRecord(
                    operation=change.operation.value,
                    path=change.path[:500],
                    outcome="rejected" if detail else "planned",
                    characters=len(change.content or ""),
                    detail=detail or "",
                )
            )
            if detail:
                problems.append(f"{change.path}: {detail}")
        if not problems:
            return records, None
        listed = "\n".join(f"- {item}" for item in problems)
        return records, (
            f"change set refused in full; {len(problems)} problem(s) found and "
            "nothing was written:\n"
            f"{listed}\n"
            "The sandbox is unchanged. Send one corrected change set."
        )

    def _validate_change(
        self, change: PowerChangeSetOperation, seen: set[str]
    ) -> str | None:
        try:
            relative = self.guard.relative(change.path).as_posix()
        except SandboxViolation as exc:
            return str(exc)
        if relative in seen:
            return (
                "named twice in one change set; a proposal must state one "
                "final intent per file"
            )
        seen.add(relative)
        try:
            target = self.guard.resolve(change.path)
        except SandboxViolation as exc:
            return str(exc)
        if change.operation == ChangeSetOperationKind.DELETE:
            if not target.is_file():
                return "file does not exist in the sandbox and cannot be deleted"
            protecting = self._verification_dependency(relative)
            if protecting is not None:
                return (
                    f"is named by the configured verification command "
                    f"{protecting!r} and cannot be deleted by a proposal; "
                    "checks are not removed to make them pass"
                )
            return None
        try:
            self.guard.validate_content(change.path, change.content or "")
        except SandboxViolation as exc:
            return str(exc)
        return None

    def _verification_dependency(self, relative: str) -> str | None:
        """The configured command, if any, that names ``relative`` in its argv.

        Narrow and literal on purpose. It does not try to infer what a check
        reads; it only refuses deleting a path the harness can see the check
        was pointed at, which is the one destructive shortcut worth naming.
        """

        for command in self.verification_config.commands:
            for token in command.argv[1:]:
                normalized = token.replace("\\", "/")
                # One leading `./` only. `str.lstrip("./")` would strip a
                # character set and turn `.github/workflows` into
                # `github/workflows`, matching the wrong file.
                while normalized.startswith("./"):
                    normalized = normalized[2:]
                normalized = normalized.rstrip("/")
                if not normalized:
                    continue
                if relative == normalized or relative.startswith(f"{normalized}/"):
                    return command.name
        return None

    def _write_change_set(
        self, action: PowerProposeChangeSetAction
    ) -> tuple[list[ChangeSetOperationRecord], list[tuple[Path, bytes | None]]]:
        """Perform the validated operations, keeping enough to undo all of them."""

        applied: list[ChangeSetOperationRecord] = []
        restore: list[tuple[Path, bytes | None]] = []
        for change in action.changes:
            target = self.guard.resolve(change.path)
            relative = self.guard.relative(change.path).as_posix()
            existed = target.is_file()
            restore.append((target, target.read_bytes() if existed else None))
            if change.operation == ChangeSetOperationKind.DELETE:
                target.unlink()
                applied.append(
                    ChangeSetOperationRecord(
                        operation=change.operation.value,
                        path=relative,
                        outcome="deleted",
                    )
                )
                continue
            content = change.content or ""
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="")
            applied.append(
                ChangeSetOperationRecord(
                    operation=change.operation.value,
                    path=relative,
                    outcome="replaced" if existed else "created",
                    characters=len(content),
                )
            )
        return applied, restore

    def _restore_change_set(self, restore: list[tuple[Path, bytes | None]]) -> None:
        """Put every touched file back byte-for-byte, newest change first.

        A directory created to hold a new file is left in place: it is empty,
        invisible to Git, and removing it would mean deciding whether an
        empty directory that existed beforehand was ours to remove.
        """

        for target, previous in reversed(restore):
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(previous)

    def _verify_change_set(
        self, turn: int, action: PowerProposeChangeSetAction
    ) -> None:
        """Run the required commands for the state the proposal just produced.

        The harness runs them, not the model. A coherent increment that then
        has to spend a separate turn asking whether it worked is the
        granularity problem this ADR exists to remove, and the answer belongs
        to the harness in either case.
        """

        if not self.config.verify_after_change_set:
            return
        names = {item.name for item in self.verification_config.commands if item.required}
        names.update(action.verification_commands)
        commands = [
            item for item in self.verification_config.commands if item.name in names
        ]
        if not commands:
            return
        try:
            result = self._verify(commands)
        except SandboxViolation:
            # Reached only when the identical set already ran for this exact
            # state -- a change set that wrote back byte-identical content.
            # Nothing to report that the prompt does not already carry.
            return
        failed = next(
            (item for item in result.commands if item.status != VerificationStatus.PASSED),
            None,
        )
        self._record(
            turn,
            "run_verification",
            accepted=True,
            summary=(
                "harness verified the applied change set: passed"
                if failed is None
                else (
                    f"harness verified the applied change set: {failed.name} "
                    f"{failed.status.value} with exit code {failed.exit_code}"
                )
            ),
            verification_run=len(self.verification_results),
            verification_status=result.status,
        )

    def _record_change_set(
        self,
        turn: int,
        action: PowerProposeChangeSetAction,
        *,
        applied: bool,
        operations: list[ChangeSetOperationRecord],
        observed: str,
        resulting: str | None,
        reason: str | None,
    ) -> None:
        record = ChangeSetProposalRecord(
            turn=turn,
            summary=action.summary[:8_000],
            applied=applied,
            operations=operations,
            requested_verification_commands=list(action.verification_commands),
            claimed_base_digest=action.base_worktree_digest,
            observed_base_digest=observed,
            resulting_digest=resulting,
            rejection_reason=reason[:4_000] if reason is not None else None,
        )
        self.change_sets.append(record)
        self.audit.write_json(
            f"{self.audit_prefix}change-set-{len(self.change_sets):03d}.json",
            record,
            kind="local_power_change_set",
        )

    def _run_shell(self, turn: int, action: PowerRunShellAction) -> None:
        # Parse (and therefore authorize) before spending budget, so a refused
        # command never costs the model one of its allowed executions.
        self.shell.parse(action.command)
        self.budget.consume_shell_command()
        remaining = self.budget.remaining_seconds(now=self.clock())
        outcome = self.shell.run(action.command, timeout_seconds=max(1.0, remaining))
        self._record_command(turn, outcome)
        self._add_evidence(
            ContextEvidence(
                evidence_id=f"EV-POWER-SHELL-{len(self.commands_run):03d}",
                kind=EvidenceKind.FAILURE,
                path=f"<sandbox_shell:{outcome.argv[0] if outcome.argv else 'command'}>",
                commit=f"{self.base_context.head_commit}+sandbox",
                reason_included="mediated sandbox command output",
                content=f"$ {outcome.command}\n{outcome.stdout}\n{outcome.stderr}".strip(),
                transmission_policy=TransmissionPolicy.LOCAL_ONLY,
            )
        )
        self._record(turn, action.action, accepted=True, summary=outcome.summary())

    def _run_verification(self, turn: int, action: PowerRunVerificationAction) -> None:
        commands = self._select_verification_commands(action.command_name)
        result = self._verify(commands, reuse_cached=False)
        failed = next(
            (item for item in result.commands if item.status != VerificationStatus.PASSED),
            None,
        )
        self._record(
            turn,
            action.action,
            accepted=True,
            summary=(
                "deterministic verification passed"
                if failed is None
                else f"{failed.name} {failed.status.value} with exit code {failed.exit_code}"
            ),
            verification_run=len(self.verification_results),
            verification_status=result.status,
        )

    def _select_verification_commands(
        self, command_name: str | None
    ) -> list[VerificationCommand]:
        if command_name is None:
            return list(self.verification_config.commands)
        command = next(
            (
                item
                for item in self.verification_config.commands
                if item.name == command_name
            ),
            None,
        )
        if command is None:
            allowed = [item.name for item in self.verification_config.commands]
            raise SandboxViolation(
                f"unknown verification command {command_name!r}; configured "
                f"names are {allowed}"
            )
        return [command]

    # -- harness-owned verification and completion --------------------------

    def _verification_state_digest(self) -> str:
        """The shared worktree fingerprint (ADR 0017): HEAD identity, the
        canonical tracked diff, and every permitted untracked file's exact
        content. A brand-new file written by `write_file` changes it exactly
        as an edit to a tracked file would, which matters here because the
        sandbox's normal case is creating files that never existed."""

        return compute_worktree_fingerprint(self.worktree).digest

    def _current_command_results(self) -> dict[str, VerificationStatus]:
        return dict(self.command_results.get(self._verification_state_digest(), {}))

    def _verify(
        self, commands: list[VerificationCommand], *, reuse_cached: bool = True
    ) -> VerificationResult:
        """Run ``commands`` and record the result against the current state.

        ``reuse_cached=False`` is the model-requested path: re-running an
        identical command set against an unchanged sandbox cannot produce a
        different answer, so the request is refused rather than executed.
        Refusal, not silent reuse, is deliberate -- the refusal lands in
        REFUSED_REQUESTS_JSON, which the prompt already tells the model not
        to work around, whereas a silently reused pass reads to the model
        exactly like a fresh one and invites the same request again.

        ``reuse_cached=True`` is the harness's own finalization path, where
        reuse is the correct behavior: the answer is already known for this
        exact state, and spending a sixth identical run to re-derive it
        would be waste dressed up as rigor.
        """

        if not commands:
            raise SandboxViolation("no verification commands are configured")
        state_digest = self._verification_state_digest()
        cache_key = f"{state_digest}:{','.join(sorted(item.name for item in commands))}"
        cached = self.verification_cache.get(cache_key)
        if cached is not None:
            if reuse_cached:
                return cached
            raise SandboxViolation(
                "this exact verification already ran for the current sandbox "
                "state and its result cannot change until a file changes; "
                "edit a file or finish"
            )
        selected = self.verification_config.model_copy(update={"commands": commands})
        attempt = len(self.verification_results) + 1
        result = VerificationRunner(selected).run(
            self.specification.task_id, self.worktree, attempt=attempt
        )
        self.verification_results.append(result)
        self.verification_cache[cache_key] = result
        digest_results = self.command_results.setdefault(state_digest, {})
        for item in result.commands:
            if item.status == VerificationStatus.SKIPPED:
                continue
            digest_results[item.name] = item.status
        self.audit.write_json(
            f"{self.audit_prefix}verification-{attempt:03d}.json",
            result,
            kind="verification_result",
        )
        self.edits_since_verification = 0
        self._record_failure_evidence(result, attempt)
        return result

    def _record_failure_evidence(
        self, result: VerificationResult, attempt: int
    ) -> None:
        """Normalize a failing check and put it where the model will read it.

        This is the whole of ADR 0070's first defect. On live task
        `TASK-E01762481075` the harness recorded `web-product-integrity`'s
        failure correctly, carried it into the continuation package
        correctly, and then built the next Local Power prompt without it.
        The model saw a history in which the only check it had ever run had
        passed, re-ran that check, and reported that everything passed. It
        was not lying about what it could see. `BoundedAgentSession` has
        done this since ADR 0011; the sandbox path simply never did.

        An acceptance-designated command's failure counts even when the
        aggregate status stays PASSED because the command is
        `required = false` (ADR 0018) -- that is exactly the shape of the
        contract this feature exists to support.
        """

        acceptance_failed = any(
            item.acceptance and item.status in _NOT_PASSED_STATUSES
            for item in result.commands
        )
        if result.status == VerificationStatus.PASSED and not acceptance_failed:
            return
        try:
            _, failure = self.failure_normalizer.extract(result, self.worktree)
        except ValueError:
            # No required or acceptance-designated command failed, so there
            # is nothing the model is obliged to repair. Never invent
            # failure evidence from a development-only command's noise.
            return
        self.audit.write_json(
            f"{self.audit_prefix}verification-failure-{attempt:03d}.json",
            failure,
            kind="normalized_failure",
        )
        self._add_failure_evidence(failure, attempt)

    def _add_failure_evidence(self, failure: NormalizedFailure, attempt: int) -> None:
        self._add_evidence(
            ContextEvidence(
                evidence_id=f"EV-POWER-FAILURE-{attempt:03d}",
                kind=EvidenceKind.FAILURE,
                path=f"<verification:{failure.command_name}>",
                commit=f"{self.base_context.head_commit}+sandbox",
                reason_included=(
                    "normalized output of a configured check that did not "
                    "pass; this is the work that remains"
                ),
                content=(
                    f"$ {' '.join(failure.argv)}\n"
                    f"status: {failure.status.value}"
                    f" (exit code {failure.exit_code})\n"
                    f"{failure.relevant_error}"
                ),
                transmission_policy=TransmissionPolicy.LOCAL_ONLY,
            )
        )

    def _required_command_states(self) -> list[dict[str, object]]:
        """Per configured command: what is known about it *right now*.

        Deliberately four-valued. "Passed, but for code that has since
        changed" and "passed, for this exact code" are different facts, and
        collapsing them is how a model comes to believe a stale result
        still counts.
        """

        current = self._current_command_results()
        ever: dict[str, VerificationStatus] = {}
        for results in self.command_results.values():
            ever.update(results)
        states: list[dict[str, object]] = []
        for item in self.verification_config.commands:
            status = current.get(item.name)
            if status == VerificationStatus.PASSED:
                state = "passing_for_current_code"
            elif status is not None:
                state = f"{status.value}_for_current_code"
            elif item.name in ever:
                state = "passed_earlier_but_the_code_has_changed_since"
            else:
                state = "never_run"
            states.append(
                {
                    "command": item.name,
                    "required": item.required,
                    "acceptance_designated": item.acceptance,
                    "state": state,
                    "counts_as_outstanding": item.name in self._outstanding_commands(),
                }
            )
        return states

    def _outstanding_commands(self) -> list[str]:
        """Required commands with no passing result for the current code.

        These, and only these, are what stands between this sandbox and a
        completed task. Non-required commands are excluded because they
        cannot gate the outcome, and saying otherwise would send the model
        chasing something that does not matter.
        """

        current = self._current_command_results()
        return [
            item.name
            for item in self.verification_config.commands
            if item.required
            and current.get(item.name) != VerificationStatus.PASSED
        ]

    def _finish_blocked_reason(self) -> str | None:
        """Refuse `finish` when nothing has been done about what is failing.

        ADR 0070. The bar is deliberately low and mechanical: the model may
        finish once it has either changed a file since the last check or
        actually run the outstanding command at the current state. It does
        not have to succeed. Refusal targets exactly one behavior -- ending
        the session having neither attempted a repair nor looked at the
        check that is failing -- which is what the live continuation did.

        Refusals are capped. A model that insists it is finished after
        being told twice is not going to be argued into repairing anything,
        and burning the rest of its budget on the argument helps nobody:
        the harness runs its own final verification either way, and the
        outcome will be human review.
        """

        if self.finish_refusals >= _MAX_FINISH_REFUSALS:
            return None
        if not self._permitted_changed_paths():
            # Nothing was built, so there is nothing to repair. Such a
            # session cannot complete anyway, and holding it open would
            # spend turns arguing about work that does not exist.
            return None
        outstanding = self._outstanding_commands()
        if not outstanding:
            return None
        if self.edits_since_verification > 0:
            return None
        current = self._current_command_results()
        if all(name in current for name in outstanding):
            return None
        unrun = [name for name in outstanding if name not in current]
        return (
            "not finished: "
            + ", ".join(repr(name) for name in unrun)
            + " is required and has no result for the current sandbox state, "
            "and nothing has been changed since the last check. Read the "
            "recorded failure output, edit the files it names, or run that "
            "command -- then finish."
        )

    def _verification_sufficient(self) -> bool:
        """Has the configured contract already been satisfied, for exactly
        the code that is in the sandbox right now?

        Every clause is a fact the harness owns. None of them consults the
        model's opinion, and a stale result cannot satisfy any of them
        because results are scoped to the worktree fingerprint.
        """

        if not self.verification_config.commands:
            return False
        if not self._permitted_changed_paths():
            return False
        required = {
            item.name for item in self.verification_config.commands if item.required
        }
        if not required:
            # A contract with nothing required cannot prove itself satisfied:
            # `required.issubset(passed)` would be vacuously true and would
            # end the session on the first check of any kind. Doctor already
            # reports this configuration as an error; here it simply means
            # early termination is never claimed.
            return False
        current = self._current_command_results()
        passed = {
            name
            for name, status in current.items()
            if status == VerificationStatus.PASSED
        }
        if not required.issubset(passed):
            return False
        if self.completion_policy == CompletionPolicy.BASELINE:
            return True
        self.acceptance_coverage = compute_acceptance_coverage(
            self.specification,
            self.verification_config.commands,
            current,
        )
        return acceptance_coverage_satisfied(self.acceptance_coverage)

    def _final_verification_passed(self) -> bool:
        """Run the full configured verification and decide, from its result
        alone, whether this sandbox produced acceptable work.

        Called unconditionally at the end of every session -- including one
        the model ended with `finish` -- because the model's own claim of
        completion carries no authority in either execution mode.
        """

        if not self._permitted_changed_paths():
            return False
        if not self.verification_config.commands:
            # Nothing configured can prove anything. Under the default policy
            # this is a human-review outcome, never a silent success.
            return not self.config.require_verification
        if self._verification_sufficient():
            # ADR 0069: the full required set already passed for exactly this
            # sandbox state. Running it again would consume time and produce
            # a result the harness has already recorded, so the recorded one
            # is used. This is reuse of current evidence, not an assumption:
            # the fingerprint proves nothing has changed since.
            return True
        try:
            result = self._verify(list(self.verification_config.commands))
        except SandboxViolation:
            return False
        if result.status != VerificationStatus.PASSED:
            return False
        current = self._current_command_results()
        required = {
            item.name for item in self.verification_config.commands if item.required
        }
        passed = {
            name
            for name, status in current.items()
            if status == VerificationStatus.PASSED
        }
        if not required.issubset(passed):
            return False
        if self.completion_policy == CompletionPolicy.BASELINE:
            return True
        self.acceptance_coverage = compute_acceptance_coverage(
            self.specification,
            self.verification_config.commands,
            current,
        )
        return acceptance_coverage_satisfied(self.acceptance_coverage)

    def _finalize(
        self, stop_reason: str, *, run_verification: bool = True
    ) -> AgentSessionResult:
        verification_passed = (
            self._final_verification_passed() if run_verification else False
        )
        diff_evidence = self.inspector.diff()
        changed_files = self._permitted_changed_paths()
        generated_byproducts = self._generated_byproduct_paths()
        final_diff = self._permitted_diff(
            diff_evidence.content if diff_evidence is not None else "",
            set(changed_files),
        )
        package = LocalPowerReviewPackage(
            task_id=self.specification.task_id,
            sandbox_root=str(self.worktree),
            base_commit=self.base_context.head_commit,
            stop_reason=stop_reason,
            model_summary=self.model_summary,
            contract_assessment=self.contract_assessment,
            final_diff=final_diff,
            changed_files=changed_files,
            generated_byproducts=generated_byproducts,
            commands_run=self.commands_run,
            rejected_requests=self.rejections,
            change_sets=self.change_sets,
            transcript=self.records,
            verification_results=self.verification_results,
            verification_passed=verification_passed,
            acceptance_coverage=self.acceptance_coverage,
            requires_human_review=not verification_passed,
        )
        self.review_package = package
        self.audit.write_json(
            f"{self.audit_prefix}review-package.json",
            package,
            kind="local_power_review_package",
        )
        # Written separately as well as inside the package so that a reviewer
        # reading the audit directory sees the strength of the evidence next
        # to the evidence itself, rather than having to reconstruct it from
        # configuration that may since have changed (ADR 0069).
        self.audit.write_json(
            f"{self.audit_prefix}verification-contract.json",
            self.contract_assessment,
            kind="verification_contract_assessment",
        )
        outcome = (
            AgentSessionOutcome.COMPLETE
            if verification_passed
            else AgentSessionOutcome.ESCALATION_REQUIRED
        )
        resolved_reason = (
            f"{stop_reason}; harness-computed diff passed configured verification"
            if verification_passed
            else f"{stop_reason}; configured verification did not pass"
        )
        if verification_passed and not self.contract_assessment.proves_configured_criteria:
            # A success is still a success -- the configured commands really
            # did pass -- but the sentence a human reads must not imply more
            # than the contract can support (ADR 0069).
            resolved_reason += (
                f"; verification-contract evidence level is "
                f"{self.contract_assessment.evidence_level.value}: "
                f"{self.contract_assessment.qualification}"
            )
        result = AgentSessionResult(
            outcome=outcome,
            stop_reason=resolved_reason,
            turns=len(self.records),
            patch_attempts=sum(
                1
                for item in self.records
                if item.action in {"write_file", "delete_file"} and item.accepted
            ),
            verification_runs=len(self.verification_results),
            changed_files=changed_files,
            generated_byproducts=generated_byproducts,
            turn_records=self.records,
            verification_results=self.verification_results,
            acceptance_coverage=self.acceptance_coverage,
        )
        self.audit.write_json(
            f"{self.audit_prefix}session.json", result, kind="agent_session"
        )
        return result

    # -- bookkeeping --------------------------------------------------------

    def _assert_within_change_budget(self) -> None:
        changed = self._permitted_changed_paths()
        diff_evidence = self.inspector.diff()
        diff_text = diff_evidence.content if diff_evidence is not None else ""
        changed_lines = self._changed_line_count(diff_text, set(changed))
        self.budget.assert_change_size(
            changed_files=len(changed), changed_lines=changed_lines
        )

    def _permitted_changed_paths(self) -> list[str]:
        """Reviewer-facing changed paths: sandbox-permitted and authored by the
        model. Byproducts of running tools are reported separately by
        `_generated_byproduct_paths` (ADR 0063), never folded in here."""

        return [
            path
            for path in self.inspector.reviewable_changed_paths()
            if self.guard.forbidden_reason(Path(path)) is None
        ]

    def _generated_byproduct_paths(self) -> list[str]:
        return [
            path
            for path in self.inspector.generated_byproduct_changed_paths()
            if self.guard.forbidden_reason(Path(path)) is None
        ]

    def _changed_line_count(self, diff_text: str, allowed: set[str]) -> int:
        current_allowed = False
        changed_lines = 0
        for line in diff_text.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                current_allowed = (
                    len(parts) >= 4
                    and parts[3].startswith("b/")
                    and parts[3][2:] in allowed
                )
                continue
            if (
                current_allowed
                and (line.startswith("+") or line.startswith("-"))
                and not line.startswith(("+++", "---"))
            ):
                changed_lines += 1
        return changed_lines

    def _permitted_diff(self, diff_text: str, allowed: set[str]) -> str:
        if not diff_text or not allowed:
            return ""
        chunks: list[list[str]] = []
        current: list[str] = []
        current_allowed = False
        for line in diff_text.splitlines():
            if line.startswith("diff --git "):
                if current and current_allowed:
                    chunks.append(current)
                parts = line.split()
                current_allowed = (
                    len(parts) >= 4
                    and parts[3].startswith("b/")
                    and parts[3][2:] in allowed
                )
                current = [line]
                continue
            if current:
                current.append(line)
        if current and current_allowed:
            chunks.append(current)
        return "\n".join("\n".join(chunk) for chunk in chunks)

    def _record_command(self, turn: int, outcome: ShellOutcome) -> None:
        record = SandboxCommandRecord(
            turn=turn,
            command=outcome.command,
            argv=outcome.argv,
            cwd=outcome.cwd,
            exit_code=outcome.exit_code,
            timed_out=outcome.timed_out,
            duration_seconds=outcome.duration_seconds,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
        )
        self.commands_run.append(record)
        self.audit.write_json(
            f"{self.audit_prefix}shell-{len(self.commands_run):03d}.json",
            record,
            kind="local_power_shell_command",
        )

    def _reject(self, turn: int, action: object, reason: str) -> None:
        kind = getattr(action, "action", "unknown")
        detail = (
            getattr(action, "path", None)
            or self._change_set_detail(action)
            or getattr(action, "command", None)
            or getattr(action, "command_name", None)
            or getattr(action, "query", None)
            or ""
        )
        record = SandboxRejectionRecord(
            turn=turn, action=str(kind), detail=str(detail)[:500], reason=reason[:2_000]
        )
        self.rejections.append(record)
        self.audit.write_json(
            f"{self.audit_prefix}rejection-{len(self.rejections):03d}.json",
            record,
            kind="local_power_rejected_request",
        )
        self._record(turn, str(kind), accepted=False, summary=f"refused: {reason}"[:2_000])

    def _change_set_detail(self, action: object) -> str:
        """Name the files a refused proposal wanted, since it has no `path`."""

        if not isinstance(action, PowerProposeChangeSetAction):
            return ""
        return ", ".join(item.path for item in action.changes)

    def _record(
        self,
        turn: int,
        action: str,
        *,
        accepted: bool,
        summary: str,
        verification_run: int | None = None,
        verification_status: VerificationStatus | None = None,
    ) -> None:
        record = AgentTurnRecord(
            turn=turn,
            action=action,
            accepted=accepted,
            summary=summary,
            verification_run=verification_run,
            verification_status=verification_status,
            observation_ledger_chars=self.observation_chars,
        )
        self.records.append(record)
        self.audit.write_json(
            f"{self.audit_prefix}turn-{turn:03d}-{len(self.records):03d}.json",
            record,
            kind="agent_turn",
        )

    def _add_evidence(self, item: ContextEvidence) -> None:
        remaining = self.config.max_observation_chars - self.observation_chars
        if remaining <= 0:
            return
        selected = item
        if len(item.content) > remaining:
            payload = item.model_dump(mode="python")
            payload["content"] = item.content[:remaining]
            payload["content_sha256"] = None
            selected = ContextEvidence.model_validate(payload)
        self.observations.append(selected)
        self.observation_chars += len(selected.content)

    # -- prompt construction ------------------------------------------------

    def _prompt(self, turn: int) -> str:
        return local_power_step_prompt(
            self._context_for_turn(turn),
            turn=turn,
            remaining_budgets=self._remaining_budgets(turn),
            verification_commands=[
                item.name for item in self.verification_config.commands
            ],
            verification_state=self._required_command_states(),
            outstanding_commands=self._outstanding_commands(),
            atomic_change_sets=self.config.atomic_change_sets,
            max_change_set_files=self._max_change_set_files(),
            worktree_digest=self._verification_state_digest(),
            changed_paths=self._permitted_changed_paths(),
            allowed_project_root=str(self.worktree),
            forbidden_paths=list(self.config.forbidden_paths),
            allowed_shell_prefixes=[
                " ".join(item) for item in self.shell.policy.allowed_prefixes
            ]
            if self.config.allow_shell
            else [],
            network_enabled=self.config.allow_network,
            history=[
                item.model_dump(mode="json", exclude={"observation_ledger"})
                for item in self.records
            ],
            rejected_requests=[
                f"{item.action} {item.detail}: {item.reason}" for item in self.rejections
            ],
            acceptance_criteria=[
                f"{item.id}: {item.text}"
                for item in self.specification.active_acceptance_criteria
            ],
        )

    def _remaining_budgets(self, turn: int) -> dict[str, object]:
        return {
            "turns": self.config.max_turns - turn + 1,
            "shell_commands": (
                self.budget.max_shell_commands - self.budget.shell_commands_used
            ),
            "seconds": round(self.budget.remaining_seconds(now=self.clock())),
            "observation_characters": (
                self.config.max_observation_chars - self.observation_chars
            ),
            "max_changed_files": self.config.max_changed_files,
            "max_changed_lines": self.config.max_changed_lines,
        }

    def _context_for_turn(self, turn: int) -> ContextPackage:
        parameters = dict(self.base_context.compiler_parameters)
        parameters["local_power_turn"] = turn
        parameters["local_power"] = self.config.model_dump(mode="json")
        parameters["sandbox_shell_commands_used"] = self.budget.shell_commands_used
        parameters["sandbox_rejected_requests"] = len(self.rejections)
        seen: set[tuple[str, int | None, int | None, str | None]] = set()
        unique: list[ContextEvidence] = []
        for item in [*self.base_context.evidence, *self.observations]:
            key = (item.path, item.start_line, item.end_line, item.content_sha256)
            if key in seen:
                continue
            seen.add(key)
            payload = item.model_dump(mode="python")
            payload["evidence_id"] = f"EV-{len(unique) + 1:03d}"
            unique.append(ContextEvidence.model_validate(payload))
        return ContextPackage(
            task_id=self.base_context.task_id,
            specification=self.base_context.specification,
            head_commit=self.base_context.head_commit,
            query_terms=self.base_context.query_terms,
            retrieval_tools=sorted(
                set(
                    [
                        *self.base_context.retrieval_tools,
                        "local_power_sandbox_read",
                        "local_power_sandbox_search",
                        "local_power_mediated_shell",
                    ]
                )
            ),
            compiler_parameters=parameters,
            external_research_brief=self.base_context.external_research_brief,
            research_evidence_ids=self.base_context.research_evidence_ids,
            evidence=unique,
        )
