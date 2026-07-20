from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from pydantic import ValidationError

from apoapsis.architect.errors import ArchitectError, PlanImportError
from apoapsis.audit.store import TaskAuditStore
from apoapsis.architect.importer import import_planner_response
from apoapsis.architect.package import build_planner_request_package
from apoapsis.architect.audit import PlanAuditStore, write_package_artifact
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.architect.validation import validate_plan
from apoapsis.architect.schema import PlanValidationResult, ValidationSeverity
from apoapsis.architect.slice_service import (
    approve_slice,
    package_slice,
    project_slice_status,
    start_slice,
)
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.execution.operation_errors import ExecutionOperationError
from apoapsis.execution.operation_recovery import recover_stale_execution_operations
from apoapsis.execution.operation_service import (
    execute_execution_operation,
    run_execution_operation,
)
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.intake.errors import IntakeError
from apoapsis.intake.execution import execute_intake_operation, run_intake_operation
from apoapsis.intake.recovery import recover_stale_intake_operations
from apoapsis.discovery.api import (
    preview_frontier_planning_api_call as discovery_preview_api_call,
    run_frontier_planning_api_call as discovery_call_api,
)
from apoapsis.discovery.errors import DiscoveryError
from apoapsis.discovery.frontier_package import load_package as discovery_load_package
from apoapsis.discovery.manual import (
    import_manual_frontier_planning_response as discovery_import_manual_response,
)
from apoapsis.discovery.schema import ClarificationAnswer
from apoapsis.discovery.service import (
    approve_idea_brief_step as discovery_approve_idea_brief,
    export_frontier_planning_package as discovery_export_frontier_package,
    propose_idea_brief_step as discovery_propose_idea_brief,
    propose_local_clarification_questions as discovery_propose_local_questions,
    record_frontier_answers as discovery_record_frontier_answers,
    record_local_answers as discovery_record_local_answers,
    start_session as discovery_start_session,
)
from apoapsis.discovery.store import SQLiteDiscoveryStore
from apoapsis.intake.store import IntakeOperationStore
from apoapsis.manual_frontier.approve import approve_manual_frontier_preview
from apoapsis.manual_frontier.errors import ManualFrontierError
from apoapsis.manual_frontier.importer import import_manual_frontier_response
from apoapsis.manual_frontier.package import (
    build_manual_frontier_handoff_package,
    load_package as load_manual_frontier_package,
    write_handoff_artifacts,
)
from apoapsis.manual_frontier.store import ManualFrontierPreviewStore
from apoapsis.review.case import build_review_case
from apoapsis.review.errors import ReviewError
from apoapsis.review.execution import execute_review_action, run_review_operation
from apoapsis.review.recovery import recover_stale_operations
from apoapsis.review.schema import ReviewActionKind
from apoapsis.review.store import ReviewOperationStore
from apoapsis.config import (
    AgentRoute,
    CompletionPolicy,
    ExecutionMode,
    FrontierProviderConfig,
    ApoapsisConfig,
)
from apoapsis.doctor import run_doctor
from apoapsis.evaluation.aggregate import aggregate_evaluations
from apoapsis.evaluation.diagnostic_probe import (
    AlternateModelSpec,
    DiagnosticProbeError,
    ModelSelection,
    PromptCondition,
    alternate_model_provider_config,
    run_single_slice_diagnostic_probe,
    validate_single_independent_variable,
    verify_alternate_model_authorized,
)
from apoapsis.evaluation.diagnostic_probe_report import write_diagnostic_probe_report
from apoapsis.evaluation.fixture import prepare_fixture_repository
from apoapsis.evaluation.harness import run_eval_lane
from apoapsis.evaluation.lanes import requires_frontier_coder
from apoapsis.evaluation.oracle import HeldOutOracleDefinition
from apoapsis.evaluation.planning_harness import (
    run_monolithic_condition,
    run_planned_condition,
)
from apoapsis.evaluation.planning_report import write_planning_comparison
from apoapsis.evaluation.planning_schemas import (
    PlannerMethod,
    PlannerProvenance,
    PlanningComparisonReport,
)
from apoapsis.evaluation.report import write_aggregate, write_comparison
from apoapsis.evaluation.spend_ceiling import (
    HostedSpendCeilingExceededError,
    SpendCeilingModelProvider,
    SpendLedger,
    estimate_worst_case_run_cost_usd,
)
from apoapsis.evaluation.schemas import (
    DEFAULT_LANE_ORDER,
    EvalComparisonReport,
    EvalEvidenceKind,
    EvalLane,
    EvalLaneResult,
    MetricStatus,
)
from apoapsis.execution.worktree import WorktreeError, WorktreeManager
from apoapsis.models.frontier import OpenAICompatibleFrontierProvider
from apoapsis.models.local import OllamaProvider
from apoapsis.models.provider import ModelProvider, ProviderError
from apoapsis.models.telemetry import InstrumentedModelProvider, InstrumentedProviderError
from apoapsis.research.cache import ResearchCache
from apoapsis.research.engine import ResearchEngine, ResearchEngineError
from apoapsis.research.fetcher import ResearchFetchProcess
from apoapsis.research.model import LocalResearchModelClient, ResearchModelError
from apoapsis.research.schemas import ResearchMode, ResearchSourceName
from apoapsis.research.sources.github import GitHubSource
from apoapsis.research.sources.official import OfficialDocumentationSource
from apoapsis.research.sources.reddit import RedditSource
from apoapsis.repository.git import GitCommandError, GitRepository
from apoapsis.specification.schema import (
    AcceptanceCriterion,
    HardConstraint,
    SourceKind,
    TaskSpecification,
    TraceableStatement,
)
from apoapsis.verification.runner import (
    VerificationCommand,
    VerificationConfig,
    VerificationRunner,
)
from apoapsis.workflow.engine import SQLiteTaskStore, TaskStoreError
from apoapsis.workflow.events import WorkflowActor
from apoapsis.workflow.states import WorkflowState
from apoapsis.workflow.vertical_slice import VerticalSliceRunner


