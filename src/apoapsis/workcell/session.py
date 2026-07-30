"""The session coordinator: the caller Slice 5's machinery did not have.

Slice 5 built a kernel, a capsule, a compaction policy and a budget, and wired
none of them to anything. This module is the loop that owns all four and drives
`run_checkpoint`. It is deliberately the *only* place where those four meet, so
that "what stopped this session, and on whose authority" has one answer.

Three properties are load-bearing, and each replaces something weaker.

**The kernel is bytes, not a model.** It is rendered once at session start,
written, hashed, and read back for every call. The first version of this work
tried to keep the prefix stable by refusing timestamp-shaped and UUID-shaped
text at construction, which is the wrong test in both directions: a legitimate
objective may quote a fixed upstream UUID, and a genuinely per-run value need
not look like any of the patterns. Volatility is a provenance property, so the
control is provenance -- one artifact, reused, and `KernelDriftError` if it
moves.

**Tokens come from the provider.** Compaction and the token ceilings read
`TokenLedger.reported` usage lifted from the CLI's own events. The controller's
estimate is carried alongside for diagnosis and is barred from both, because a
session stopped or compacted on an estimate is a session governed by the
estimator's error rather than by the owner's budget.

**Every stop is a recorded transition.** There is no `return` from the middle
of the loop: each ending goes through `_stop`, which appends a transition with
a reason and sets one of the `SessionOutcome` values. An owner reading a
`SessionRecord` can see the state machine's path, not just its verdict.

What this module still does not do is decide anything about correctness. It
hands admitted work to `run_checkpoint`, and `evaluate_checkpoint` -- which
cannot see a command's exit code -- decides completion. That separation is ADR
0079's and is unchanged here.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol, Sequence

from pydantic import Field

from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.acceptance import (
    CheckpointOutcome,
    ObligationStatus,
    SliceAcceptanceContract,
)
from apoapsis.workcell.budgets import (
    BudgetUsage,
    BudgetVerdict,
    ProgressTracker,
    SessionBudget,
    SessionClock,
    TokenLedger,
    TurnObservation,
    evaluate_budget,
)
from apoapsis.workcell.checkpoint import CheckpointRecord, WitnessEmitter, run_checkpoint
from apoapsis.workcell.compaction import (
    CompactionDecision,
    CompactionPolicy,
    CompactionTier,
    ContextReading,
    HistorySegment,
    SegmentKind,
    compact,
)
from apoapsis.workcell.context import (
    KernelArtifact,
    KernelDriftError,
    ObservedWitness,
    PromptLayout,
    StateCapsule,
    build_layout,
    check_prefix_stability,
    PrefixDrift,
)


class SessionState(StrEnum):
    STARTING = "starting"
    AWAITING_MODEL = "awaiting_model"
    COMPACTING = "compacting"
    CHECKPOINTING = "checkpointing"
    STOPPED = "stopped"


class SessionOutcome(StrEnum):
    """Every way a session can end. All of them are recorded, none inferred."""

    COMPLETE = "complete"
    #: Readiness said continue, but the owner's allowance ran out first.
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANDIDATE_REFUSED = "candidate_refused"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    #: Mechanical compaction was not enough and semantic compaction failed or
    #: was unavailable. A stop, never a silent continuation over a context that
    #: is known to be too full.
    COMPACTION_FAILED = "compaction_failed"
    #: The kernel artifact changed underneath a running session.
    KERNEL_DRIFT = "kernel_drift"
    #: The agent stopped asking for turns without requesting a checkpoint.
    AGENT_STOPPED = "agent_stopped"


class SessionTransition(StrictModel):
    turn: int = Field(ge=0)
    from_state: SessionState
    to_state: SessionState
    reason: str = Field(min_length=1)


class TurnResult(StrictModel):
    """What one model turn produced, as the controller observed it."""

    #: A stable signature for the action taken, used for identical-action
    #: detection. The controller derives it from the tool call, not from prose.
    action_signature: str = Field(min_length=1)
    #: Text to show the model next turn, before bounding.
    observation: str = ""
    #: New history to carry, already segmented.
    segments: list[HistorySegment] = Field(default_factory=list)
    #: Provider-reported usage after this turn.
    tokens: TokenLedger = Field(default_factory=TokenLedger)
    #: Worktree fingerprint after this turn, if the controller computed one.
    worktree_fingerprint: str | None = None
    #: Seconds spent running commands inside the workcell this turn.
    process_seconds: float = Field(default=0.0, ge=0)
    #: The agent asked to be inspected.
    requested_checkpoint: bool = False
    #: The agent ended its own turn loop without asking for a checkpoint.
    finished: bool = False
    destructive_actions: int = Field(default=0, ge=0)
    model_notes: list[str] = Field(default_factory=list)


class AgentDriver(Protocol):
    """Whatever actually runs the model. The coordinator does not care which."""

    def advance(self, layout: PromptLayout, turn: int) -> TurnResult: ...


#: Asked to summarise history when mechanical compaction was not enough.
#: Returning `None` is a refusal, and a refusal stops the session -- see
#: `SessionOutcome.COMPACTION_FAILED`.
SemanticCompactor = Callable[[Sequence[HistorySegment]], str | None]


class SessionRecord(StrictModel):
    """The audit trail for one slice's session."""

    schema_version: str = "1.0"
    slice_id: str = Field(min_length=1)
    outcome: SessionOutcome
    detail: str = Field(min_length=1)
    turns: int = Field(ge=0)
    kernel_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    transitions: list[SessionTransition] = Field(default_factory=list)
    compaction_events: list[CompactionDecision] = Field(default_factory=list)
    checkpoints: list[CheckpointRecord] = Field(default_factory=list)
    budget: BudgetVerdict | None = None
    prefix_drift: PrefixDrift | None = None
    final_capsule: StateCapsule | None = None
    #: Provider-reported cached input tokens over the session, against the
    #: controller's estimate. This is the only number that can say whether the
    #: stable prefix actually bought anything; it is reported and not asserted.
    tokens: TokenLedger | None = None


