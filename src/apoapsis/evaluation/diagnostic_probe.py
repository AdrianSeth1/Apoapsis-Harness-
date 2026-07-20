"""D4c: a minimal, evaluation-only diagnostic probe (ADR 0029).

The D4b live planning comparison (2026-07-20) found a repeatable
model-logic failure: `qwen3-coder-next:q4_K_M` made exactly one accepted
edit and then spent every remaining turn re-issuing a byte-identical
`read_file` call, never once invoking `run_check` or
`submit_for_verification`, across all six live attempts (three
monolithic, three planned). A read-only forensic pass over the preserved
turn/call artifacts found the same model reliably calls verification on
every other preserved fixture, so the loop is not a general capability
gap -- it is specific to this fixture/task/prompt combination in a way
that has not yet been isolated.

This module runs exactly one already-approved, dependency-free plan slice
at a time (never a full monolithic-vs-planned comparison, never more than
one independent variable) so a live probe can distinguish:

- Probe 2: does a narrowly revised, still-advisory prompt change the
  behavior for the *same* model on the *same* slice?
- Probe 3: does a *different*, already-installed local model behave
  differently under the *unchanged* production prompt on the same slice?

`validate_single_independent_variable()` fails closed on the only
forbidden combination (an advisory prompt paired with an alternate
model), checked first, before any filesystem access, installed-model
lookup, or provider construction, both by `run_single_slice_diagnostic_
probe()` itself and independently by the CLI.

Both probes reuse the exact, unmodified `apoapsis.architect.slice_service
.package_slice`/`approve_slice` functions (ADR 0027) to obtain the real,
deterministic derived specification, then call `VerticalSliceRunner
.execute_approved_task()` directly -- the same function
`run_monolithic_condition` (ADR 0028) already calls directly for
evaluation purposes, deliberately bypassing `start_slice`'s durable
execution-operation ledger/lease/authorization-package machinery. That
machinery is orthogonal audit/consistency bookkeeping (crash recovery,
drift detection, idempotent resubmission) that never alters the
specification, context, configuration, or agent session a live run
actually experiences -- `run_execution_operation` itself only ever
constructs providers and calls `VerticalSliceRunner.execute_approved_task
()`, exactly what this module does. Bypassing it keeps this diagnostic
infrastructure entirely inside `evaluation/`, touching no execution,
workflow, or authority code beyond the one additive, default-`None`
`agent_step_prompt_fn` parameter added to `BoundedAgentSession` and
`VerticalSliceRunner` for this purpose.

Never runs a live model, downloads a model, or mutates Ollama/model
lifecycle state on its own -- `verify_alternate_model_authorized` performs
a single read-only ``GET /api/tags`` only when an actual probe execution
requests an alternate model, and only after that model's name has already
been explicitly authorized by the caller.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable, Literal

from pydantic import Field

from apoapsis.agent.session import AgentStepPromptBuilder, AgentTurnRecord
from apoapsis.architect.errors import ArchitectError
from apoapsis.architect.slice_service import approve_slice, package_slice
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.config import ApoapsisConfig, FrontierProviderConfig
from apoapsis.evaluation.schemas import EvalEvidenceKind
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.models.prompts import agent_step_prompt
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.reporting.report import FinalTaskReport, TaskOutcome
from apoapsis.specification.schema import StrictModel, utc_now
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.vertical_slice import VerticalSliceRunner


class DiagnosticProbeError(ArchitectError):
    """Raised for a D4c-probe-level problem -- an unauthorized or
    not-installed alternate model, for example. Never raised for a
    slice's own task-level outcome (HUMAN_REVIEW_REQUIRED, FAILED, ...),
    which is always recorded as data, exactly like the rest of the
    evaluation framework (ADR 0028)."""


class PromptCondition(StrEnum):
    """The one independent variable Probe 2 isolates. `PRODUCTION` uses
    the real `apoapsis.models.prompts.agent_step_prompt` unmodified, byte
    for byte. `PROGRESS_ADVISORY` uses this module's own
    `progress_advisory_agent_step_prompt`, which calls the production
    function first and only ever appends a short, additive, advisory
    section -- it never edits, removes, or reorders anything the
    production prompt already says, and never touches
    `_AGENT_STEP_STATIC_PREFIX` itself."""

    PRODUCTION = "production"
    PROGRESS_ADVISORY = "progress_advisory"


_PROGRESS_ADVISORY_NOTE = """PROGRESS_ADVISORY_NOTE (evaluation-only diagnostic addition, ADR 0029; not part of the production prompt)
- Repeating a read of an unchanged file range that already added no new
  evidence does not advance the task.