DEFAULT_CONFIG = """# Apoapsis Harness project configuration
[project]
language = "python"

[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3-coder-next:q4_K_M"
timeout_seconds = 900
max_output_tokens = 8192
temperature = 0.0
context_window_tokens = 65536
think = false
specification_think = false

[models.frontier.pricing]
input_per_million_usd = 0
output_per_million_usd = 0
cached_input_per_million_usd = 0

[models.local_coder]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3-coder-next:q4_K_M"
api_key_env = "APOAPSIS_LOCAL_CODER_API_KEY"
timeout_seconds = 900
max_output_tokens = 8192
temperature = 0.0
context_window_tokens = 65536
think = false
specification_think = false

[models.local_coder.pricing]
input_per_million_usd = 0
output_per_million_usd = 0
cached_input_per_million_usd = 0

[execution]
mode = "agent"
route = "auto"
completion_policy = "strict"

[execution.agent]
max_turns = 12
max_patch_attempts = 4
max_verification_runs = 4
max_search_results = 20
max_read_lines = 240
max_observation_chars = 48000
max_transmitted_observation_chars = 24000

[execution.frontier_agent]
max_turns = 8
max_patch_attempts = 3
max_verification_runs = 3
max_search_results = 20
max_read_lines = 240
max_observation_chars = 48000
max_transmitted_observation_chars = 24000

[models.local_research]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3.6:27b"
api_key_env = "APOAPSIS_LOCAL_RESEARCH_API_KEY"
timeout_seconds = 600
max_output_tokens = 8192
temperature = 0.0
context_window_tokens = 32768
max_structured_retries = 1

[models.local_research.modes.extraction]
think = false
require_structured_output = true

[models.local_research.modes.synthesis]
think = true
require_structured_output = true

[context]
max_files = 24
max_excerpt_lines = 240
max_total_chars = 180000
match_context_lines = 20
max_search_terms = 12
max_import_depth = 2
cloud_excluded_paths = [
  ".env", ".env.*", "*.pem", "*.key", "secrets/**", ".apoapsis/**",
  ".sol/**", ".git/**"
]

[patch]
max_changed_lines = 500
max_files = 20
allow_dependency_changes = false
allow_test_changes = false
dependency_files = [
  "pyproject.toml", "requirements*.txt", "poetry.lock", "uv.lock",
  "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"
]
verification_files = [
  ".apoapsis/config.toml", ".sol/config.toml", "pytest.ini", "tox.ini",
  "mypy.ini", "ruff.toml", ".github/workflows/**"
]

[research]
default_mode = "AUTO"

[research.budget]
max_queries = 4
max_candidates = 12
max_fetched_sources = 5
max_extracted_characters_per_source = 20000
max_research_context_tokens = 16000
max_seconds = 300

[research.sources.official_docs]
enabled = true
priority = 1
allowed_domains = ["docs.python.org"]

[research.sources.github]
enabled = true
priority = 2
authentication = "auto"
require_license_for_code_reuse = true

[research.sources.reddit]
enabled = false
priority = 4
client_id_env = "REDDIT_CLIENT_ID"
client_secret_env = "REDDIT_CLIENT_SECRET"
user_agent = "apoapsis-harness-research/0.7"
purposes = ["user_pain_points", "product_expectations", "failure_discovery"]

[research.security]
allow_domains = [
  "docs.python.org", "github.com", "api.github.com", "reddit.com",
  "www.reddit.com", "oauth.reddit.com"
]
allowed_content_types = [
  "application/json", "text/plain", "text/html", "text/markdown"
]
max_response_bytes = 1000000
max_redirects = 3
request_timeout_seconds = 20
execute_downloaded_code = false
project_write_access = false
expose_project_secrets = false

[research.synthesis]
minimum_distinct_sources = 3
prefer_comparative_patterns = true
require_provenance = true

[research.cache]
default_ttl_hours = 168
reddit_ttl_hours = 24

[verification]
stop_on_failure = false
output_limit_chars = 100000
environment_allowlist = [
  "PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP",
  "USERPROFILE", "HOME", "VIRTUAL_ENV"
]

[[verification.commands]]
name = "unit-tests"
category = "tests"
description = "Runs the project's full test suite."
argv = ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
timeout_seconds = 120
required = true
# Acceptance designation is an explicit owner decision (ADR 0017), never
# generated automatically: mark acceptance = true only once you have
# decided this command's pass is strong enough evidence that a criterion
# is genuinely done, then map AcceptanceCriterion.verification_method to
# "unit-tests" (or add a separate, stronger acceptance command). Until you
# do, `apoapsis doctor` will warn that strict has no acceptance-designated
# command, and tasks with active acceptance criteria correctly stop at
# HUMAN_REVIEW_REQUIRED instead of silently reaching COMPLETE.
acceptance = false

[architect.ceilings]
max_slices = 40
max_dependency_depth = 15
max_suggested_paths_per_slice = 12
max_criteria_per_slice = 12
max_work_brief_chars = 2000

[review]
max_continuations_per_task = 5
max_additional_turns_per_continuation = 12
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apoapsis",
        description="Local-first deterministic coding-task harness",
    )
    parser.add_argument(
        "--project-root", type=Path, default=Path.cwd(), help=argparse.SUPPRESS
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="initialize Apoapsis metadata")

    doctor = subparsers.add_parser(
        "doctor", help="run read-only diagnostics for this project"
    )
    doctor.add_argument(
        "--probe",
        action="store_true",
        help=(
            "optionally probe configured provider connectivity and "
            "structured-output support; hosted providers may incur real cost"
        ),
    )

    ui = subparsers.add_parser(
        "ui", help="open the local Apoapsis operator interface"
    )
    ui.add_argument(
        "--port",
        type=int,
        default=7331,
        help="loopback port for the local interface (default: 7331)",
    )
    ui.add_argument(
        "--no-open",
        action="store_true",
        help="serve the interface without opening a browser window",
    )

    run = subparsers.add_parser(
        "run", help="run an approved verified coding workflow"
    )
    run.add_argument("request")
    run.add_argument(
        "--yes",
        action="store_true",
        help="approve the extracted specification non-interactively",
    )
    run.add_argument(
        "--research",
        choices=["off", "auto", "github", "community", "full"],
        help="external-research policy for this task",
    )
    run.add_argument(
        "--execution-mode",
        choices=[item.value for item in ExecutionMode],
        help="override the configured one-shot or bounded-agent execution mode",
    )
    run.add_argument(
        "--agent-route",
        choices=[
            AgentRoute.AUTO.value,
            AgentRoute.LOCAL_ONLY.value,
            AgentRoute.LOCAL_THEN_FRONTIER.value,
            AgentRoute.FRONTIER_ONLY.value,
        ],
        help="override deterministic agent provider routing",
    )
    run.add_argument(
        "--context-profile",
        choices=["16k", "32k", "64k", "128k", "256k"],
        help=(
            "override the native Ollama window and repository excerpt "
            "budget for a reproducible comparison"
        ),
    )

    task = subparsers.add_parser("task", help="draft a structured task")
    task.add_argument("request")
    task.add_argument(
        "--constraint", action="append", default=[], help="verbatim hard constraint"
    )
    task.add_argument(
        "--acceptance", action="append", default=[], help="acceptance criterion"
    )
    task.add_argument(
        "--research",
        choices=["off", "auto", "github", "community", "full"],
        default="off",
        help="record the requested research policy",
    )

    research = subparsers.add_parser(
        "research", help="run, inspect, refresh, or manage research cache"
    )
    research.add_argument("research_args", nargs="+")
    research.add_argument(
        "--mode",
        choices=["off", "auto", "github", "community", "full"],
    )

    inspect = subparsers.add_parser("inspect", help="show a task and audit events")
    inspect.add_argument("task_id")

    approve = subparsers.add_parser("approve", help="approve a drafted task spec")
    approve.add_argument("task_id")
    approve.add_argument("--version", type=int)

    worktree = subparsers.add_parser(
        "worktree-create", help="create an isolated task worktree"
    )
    worktree.add_argument("task_id")
    worktree.add_argument("--base", default="HEAD")

    verify = subparsers.add_parser("verify", help="run configured checks")
    verify.add_argument("task_id")
    verify.add_argument("--path", type=Path)

    rollback = subparsers.add_parser(
        "rollback", help="remove a task worktree and mark it rolled back"
    )
    rollback.add_argument("task_id")
    rollback.add_argument(
        "--delete-branch", action="store_true", help="also delete the task branch"
    )

    evaluate_planning = subparsers.add_parser(
        "eval-planning",
        help=(
            "compare a monolithic request against an approved, plan-then-"
            "execute decomposition (ADR 0028) -- never generates a plan "
            "itself; requires one already approved via `plan export/"
            "import/validate/approve` against --planned-project-root"
        ),
    )
    evaluate_planning.add_argument("scenario", choices=["download-service-v2"])
    evaluate_planning.add_argument("--plan-id", required=True)
    evaluate_planning.add_argument(
        "--expected-plan-version", type=int, required=True
    )
    evaluate_planning.add_argument(
        "--planned-project-root",
        type=Path,
        required=True,
        help=(
            "an already-initialized project directory where the fixed "
            "plan was already exported, imported, validated, and approved "
            "-- must still be at its untouched scenario baseline (no slice "
            "packaged or started yet); this command executes the plan's "
            "slices inside it"
        ),
    )
    evaluate_planning.add_argument(
        "--planner-model",
        required=True,
        help=(
            "which strong model produced this plan (e.g. "
            "'claude-opus-4-8-web'); recorded for provenance only -- this "
            "command never calls a planner itself, and always records "
            "planner tokens/cost as unmeasured for a manually-pasted "
            "subscription session, never a fabricated zero"
        ),
    )
    evaluate_planning.add_argument(
        "--context-profile",
        choices=["16k", "32k", "64k", "128k", "256k"],
        help="override the native Ollama window and repository evidence budget",
    )
    evaluate_planning.add_argument(
        "--output-dir",
        type=Path,
        help="directory for the fresh monolithic fixture copy and the comparison report",
    )

    evaluate_planning_probe = subparsers.add_parser(
        "eval-planning-probe",
        help=(
            "D4c (ADR 0029): a minimal, evaluation-only single-slice "
            "diagnostic probe -- packages and approves exactly one "
            "already-approved slice and runs it once, varying only the "
            "prompt condition or the coding model, never both, and never "
            "runs the full monolithic-vs-planned comparison. Always "
            "inherits the project's baseline configuration (including "
            "context window) unchanged -- there is no --context-profile "
            "override here, so this narrowly scoped command cannot "
            "introduce a second, unrecorded independent variable"
        ),
    )
    evaluate_planning_probe.add_argument("scenario", choices=["download-service-v2"])
    evaluate_planning_probe.add_argument("--plan-id", required=True)
    evaluate_planning_probe.add_argument(
        "--expected-plan-version", type=int, required=True
    )
    evaluate_planning_probe.add_argument(
        "--planned-project-root",
        type=Path,
        required=True,
        help=(
            "a disposable, already-initialized project directory where the "
            "fixed plan was already exported, imported, validated, and "
            "approved -- must still be at its untouched scenario baseline "
            "(no slice packaged or started yet); use a fresh copy per "
            "probe attempt so every attempt starts from identical fixture "
            "state"
        ),
    )
    evaluate_planning_probe.add_argument(
        "--slice-id",
        required=True,
        help="the plan's slice id to probe, ordinarily the first dependency-free slice",
    )
    evaluate_planning_probe.add_argument(
        "--prompt-condition",
        choices=["production", "progress_advisory"],
        required=True,
        help=(
            "'production' uses the unmodified, byte-for-byte production "
            "agent-step prompt; 'progress_advisory' appends the evaluation-"
            "only advisory note (ADR 0029) and nothing else, and requires "
            "the project's own configured coding model (no --alternate-"
            "model) -- a probe varies exactly one independent variable"
        ),
    )
    evaluate_planning_probe.add_argument(
        "--alternate-model",
        help=(
            "run this slice against a different installed local coding "
            "model instead of the project's configured local_coder, with "
            "every other decoding/config setting unchanged; requires "
            "--prompt-condition production (a probe varies exactly one "
            "independent variable) and --authorize-alternate-model to "
            "match exactly, and fails closed if the model is not actually "
            "installed at the configured Ollama endpoint or is identical "
            "to the project's already-configured coding model"
        ),
    )
    evaluate_planning_probe.add_argument(
        "--authorize-alternate-model",
        help=(
            "must exactly match --alternate-model -- an explicit, separate "
            "confirmation that this specific model is authorized to run; "
            "omitting it (or a mismatch) refuses the probe before any "
            "provider is constructed"
        ),
    )
    evaluate_planning_probe.add_argument(
        "--output-dir",
        type=Path,
        help="directory for the probe's JSON/Markdown report",
    )

    evaluate = subparsers.add_parser(
        "eval", help="run controlled evaluation lanes against a fixture"
    )
    evaluate.add_argument("fixture", choices=["download-service"])
    evaluate.add_argument(
        "--lane",
        action="append",
        choices=[item.value for item in EvalLane],
        help="lane(s) to run; defaults to every lane",
    )
    evaluate.add_argument(
        "--context-profile",
        choices=["16k", "32k", "64k", "128k", "256k"],
        help="override the native Ollama window and repository evidence budget",
    )
    evaluate.add_argument(
        "--output-dir",
        type=Path,
        help="directory for fixture copies and the comparison report",
    )
    evaluate.add_argument(
        "--max-hosted-spend-usd",
        type=float,
        help=(
            "hard aggregate spend ceiling in USD for every hosted "
            "(models.frontier_coder) call this invocation makes; required "
            "whenever a requested lane needs the frontier coder (hybrid, "
            "forced-escalation, frontier), refused before any lane runs if "
            "the configured worst-case allowance for this run already "
            "exceeds it (D5b, ADR 0030)"
        ),
    )
    plan = subparsers.add_parser(
        "plan", help="Architect Mode: deterministic planning workflow"
    )
    plan_subparsers = plan.add_subparsers(dest="plan_command", required=True)
    plan_export = plan_subparsers.add_parser(
        "export", help="export a reproducible planner request package for an idea"
    )
    plan_export.add_argument("idea")
    plan_import = plan_subparsers.add_parser(
        "import", help="import a manually-obtained planner response as a new plan"
    )
    plan_import.add_argument("response_path", type=Path)
    plan_validate = plan_subparsers.add_parser(
        "validate", help="run deterministic validation against a plan"
    )
    plan_validate.add_argument("plan_id")
    plan_inspect = plan_subparsers.add_parser(
        "inspect", help="show a plan, its events, and its audit artifacts"
    )
    plan_inspect.add_argument("plan_id")
    plan_approve = plan_subparsers.add_parser(
        "approve", help="approve a validated plan"
    )
    plan_approve.add_argument("plan_id")
    plan_approve.add_argument("--expected-version", type=int, required=True)

    plan_slice = plan_subparsers.add_parser(
        "slice",
        help=(
            "approved-plan to single-slice execution (ADR 0027): package, "
            "approve, and start one explicitly selected slice through the "
            "existing durable execution service -- never automatic, never "
            "more than one active slice per plan"
        ),
    )
    plan_slice_subparsers = plan_slice.add_subparsers(
        dest="plan_slice_command", required=True
    )
    plan_slice_list = plan_slice_subparsers.add_parser(
        "list", help="show every slice's real, current status for a plan"
    )
    plan_slice_list.add_argument("plan_id")
    plan_slice_inspect = plan_slice_subparsers.add_parser(
        "inspect", help="show one slice's status, record, and (if packaged) its package"
    )
    plan_slice_inspect.add_argument("plan_id")
    plan_slice_inspect.add_argument("slice_id")
    plan_slice_package = plan_slice_subparsers.add_parser(
        "package",
        help=(
            "deterministically compile and durably record an immutable "
            "execution package for one slice -- no model call, no task "
            "created yet"
        ),
    )
    plan_slice_package.add_argument("plan_id")
    plan_slice_package.add_argument("slice_id")
    plan_slice_package.add_argument("--expected-plan-version", type=int, required=True)
    plan_slice_approve = plan_slice_subparsers.add_parser(
        "approve",
        help=(
            "approve exactly the previewed package: creates and approves "
            "the derived task, but does not start execution"
        ),
    )
    plan_slice_approve.add_argument("plan_id")
    plan_slice_approve.add_argument("slice_id")
    plan_slice_approve.add_argument("--expected-package-sha256", required=True)
    plan_slice_status = plan_slice_subparsers.add_parser(
        "status", help="real, current status for one slice, read from persisted facts"
    )
    plan_slice_status.add_argument("plan_id")
    plan_slice_status.add_argument("slice_id")
    plan_slice_start = plan_slice_subparsers.add_parser(
        "start",
        help=(
            "start an approved slice's derived task through the existing "
            "D2 durable execution service"
        ),
    )
    plan_slice_start.add_argument("plan_id")
    plan_slice_start.add_argument("slice_id")
    plan_slice_start.add_argument("--operation-id")

    discover = subparsers.add_parser(
        "discover",
        help=(
            "local-first Architect Mode discovery followed by an optional "
            "frontier planning stage (ADR 0032): a bounded, deterministic "
            "workflow, never a general chat"
        ),
    )
    discover_subparsers = discover.add_subparsers(
        dest="discover_command", required=True
    )
    discover_start = discover_subparsers.add_parser(
        "start", help="enter an idea and create a new discovery session"
    )
    discover_start.add_argument("idea_text")
    discover_inspect = discover_subparsers.add_parser(
        "inspect", help="show one discovery session's current state"
    )
    discover_inspect.add_argument("session_id")
    discover_propose_questions = discover_subparsers.add_parser(
        "propose-questions",
        help="the configured local model proposes up to the configured maximum of clarification questions",
    )
    discover_propose_questions.add_argument("session_id")
    discover_propose_questions.add_argument("--expected-version", type=int, required=True)
    discover_answer_questions = discover_subparsers.add_parser(
        "answer-questions",
        help="record the user's own verbatim answers to the local model's questions",
    )
    discover_answer_questions.add_argument("session_id")
    discover_answer_questions.add_argument("--expected-version", type=int, required=True)
    discover_answer_questions.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="QUESTION_ID=TEXT",
        help="repeatable; one per question",
    )
    discover_propose_brief = discover_subparsers.add_parser(
        "propose-brief", help="the configured local model proposes an IdeaBrief"
    )
    discover_propose_brief.add_argument("session_id")
    discover_propose_brief.add_argument("--expected-version", type=int, required=True)
    discover_approve_brief = discover_subparsers.add_parser(
        "approve-brief", help="explicit user approval of the proposed idea brief"
    )
    discover_approve_brief.add_argument("session_id")
    discover_approve_brief.add_argument("--expected-version", type=int, required=True)
    discover_export = discover_subparsers.add_parser(
        "export-frontier-package",
        help=(
            "export an immutable FrontierPlanningRequestPackage plus a "
            "self-contained FRONTIER-PLANNING-HANDOFF.md; requires an "
            "approved idea brief"
        ),
    )
    discover_export.add_argument("session_id")
    discover_export.add_argument("--transport", choices=["api", "manual"], required=True)
    discover_export.add_argument("--expected-version", type=int, required=True)
    discover_preview_api = discover_subparsers.add_parser(
        "preview-api-call",
        help=(
            "show the configured frontier provider/model and a pessimistic "
            "worst-case cost for one API planning call, before any "
            "separate spend-ceiling authorization or call is made"
        ),
    )
    discover_preview_api.add_argument("session_id")
    discover_call_api = discover_subparsers.add_parser(
        "call-api",
        help=(
            "make exactly one real, explicitly authorized, spend-ceilinged "
            "API planning call for the session's current outstanding "
            "package"
        ),
    )
    discover_call_api.add_argument("session_id")
    discover_call_api.add_argument(
        "--authorize-planning-spend-usd",
        type=float,
        required=True,
        help="hard ceiling in USD for this one call, checked before and after it",
    )
    discover_import_manual = discover_subparsers.add_parser(
        "import-manual-response",
        help="validate and apply a pasted manual-subscription response",
    )
    discover_import_manual.add_argument("session_id")
    discover_import_manual.add_argument("--package-id", required=True)
    discover_import_manual.add_argument(
        "--response", required=True, type=Path, help="path to the pasted JSON response"
    )
    discover_import_manual.add_argument(
        "--declared-model-name",
        required=True,
        help="operator-declared provenance for which subscription model produced this response",
    )
    discover_answer_frontier = discover_subparsers.add_parser(
        "answer-frontier-questions",
        help="record the user's own verbatim answers to the frontier model's clarification questions",
    )
    discover_answer_frontier.add_argument("session_id")
    discover_answer_frontier.add_argument("--expected-version", type=int, required=True)
    discover_answer_frontier.add_argument(
        "--answer", action="append", default=[], metavar="QUESTION_ID=TEXT"
    )

    intake = subparsers.add_parser(
        "intake",
        help=(
            "durable model-assisted new-task intake (ADR 0023): a CLI/"
            "service seam for creating, inspecting, and recovering intake "
            "operations without requiring `apoapsis ui`"
        ),
    )
    intake_subparsers = intake.add_subparsers(dest="intake_command", required=True)
    intake_submit = intake_subparsers.add_parser(
        "submit",
        help=(
            "run model-assisted specification extraction for a new "
            "natural-language request, stopping at SPEC_DRAFTED -- never "
            "executes the resulting task"
        ),
    )
    intake_submit.add_argument("request_text")
    intake_submit.add_argument("--operation-id", required=True)
    intake_inspect = intake_subparsers.add_parser(
        "inspect", help="show one intake operation's durable record"
    )
    intake_inspect.add_argument("operation_id")
    intake_recover = intake_subparsers.add_parser(
        "recover",
        help=(
            "explicit crash recovery: reclaim never-started intake "
            "operations, mark stale running ones ambiguous, and return "
            "stuck tasks to human review"
        ),
    )
    intake_recover.add_argument(
        "--resume-recorded",
        action="store_true",
        help=(
            "also actually run every reclaimed RECORDED operation now, "
            "synchronously, in this process -- without this flag, "
            "recover only reports what it found reclaimable/ambiguous "
            "and runs nothing. Running recovered model work is only ever "
            "done when explicitly requested."
        ),
    )

    execute = subparsers.add_parser(
        "execute",
        help=(
            "durable post-approval task execution (ADR 0024): a CLI/"
            "service seam for starting, inspecting, and recovering "
            "execution operations without requiring `apoapsis ui`"
        ),
    )
    execute_subparsers = execute.add_subparsers(
        dest="execute_command", required=True
    )
    execute_start = execute_subparsers.add_parser(
        "start",
        help=(
            "start the normal routing/context/agent/verification pipeline "
            "for an already-approved task"
        ),
    )
    execute_start.add_argument("task_id")
    execute_start.add_argument("--expected-version", type=int, required=True)
    execute_start.add_argument("--operation-id", required=True)
    execute_inspect = execute_subparsers.add_parser(
        "inspect", help="show one execution operation's durable record"
    )
    execute_inspect.add_argument("operation_id")
    execute_recover = execute_subparsers.add_parser(
        "recover",
        help=(
            "explicit crash recovery: reclaim never-started execution "
            "operations, mark stale running ones ambiguous, and return "
            "stuck tasks to human review with their worktree preserved"
        ),
    )
    execute_recover.add_argument(
        "--resume-recorded",
        action="store_true",
        help=(
            "also actually run every reclaimed RECORDED operation now, "
            "synchronously, in this process -- without this flag, "
            "recover only reports what it found reclaimable/ambiguous "
            "and runs nothing. Running recovered model work is only ever "
            "done when explicitly requested."
        ),
    )

    review = subparsers.add_parser(
        "review", help="deterministic human-review and resume (ADR 0020)"
    )
    review_subparsers = review.add_subparsers(dest="review_command", required=True)
    review_subparsers.add_parser(
        "list", help="list every task currently at HUMAN_REVIEW_REQUIRED"
    )
    review_inspect = review_subparsers.add_parser(
        "inspect", help="show one task's deterministic review case"
    )
    review_inspect.add_argument("task_id")
    review_abandon = review_subparsers.add_parser(
        "abandon", help="abandon and roll back a task from human review"
    )
    review_abandon.add_argument("task_id")
    review_abandon.add_argument("--expected-version", type=int, required=True)
    review_abandon.add_argument("--operation-id", required=True)
    review_retry = review_subparsers.add_parser(
        "retry-verification", help="re-run configured verification, no model call"
    )
    review_retry.add_argument("task_id")
    review_retry.add_argument("--expected-version", type=int, required=True)
    review_retry.add_argument("--expected-fingerprint", required=True)
    review_retry.add_argument("--operation-id", required=True)
    review_continue_local = review_subparsers.add_parser(
        "continue-local", help="resume the bounded local coding agent"
    )
    review_continue_local.add_argument("task_id")
    review_continue_local.add_argument("--expected-version", type=int, required=True)
    review_continue_local.add_argument("--expected-fingerprint", required=True)
    review_continue_local.add_argument("--operation-id", required=True)
    review_continue_local.add_argument("--additional-turns", type=int, required=True)
    review_continue_frontier = review_subparsers.add_parser(
        "continue-frontier", help="resume the bounded frontier coding agent"
    )
    review_continue_frontier.add_argument("task_id")
    review_continue_frontier.add_argument(
        "--expected-version", type=int, required=True
    )
    review_continue_frontier.add_argument("--expected-fingerprint", required=True)
    review_continue_frontier.add_argument("--operation-id", required=True)
    review_continue_frontier.add_argument(
        "--additional-turns", type=int, required=True
    )
    review_authorize_frontier = review_subparsers.add_parser(
        "authorize-frontier-stage",
        help=(
            "start a fresh configured frontier stage after a local session "
            "stopped (never continues an existing frontier session -- use "
            "continue-frontier for that)"
        ),
    )
    review_authorize_frontier.add_argument("task_id")
    review_authorize_frontier.add_argument(
        "--expected-version", type=int, required=True
    )
    review_authorize_frontier.add_argument(
        "--expected-fingerprint", required=True
    )
    review_authorize_frontier.add_argument("--operation-id", required=True)
    review_recover = review_subparsers.add_parser(
        "recover",
        help=(
            "explicit crash recovery: reclaim never-started operations, "
            "mark stale running ones ambiguous, and return stuck tasks to "
            "human review"
        ),
    )
    review_recover.add_argument(
        "--resume-recorded",
        action="store_true",
        help=(
            "also actually run every reclaimed RECORDED operation now, "
            "synchronously, in this process -- without this flag, "
            "recover only reports what it found reclaimable/ambiguous "
            "and runs nothing. Running recovered model work is only ever "
            "done when explicitly requested."
        ),
    )

    frontier_manual = subparsers.add_parser(
        "frontier-manual",
        help=(
            "manual subscription-based frontier coding handoff (ADR 0031): "
            "export a hashed package to upload to a ChatGPT/Claude "
            "subscription session by hand, then import, approve, and apply "
            "one bounded response -- never automates either website, never "
            "reuses subscription credentials, never calls a hosted API"
        ),
    )
    frontier_manual_subparsers = frontier_manual.add_subparsers(
        dest="frontier_manual_command", required=True
    )
    fm_export = frontier_manual_subparsers.add_parser(
        "export",
        help=(
            "export an immutable handoff package plus a self-contained "
            "FRONTIER-CODING-HANDOFF.md to upload by hand"
        ),
    )
    fm_export.add_argument("task_id")
    fm_import = frontier_manual_subparsers.add_parser(
        "import",
        help="validate a pasted response and create an applyable preview",
    )
    fm_import.add_argument("task_id")
    fm_import.add_argument("--package-id", required=True)
    fm_import.add_argument(
        "--response", required=True, type=Path, help="path to the pasted JSON response"
    )
    fm_import.add_argument(
        "--declared-model-name",
        required=True,
        help=(
            "operator-declared provenance for which subscription model "
            "produced this response (e.g. 'claude-opus-4.6-web') -- never "
            "inferred, never defaulted"
        ),
    )
    fm_import.add_argument("--preview-id", required=True)
    fm_inspect = frontier_manual_subparsers.add_parser(
        "inspect", help="show one exported package or imported preview"
    )
    fm_inspect.add_argument("task_id")
    fm_inspect_group = fm_inspect.add_mutually_exclusive_group(required=True)
    fm_inspect_group.add_argument("--package-id")
    fm_inspect_group.add_argument("--preview-id")
    fm_approve = frontier_manual_subparsers.add_parser(
        "approve",
        help="step 1 of 2: record explicit intent to apply a previewed patch",
    )
    fm_approve.add_argument("task_id")
    fm_approve.add_argument("--preview-id", required=True)
    fm_approve.add_argument("--expected-version", type=int, required=True)
    fm_apply = frontier_manual_subparsers.add_parser(
        "apply",
        help=(
            "step 2 of 2: apply an approved preview's patch and run "
            "verification -- only the verifier may complete the task"
        ),
    )
    fm_apply.add_argument("task_id")
    fm_apply.add_argument("--preview-id", required=True)
    fm_apply.add_argument("--expected-version", type=int, required=True)
    fm_apply.add_argument("--expected-fingerprint", required=True)
    fm_apply.add_argument("--operation-id", required=True)
    fm_status = frontier_manual_subparsers.add_parser(
        "status", help="show manual-frontier eligibility and repair-round usage"
    )
    fm_status.add_argument("task_id")

    aggregate = subparsers.add_parser(
        "eval-aggregate",
        help="aggregate one or more persisted evaluation comparison reports",
    )
    aggregate.add_argument(
        "comparisons",
        nargs="+",
        type=Path,
        help="comparison.json files produced by `apoapsis eval`",
    )
    aggregate.add_argument(
        "--output-dir",
        type=Path,
        help="directory for aggregate.json and aggregate.md",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
    except (
        TaskStoreError,
        WorktreeError,
        GitCommandError,
        ValidationError,
        ResearchEngineError,
        ResearchModelError,
        ProviderError,
        InstrumentedProviderError,
        ArchitectError,
        ReviewError,
        IntakeError,
        ExecutionOperationError,
        HostedSpendCeilingExceededError,
        ManualFrontierError,
        DiscoveryError,
    ) as exc:
        parser.exit(2, f"error: {exc}\n")
    if result is not None:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))


def _dispatch(args: argparse.Namespace) -> dict[str, object] | None:
    root = args.project_root.resolve()
    if args.command == "init":
        return _init(root)
    if args.command == "doctor":
        return run_doctor(root, probe_providers=args.probe).model_dump(mode="json")
    if args.command == "ui":
        from apoapsis.ui.server import serve_ui

        serve_ui(root, port=args.port, open_browser=not args.no_open)
        return None
    if args.command == "eval":
        return _eval_download_service(
            root,
            args.lane,
            args.context_profile,
            args.output_dir,
            max_hosted_spend_usd=args.max_hosted_spend_usd,
        )
    if args.command == "eval-planning":
        return _eval_planning(
            root,
            args.plan_id,
            args.expected_plan_version,
            args.planned_project_root.resolve(),
            args.planner_model,
            args.context_profile,
            args.output_dir,
        )
    if args.command == "eval-planning-probe":
        return _eval_planning_probe(
            root,
            args.plan_id,
            args.expected_plan_version,
            args.planned_project_root.resolve(),
            args.slice_id,
            args.prompt_condition,
            args.alternate_model,
            args.authorize_alternate_model,
            args.output_dir,
        )
    if args.command == "eval-aggregate":
        return _aggregate_eval_reports(root, args.comparisons, args.output_dir)
    if args.command == "plan":
        return _plan_command(root, args)
    if args.command == "discover":
        return _discover_command(root, args)
    store = _store(root)
    if args.command == "task":
        return _task(
            store,
            args.request,
            args.constraint,
            args.acceptance,
            research_mode=args.research,
        )
    if args.command == "run":
        return _run_vertical_slice(
            root,
            store,
            args.request,
            assume_yes=args.yes,
            requested_research=args.research,
            context_profile=args.context_profile,
            execution_mode=args.execution_mode,
            agent_route=args.agent_route,
        )
    if args.command == "research":
        return _research_command(
            root, store, args.research_args, requested_mode=args.mode
        )
    if args.command == "review":
        return _review_command(root, store, args)
    if args.command == "intake":
        return _intake_command(root, store, args)
    if args.command == "execute":
        return _execute_command(root, store, args)
    if args.command == "frontier-manual":
        return _manual_frontier_command(root, store, args)
    if args.command == "inspect":
        record = store.get_task(args.task_id)
        result: dict[str, object] = {
            "task": record.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in store.events(args.task_id)],
        }
        report_path = root / ".apoapsis" / "tasks" / args.task_id / "report.json"
        if report_path.is_file():
            result["report"] = json.loads(report_path.read_text(encoding="utf-8"))
        return result
    if args.command == "approve":
        record = store.transition(
            args.task_id,
            WorkflowState.SPEC_APPROVED,
            actor=WorkflowActor.USER,
            event_type="specification_approved",
            expected_version=args.version,
        )
        return record.model_dump(mode="json")
    if args.command == "worktree-create":
        store.get_task(args.task_id)
        manager = WorktreeManager(root)
        worktree = manager.create(_task_slug(args.task_id), base_ref=args.base)
        return worktree.model_dump(mode="json")
    if args.command == "verify":
        return _verify(root, store, args.task_id, args.path)
    if args.command == "rollback":
        return _rollback(root, store, args.task_id, args.delete_branch)
    raise AssertionError(f"unhandled command: {args.command}")


def _init(root: Path) -> dict[str, object]:
    GitRepository(root)
    metadata = root / ".apoapsis"
    metadata.mkdir(parents=True, exist_ok=True)
    config = metadata / "config.toml"
    created_config = False
    if not config.exists():
        config.write_text(DEFAULT_CONFIG, encoding="utf-8")
        created_config = True
    SQLiteTaskStore(metadata / "apoapsis.db")
    return {
        "initialized": True,
        "metadata_directory": str(metadata),
        "config_created": created_config,
    }


def _store(root: Path) -> SQLiteTaskStore:
    metadata = root / ".apoapsis"
    if not (metadata / "config.toml").is_file():
        raise TaskStoreError("Apoapsis is not initialized; run 'apoapsis init' first")
    return SQLiteTaskStore(metadata / "apoapsis.db")


def _plan_store(root: Path) -> SQLitePlanStore:
    metadata = root / ".apoapsis"
    if not (metadata / "config.toml").is_file():
        raise TaskStoreError("Apoapsis is not initialized; run 'apoapsis init' first")
    return SQLitePlanStore(metadata / "architect-plans.db")


def _plan_slice_store(root: Path) -> PlanSliceExecutionStore:
    metadata = root / ".apoapsis"
    if not (metadata / "config.toml").is_file():
        raise TaskStoreError("Apoapsis is not initialized; run 'apoapsis init' first")
    return PlanSliceExecutionStore(metadata / "plan-slice-executions.db")


def _plan_command(root: Path, args: argparse.Namespace) -> dict[str, object]:
    if args.plan_command == "export":
        return _plan_export(root, args.idea)
    if args.plan_command == "slice":
        return _plan_slice_command(root, args)
    plan_store = _plan_store(root)
    if args.plan_command == "import":
        return _plan_import(root, plan_store, args.response_path)
    if args.plan_command == "validate":
        return _plan_validate(root, plan_store, args.plan_id)
    if args.plan_command == "inspect":
        return _plan_inspect(root, plan_store, args.plan_id)
    if args.plan_command == "approve":
        return _plan_approve(root, plan_store, args.plan_id, args.expected_version)
    raise AssertionError(f"unhandled plan command: {args.plan_command}")


def _plan_export(root: Path, idea: str) -> dict[str, object]:
    config = ApoapsisConfig.from_toml(root / ".apoapsis" / "config.toml")
    package = build_planner_request_package(root, idea, config)
    artifact_path = write_package_artifact(root, package)
    return {"package": package.model_dump(mode="json"), "artifact_path": artifact_path}


def _plan_import(
    root: Path, plan_store: SQLitePlanStore, response_path: Path
) -> dict[str, object]:
    resolved = response_path.resolve()
    if not resolved.is_file():
        raise PlanImportError(f"planner response file not found: {resolved}")
    try:
        raw_payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlanImportError(f"planner response is not valid JSON: {exc}") from exc
    if not isinstance(raw_payload, dict):
        raise PlanImportError("planner response must be a JSON object")
    record = import_planner_response(root, plan_store, raw_payload)
    return record.model_dump(mode="json")


def _plan_validate(
    root: Path, plan_store: SQLitePlanStore, plan_id: str
) -> dict[str, object]:
    config = ApoapsisConfig.from_toml(root / ".apoapsis" / "config.toml")
    record = plan_store.get_plan(plan_id)
    configured_names = {command.name for command in config.verification.commands}
    findings = validate_plan(
        record.plan,
        configured_verification_commands=configured_names,
        ceilings=config.architect.ceilings,
    )
    result = PlanValidationResult(
        plan_id=plan_id,
        plan_version=record.version,
        valid=not any(item.severity == ValidationSeverity.ERROR for item in findings),
        findings=findings,
    )
    updated = plan_store.record_validation(
        plan_id, result, expected_version=record.version
    )
    PlanAuditStore(root, plan_id).write_json(
        f"validation-v{record.version}.json", result, kind="plan_validation_result"
    )
    return {"plan": updated.model_dump(mode="json"), "validation": result.model_dump(mode="json")}


def _plan_inspect(
    root: Path, plan_store: SQLitePlanStore, plan_id: str
) -> dict[str, object]:
    record = plan_store.get_plan(plan_id)
    events = plan_store.events(plan_id)
    return {
        "plan": record.model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in events],
        "artifacts": PlanAuditStore(root, plan_id).artifacts(),
    }


def _plan_approve(
    root: Path, plan_store: SQLitePlanStore, plan_id: str, expected_version: int
) -> dict[str, object]:
    record = plan_store.approve_plan(plan_id, expected_version=expected_version)
    PlanAuditStore(root, plan_id).write_json(
        "approval-event.json",
        {
            "plan_id": plan_id,
            "approved_version": record.version,
            "approved_at": record.updated_at.isoformat(),
        },
        kind="plan_approval",
    )
    return record.model_dump(mode="json")


def _plan_slice_command(root: Path, args: argparse.Namespace) -> dict[str, object]:
    plan_store = _plan_store(root)
    slice_store = _plan_slice_store(root)
    task_store = _store(root)
    operation_store = _execution_operation_store(root)
    if args.plan_slice_command == "list":
        plan_record = plan_store.get_plan(args.plan_id)
        return {
            "plan_id": args.plan_id,
            "slices": [
                project_slice_status(
                    root, plan_store, slice_store, task_store, args.plan_id, item.slice_id
                )
                for item in plan_record.plan.slices
            ],
        }
    if args.plan_slice_command == "inspect":
        status = project_slice_status(
            root, plan_store, slice_store, task_store, args.plan_id, args.slice_id
        )
        status["artifacts"] = PlanAuditStore(root, args.plan_id).artifacts()
        return status
    if args.plan_slice_command == "status":
        return project_slice_status(
            root, plan_store, slice_store, task_store, args.plan_id, args.slice_id
        )
    if args.plan_slice_command == "package":
        config = ApoapsisConfig.from_toml(root / ".apoapsis" / "config.toml")
        package = package_slice(
            root,
            plan_store,
            slice_store,
            task_store,
            operation_store,
            args.plan_id,
            args.slice_id,
            expected_plan_version=args.expected_plan_version,
            config=config,
        )
        return package.model_dump(mode="json")
    if args.plan_slice_command == "approve":
        record = approve_slice(
            root,
            task_store,
            slice_store,
            args.plan_id,
            args.slice_id,
            expected_package_sha256=args.expected_package_sha256,
        )
        return record.model_dump(mode="json")
    if args.plan_slice_command == "start":
        config = ApoapsisConfig.from_toml(root / ".apoapsis" / "config.toml")
        result = start_slice(
            root,
            task_store,
            slice_store,
            operation_store,
            args.plan_id,
            args.slice_id,
            config,
            operation_id=args.operation_id,
        )
        return result.model_dump(mode="json")
    raise AssertionError(f"unhandled plan slice command: {args.plan_slice_command}")


def _discovery_store(root: Path) -> SQLiteDiscoveryStore:
    metadata = root / ".apoapsis"
    if not (metadata / "config.toml").is_file():
        raise TaskStoreError("Apoapsis is not initialized; run 'apoapsis init' first")
    return SQLiteDiscoveryStore(metadata / "discovery-sessions.db")


def _parse_answers(raw_answers: list[str]) -> list[ClarificationAnswer]:
    answers: list[ClarificationAnswer] = []
    for item in raw_answers:
        if "=" not in item:
            raise DiscoveryError(
                f"--answer must be QUESTION_ID=TEXT, got: {item!r}"
            )
        question_id, text = item.split("=", 1)
        answers.append(ClarificationAnswer(question_id=question_id, text=text))
    return answers


def _discover_command(root: Path, args: argparse.Namespace) -> dict[str, object]:
    discovery_store = _discovery_store(root)
    config = ApoapsisConfig.from_toml(root / ".apoapsis" / "config.toml")
    if args.discover_command == "start":
        record = discovery_start_session(discovery_store, args.idea_text)
        return record.model_dump(mode="json")
    if args.discover_command == "inspect":
        return discovery_store.get_session(args.session_id).model_dump(mode="json")
    if args.discover_command == "propose-questions":
        record = discovery_propose_local_questions(
            root,
            discovery_store,
            config,
            args.session_id,
            expected_version=args.expected_version,
        )
        return record.model_dump(mode="json")
    if args.discover_command == "answer-questions":
        record = discovery_record_local_answers(
            discovery_store,
            args.session_id,
            _parse_answers(args.answer),
            expected_version=args.expected_version,
        )
        return record.model_dump(mode="json")
    if args.discover_command == "propose-brief":
        record = discovery_propose_idea_brief(
            root,
            discovery_store,
            config,
            args.session_id,
            expected_version=args.expected_version,
        )
        return record.model_dump(mode="json")
    if args.discover_command == "approve-brief":
        record = discovery_approve_idea_brief(
            discovery_store, args.session_id, expected_version=args.expected_version
        )
        return record.model_dump(mode="json")
    if args.discover_command == "export-frontier-package":
        record, package, json_path, markdown_path = discovery_export_frontier_package(
            root,
            discovery_store,
            config,
            args.session_id,
            transport=args.transport,
            expected_version=args.expected_version,
        )
        return {
            "session": record.model_dump(mode="json"),
            "package": package.model_dump(mode="json"),
            "package_artifact_path": json_path,
            "markdown_artifact_path": markdown_path,
        }
    if args.discover_command == "preview-api-call":
        session = discovery_store.get_session(args.session_id)
        if session.frontier_package_id is None:
            raise DiscoveryError(
                f"session {args.session_id} has no outstanding frontier package "
                "yet; run export-frontier-package --transport api first"
            )
        package = discovery_load_package(root, session.frontier_package_id)
        preview = discovery_preview_api_call(config, package)
        return preview.model_dump(mode="json")
    if args.discover_command == "call-api":
        session = discovery_store.get_session(args.session_id)
        if session.frontier_package_id is None:
            raise DiscoveryError(
                f"session {args.session_id} has no outstanding frontier package "
                "yet; run export-frontier-package --transport api first"
            )
        package = discovery_load_package(root, session.frontier_package_id)
        plan_store = _plan_store(root)
        record, cost_usd = discovery_call_api(
            root,
            discovery_store,
            plan_store,
            config,
            session_id=args.session_id,
            package=package,
            authorized_max_spend_usd=args.authorize_planning_spend_usd,
        )
        return {"session": record.model_dump(mode="json"), "measured_cost_usd": cost_usd}
    if args.discover_command == "import-manual-response":
        plan_store = _plan_store(root)
        response_bytes = args.response.resolve().read_bytes()
        record = discovery_import_manual_response(
            root,
            discovery_store,
            plan_store,
            config,
            session_id=args.session_id,
            package_id=args.package_id,
            response_bytes=response_bytes,
            declared_model_name=args.declared_model_name,
        )
        return record.model_dump(mode="json")
    if args.discover_command == "answer-frontier-questions":
        record = discovery_record_frontier_answers(
            discovery_store,
            args.session_id,
            _parse_answers(args.answer),
            expected_version=args.expected_version,
        )
        return record.model_dump(mode="json")
    raise AssertionError(f"unhandled discover command: {args.discover_command}")


def _intake_operation_store(root: Path) -> IntakeOperationStore:
    metadata = root / ".apoapsis"
    if not (metadata / "config.toml").is_file():
        raise TaskStoreError("Apoapsis is not initialized; run 'apoapsis init' first")
    return IntakeOperationStore(metadata / "intake-operations.db")


def _intake_command(
    root: Path, store: SQLiteTaskStore, args: argparse.Namespace
) -> dict[str, object]:
    operation_store = _intake_operation_store(root)
    if args.intake_command == "inspect":
        return operation_store.get(args.operation_id).model_dump(mode="json")
    if args.intake_command == "recover":
        report = recover_stale_intake_operations(store, operation_store)
        result = report.model_dump(mode="json")
        if args.resume_recorded and report.reclaimed_operation_ids:
            config = ApoapsisConfig.from_toml(root / ".apoapsis" / "config.toml")
            result["resumed"] = []
            for reclaimed_id in report.reclaimed_operation_ids:
                record = run_intake_operation(
                    root, store, operation_store, config, operation_id=reclaimed_id
                )
                result["resumed"].append(record.model_dump(mode="json"))
        return result
    if args.intake_command == "submit":
        config = ApoapsisConfig.from_toml(root / ".apoapsis" / "config.toml")
        record = execute_intake_operation(
            root,
            store,
            operation_store,
            config,
            request_text=args.request_text,
            operation_id=args.operation_id,
        )
        return record.model_dump(mode="json")
    raise AssertionError(f"unhandled intake command: {args.intake_command}")


def _execution_operation_store(root: Path) -> ExecutionOperationStore:
    metadata = root / ".apoapsis"
    if not (metadata / "config.toml").is_file():
        raise TaskStoreError("Apoapsis is not initialized; run 'apoapsis init' first")
    return ExecutionOperationStore(metadata / "execution-operations.db")


def _execute_command(
    root: Path, store: SQLiteTaskStore, args: argparse.Namespace
) -> dict[str, object]:
    operation_store = _execution_operation_store(root)
    if args.execute_command == "inspect":
        return operation_store.get(args.operation_id).model_dump(mode="json")
    if args.execute_command == "recover":
        report = recover_stale_execution_operations(store, operation_store)
        result = report.model_dump(mode="json")
        if args.resume_recorded and report.reclaimed_operation_ids:
            config = ApoapsisConfig.from_toml(root / ".apoapsis" / "config.toml")
            result["resumed"] = []
            for reclaimed_id in report.reclaimed_operation_ids:
                record = run_execution_operation(
                    root, store, operation_store, config, operation_id=reclaimed_id
                )
                result["resumed"].append(record.model_dump(mode="json"))
        return result
    if args.execute_command == "start":
        config = ApoapsisConfig.from_toml(root / ".apoapsis" / "config.toml")
        record = execute_execution_operation(
            root,
            store,
            operation_store,
            config,
            task_id=args.task_id,
            operation_id=args.operation_id,
            expected_version=args.expected_version,
        )
        return record.model_dump(mode="json")
    raise AssertionError(f"unhandled execute command: {args.execute_command}")


def _manual_frontier_preview_store(root: Path) -> ManualFrontierPreviewStore:
    metadata = root / ".apoapsis"
    if not (metadata / "config.toml").is_file():
        raise TaskStoreError("Apoapsis is not initialized; run 'apoapsis init' first")
    return ManualFrontierPreviewStore(metadata / "manual-frontier-previews.db")


def _manual_frontier_command(
    root: Path, store: SQLiteTaskStore, args: argparse.Namespace
) -> dict[str, object]:
    config = ApoapsisConfig.from_toml(root / ".apoapsis" / "config.toml")
    preview_store = _manual_frontier_preview_store(root)
    review_operation_store = _review_operation_store(root)

    if args.frontier_manual_command == "export":
        review_case = build_review_case(root, store, config, args.task_id)
        if ReviewActionKind.MANUAL_FRONTIER_HANDOFF not in review_case.eligible_actions:
            raise ManualFrontierError(
                f"manual_frontier_handoff is not currently eligible for "
                f"{args.task_id} (eligible actions: "
                f"{[item.value for item in review_case.eligible_actions]})"
            )
        specification = store.get_task(args.task_id).specification
        package = build_manual_frontier_handoff_package(
            review_case,
            specification,
            config.verification.commands,
            repair_round=review_case.manual_frontier_rounds_used,
        )
        audit = TaskAuditStore(root, args.task_id)
        json_artifact, markdown_artifact = write_handoff_artifacts(audit, package)
        return {
            "package": package.model_dump(mode="json"),
            "package_artifact_path": json_artifact.path,
            "markdown_artifact_path": markdown_artifact.path,
        }
    if args.frontier_manual_command == "import":
        response_bytes = args.response.resolve().read_bytes()
        preview = import_manual_frontier_response(
            root,
            store,
            preview_store,
            review_operation_store,
            config,
            task_id=args.task_id,
            package_id=args.package_id,
            response_bytes=response_bytes,
            declared_model_name=args.declared_model_name,
            preview_id=args.preview_id,
        )
        return preview.model_dump(mode="json")
    if args.frontier_manual_command == "inspect":
        if args.package_id:
            return load_manual_frontier_package(
                root, args.task_id, args.package_id
            ).model_dump(mode="json")
        preview = preview_store.get(args.preview_id)
        if preview.task_id != args.task_id:
            raise ManualFrontierError(
                f"preview {args.preview_id} belongs to task {preview.task_id}, "
                f"not {args.task_id}"
            )
        return preview.model_dump(mode="json")
    if args.frontier_manual_command == "approve":
        preview = approve_manual_frontier_preview(
            root,
            store,
            preview_store,
            config,
            task_id=args.task_id,
            preview_id=args.preview_id,
            expected_task_version=args.expected_version,
        )
        return preview.model_dump(mode="json")
    if args.frontier_manual_command == "apply":
        record = execute_review_action(
            root,
            store,
            review_operation_store,
            config,
            task_id=args.task_id,
            action=ReviewActionKind.MANUAL_FRONTIER_HANDOFF,
            operation_id=args.operation_id,
            expected_version=args.expected_version,
            expected_worktree_fingerprint=args.expected_fingerprint,
            manual_frontier_preview_id=args.preview_id,
        )
        return record.model_dump(mode="json")
    if args.frontier_manual_command == "status":
        review_case = build_review_case(root, store, config, args.task_id)
        previews = preview_store.list_for_task(args.task_id)
        return {
            "task_id": args.task_id,
            "manual_frontier_eligible": (
                ReviewActionKind.MANUAL_FRONTIER_HANDOFF in review_case.eligible_actions
            ),
            "manual_frontier_rounds_used": review_case.manual_frontier_rounds_used,
            "max_manual_frontier_rounds": review_case.max_manual_frontier_rounds,
            "previews": [item.model_dump(mode="json") for item in previews],
        }
    raise AssertionError(
        f"unhandled frontier-manual command: {args.frontier_manual_command}"
    )


def _review_operation_store(root: Path) -> ReviewOperationStore:
    metadata = root / ".apoapsis"
    if not (metadata / "config.toml").is_file():
        raise TaskStoreError("Apoapsis is not initialized; run 'apoapsis init' first")
    return ReviewOperationStore(metadata / "review-operations.db")


def _review_command(
    root: Path, store: SQLiteTaskStore, args: argparse.Namespace
) -> dict[str, object]:
    config = ApoapsisConfig.from_toml(root / ".apoapsis" / "config.toml")
    if args.review_command == "list":
        cases = [
            build_review_case(root, store, config, record.task_id).model_dump(
                mode="json"
            )
            for record in store.list_tasks(limit=200)
            if record.state == WorkflowState.HUMAN_REVIEW_REQUIRED
        ]
        return {"cases": cases}
    if args.review_command == "inspect":
        return build_review_case(root, store, config, args.task_id).model_dump(
            mode="json"
        )

    operation_store = _review_operation_store(root)
    if args.review_command == "abandon":
        record = execute_review_action(
            root,
            store,
            operation_store,
            config,
            task_id=args.task_id,
            action=ReviewActionKind.ABANDON,
            operation_id=args.operation_id,
            expected_version=args.expected_version,
        )
        return record.model_dump(mode="json")
    if args.review_command == "retry-verification":
        record = execute_review_action(
            root,
            store,
            operation_store,
            config,
            task_id=args.task_id,
            action=ReviewActionKind.VERIFICATION_ONLY_RETRY,
            operation_id=args.operation_id,
            expected_version=args.expected_version,
            expected_worktree_fingerprint=args.expected_fingerprint,
        )
        return record.model_dump(mode="json")
    if args.review_command in {"continue-local", "continue-frontier"}:
        _, local_coder_provider, frontier_coder_provider = _build_agent_providers(
            config
        )
        action = (
            ReviewActionKind.LOCAL_CONTINUATION
            if args.review_command == "continue-local"
            else ReviewActionKind.FRONTIER_CONTINUATION
        )
        record = execute_review_action(
            root,
            store,
            operation_store,
            config,
            task_id=args.task_id,
            action=action,
            operation_id=args.operation_id,
            expected_version=args.expected_version,
            expected_worktree_fingerprint=args.expected_fingerprint,
            additional_turns=args.additional_turns,
            local_coder_provider=local_coder_provider,
            frontier_coder_provider=frontier_coder_provider,
        )
        return record.model_dump(mode="json")
    if args.review_command == "authorize-frontier-stage":
        _, _, frontier_coder_provider = _build_agent_providers(config)
        record = execute_review_action(
            root,
            store,
            operation_store,
            config,
            task_id=args.task_id,
            action=ReviewActionKind.AUTHORIZE_FRONTIER_STAGE,
            operation_id=args.operation_id,
            expected_version=args.expected_version,
            expected_worktree_fingerprint=args.expected_fingerprint,
            frontier_coder_provider=frontier_coder_provider,
        )
        return record.model_dump(mode="json")
    if args.review_command == "recover":
        report = recover_stale_operations(store, operation_store)
        result = report.model_dump(mode="json")
        if args.resume_recorded and report.reclaimed_operation_ids:
            result["resumed"] = []
            for reclaimed_id in report.reclaimed_operation_ids:
                record = run_review_operation(
                    root, store, operation_store, config, operation_id=reclaimed_id
                )
                result["resumed"].append(record.model_dump(mode="json"))
        return result
    raise AssertionError(f"unhandled review command: {args.review_command}")


def _task(
    store: SQLiteTaskStore,
    request: str,
    constraints: list[str],
    acceptance: list[str],
    research_mode: str = "off",
) -> dict[str, object]:
    task_id = f"TASK-{uuid.uuid4().hex[:12].upper()}"
    specification = TaskSpecification(
        task_id=task_id,
        objective=TraceableStatement(
            text=request,
            source=SourceKind.USER,
            source_reference="cli-request",
        ),
        acceptance_criteria=[
            AcceptanceCriterion(
                id=f"AC-{index}",
                text=text,
                source=SourceKind.USER,
                source_reference=f"cli-acceptance-{index}",
            )
            for index, text in enumerate(acceptance, start=1)
        ],
        hard_constraints=[
            HardConstraint(
                id=f"HC-{index}",
                text=text,
                verbatim_source=text,
                interpreted_meaning=text,
                source=SourceKind.USER,
                source_reference=f"cli-constraint-{index}",
                verification_method="pending specification review",
            )
            for index, text in enumerate(constraints, start=1)
        ],
    )
    store.create_task(specification)
    record = store.transition(
        task_id,
        WorkflowState.SPEC_DRAFTED,
        actor=WorkflowActor.SYSTEM,
        event_type="deterministic_specification_drafted",
        payload={
            "natural_language_extraction_used": False,
            "requested_research_mode": ResearchMode.from_cli(research_mode).value,
        },
    )
    return record.model_dump(mode="json")


def _run_vertical_slice(
    root: Path,
    store: SQLiteTaskStore,
    request: str,
    *,
    assume_yes: bool,
    requested_research: str | None,
    context_profile: str | None,
    execution_mode: str | None,
    agent_route: str | None,
) -> dict[str, object]:
    config = ApoapsisConfig.from_toml(root / ".apoapsis" / "config.toml")
    if execution_mode is not None:
        config = config.model_copy(
            update={
                "execution": config.execution.model_copy(
                    update={"mode": ExecutionMode(execution_mode)}
                )
            }
        )
    if agent_route is not None:
        config = config.model_copy(
            update={
                "execution": config.execution.model_copy(
                    update={"route": AgentRoute(agent_route)}
                )
            }
        )
    if context_profile is not None:
        config = _apply_context_profile(config, context_profile)
    provider, local_coder_provider, frontier_coder_provider = _build_agent_providers(
        config
    )

    def approve(specification: TaskSpecification) -> bool:
        if assume_yes:
            return True
        print("\nExtracted specification:\n")
        print(specification.model_dump_json(indent=2))
        answer = input("\nApprove this specification? [y/N] ")
        return answer.strip().lower() in {"y", "yes"}

    research_mode = (
        ResearchMode.from_cli(requested_research)
        if requested_research
        else config.research.default_mode
    )
    research_engine = None
    fetch_process = None
    if research_mode != ResearchMode.OFF:
        research_engine, fetch_process = _build_research_engine(root, config)
    try:
        report = VerticalSliceRunner(
            root,
            store,
            provider,
            config,
            local_coder_provider=local_coder_provider,
            frontier_coder_provider=frontier_coder_provider,
            research_engine=research_engine,
            research_mode=research_mode,
        ).run(request, approve=approve)
    finally:
        if fetch_process is not None:
            fetch_process.close()
    return report.model_dump(mode="json")


_CONTEXT_PROFILES: dict[str, dict[str, int]] = {
    "16k": {
        "context_window_tokens": 16_384,
        "max_files": 10,
        "max_excerpt_lines": 100,
        "max_total_chars": 24_000,
    },
    "32k": {
        "context_window_tokens": 32_768,
        "max_files": 16,
        "max_excerpt_lines": 160,
        "max_total_chars": 72_000,
    },
    "64k": {
        "context_window_tokens": 65_536,
        "max_files": 24,
        "max_excerpt_lines": 240,
        "max_total_chars": 180_000,
    },
    # 128k and 256k are explicit, opt-in profiles (ADR 0010). They are not
    # the default merely because a model or VRAM budget can fit them --
    # `apoapsis doctor` and per-call ContextMeasurement/model_window_
    # utilization are how their actual usefulness gets measured, not
    # assumed. 256k matches qwen3-coder-next's reported native context
    # length exactly; going further would exceed the installed model.
    "128k": {
        "context_window_tokens": 131_072,
        "max_files": 32,
        "max_excerpt_lines": 320,
        "max_total_chars": 360_000,
    },
    "256k": {
        "context_window_tokens": 262_144,
        "max_files": 40,
        "max_excerpt_lines": 400,
        "max_total_chars": 600_000,
    },
}


def _apply_context_profile(config: ApoapsisConfig, profile_name: str) -> ApoapsisConfig:
    """Apply a deterministic coding-context profile without mutating config files."""

    coding = config.models.local_coder or config.models.frontier
    if coding.provider != "ollama":
        raise TaskStoreError(
            "context profiles require the native Ollama local coding provider"
        )
    try:
        profile = _CONTEXT_PROFILES[profile_name]
    except KeyError as exc:
        raise TaskStoreError(f"unsupported context profile: {profile_name}") from exc
    model_updates = {}
    if config.models.frontier.provider == "ollama":
        model_updates["frontier"] = config.models.frontier.model_copy(
            update={"context_window_tokens": profile["context_window_tokens"]}
        )
    if config.models.local_coder is not None:
        model_updates["local_coder"] = config.models.local_coder.model_copy(
            update={"context_window_tokens": profile["context_window_tokens"]}
        )
    models = config.models.model_copy(update=model_updates)
    context = config.context.model_copy(
        update={
            "max_files": profile["max_files"],
            "max_excerpt_lines": profile["max_excerpt_lines"],
            "max_total_chars": profile["max_total_chars"],
        }
    )
    return config.model_copy(update={"models": models, "context": context})


def _build_frontier_adapter(config: FrontierProviderConfig) -> ModelProvider:
    if config.provider == "ollama":
        return OllamaProvider(config)
    if config.provider == "openai_compatible":
        return OpenAICompatibleFrontierProvider(config)
    raise TaskStoreError(f"unsupported frontier provider: {config.provider}")


def _build_agent_providers(
    config: ApoapsisConfig,
) -> tuple[
    InstrumentedModelProvider, InstrumentedModelProvider, InstrumentedModelProvider | None
]:
    provider = InstrumentedModelProvider(
        _build_frontier_adapter(config.models.frontier), config.models.frontier.pricing
    )
    local_coder_provider = provider
    if config.models.local_coder is not None:
        local_coder_provider = InstrumentedModelProvider(
            _build_frontier_adapter(config.models.local_coder),
            config.models.local_coder.pricing,
        )
    frontier_coder_provider = None
    if config.models.frontier_coder is not None:
        frontier_coder_provider = InstrumentedModelProvider(
            _build_frontier_adapter(config.models.frontier_coder),
            config.models.frontier_coder.pricing,
        )
    return provider, local_coder_provider, frontier_coder_provider


_DOWNLOAD_SERVICE_TASK = (
    "Add resumable downloads.\n"
    "Preserve the current public API.\n"
    "Do not add runtime dependencies.\n"
    "Existing clients must continue working."
)
_DOWNLOAD_SERVICE_HOLDOUT = "tests/test_resumable_acceptance.py"


def _lane_evidence_kind(
    config: ApoapsisConfig, lane: EvalLane
) -> EvalEvidenceKind:
    if requires_frontier_coder(lane):
        assert config.models.frontier_coder is not None
        if config.models.frontier_coder.provider == "openai_compatible":
            return EvalEvidenceKind.LIVE_HOSTED
    coding = config.models.local_coder or config.models.frontier
    if coding.provider == "openai_compatible":
        return EvalEvidenceKind.LIVE_HOSTED
    return EvalEvidenceKind.LIVE_LOCAL


def _eval_download_service(
    root: Path,
    requested_lanes: list[str] | None,
    context_profile: str | None,
    output_dir: Path | None,
    *,
    max_hosted_spend_usd: float | None = None,
) -> dict[str, object]:
    fixture_source = root / "examples" / "download-service"
    if not fixture_source.is_dir():
        raise TaskStoreError(
            f"fixture not found: {fixture_source}; run this command from the "
            "apoapsis-harness checkout"
        )
    config_path = root / ".apoapsis" / "config.toml"
    if not config_path.is_file():
        raise TaskStoreError(
            "Apoapsis is not initialized; run 'apoapsis init' first"
        )
    config = ApoapsisConfig.from_toml(config_path)
    if context_profile is not None:
        config = _apply_context_profile(config, context_profile)
    provider, local_coder_provider, frontier_coder_provider = _build_agent_providers(
        config
    )

    lanes = (
        [EvalLane(item) for item in requested_lanes]
        if requested_lanes
        else list(DEFAULT_LANE_ORDER)
    )
    run_id = f"EVAL-{uuid.uuid4().hex[:12].upper()}"
    resolved_output_dir = (
        output_dir if output_dir is not None else root / ".apoapsis-eval" / run_id
    )

    hosted_lane_count = sum(1 for lane in lanes if requires_frontier_coder(lane))
    spend_ledger: SpendLedger | None = None
    if hosted_lane_count > 0 and frontier_coder_provider is not None:
        if max_hosted_spend_usd is None:
            raise TaskStoreError(
                "--max-hosted-spend-usd is required: this run requests "
                f"{hosted_lane_count} lane(s) that call the configured "
                "models.frontier_coder (D5b, ADR 0030) -- refusing to make any "
                "hosted call without an explicit aggregate spend ceiling"
            )
        if max_hosted_spend_usd < 0:
            raise TaskStoreError("--max-hosted-spend-usd must not be negative")
        worst_case_usd = estimate_worst_case_run_cost_usd(config, lanes)
        if worst_case_usd > max_hosted_spend_usd:
            raise TaskStoreError(
                f"refusing to start: this run's configured worst-case hosted "
                f"spend allowance is ${worst_case_usd:.4f} "
                f"({hosted_lane_count} lane(s) x up to "
                f"{config.execution.frontier_agent.max_turns} call(s) each at "
                f"up to {config.models.frontier_coder.max_output_tokens} output "
                f"tokens), which exceeds the configured "
                f"${max_hosted_spend_usd:.4f} ceiling -- raise "
                "--max-hosted-spend-usd, lower frontier_agent.max_turns/"
                "max_output_tokens, or request fewer hosted lanes"
            )
        spend_ledger = SpendLedger(ceiling_usd=max_hosted_spend_usd)
        frontier_coder_provider = SpendCeilingModelProvider(
            frontier_coder_provider,
            spend_ledger,
            default_max_output_tokens=config.models.frontier_coder.max_output_tokens,
        )
        hosted_spend_plan = {
            "hosted_lanes": [
                lane.value for lane in lanes if requires_frontier_coder(lane)
            ],
            "model": config.models.frontier_coder.model,
            "max_calls_per_lane": config.execution.frontier_agent.max_turns,
            "max_output_tokens_per_call": (
                config.models.frontier_coder.max_output_tokens
            ),
            "worst_case_total_usd": round(worst_case_usd, 4),
            "ceiling_usd": max_hosted_spend_usd,
        }
        resolved_output_dir.mkdir(parents=True, exist_ok=True)
        (resolved_output_dir / "hosted-spend-plan.json").write_text(
            json.dumps(hosted_spend_plan, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # stderr, never stdout: `apoapsis eval`'s stdout is exactly one JSON
        # object (the final comparison report) by convention -- printing a
        # second one here would break every caller that parses it that way.
        print(
            "hosted spend plan (before any call): "
            f"{json.dumps(hosted_spend_plan, sort_keys=True)}",
            file=sys.stderr,
        )

    results: list[EvalLaneResult] = []
    oracle = HeldOutOracleDefinition(
        oracle_id="download-service-resumable-v1",
        version="1.0",
        source_path=fixture_source / _DOWNLOAD_SERVICE_HOLDOUT,
        withheld_relative_path=_DOWNLOAD_SERVICE_HOLDOUT,
    )
    for lane in lanes:
        if requires_frontier_coder(lane) and frontier_coder_provider is None:
            results.append(
                EvalLaneResult(
                    lane=lane,
                    skipped=True,
                    skip_reason=(
                        "lane requires [models.frontier_coder], which is not "
                        "configured in this project"
                    ),
                )
            )
            continue
        fixture_root = resolved_output_dir / lane.value / "download-service"
        prepare_fixture_repository(
            fixture_source,
            fixture_root,
            excluded_relative_files=[_DOWNLOAD_SERVICE_HOLDOUT],
        )
        results.append(
            run_eval_lane(
                fixture_root,
                lane,
                config,
                provider,
                local_coder_provider=local_coder_provider,
                frontier_coder_provider=frontier_coder_provider,
                task_text=_DOWNLOAD_SERVICE_TASK,
                evidence_kind=_lane_evidence_kind(config, lane),
                held_out_oracle=oracle,
            )
        )
        if spend_ledger is not None and spend_ledger.exceeded:
            # A ceiling breach inside VerticalSliceRunner is caught and
            # reported as an ordinary per-task failure, not re-raised --
            # correct for one lane's report, wrong for the whole invocation.
            # Stop starting further lanes immediately rather than trusting
            # that every remaining lane would also happen to fail cheaply.
            raise HostedSpendCeilingExceededError(
                f"stopping this run: the hosted spend ceiling was exceeded "
                f"during lane {lane.value!r} (${spend_ledger.spent_usd:.4f} "
                f"spent of ${spend_ledger.ceiling_usd:.4f}); no further lanes "
                "were started"
            )

    comparison = EvalComparisonReport(
        run_id=run_id,
        fixture_source=str(fixture_source),
        task_text=_DOWNLOAD_SERVICE_TASK,
        context_profile=context_profile,
        lanes=results,
    )
    write_comparison(resolved_output_dir, comparison)
    if spend_ledger is not None:
        resolved_output_dir.mkdir(parents=True, exist_ok=True)
        (resolved_output_dir / "hosted-spend.json").write_text(
            json.dumps(
                {
                    "ceiling_usd": spend_ledger.ceiling_usd,
                    "spent_usd": round(spend_ledger.spent_usd, 6),
                    "calls_recorded": spend_ledger.calls_recorded,
                    "calls_refused": spend_ledger.calls_refused,
                    "exceeded": spend_ledger.exceeded,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return comparison.model_dump(mode="json")


_DOWNLOAD_SERVICE_V2_HOLDOUT = "tests/test_v2_holdout_acceptance.py"
_DOWNLOAD_SERVICE_V2_SCENARIO_VERSION = "1.0"
_DOWNLOAD_SERVICE_V2_TASK = (
    "Add durable job-record bookkeeping (attempt count, transferred bytes, "
    "an expected checksum, a lifecycle state, and failure information), a "
    "resilient resumable downloader (Range-based resume, deterministic "
    "retry with backoff on transient transport failures, and structured "
    "progress reporting), and an integrated download service that persists "
    "progress and attempt state and verifies a SHA-256 checksum before "
    "reporting completion.\n"
    "Preserve the current public API.\n"
    "Do not add runtime dependencies.\n"
    "Never sleep for real or touch the network; use an injectable clock "
    "and a fake transport, exactly like the existing tests already do.\n"
)


def _download_service_v2_config(base: ApoapsisConfig) -> ApoapsisConfig:
    """Three acceptance-designated, non-required commands -- one per
    slice -- under `STRICT` completion policy: each slice's derived task's
    own inherited acceptance criterion gates it on exactly its own command,
    never on an unrelated slice's not-yet-implemented one. See ADR 0028."""

    commands = [
        VerificationCommand(
            name="v2-jobs-tests",
            category="tests",
            description="Durable job-record bookkeeping (Slice A).",
            argv=[sys.executable, "-m", "unittest", "tests.test_jobs_contract", "-v"],
            timeout_seconds=30,
            # Slice A has no dependencies and always runs first in this
            # fixed DAG, so this is the one command `VerticalSliceRunner`'s
            # "at least one required command" floor can safely require --
            # by the time any other slice's isolated worktree is created,
            # Slice A's fix is already merged into its base, so this
            # command has already passed there too. See ADR 0028.
            required=True,
            acceptance=True,
        ),
        VerificationCommand(
            name="v2-downloader-tests",
            category="tests",
            description="Resilient resumable downloading (Slice B).",
            argv=[
                sys.executable,
                "-m",
                "unittest",
                "tests.test_resilient_downloader",
                "-v",
            ],
            timeout_seconds=30,
            required=False,
            acceptance=True,
        ),
        VerificationCommand(
            name="v2-service-tests",
            category="acceptance",
            description="Integrated download service (Slice C).",
            argv=[
                sys.executable,
                "-m",
                "unittest",
                "tests.test_service_integration_visible",
                "-v",
            ],
            timeout_seconds=30,
            required=False,
            acceptance=True,
        ),
    ]
    verification = base.verification.model_copy(update={"commands": commands})
    execution = base.execution.model_copy(
        update={
            "mode": ExecutionMode.AGENT,
            "route": AgentRoute.LOCAL_ONLY,
            "completion_policy": CompletionPolicy.STRICT,
        }
    )
    return base.model_copy(update={"verification": verification, "execution": execution})