class SessionCoordinator:
    """Owns the kernel, capsule, budget and compaction for one slice."""

    def __init__(
        self,
        *,
        contract: SliceAcceptanceContract,
        kernel_artifact: KernelArtifact,
        driver: AgentDriver,
        system_prompt: str,
        tool_schemas: list[str],
        compaction_policy: CompactionPolicy,
        budget: SessionBudget | None = None,
        spill_directory: Path | None = None,
        semantic_compactor: SemanticCompactor | None = None,
        base_root: str | Path,
        candidate_root: str | Path,
        snapshot_root: str | Path,
        emit_witnesses: WitnessEmitter,
        base_commit: str | None = None,
    ) -> None:
        self.contract = contract
        self.kernel_artifact = kernel_artifact
        self.driver = driver
        self.system_prompt = system_prompt
        self.tool_schemas = tool_schemas
        self.policy = compaction_policy
        self.budget = budget or SessionBudget()
        self.spill_directory = spill_directory
        self.semantic_compactor = semantic_compactor
        self.base_root = base_root
        self.candidate_root = candidate_root
        self.snapshot_root = snapshot_root
        self.emit_witnesses = emit_witnesses
        self.base_commit = base_commit

        self.capsule = StateCapsule(
            slice_id=contract.slice_id,
            unresolved_obligations=[
                item.obligation_id for item in contract.obligations
            ],
        )
        self.segments: list[HistorySegment] = []
        self.clock = SessionClock()
        self.progress = ProgressTracker()
        self.state = SessionState.STARTING
        self._transitions: list[SessionTransition] = []
        self._prefix_digests: list[str] = []
        self._compactions: list[CompactionDecision] = []
        self._checkpoints: list[CheckpointRecord] = []
        self._tokens = TokenLedger()
        self._model_calls = 0
        self._destructive = 0
        self._turn = 0

    # -- state machine -------------------------------------------------

    def _to(self, state: SessionState, reason: str) -> None:
        self._transitions.append(
            SessionTransition(
                turn=self._turn,
                from_state=self.state,
                to_state=state,
                reason=reason,
            )
        )
        self.state = state

    def _stop(self, outcome: SessionOutcome, reason: str) -> SessionRecord:
        """The single exit. Every ending is a recorded transition."""

        self._to(SessionState.STOPPED, reason)
        return SessionRecord(
            slice_id=self.contract.slice_id,
            outcome=outcome,
            detail=reason,
            turns=self._turn,
            kernel_digest=self.kernel_artifact.digest,
            transitions=self._transitions,
            compaction_events=self._compactions,
            checkpoints=self._checkpoints,
            budget=evaluate_budget(self.budget, self._usage()),
            prefix_drift=check_prefix_stability(self._prefix_digests),
            final_capsule=self.capsule,
            tokens=self._tokens,
        )

    def _usage(self) -> BudgetUsage:
        return self.clock.usage(
            tokens=self._tokens,
            model_calls=self._model_calls,
            destructive_actions=self._destructive,
            consecutive_no_progress_turns=self.progress.consecutive_no_progress,
            max_identical_action_run=self.progress.max_identical_run,
        )

    # -- the loop ------------------------------------------------------

    def run(self) -> SessionRecord:
        """Drive the slice to a recorded outcome."""

        while True:
            self._turn += 1

            verdict = evaluate_budget(self.budget, self._usage())
            if not verdict.within_budget:
                # Checked before the call, not after: spending a call and then
                # discovering the budget was already gone is how a ceiling
                # becomes advisory.
                return self._stop(
                    SessionOutcome.BUDGET_EXHAUSTED, verdict.detail
                )

            compaction_stop = self._maybe_compact()
            if compaction_stop is not None:
                return compaction_stop

            self._to(SessionState.AWAITING_MODEL, f"turn {self._turn}")
            try:
                layout = self._layout(verdict)
            except KernelDriftError as exc:
                return self._stop(SessionOutcome.KERNEL_DRIFT, str(exc))

            self._prefix_digests.append(layout.prefix_digest())
            result = self.driver.advance(layout, self._turn)
            self._absorb(result)

            if result.requested_checkpoint:
                self._to(SessionState.CHECKPOINTING, "the agent asked to be inspected")
                record = self._checkpoint()
                self._checkpoints.append(record)
                outcome = record.decision.outcome
                if outcome is CheckpointOutcome.COMPLETE:
                    return self._stop(
                        SessionOutcome.COMPLETE, record.decision.detail
                    )
                if outcome is CheckpointOutcome.CANDIDATE_REFUSED:
                    return self._stop(
                        SessionOutcome.CANDIDATE_REFUSED, record.decision.detail
                    )
                if outcome is CheckpointOutcome.HUMAN_REVIEW_REQUIRED:
                    return self._stop(
                        SessionOutcome.HUMAN_REVIEW_REQUIRED, record.decision.detail
                    )
                # CONTINUE: the outcome Crisis Atlas Slice 2 was denied. The
                # agent gets another turn with what is still outstanding.
                self._carry_forward(record)
                continue

            if result.finished:
                return self._stop(
                    SessionOutcome.AGENT_STOPPED,
                    "the agent ended its turn loop without requesting a "
                    "checkpoint, so no completion decision was ever made",
                )

    # -- pieces --------------------------------------------------------

    def _layout(self, verdict: BudgetVerdict) -> PromptLayout:
        observation_parts = []
        for breach in verdict.breaches:
            if breach.guidance:
                observation_parts.append(breach.guidance)
        return build_layout(
            system_prompt=self.system_prompt,
            tool_schemas=self.tool_schemas,
            kernel=self.kernel_artifact,
            capsule=self.capsule,
            recent_history="\n\n".join(item.text for item in self.segments),
            observation="\n".join(observation_parts),
        )

    def _maybe_compact(self) -> SessionRecord | None:
        """Compact if the provider says we are over the threshold.

        Returns a stopping record only when compaction was needed and could not
        be achieved. Continuing over a context known to be too full is how the
        control reached a 64,409-token prompt against a 65,536-token window.
        """

        reading = ContextReading.from_ledger(self._tokens)
        if not self.policy.should_compact_reading(reading):
            return None

        self._to(
            SessionState.COMPACTING,
            f"provider-reported input at {self.policy.utilisation(reading.input_tokens):.0%} "
            f"of the window, over the {self.policy.threshold:.0%} threshold",
        )
        survivors, decision = compact(
            self.segments,
            self.policy,
            spill_directory=self.spill_directory,
            current_turn=self._turn,
        )
        self.segments = survivors
        self._compactions.append(decision)
        for segment in self.segments:
            pointer = segment.artifact_pointer
            if pointer and pointer not in self.capsule.artifact_pointers:
                self.capsule.artifact_pointers.append(pointer)

        if decision.utilisation_after < self.policy.threshold:
            return None

        # Mechanical was not enough. Semantic costs a model call and produces a
        # summary, so it is requested rather than assumed, and its failure is a
        # stop with a name.
        if self.semantic_compactor is None:
            return self._stop(
                SessionOutcome.COMPACTION_FAILED,
                f"mechanical compaction left the context at "
                f"{decision.utilisation_after:.0%}, still over the "
                f"{self.policy.threshold:.0%} threshold, and no semantic "
                "compactor is configured",
            )
        try:
            summary = self.semantic_compactor(self.segments)
        except Exception as exc:  # noqa: BLE001 -- reported as an outcome
            return self._stop(
                SessionOutcome.COMPACTION_FAILED,
                f"semantic compaction raised {type(exc).__name__}: {exc}",
            )
        if not summary:
            return self._stop(
                SessionOutcome.COMPACTION_FAILED,
                "semantic compaction returned no summary, so the context "
                "cannot be brought under the threshold; continuing would send "
                "a request the window cannot hold",
            )
        # The summary replaces the segments it summarised, and is a MESSAGE
        # rather than a distinct kind so that a later compaction pass treats it
        # like any other content -- a summary of a summary is not privileged.
        self.segments = [
            HistorySegment(
                segment_id=f"summary-turn-{self._turn}",
                kind=SegmentKind.MESSAGE,
                text=summary,
                estimated_tokens=max(1, len(summary) // 4),
                turn=self._turn,
            )
        ]
        self._compactions.append(
            decision.model_copy(update={"tier": CompactionTier.SEMANTIC})
        )
        return None

    def _absorb(self, result: TurnResult) -> None:
        self._model_calls += 1
        self._destructive += result.destructive_actions
        self.clock.add_process_time(result.process_seconds)
        if result.tokens.reported:
            self._tokens = result.tokens
        else:
            # Keep the estimate for diagnosis; it governs nothing.
            self._tokens = self._tokens.model_copy(
                update={
                    "estimated_input_tokens": result.tokens.estimated_input_tokens,
                    "estimated_output_tokens": result.tokens.estimated_output_tokens,
                }
            )
        self.segments.extend(result.segments)
        for note in result.model_notes:
            if note not in self.capsule.model_notes:
                self.capsule.model_notes.append(note)

        progress = self.progress.record_turn(
            TurnObservation(
                action_signature=result.action_signature,
                worktree_fingerprint=result.worktree_fingerprint,
            )
        )
        if not progress.made_progress:
            if result.action_signature not in self.capsule.no_progress_actions:
                self.capsule.no_progress_actions.append(result.action_signature)

    def _checkpoint(self) -> CheckpointRecord:
        return run_checkpoint(
            self.contract,
            base_root=self.base_root,
            candidate_root=self.candidate_root,
            snapshot_root=self.snapshot_root,
            emit_witnesses=self.emit_witnesses,
            base_commit=self.base_commit,
        )

    def _carry_forward(self, record: CheckpointRecord) -> None:
        """Fold a CONTINUE checkpoint into the capsule.

        The capsule is what survives compaction, so this is where a checkpoint
        stops being an event and becomes state. Obligations discharged here are
        also progress -- a turn that proves something without editing a file
        has advanced the session, and counting it as no-progress would punish
        the debugging behaviour the unrestricted control did well.
        """

        readiness = record.readiness
        fingerprint = record.candidate_fingerprint
        self.capsule.worktree_fingerprint = fingerprint

        discharged: list[str] = []
        if readiness is not None:
            discharged = [
                item.obligation_id
                for item in readiness.obligations
                if item.status is ObligationStatus.SATISFIED
            ]
            self.capsule.unresolved_obligations = sorted(
                item.obligation_id
                for item in readiness.obligations
                if item.status is not ObligationStatus.SATISFIED
            )
            # Findings, not raw command output: the capsule carries what a
            # failure *was*, not the terminal scrollback it arrived in.
            self.capsule.latest_failures = [
                f"{item.block.value}: {item.detail}" for item in readiness.findings
            ][:20]

        self.capsule.observed_witnesses = [
            ObservedWitness(
                command_name=witness_id,
                passed=True,
                worktree_fingerprint=fingerprint,
            )
            for witness_id in record.witness_ids
        ]

        self.progress.record_turn(
            TurnObservation(
                action_signature=f"checkpoint:{record.candidate_fingerprint[:12]}",
                worktree_fingerprint=fingerprint,
                discharged_obligations=discharged,
                evidence_artifacts=list(record.witness_ids),
            )
        )