- After an accepted edit, inspect the current diff and run the
  appropriate configured verification command.
- If no useful action remains, request escalation instead of repeating a
  no-progress action.
This note is advisory only. It does not select, force, or forbid any
specific action; Apoapsis alone still decides whether a requested action is
executed."""


def progress_advisory_agent_step_prompt(
    context,
    *,
    turn: int,
    remaining_budgets: dict[str, int],
    verification_commands: list[str],
    history: list[dict[str, object]],
) -> str:
    """The evaluation-only Probe 2 prompt variant (ADR 0029). Identical
    signature to `apoapsis.models.prompts.agent_step_prompt` so it can be
    passed as a `BoundedAgentSession`/`VerticalSliceRunner`
    `agent_step_prompt_fn` override. Computes the exact, unmodified
    production prompt first, then appends one short advisory section --
    never a replacement, never a mutation of the production static
    prefix or action schema."""

    base = agent_step_prompt(
        context,
        turn=turn,
        remaining_budgets=remaining_budgets,
        verification_commands=verification_commands,
        history=history,
    )
    return f"{base}\n{_PROGRESS_ADVISORY_NOTE}\n"


class AlternateModelSpec(StrictModel):
    """An explicitly authorized substitute for the project's configured
    coding model -- Probe 3's one independent variable. Every other
    decoding/config field (temperature, context window, think, timeout,
    base URL) is inherited unchanged from the project's own
    `local_coder`/`frontier` config; only `.model` differs."""

    model: str = Field(min_length=1)


def alternate_model_provider_config(
    base: FrontierProviderConfig, alternate: AlternateModelSpec
) -> FrontierProviderConfig:
    """Clones `base` with only `.model` overridden, so decoding settings,
    context window, timeouts, and endpoint stay identical to the baseline
    condition -- the model name is the single intended independent
    variable."""

    return base.model_copy(update={"model": alternate.model})


InstalledModelLister = Callable[[str], set[str]]


def _default_installed_models(base_url: str) -> set[str]:
    """Read-only ``GET /api/tags`` against an already-running Ollama
    endpoint -- never starts, stops, downloads, or configures anything.
    Only ever called by a live probe execution; every deterministic test
    injects `installed_models` instead."""

    request = urllib.request.Request(
        f"{base_url}/api/tags", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:  # noqa: S310
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DiagnosticProbeError(
            f"could not reach Ollama at {base_url!r} to verify the "
            f"alternate model is installed: {exc}"
        ) from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticProbeError(
            f"Ollama at {base_url!r} returned invalid JSON while listing "
            "installed models"
        ) from exc
    names: set[str] = set()
    raw_models = decoded.get("models") if isinstance(decoded, dict) else None
    if isinstance(raw_models, list):
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            for field in ("name", "model"):
                value = item.get(field)
                if isinstance(value, str) and value:
                    names.add(value)
    return names


def verify_alternate_model_authorized(
    alternate: AlternateModelSpec,
    *,
    base_url: str,
    authorized_model_names: frozenset[str],
    installed_models: InstalledModelLister = _default_installed_models,
) -> None:
    """Fail closed (ADR 0029): Probe 3 may only run against a model the
    caller explicitly authorized by name *and* that is actually installed
    at `base_url` right now. Neither condition alone is sufficient -- an
    authorized-but-not-installed name must never silently download or
    fall back to a different model, and an installed-but-unauthorized name
    must never be silently substituted in. Raises `DiagnosticProbeError`
    on any mismatch; never returns a value to act on."""

    if alternate.model not in authorized_model_names:
        raise DiagnosticProbeError(
            f"alternate model {alternate.model!r} was not explicitly "
            f"authorized (authorized: {sorted(authorized_model_names)}); "
            "refusing to run"
        )
    installed = installed_models(base_url)
    if alternate.model not in installed and f"{alternate.model}:latest" not in installed:
        raise DiagnosticProbeError(
            f"alternate model {alternate.model!r} is not installed at "
            f"{base_url!r}; refusing to run rather than silently "
            "substitute or download a model"
        )


class ModelSelection(StrictModel):
    """Which model actually ran a probe attempt, and why -- always
    recorded explicitly (ADR 0029), never left implicit or inferred from
    project configuration after the fact."""

    model: str = Field(min_length=1)
    source: Literal["project_local_coder", "explicit_alternate"]


def validate_single_independent_variable(
    prompt_condition: PromptCondition, model_selection: ModelSelection
) -> None:
    """Fail closed (ADR 0029): a D4c probe varies exactly one independent
    variable -- the prompt condition or the model identity, never both.
    `PROGRESS_ADVISORY` may only run against the project's own configured
    coding model; an explicit alternate model may only run under the
    unmodified `PRODUCTION` prompt. Pure, no I/O -- safe and required to
    call before any filesystem access, installed-model lookup, or
    provider construction. Called both by the CLI (as early as possible,
    on the raw arguments) and, independently, as the first statement of
    `run_single_slice_diagnostic_probe` itself, so a caller that reaches
    the orchestration function directly (bypassing the CLI) still cannot
    violate the invariant. Raises `DiagnosticProbeError` on any
    violation."""

    if (
        prompt_condition == PromptCondition.PROGRESS_ADVISORY
        and model_selection.source != "project_local_coder"
    ):
        raise DiagnosticProbeError(
            "PROGRESS_ADVISORY must run against the project's configured "
            "coding model -- a probe varies exactly one independent "
            f"variable; got model source {model_selection.source!r}"
        )
    if (
        model_selection.source == "explicit_alternate"
        and prompt_condition != PromptCondition.PRODUCTION
    ):
        raise DiagnosticProbeError(
            "an explicit alternate model requires prompt_condition="
            "'production' -- a probe varies exactly one independent "
            f"variable; got prompt_condition={prompt_condition.value!r}"
        )


_NO_PROGRESS_ACTIONS = frozenset({"read_file", "search_repository", "inspect_diff"})


class ProbeBehaviorSummary(StrictModel):
    """Deterministic behavioral signals computed only from a session's own
    persisted turn records -- never a model's own claim, and never
    requiring a live model call to compute (fully unit-testable against a
    constructed `list[AgentTurnRecord]`).

    `first_no_progress_turn`: the first turn that is **all three** of --
    (a) accepted, (b) one of `read_file`/`search_repository`/
    `inspect_diff`, and (c) exactly repeats an *earlier* turn's `(action,
    summary)` pair *and* adds zero new evidence (`evidence_ids == []`).
    All three conditions are required together: a turn's very first,
    novel inspection is never flagged merely for adding no evidence (e.g.
    `inspect_diff` on an untouched worktree), and a *repeated* inspection
    that nonetheless adds real new evidence -- the legitimate one-time
    reread right after an accepted edit, whose content (and so
    `evidence_ids`) differs from the pre-edit read even though its
    `(action, summary)` text is identical -- is never flagged either.
    Only a turn that repeats an already-seen inspection *and* contributes
    nothing new counts: the exact signature the D4b forensic analysis
    found. `run_check`/`submit_for_verification` turns are deliberately
    excluded from this definition entirely, since verification turns never
    populate `evidence_ids` regardless of outcome
    (`BoundedAgentSession._record_verification`) and the harness's own
    identical-verification dedup already prevents a literal repeat from
    ever being accepted twice -- a real verification attempt, even a
    failing one, must never be misclassified as "no progress".

    `max_identical_action_streak`: the longest run of consecutive turns
    sharing an identical `(action, summary)` pair -- a normalized-equality
    proxy for "the model requested the same thing again". This is not a
    raw byte comparison of the model's JSON output; that level of detail
    remains available in the preserved `call-NNN-response.json` audit
    artifacts for full forensic comparison.
    """

    total_turns: int = Field(ge=0)
    invoked_run_check: bool
    invoked_submit_for_verification: bool
    first_no_progress_turn: int | None = Field(default=None, ge=1)
    max_identical_action_streak: int = Field(default=0, ge=0)
    outcome: TaskOutcome | None = None
    stop_reason: str | None = None
    verification_runs: int = Field(default=0, ge=0)
    patch_attempts: int = Field(default=0, ge=0)


def summarize_diagnostic_probe(
    turn_records: list[AgentTurnRecord],
    *,
    outcome: TaskOutcome | None = None,
    stop_reason: str | None = None,
    verification_runs: int = 0,
    patch_attempts: int = 0,
) -> ProbeBehaviorSummary:
    """Pure, deterministic, no I/O -- see `ProbeBehaviorSummary` for the
    exact definition of each field."""

    invoked_run_check = any(item.action == "run_check" for item in turn_records)
    invoked_submit_for_verification = any(
        item.action == "submit_for_verification" for item in turn_records
    )
    first_no_progress_turn: int | None = None
    seen_inspection_keys: set[tuple[str, str]] = set()
    for item in turn_records:
        key = (item.action, item.summary)
        if item.accepted and item.action in _NO_PROGRESS_ACTIONS:
            if key in seen_inspection_keys and not item.evidence_ids:
                first_no_progress_turn = item.turn
                break
            seen_inspection_keys.add(key)
    max_streak = 0
    current_streak = 0
    previous_key: tuple[str, str] | None = None
    for item in turn_records:
        key = (item.action, item.summary)
        current_streak = current_streak + 1 if key == previous_key else 1
        max_streak = max(max_streak, current_streak)
        previous_key = key
    return ProbeBehaviorSummary(
        total_turns=len(turn_records),
        invoked_run_check=invoked_run_check,
        invoked_submit_for_verification=invoked_submit_for_verification,
        first_no_progress_turn=first_no_progress_turn,
        max_identical_action_streak=max_streak,
        outcome=outcome,
        stop_reason=stop_reason,
        verification_runs=verification_runs,
        patch_attempts=patch_attempts,
    )


class DiagnosticProbeResult(StrictModel):
    """One D4c single-slice diagnostic probe attempt (ADR 0029) -- the
    smallest controlled unit that can help isolate which independent
    variable (prompt condition or model identity) is responsible for the
    D4b read-loop. Never a full monolithic-vs-planned comparison; always
    exactly one already-approved slice."""

    schema_version: str = "1.0"
    probe_id: str = Field(min_length=1)
    generated_at: datetime = Field(default_factory=utc_now)
    scenario_id: str = Field(min_length=1)
    scenario_version: str = Field(min_length=1)
    plan_id: str = Field(pattern=r"^PLAN-[A-Za-z0-9._-]+$")
    plan_version: int = Field(ge=1)
    slice_id: str = Field(pattern=r"^SLICE-[A-Za-z0-9._-]+$")
    task_id: str | None = None
    prompt_condition: PromptCondition
    model: ModelSelection
    report: FinalTaskReport | None = None
    behavior: ProbeBehaviorSummary
    duration_seconds: float = Field(default=0.0, ge=0)
    evidence_kind: EvalEvidenceKind = EvalEvidenceKind.DETERMINISTIC_FAKE


def run_single_slice_diagnostic_probe(
    project_root: str | Path,
    plan_store: SQLitePlanStore,
    slice_store: PlanSliceExecutionStore,
    task_store: SQLiteTaskStore,
    operation_store: ExecutionOperationStore,
    plan_id: str,
    slice_id: str,
    *,
    expected_plan_version: int,
    config: ApoapsisConfig,
    provider: InstrumentedModelProvider,
    local_coder_provider: InstrumentedModelProvider,
    prompt_condition: PromptCondition,
    model_selection: ModelSelection,
    scenario_id: str,
    scenario_version: str,
    evidence_kind: EvalEvidenceKind = EvalEvidenceKind.DETERMINISTIC_FAKE,
) -> DiagnosticProbeResult:
    """Packages and approves exactly one already-approved plan's slice
    (ordinarily the first, dependency-free one), then runs its derived
    task directly through `VerticalSliceRunner.execute_approved_task()` --
    see this module's docstring for why bypassing `start_slice`'s
    execution-operation ledger is a documented, non-material difference
    from how D4b's live attempts were started.

    `provider`/`local_coder_provider` are supplied by the caller exactly
    once. For a Probe 2 (prompt-condition) attempt both stay the
    project's normal, unmodified providers -- only `prompt_condition`
    changes. For a Probe 3 (model) attempt, `local_coder_provider` alone
    is built from an already fail-closed-verified
    `AlternateModelSpec`-derived config, and every other setting
    (specification, context profile, budgets, completion policy,
    fixture state) is inherited unchanged from `config`.

    Never generates or approves a plan itself -- `plan_id`/
    `expected_plan_version` must already reference a plan approved via
    the existing, unmodified `apoapsis plan export/import/validate/
    approve` workflow, exactly like `run_planned_condition` (ADR 0028)
    requires.

    Rejects a `prompt_condition`/`model_selection` combination that would
    vary more than one independent variable
    (`validate_single_independent_variable`) before touching the
    filesystem, the plan/slice/task stores, or any provider.
    """

    validate_single_independent_variable(prompt_condition, model_selection)
    root = Path(project_root).resolve()
    started = time.monotonic()
    package = package_slice(
        root,
        plan_store,
        slice_store,
        task_store,
        operation_store,
        plan_id,
        slice_id,
        expected_plan_version=expected_plan_version,
        config=config,
    )
    record = approve_slice(
        root,
        task_store,
        slice_store,
        plan_id,
        slice_id,
        expected_package_sha256=package.package_sha256,
    )
    assert record.task_id is not None

    prompt_fn: AgentStepPromptBuilder | None = (
        progress_advisory_agent_step_prompt
        if prompt_condition == PromptCondition.PROGRESS_ADVISORY
        else None
    )
    runner = VerticalSliceRunner(
        root,
        task_store,
        provider,
        config,
        local_coder_provider=local_coder_provider,
        agent_step_prompt_fn=prompt_fn,
    )
    report = runner.execute_approved_task(record.task_id)
    session_result = runner.local_agent_result or runner.agent_result
    turn_records = session_result.turn_records if session_result is not None else []
    behavior = summarize_diagnostic_probe(
        turn_records,
        outcome=report.outcome,
        stop_reason=report.agent_stop_reason,
        verification_runs=report.agent_verification_runs,
        patch_attempts=report.agent_patch_attempts,
    )
    return DiagnosticProbeResult(
        probe_id=f"PROBE-{uuid.uuid4().hex[:12].upper()}",
        scenario_id=scenario_id,
        scenario_version=scenario_version,
        plan_id=plan_id,
        plan_version=expected_plan_version,
        slice_id=slice_id,
        task_id=record.task_id,
        prompt_condition=prompt_condition,
        model=model_selection,
        report=report,
        behavior=behavior,
        duration_seconds=time.monotonic() - started,
        evidence_kind=evidence_kind,
    )


__all__ = [
    "AlternateModelSpec",
    "DiagnosticProbeError",
    "DiagnosticProbeResult",
    "InstalledModelLister",
    "ModelSelection",
    "ProbeBehaviorSummary",
    "PromptCondition",
    "alternate_model_provider_config",
    "progress_advisory_agent_step_prompt",
    "run_single_slice_diagnostic_probe",
    "summarize_diagnostic_probe",
    "validate_single_independent_variable",
    "verify_alternate_model_authorized",
]