def _planner_provenance(
    root: Path, plan_id: str, planner_model: str
) -> PlannerProvenance:
    from apoapsis.architect.schema import PlannerRequestPackage
    from apoapsis.architect.store import SQLitePlanStore as _PlanStore

    plan_store = _PlanStore(root / ".apoapsis" / "architect-plans.db")
    record = plan_store.get_plan(plan_id)
    package_path = (
        root
        / ".apoapsis"
        / "plan-packages"
        / record.package_id
        / "request-package.json"
    )
    package = PlannerRequestPackage.model_validate_json(
        package_path.read_text(encoding="utf-8")
    )
    return PlannerProvenance(
        package_id=record.package_id,
        plan_id=plan_id,
        plan_version=record.version,
        request_package_sha256=package.package_sha256,
        planner_model=planner_model,
        planner_method=PlannerMethod.MANUAL_SUBSCRIPTION_PASTE,
        planner_tokens_status=MetricStatus.UNMEASURED,
        reason="manually-pasted subscription session; no API telemetry exists",
    )


def _eval_planning(
    root: Path,
    plan_id: str,
    expected_plan_version: int,
    planned_project_root: Path,
    planner_model: str,
    context_profile: str | None,
    output_dir: Path | None,
) -> dict[str, object]:
    fixture_source = root / "examples" / "download-service-v2"
    if not fixture_source.is_dir():
        raise TaskStoreError(
            f"fixture not found: {fixture_source}; run this command from the "
            "apoapsis-harness checkout"
        )
    if not planned_project_root.is_dir():
        raise TaskStoreError(
            f"--planned-project-root not found: {planned_project_root}"
        )
    config_path = root / ".apoapsis" / "config.toml"
    if not config_path.is_file():
        raise TaskStoreError("Apoapsis is not initialized; run 'apoapsis init' first")
    base_config = ApoapsisConfig.from_toml(config_path)
    if context_profile is not None:
        base_config = _apply_context_profile(base_config, context_profile)
    config = _download_service_v2_config(base_config)
    coding_model = (config.models.local_coder or config.models.frontier).model

    planner = _planner_provenance(planned_project_root, plan_id, planner_model)

    run_id = f"EVALPLAN-{uuid.uuid4().hex[:12].upper()}"
    resolved_output_dir = (
        output_dir if output_dir is not None else root / ".apoapsis-eval" / run_id
    )
    oracle = HeldOutOracleDefinition(
        oracle_id="download-service-v2-holdout-v1",
        version="1.0",
        source_path=fixture_source / _DOWNLOAD_SERVICE_V2_HOLDOUT,
        withheld_relative_path=_DOWNLOAD_SERVICE_V2_HOLDOUT,
    )

    monolithic_root = resolved_output_dir / "monolithic" / "download-service-v2"
    prepare_fixture_repository(
        fixture_source,
        monolithic_root,
        excluded_relative_files=[_DOWNLOAD_SERVICE_V2_HOLDOUT],
    )
    provider, local_coder_provider, frontier_coder_provider = _build_agent_providers(
        config
    )
    monolithic = run_monolithic_condition(
        monolithic_root,
        config,
        provider,
        local_coder_provider=local_coder_provider,
        frontier_coder_provider=frontier_coder_provider,
        task_text=_DOWNLOAD_SERVICE_V2_TASK,
        scenario_id="download-service-v2",
        scenario_version=_DOWNLOAD_SERVICE_V2_SCENARIO_VERSION,
        evidence_kind=_lane_evidence_kind(config, EvalLane.LOCAL),
        held_out_oracle=oracle,
    )

    plan_store = SQLitePlanStore(planned_project_root / ".apoapsis" / "architect-plans.db")
    slice_store = PlanSliceExecutionStore(
        planned_project_root / ".apoapsis" / "plan-slice-executions.db"
    )
    planned_task_store = SQLiteTaskStore(planned_project_root / ".apoapsis" / "apoapsis.db")
    planned_operation_store = ExecutionOperationStore(
        planned_project_root / ".apoapsis" / "execution-operations.db"
    )
    planned = run_planned_condition(
        planned_project_root,
        plan_store,
        slice_store,
        planned_task_store,
        planned_operation_store,
        plan_id,
        expected_plan_version=expected_plan_version,
        config=config,
        planner=planner,
        scenario_id="download-service-v2",
        scenario_version=_DOWNLOAD_SERVICE_V2_SCENARIO_VERSION,
        evidence_kind=_lane_evidence_kind(config, EvalLane.LOCAL),
        held_out_oracle=oracle,
    )

    comparison = PlanningComparisonReport(
        run_id=run_id,
        scenario_id="download-service-v2",
        scenario_version=_DOWNLOAD_SERVICE_V2_SCENARIO_VERSION,
        task_text=_DOWNLOAD_SERVICE_V2_TASK,
        coding_model=coding_model,
        context_profile=context_profile,
        monolithic=monolithic,
        planned=planned,
    )
    write_planning_comparison(resolved_output_dir, comparison)
    return comparison.model_dump(mode="json")


