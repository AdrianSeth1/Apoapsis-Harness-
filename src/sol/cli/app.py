from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from pydantic import ValidationError

from sol.config import FrontierProviderConfig, SolConfig
from sol.execution.worktree import WorktreeError, WorktreeManager
from sol.models.frontier import OpenAICompatibleFrontierProvider
from sol.models.local import OllamaProvider
from sol.models.provider import ModelProvider, ProviderError
from sol.models.telemetry import InstrumentedModelProvider, InstrumentedProviderError
from sol.research.cache import ResearchCache
from sol.research.engine import ResearchEngine, ResearchEngineError
from sol.research.fetcher import ResearchFetchProcess
from sol.research.model import LocalResearchModelClient, ResearchModelError
from sol.research.schemas import ResearchMode, ResearchSourceName
from sol.research.sources.github import GitHubSource
from sol.research.sources.official import OfficialDocumentationSource
from sol.research.sources.reddit import RedditSource
from sol.repository.git import GitCommandError, GitRepository
from sol.specification.schema import (
    AcceptanceCriterion,
    HardConstraint,
    SourceKind,
    TaskSpecification,
    TraceableStatement,
)
from sol.verification.runner import VerificationConfig, VerificationRunner
from sol.workflow.engine import SQLiteTaskStore, TaskStoreError
from sol.workflow.events import WorkflowActor
from sol.workflow.states import WorkflowState
from sol.workflow.vertical_slice import VerticalSliceRunner


DEFAULT_CONFIG = """# SOL Harness project configuration
[project]
language = "python"

[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3-coder:30b"
timeout_seconds = 900
max_output_tokens = 8192
context_window_tokens = 32768
think = false
specification_think = false

[models.frontier.pricing]
input_per_million_usd = 0
output_per_million_usd = 0
cached_input_per_million_usd = 0

[models.local_research]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3.6:27b"
api_key_env = "SOL_LOCAL_RESEARCH_API_KEY"
timeout_seconds = 600
max_output_tokens = 8192
context_window_tokens = 32768
max_structured_retries = 1

[models.local_research.modes.extraction]
think = false
require_structured_output = true

[models.local_research.modes.synthesis]
think = true
require_structured_output = true

[context]
max_files = 16
max_excerpt_lines = 160
max_total_chars = 72000
match_context_lines = 20
max_search_terms = 12
cloud_excluded_paths = [
  ".env", ".env.*", "*.pem", "*.key", "secrets/**", ".sol/**", ".git/**"
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
  ".sol/config.toml", "pytest.ini", "tox.ini", "mypy.ini", "ruff.toml",
  ".github/workflows/**"
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
user_agent = "sol-harness-research/0.4"
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
argv = ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
timeout_seconds = 120
required = true
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sol",
        description="Local-first deterministic coding-task harness",
    )
    parser.add_argument(
        "--project-root", type=Path, default=Path.cwd(), help=argparse.SUPPRESS
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="initialize SOL metadata")

    run = subparsers.add_parser(
        "run", help="run the approved frontier-model vertical slice"
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
        "--context-profile",
        choices=["16k", "32k", "64k"],
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
    ) as exc:
        parser.exit(2, f"error: {exc}\n")
    if result is not None:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))


def _dispatch(args: argparse.Namespace) -> dict[str, object] | None:
    root = args.project_root.resolve()
    if args.command == "init":
        return _init(root)
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
        )
    if args.command == "research":
        return _research_command(
            root, store, args.research_args, requested_mode=args.mode
        )
    if args.command == "inspect":
        record = store.get_task(args.task_id)
        result: dict[str, object] = {
            "task": record.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in store.events(args.task_id)],
        }
        report_path = root / ".sol" / "tasks" / args.task_id / "report.json"
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
    metadata = root / ".sol"
    metadata.mkdir(parents=True, exist_ok=True)
    config = metadata / "config.toml"
    created_config = False
    if not config.exists():
        config.write_text(DEFAULT_CONFIG, encoding="utf-8")
        created_config = True
    SQLiteTaskStore(metadata / "sol.db")
    return {
        "initialized": True,
        "metadata_directory": str(metadata),
        "config_created": created_config,
    }


def _store(root: Path) -> SQLiteTaskStore:
    metadata = root / ".sol"
    if not (metadata / "config.toml").is_file():
        raise TaskStoreError("SOL is not initialized; run 'sol init' first")
    return SQLiteTaskStore(metadata / "sol.db")


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
) -> dict[str, object]:
    config = SolConfig.from_toml(root / ".sol" / "config.toml")
    if context_profile is not None:
        config = _apply_context_profile(config, context_profile)
    adapter = _build_frontier_adapter(config.models.frontier)
    provider = InstrumentedModelProvider(
        adapter, config.models.frontier.pricing
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
}


def _apply_context_profile(config: SolConfig, profile_name: str) -> SolConfig:
    """Apply a deterministic coding-context profile without mutating config files."""

    if config.models.frontier.provider != "ollama":
        raise TaskStoreError(
            "context profiles require the native Ollama frontier provider"
        )
    try:
        profile = _CONTEXT_PROFILES[profile_name]
    except KeyError as exc:
        raise TaskStoreError(f"unsupported context profile: {profile_name}") from exc
    frontier = config.models.frontier.model_copy(
        update={"context_window_tokens": profile["context_window_tokens"]}
    )
    models = config.models.model_copy(update={"frontier": frontier})
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


def _build_research_engine(
    root: Path, config: SolConfig
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
    config = SolConfig.from_toml(root / ".sol" / "config.toml")
    cache = ResearchCache(root / ".sol" / "research-cache.db")
    if arguments[0] == "cache":
        if len(arguments) != 2 or arguments[1] not in {"inspect", "clear"}:
            raise TaskStoreError("use 'sol research cache inspect' or 'clear'")
        if arguments[1] == "inspect":
            return {
                "entries": [
                    item.model_dump(mode="json") for item in cache.inspect()
                ]
            }
        return {"cleared_entries": cache.clear()}
    if arguments[0] == "inspect":
        if len(arguments) != 2:
            raise TaskStoreError("use 'sol research inspect <task-id>'")
        task_id = arguments[1]
        store.get_task(task_id)
        research_root = root / ".sol" / "tasks" / task_id / "research"
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
            raise TaskStoreError("use 'sol research refresh <task-id>'")
        task_id = arguments[1]
    else:
        if len(arguments) != 1:
            raise TaskStoreError("use 'sol research <task-id>'")
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
    config = VerificationConfig.from_toml(root / ".sol" / "config.toml")
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