def _eval_planning_probe(
    root: Path,
    plan_id: str,
    expected_plan_version: int,
    planned_project_root: Path,
    slice_id: str,
    prompt_condition: str,
    alternate_model: str | None,
    authorize_alternate_model: str | None,
    output_dir: Path | None,
) -> dict[str, object]:
    """D4c (ADR 0029): run exactly one already-approved plan slice once,
    varying only `--prompt-condition` or `--alternate-model` -- never
    both, and never the full monolithic-vs-planned comparison. See
    `apoapsis.evaluation.diagnostic_probe` for the full design rationale.

    Always inherits the harness checkout's baseline `.apoapsis/config.toml`
    unchanged (no context-profile override here): this narrowly scoped
    command must never introduce a second, unrecorded independent
    variable beyond the one `--prompt-condition`/`--alternate-model`
    already isolates.
    """

    # Pure argument-shape checks first, before any filesystem access,
    # installed-model lookup, or provider construction: an explicit
    # alternate model requires the unmodified production prompt (a probe
    # varies exactly one independent variable), and its authorization
    # flag must exactly match.
    if alternate_model is not None and prompt_condition != "production":
        raise DiagnosticProbeError(
            "--alternate-model requires --prompt-condition production -- "
            "a probe varies exactly one independent variable; got "
            f"--prompt-condition={prompt_condition!r} with "
            f"--alternate-model={alternate_model!r}"
        )
    if alternate_model is not None and authorize_alternate_model != alternate_model:
        raise DiagnosticProbeError(
            "--alternate-model requires --authorize-alternate-model to "
            "exactly match it; refusing to run an unauthorized model "
            f"(--alternate-model={alternate_model!r}, "
            f"--authorize-alternate-model={authorize_alternate_model!r})"
        )
    if not planned_project_root.is_dir():
        raise TaskStoreError(
            f"--planned-project-root not found: {planned_project_root}"
        )
    config_path = root / ".apoapsis" / "config.toml"
    if not config_path.is_file():
        raise TaskStoreError("Apoapsis is not initialized; run 'apoapsis init' first")
    base_config = ApoapsisConfig.from_toml(config_path)
    config = _download_service_v2_config(base_config)

    coding_config = config.models.local_coder or config.models.frontier
    if alternate_model is None:
        model_selection = ModelSelection(
            model=coding_config.model, source="project_local_coder"
        )
    else:
        if alternate_model == coding_config.model:
            raise DiagnosticProbeError(
                f"--alternate-model {alternate_model!r} is identical to "
                "the project's already-configured coding model "
                f"({coding_config.model!r}); this would not vary any "
                "independent variable. Use --prompt-condition production "
                "without --alternate-model instead, or choose a "
                "genuinely different model."
            )
        alternate = AlternateModelSpec(model=alternate_model)
        verify_alternate_model_authorized(
            alternate,
            base_url=coding_config.base_url,
            authorized_model_names=frozenset({authorize_alternate_model}),
        )
        local_coder_config = alternate_model_provider_config(coding_config, alternate)
        config = config.model_copy(
            update={
                "models": config.models.model_copy(
                    update={"local_coder": local_coder_config}
                )
            }
        )
        model_selection = ModelSelection(
            model=alternate_model, source="explicit_alternate"
        )
    # Defense in depth: the same invariant `run_single_slice_diagnostic_
    # probe` itself enforces first, called here too so a CLI-level
    # rejection never depends on the orchestration function alone.
    validate_single_independent_variable(PromptCondition(prompt_condition), model_selection)

    provider, local_coder_provider, _frontier_coder_provider = _build_agent_providers(
        config
    )

    plan_store = SQLitePlanStore(planned_project_root / ".apoapsis" / "architect-plans.db")
    slice_store = PlanSliceExecutionStore(
        planned_project_root / ".apoapsis" / "plan-slice-executions.db"
    )
    task_store = SQLiteTaskStore(planned_project_root / ".apoapsis" / "apoapsis.db")
    operation_store = ExecutionOperationStore(
        planned_project_root / ".apoapsis" / "execution-operations.db"
    )

    run_id = f"PROBE-{uuid.uuid4().hex[:12].upper()}"
    resolved_output_dir = (
        output_dir if output_dir is not None else root / ".apoapsis-eval" / run_id
    )

    result = run_single_slice_diagnostic_probe(
        planned_project_root,
        plan_store,
        slice_store,
        task_store,
        operation_store,
        plan_id,
        slice_id,
        expected_plan_version=expected_plan_version,
        config=config,
        provider=provider,
        local_coder_provider=local_coder_provider,
        prompt_condition=PromptCondition(prompt_condition),
        model_selection=model_selection,
        scenario_id="download-service-v2",
        scenario_version=_DOWNLOAD_SERVICE_V2_SCENARIO_VERSION,
        evidence_kind=_lane_evidence_kind(config, EvalLane.LOCAL),
    )
    write_diagnostic_probe_report(resolved_output_dir, result)
    return result.model_dump(mode="json")


def _aggregate_eval_reports(
    root: Path,
    comparison_paths: list[Path],
    output_dir: Path | None,
) -> dict[str, object]:
    comparisons: list[EvalComparisonReport] = []
    for path in comparison_paths:
        resolved = path.resolve()
        if not resolved.is_file():
            raise TaskStoreError(f"evaluation comparison not found: {resolved}")
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskStoreError(
                f"failed to read evaluation comparison {resolved}: {exc}"
            ) from exc
        comparisons.append(EvalComparisonReport.model_validate(payload))
    run_ids = [item.run_id for item in comparisons]
    if len(run_ids) != len(set(run_ids)):
        raise TaskStoreError("duplicate evaluation run_id would double-count results")
    aggregate_id = f"EVAL-AGG-{uuid.uuid4().hex[:12].upper()}"
    report = aggregate_evaluations(comparisons, aggregate_id=aggregate_id)
    resolved_output = (
        output_dir
        if output_dir is not None
        else root / ".apoapsis-eval" / aggregate_id
    )
    write_aggregate(resolved_output, report)
    return report.model_dump(mode="json")


def _build_research_engine(
    root: Path, config: ApoapsisConfig
) -> tuple[ResearchEngine, ResearchFetchProcess]:
    local_config = config.models.local_research
    if local_config is None:
        raise TaskStoreError(
            "Research Mode requires [models.local_research] configuration"
    )
    if local_config.provider == "ollama":
        local_adapter = OllamaProvider(local_config)
    else:
        local_adapter = OpenAICompatibleFrontierProvider(
            FrontierProviderConfig(
                provider="openai_compatible",
                base_url=local_config.base_url,
                model=local_config.model,
                api_key_env=local_config.api_key_env,
                timeout_seconds=min(local_config.timeout_seconds, 600),
            )
        )
    local_model = LocalResearchModelClient(
        InstrumentedModelProvider(local_adapter), local_config
    )
    fetch_process = ResearchFetchProcess(config.research.security)
    sources = {}
    if config.research.sources.official_docs.enabled:
        sources[ResearchSourceName.OFFICIAL_DOCS] = OfficialDocumentationSource(
            fetch_process,
            config.research.sources.official_docs.allowed_domains,
        )
    if config.research.sources.github.enabled:
        sources[ResearchSourceName.GITHUB] = GitHubSource(
            fetch_process, config.research.sources.github
        )
    if config.research.sources.reddit.enabled:
        sources[ResearchSourceName.REDDIT] = RedditSource(
            fetch_process, config.research.sources.reddit
        )
    return (
        ResearchEngine(root, config.research, local_model, sources),
        fetch_process,
    )


def _research_command(
    root: Path,
    store: SQLiteTaskStore,
    arguments: list[str],
    *,
    requested_mode: str | None,
) -> dict[str, object]:
    if not arguments:
        raise TaskStoreError("research command requires a task or cache action")
    config = ApoapsisConfig.from_toml(root / ".apoapsis" / "config.toml")
    cache = ResearchCache(root / ".apoapsis" / "research-cache.db")
    if arguments[0] == "cache":
        if len(arguments) != 2 or arguments[1] not in {"inspect", "clear"}:
            raise TaskStoreError("use 'apoapsis research cache inspect' or 'clear'")
        if arguments[1] == "inspect":
            return {
                "entries": [
                    item.model_dump(mode="json") for item in cache.inspect()
                ]
            }
        return {"cleared_entries": cache.clear()}
    if arguments[0] == "inspect":
        if len(arguments) != 2:
            raise TaskStoreError("use 'apoapsis research inspect <task-id>'")
        task_id = arguments[1]
        store.get_task(task_id)
        research_root = root / ".apoapsis" / "tasks" / task_id / "research"
        if not research_root.is_dir():
            raise TaskStoreError(f"no research audit exists for {task_id}")
        result: dict[str, object] = {
            "task_id": task_id,
            "audit_directory": research_root.relative_to(root).as_posix(),
            "artifacts": sorted(path.name for path in research_root.iterdir()),
        }
        for filename, key in [
            ("research-spec.json", "specification"),
            ("synthesis.json", "synthesis"),
            ("telemetry.json", "telemetry"),
        ]:
            path = research_root / filename
            if path.is_file():
                result[key] = json.loads(path.read_text(encoding="utf-8"))
        brief = research_root / "research-brief.md"
        if brief.is_file():
            result["brief"] = brief.read_text(encoding="utf-8")
        return result
    refresh = arguments[0] == "refresh"
    if refresh:
        if len(arguments) != 2:
            raise TaskStoreError("use 'apoapsis research refresh <task-id>'")
        task_id = arguments[1]
    else:
        if len(arguments) != 1:
            raise TaskStoreError("use 'apoapsis research <task-id>'")
        task_id = arguments[0]
    record = store.get_task(task_id)
    if record.state in {
        WorkflowState.INTAKE,
        WorkflowState.SPEC_DRAFTED,
        WorkflowState.HUMAN_REVIEW_REQUIRED,
    }:
        raise TaskStoreError("research requires an approved task specification")
    mode = (
        ResearchMode.from_cli(requested_mode)
        if requested_mode
        else config.research.default_mode
    )
    engine, fetch_process = _build_research_engine(root, config)
    try:
        execution = asyncio.run(
            engine.execute(record.specification, mode, refresh=refresh)
        )
    finally:
        fetch_process.close()
    return execution.model_dump(mode="json")


def _verify(
    root: Path,
    store: SQLiteTaskStore,
    task_id: str,
    requested_path: Path | None,
) -> dict[str, object]:
    record = store.get_task(task_id)
    if record.state != WorkflowState.PATCH_READY:
        raise TaskStoreError(
            f"verification requires PATCH_READY, found {record.state.value}"
        )
    project_path = requested_path
    if project_path is None:
        manager = WorktreeManager(root)
        project_path = Path(manager.describe(_task_slug(task_id)).path)
    config = VerificationConfig.from_toml(root / ".apoapsis" / "config.toml")
    store.transition(
        task_id,
        WorkflowState.VERIFYING,
        actor=WorkflowActor.VERIFICATION_ENGINE,
        event_type="verification_started",
        expected_version=record.version,
    )
    result = VerificationRunner(config).run(task_id, project_path)
    target = (
        WorkflowState.COMPLETE
        if result.status.value == "passed"
        else WorkflowState.LOCAL_REPAIR
    )
    store.transition(
        task_id,
        target,
        actor=WorkflowActor.VERIFICATION_ENGINE,
        event_type="verification_finished",
        payload=result.model_dump(mode="json"),
    )
    return result.model_dump(mode="json")


def _rollback(
    root: Path,
    store: SQLiteTaskStore,
    task_id: str,
    delete_branch: bool,
) -> dict[str, object]:
    record = store.get_task(task_id)
    manager = WorktreeManager(root)
    manager.cleanup(
        _task_slug(task_id), force=True, delete_branch=delete_branch
    )
    rolled_back = store.transition(
        task_id,
        WorkflowState.ROLLED_BACK,
        actor=WorkflowActor.USER,
        event_type="explicit_rollback",
        payload={"branch_deleted": delete_branch},
        expected_version=record.version,
    )
    return rolled_back.model_dump(mode="json")


def _task_slug(task_id: str) -> str:
    return task_id.removeprefix("TASK-").lower()


if __name__ == "__main__":
    main(sys.argv[1:])
