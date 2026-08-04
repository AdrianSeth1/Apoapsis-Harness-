from __future__ import annotations

import ipaddress
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator

from apoapsis.research.schemas import ResearchBudget, ResearchMode
from apoapsis.specification.schema import RiskLevel, StrictModel, TaskSpecification
from apoapsis.verification.runner import VerificationConfig
# `workcell/__init__.py` imports nothing, and `workcell.parity` imports only the
# specification schema, so naming the policy where it is implemented costs no
# import cycle and keeps one definition of the modes.
from apoapsis.workcell.parity import ParityMode


def _require_loopback_http_url(value: str, label: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} base_url must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{label} base_url must not contain credentials")
    hostname = parsed.hostname.lower()
    loopback = hostname == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise ValueError(f"{label} base_url must use a loopback host")


class ProviderPricing(StrictModel):
    input_per_million_usd: float = Field(default=0.0, ge=0)
    output_per_million_usd: float = Field(default=0.0, ge=0)
    cached_input_per_million_usd: float = Field(default=0.0, ge=0)


class FrontierProviderConfig(StrictModel):
    provider: Literal["openai_compatible", "ollama"] = "openai_compatible"
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key_env: str = Field(default="OPENAI_API_KEY", min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    max_output_tokens: int = Field(default=8192, ge=256, le=131_072)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    context_window_tokens: int | None = Field(
        default=None, ge=2048, le=1_048_576
    )
    think: bool | None = None
    specification_think: bool | None = None
    pricing: ProviderPricing = Field(default_factory=ProviderPricing)

    @model_validator(mode="after")
    def restrict_native_ollama_to_loopback(self) -> FrontierProviderConfig:
        if self.provider == "ollama":
            _require_loopback_http_url(self.base_url, "frontier Ollama")
        return self


class LocalResearchModeConfig(StrictModel):
    think: bool
    require_structured_output: bool = True


class LocalResearchModesConfig(StrictModel):
    extraction: LocalResearchModeConfig = Field(
        default_factory=lambda: LocalResearchModeConfig(think=False)
    )
    synthesis: LocalResearchModeConfig = Field(
        default_factory=lambda: LocalResearchModeConfig(think=True)
    )


class LocalResearchProviderConfig(StrictModel):
    provider: Literal["ollama", "openai_compatible"] = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = Field(min_length=1)
    api_key_env: str = "APOAPSIS_LOCAL_RESEARCH_API_KEY"
    timeout_seconds: float = Field(default=600.0, gt=0, le=3600)
    max_output_tokens: int = Field(default=8192, ge=256, le=131_072)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    context_window_tokens: int | None = Field(
        default=32_768, ge=2048, le=1_048_576
    )
    max_structured_retries: int = Field(default=1, ge=0, le=3)
    modes: LocalResearchModesConfig = Field(default_factory=LocalResearchModesConfig)

    @model_validator(mode="after")
    def require_loopback_endpoint(self) -> LocalResearchProviderConfig:
        _require_loopback_http_url(self.base_url, "local research")
        return self


class ModelsConfig(StrictModel):
    frontier: FrontierProviderConfig
    local_coder: FrontierProviderConfig | None = None
    frontier_coder: FrontierProviderConfig | None = None
    local_research: LocalResearchProviderConfig | None = None


class ExecutionMode(StrEnum):
    ONE_SHOT = "one_shot"
    AGENT = "agent"


class AgentRoute(StrEnum):
    AUTO = "auto"
    LOCAL_ONLY = "local_only"
    LOCAL_THEN_FRONTIER = "local_then_frontier"
    FRONTIER_ONLY = "frontier_only"
    HUMAN_REVIEW_REQUIRED = "human_review_required"


class CompletionPolicy(StrEnum):
    """Whether COMPLETE requires proven acceptance coverage, or only that
    configured verification passed (today's behavior, kept as the default
    so held-out-oracle false-success measurement stays comparable)."""

    BASELINE = "baseline"
    STRICT = "strict"


class AgentLoopConfig(StrictModel):
    # ADR 0049: coupled to `ArchitectPlanCeilings.max_criteria_per_slice`
    # rising from 12 to 20 (~1.6-1.75x). These are the local-coder
    # defaults; `frontier_agent` below explicitly overrides every field it
    # intentionally keeps at the pre-ADR-0049 value.
    max_turns: int = Field(default=20, ge=1, le=50)
    max_patch_attempts: int = Field(default=14, ge=1, le=20)
    max_verification_runs: int = Field(default=7, ge=1, le=20)
    max_search_results: int = Field(default=24, ge=1, le=100)
    max_read_lines: int = Field(default=360, ge=1, le=2_000)
    max_observation_chars: int = Field(
        default=72_000, ge=1_000, le=1_000_000
    )
    max_transmitted_observation_chars: int = Field(
        default=36_000, ge=1_000, le=1_000_000
    )


DEFAULT_LOCAL_POWER_FORBIDDEN_PATHS = [
    ".apoapsis/**",
    ".sol/**",
    ".git/**",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
    "secrets/**",
]


class LocalPowerWorkspace(StrEnum):
    """Where a Local Power Sandbox session is allowed to run.

    Only one value exists on purpose: the disposable per-task Git worktree
    the harness already creates. There is deliberately no value that means
    "the checked-out project" or "the Apoapsis repository".
    """

    ISOLATED_WORKTREE = "isolated_worktree"


class LocalPowerConfig(StrictModel):
    """ADR 0059: the opt-in, experimental Laguna Power Sandbox execution mode.

    This widens what a *local* coding model may request inside a disposable
    sandbox derived from the approved task base. It grants no new authority
    over Apoapsis itself: workflow state, the audit log, Git metadata,
    credentials, the user's home directory, and completion authority all
    remain harness-owned exactly as under the strict one-action loop. Every
    file and shell request is still mediated, budgeted, and audited; the
    final diff is computed by the harness, and configured verification --
    never the model's own `finish` claim -- decides the outcome.
    """

    #: Legacy. ADR 0109 names the Capability Sandbox the single local execution
    #: path; this is the compatibility mode kept for environments without the
    #: container runtime and for comparison, not a peer. It is reachable only
    #: by explicit opt-in and is never selected for you.
    enabled: bool = False
    workspace: LocalPowerWorkspace = LocalPowerWorkspace.ISOLATED_WORKTREE
    allow_shell: bool = True
    allow_network: bool = False
    max_turns: int = Field(default=8, ge=1, le=40)
    max_seconds: float = Field(default=1800.0, gt=0, le=21_600)
    max_shell_commands: int = Field(default=40, ge=0, le=500)
    max_shell_seconds: float = Field(default=600.0, gt=0, le=3600)
    max_changed_files: int = Field(default=100, ge=1, le=1_000)
    max_changed_lines: int = Field(default=10_000, ge=1, le=200_000)
    max_file_chars: int = Field(default=400_000, ge=1_000, le=4_000_000)
    max_shell_output_chars: int = Field(default=40_000, ge=1_000, le=1_000_000)
    max_observation_chars: int = Field(default=72_000, ge=1_000, le=1_000_000)
    max_read_lines: int = Field(default=800, ge=1, le=5_000)
    max_search_results: int = Field(default=40, ge=1, le=200)
    forbidden_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_LOCAL_POWER_FORBIDDEN_PATHS)
    )
    require_final_diff_review: bool = True
    require_verification: bool = True
    # ADR 0071. Atomic slice proposals: one turn may propose a coherent
    # multi-file increment that applies completely or not at all. On by
    # default *within* an execution mode that is itself off by default, and
    # switchable to false to reproduce the one-action protocol exactly --
    # which is what makes the two a real comparison rather than the same
    # mode with different prompt wording.
    atomic_change_sets: bool = True
    # Files one proposal may touch. The effective ceiling is
    # `min(max_change_set_files, max_changed_files)`, so lowering the session
    # ceiling always lowers the per-proposal one and the two cannot disagree.
    max_change_set_files: int = Field(default=20, ge=1, le=200)
    # Run the required configured commands automatically once a change set
    # applies. The model asked for a coherent increment; making it spend a
    # second turn asking for the verification of that increment is the
    # granularity problem in miniature.
    verify_after_change_set: bool = True

    @model_validator(mode="after")
    def keep_secret_paths_forbidden(self) -> LocalPowerConfig:
        """A local override may widen this list but may never drop the
        boundary entries that keep Apoapsis internals, Git metadata, and
        credential material outside the model's reach."""

        required = {".apoapsis/**", ".git/**", ".env", ".env.*"}
        missing = sorted(required - set(self.forbidden_paths))
        if missing:
            raise ValueError(
                "local power forbidden_paths must retain the non-negotiable "
                f"boundary entries; missing {missing}"
            )
        if self.allow_shell and self.max_shell_commands == 0:
            raise ValueError(
                "local power allow_shell requires a non-zero max_shell_commands"
            )
        return self


class CapabilitySandboxConfig(StrictModel):
    """ADR 0095 / handoff Slice 8 product rollout selection.

    Enabled is the recommended default, but it is still an operator-visible
    selection included in every execution authorization. Disabling it selects
    the older typed Local Power compatibility path; it never weakens the
    controller's verification or completion authority.
    """

    # ADR 0109 made this default true, matching what `apoapsis init` writes and
    # what config loading already migrated a missing table to. It was false
    # here only, so a library caller or a test constructing `ApoapsisConfig()`
    # silently got a *different execution path* from every real project --
    # exactly the class-versus-template drift ADR 0104 fixed for patch
    # ceilings. One default, asserted in one test.
    enabled: bool = True
    runtime_profile: Literal["crisis-atlas-v8-qwen3.6-27b"] = (
        "crisis-atlas-v8-qwen3.6-27b"
    )
    qualified_model_alias: Literal["qwen3.6-27b"] = "qwen3.6-27b"
    #: The pre-ADR-0108 switch. Kept because operators have it set in existing
    #: configurations and it means something definite: run the control arm on
    #: every slice. It is now an input to `parity_mode` rather than a separate
    #: policy -- see the validator below -- so there is still exactly one thing
    #: deciding whether a control arm runs.
    high_assurance_parity_guard: bool = False
    #: How often the matched unrestricted control arm runs (ADR 0108).
    #: `sample` is the default: the paired qualification evidence answered the
    #: standing question, and an answered question needs monitoring rather than
    #: re-answering on every slice at 2x inference.
    parity_mode: ParityMode = ParityMode.SAMPLE
    #: Under `sample`: the first slice of a plan, then every Nth after it.
    parity_sample_every: int = Field(default=4, ge=1, le=50)
    max_native_continuations: int = Field(default=2, ge=0, le=10)
    runtime_root: str = Field(default="/tmp/apoapsis-capability-sandbox", min_length=1)

    @model_validator(mode="after")
    def honour_the_explicit_always_switch(self) -> CapabilitySandboxConfig:
        """An operator who turned the old switch on asked for every slice.

        Silently downgrading that to sampling would spend their evidence for
        them. The migration only runs when `parity_mode` is still the default,
        so a configuration that states both is taken at its word.
        """

        if self.high_assurance_parity_guard and self.parity_mode == ParityMode.SAMPLE:
            object.__setattr__(self, "parity_mode", ParityMode.ALWAYS)
        return self


class ExecutionConfig(StrictModel):
    mode: ExecutionMode = ExecutionMode.ONE_SHOT
    route: AgentRoute = AgentRoute.AUTO
    completion_policy: CompletionPolicy = CompletionPolicy.BASELINE
    capability_sandbox: CapabilitySandboxConfig = Field(
        default_factory=CapabilitySandboxConfig
    )
    local_power: LocalPowerConfig = Field(default_factory=LocalPowerConfig)
    agent: AgentLoopConfig = Field(default_factory=AgentLoopConfig)
    frontier_agent: AgentLoopConfig = Field(
        default_factory=lambda: AgentLoopConfig(
            # ADR 0049: frontier turns/patch-attempts/verification-runs
            # rise coupled to the criteria bump, but the frontier coder's
            # search/read/observation caps are a non-goal for widening and
            # stay at their pre-ADR-0049 values.
            max_turns=14,
            max_patch_attempts=9,
            max_verification_runs=5,
            max_search_results=20,
            max_read_lines=240,
            max_observation_chars=48_000,
            max_transmitted_observation_chars=24_000,
        )
    )


class ContextCompilerConfig(StrictModel):
    max_files: int = Field(default=16, ge=1, le=100)
    max_excerpt_lines: int = Field(default=160, ge=10, le=1000)
    max_total_chars: int = Field(default=72_000, ge=1_000, le=2_000_000)
    match_context_lines: int = Field(default=20, ge=0, le=200)
    max_search_terms: int = Field(default=12, ge=1, le=50)
    max_import_depth: int = Field(default=2, ge=0, le=10)
    cloud_excluded_paths: list[str] = Field(
        default_factory=lambda: [
            ".env",
            ".env.*",
            "*.pem",
            "*.key",
            "secrets/**",
            ".apoapsis/**",
            ".sol/**",
            ".git/**",
        ]
    )


class PatchPolicyConfig(StrictModel):
    #: ADR 0104 raised this from 500. 500 was sized for the bounded protocol's
    #: one-patch-per-turn shape, where a turn is a single edit; the Capability
    #: Sandbox admits a whole slice as one unit, and a slice that adds three
    #: modules with their tests routinely exceeds 500 lines. Refusing it puts
    #: the model in a loop it cannot solve -- the work is correct and the
    #: ceiling is wrong -- so live projects were already carrying 5000 by hand.
    #:
    #: `max_files` stays at 20: file *count* is the ceiling that actually
    #: catches a runaway change, and slices that touch more than twenty files
    #: are usually mis-sliced rather than large.
    max_changed_lines: int = Field(default=5_000, ge=1, le=100_000)
    max_files: int = Field(default=20, ge=1, le=1000)
    allow_dependency_changes: bool = True
    allow_test_changes: bool = True
    dependency_files: list[str] = Field(
        default_factory=lambda: [
            "pyproject.toml",
            "requirements*.txt",
            "poetry.lock",
            "uv.lock",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
        ]
    )
    verification_files: list[str] = Field(
        default_factory=lambda: [
            ".apoapsis/config.toml",
            ".sol/config.toml",
            "pytest.ini",
            "tox.ini",
            "mypy.ini",
            "ruff.toml",
            ".github/workflows/**",
        ]
    )


class ResearchSourceConfig(StrictModel):
    enabled: bool
    priority: int = Field(default=1, ge=1, le=100)


class OfficialDocsResearchSourceConfig(ResearchSourceConfig):
    allowed_domains: list[str] = Field(
        default_factory=lambda: ["docs.python.org"]
    )
    # "none" (default) keeps official_docs a direct-URL-only adapter. ADR
    # 0056 records the owner's explicit authorization of "tavily" (the
    # Tavily search API) as the one concrete provider implemented in this
    # repository (``research/sources/tavily.py``); any other value fails
    # clearly in ``research/factory.py`` rather than guessing at a vendor
    # (ADR 0055).
    search_provider: str = "none"
    # The one dedicated environment-variable name the configured provider
    # reads a credential from. The harness/provider reads this variable,
    # never the model; the variable's value must never enter a prompt, log,
    # cache key, or audit artifact. Defaults to "TAVILY_API_KEY" when
    # search_provider = "tavily" and this is left unset.
    search_credentials_env: str | None = None


class GitHubResearchSourceConfig(ResearchSourceConfig):
    authentication: Literal["auto", "github_cli", "token", "anonymous"] = "auto"
    require_license_for_code_reuse: bool = True


class RedditResearchSourceConfig(ResearchSourceConfig):
    client_id_env: str = "REDDIT_CLIENT_ID"
    client_secret_env: str = "REDDIT_CLIENT_SECRET"
    user_agent: str = "apoapsis-harness-research/1.0"
    purposes: list[str] = Field(
        default_factory=lambda: [
            "user_pain_points",
            "product_expectations",
            "failure_discovery",
        ]
    )


class ResearchSourcesConfig(StrictModel):
    official_docs: OfficialDocsResearchSourceConfig = Field(
        default_factory=lambda: OfficialDocsResearchSourceConfig(
            enabled=True, priority=1
        )
    )
    github: GitHubResearchSourceConfig = Field(
        default_factory=lambda: GitHubResearchSourceConfig(enabled=True, priority=2)
    )
    reddit: RedditResearchSourceConfig = Field(
        default_factory=lambda: RedditResearchSourceConfig(enabled=False, priority=4)
    )


class ResearchSecurityConfig(StrictModel):
    allow_domains: list[str] = Field(
        default_factory=lambda: [
            "docs.python.org",
            "github.com",
            "api.github.com",
            "reddit.com",
            "www.reddit.com",
            "oauth.reddit.com",
        ]
    )
    allowed_content_types: list[str] = Field(
        default_factory=lambda: [
            "application/json",
            "text/plain",
            "text/html",
            "text/markdown",
        ]
    )
    max_response_bytes: int = Field(default=1_000_000, ge=1_000, le=10_000_000)
    max_redirects: int = Field(default=3, ge=0, le=10)
    request_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    execute_downloaded_code: Literal[False] = False
    project_write_access: Literal[False] = False
    expose_project_secrets: Literal[False] = False


class ResearchSynthesisConfig(StrictModel):
    minimum_distinct_sources: int = Field(default=1, ge=1, le=20)
    prefer_comparative_patterns: bool = True
    require_provenance: bool = True


class ResearchCacheConfig(StrictModel):
    default_ttl_hours: int = Field(default=168, ge=1, le=8760)
    reddit_ttl_hours: int = Field(default=24, ge=1, le=168)


class ResearchConfig(StrictModel):
    default_mode: ResearchMode = ResearchMode.AUTO
    budget: ResearchBudget = Field(default_factory=ResearchBudget)
    sources: ResearchSourcesConfig = Field(default_factory=ResearchSourcesConfig)
    security: ResearchSecurityConfig = Field(default_factory=ResearchSecurityConfig)
    synthesis: ResearchSynthesisConfig = Field(default_factory=ResearchSynthesisConfig)
    cache: ResearchCacheConfig = Field(default_factory=ResearchCacheConfig)


class ArchitectPlanCeilings(StrictModel):
    """Configurable ceilings deterministic plan validation enforces (ADR
    0019). Defaults are generous but finite -- every plan, regardless of
    which model proposed it, is bounded."""

    max_slices: int = Field(default=40, ge=1, le=500)
    max_dependency_depth: int = Field(default=15, ge=1, le=100)
    max_suggested_paths_per_slice: int = Field(default=12, ge=1, le=200)
    # ADR 0049: raised from 12/2000 to 20/3500, coupled to the local/
    # frontier coder budget bump in `AgentLoopConfig` above.
    max_criteria_per_slice: int = Field(default=20, ge=1, le=200)
    max_work_brief_chars: int = Field(default=3500, ge=100, le=20_000)


class ArchitectConfig(StrictModel):
    ceilings: ArchitectPlanCeilings = Field(default_factory=ArchitectPlanCeilings)


class ReviewConfig(StrictModel):
    """Ceilings for deterministic human-review continuation (ADR 0020).

    A continuation may only ever add turns/patch-attempts/verification-runs
    on top of a task's already-consumed budget -- it never resets or
    replaces it. ``max_additional_turns_per_continuation`` is the single
    user-authorized number per continuation; the same delta is applied to
    the resumed agent's turn, patch-attempt, and verification-run ceilings
    together, so one continuation cannot expand turns while leaving the
    session unable to ever apply or verify a patch.
    """

    max_continuations_per_task: int = Field(default=5, ge=1, le=100)
    max_additional_turns_per_continuation: int = Field(default=12, ge=1, le=50)


class DiscoveryConfig(StrictModel):
    """Ceilings for local-first Architect Mode discovery followed by an
    optional frontier planning stage (ADR 0032). ``max_clarification_
    questions`` is a harness-enforced cap on the local model's proposed
    question count -- never trusted from the model's own output count.
    ``max_frontier_clarification_rounds`` bounds the frontier stage to a
    small, deterministic number of clarification exchanges before it must
    return a complete plan; this is never a general chat. Response/patch
    size ceilings mirror `ManualFrontierConfig`'s own, applied to the
    frontier planning manual-subscription transport.
    """

    max_clarification_questions: int = Field(default=5, ge=1, le=20)
    max_frontier_clarification_rounds: int = Field(default=10, ge=0, le=10)
    max_response_bytes: int = Field(default=2_000_000, ge=1_000, le=20_000_000)


class ManualFrontierConfig(StrictModel):
    """Ceilings for the manual subscription-based frontier handoff (ADR
    0031). This path never authenticates to a hosted API and never
    automates ChatGPT's or Claude's website -- the operator manually
    uploads an exported package to their own subscription session and
    pastes back one bounded response. ``max_repair_rounds`` bounds how many
    times an applied-but-failing manual patch may be handed off again with
    real failure evidence; it is deliberately small and finite, never an
    unbounded conversation. ``max_response_bytes`` bounds the raw pasted
    response file size before it is even parsed as JSON, so a caller cannot
    exhaust memory with an oversized paste before schema validation runs.
    """

    max_repair_rounds: int = Field(default=2, ge=0, le=10)
    max_response_bytes: int = Field(default=2_000_000, ge=1_000, le=20_000_000)
    max_patch_bytes: int = Field(default=1_000_000, ge=1_000, le=10_000_000)


class ApoapsisConfig(StrictModel):
    models: ModelsConfig
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    context: ContextCompilerConfig = Field(default_factory=ContextCompilerConfig)
    patch: PatchPolicyConfig = Field(default_factory=PatchPolicyConfig)
    verification: VerificationConfig
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    architect: ArchitectConfig = Field(default_factory=ArchitectConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    manual_frontier: ManualFrontierConfig = Field(default_factory=ManualFrontierConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)

    @model_validator(mode="after")
    def validate_provider_separation_and_route(self) -> ApoapsisConfig:
        local = self.models.local_research
        if local is not None and local.provider == "openai_compatible":
            coding_credentials = {
                item.api_key_env
                for item in (
                    self.models.frontier,
                    self.models.local_coder,
                    self.models.frontier_coder,
                )
                if item is not None and item.provider == "openai_compatible"
            }
            if local.api_key_env in coding_credentials:
                raise ValueError(
                    "local research and coding providers must use different "
                    "credential environment variables"
                )
        if (
            self.execution.mode == ExecutionMode.AGENT
            and self.execution.route
            in {AgentRoute.LOCAL_THEN_FRONTIER, AgentRoute.FRONTIER_ONLY}
            and self.models.frontier_coder is None
        ):
            raise ValueError(
                f"execution route {self.execution.route.value} requires "
                "[models.frontier_coder] configuration"
            )
        if self.execution.local_power.enabled:
            # ADR 0059: the sandbox is a local-model experiment layered on the
            # bounded-agent execution spine. It has no one-shot equivalent, and
            # it is never the frontier coder's execution path.
            if self.execution.mode != ExecutionMode.AGENT:
                raise ValueError(
                    "[execution.local_power] requires execution mode 'agent'"
                )
            if self.execution.route == AgentRoute.FRONTIER_ONLY:
                raise ValueError(
                    "[execution.local_power] is a local-model mode and cannot "
                    "be combined with the frontier_only route"
                )
        if self.execution.capability_sandbox.enabled:
            # Deliberately *not* refused with the frontier_only route any more
            # (ADR 0109). When enabling the sandbox was an explicit act, the
            # combination was evidence of operator confusion and refusing it
            # was a kindness. Now that it is the default, the same refusal
            # would fire on a perfectly coherent configuration -- "send
            # everything to the frontier coder" -- where the operator did
            # nothing at all. The setting means "when a local slice runs, run
            # it contained"; frontier_only means no local slice runs, so there
            # is nothing to contradict.
            if self.execution.local_power.enabled:
                raise ValueError(
                    "Capability Sandbox and Local Power compatibility mode are "
                    "mutually exclusive"
                )
        return self

    @classmethod
    def from_toml(cls, path: str | Path) -> ApoapsisConfig:
        with Path(path).open("rb") as handle:
            raw = tomllib.load(handle)
        selected = {
            key: raw[key]
            for key in (
                "models",
                "execution",
                "context",
                "patch",
                "verification",
                "research",
                "architect",
                "review",
            )
            if key in raw
        }
        execution = selected.get("execution")
        if isinstance(execution, dict) and "capability_sandbox" not in execution:
            legacy = execution.get("local_power")
            legacy_selected = isinstance(legacy, dict) and legacy.get("enabled") is True
            execution["capability_sandbox"] = {"enabled": not legacy_selected}
        return cls.model_validate(selected)


def effective_config_for_specification(
    config: ApoapsisConfig, specification: TaskSpecification
) -> ApoapsisConfig:
    """Derive the finite, auditable execution profile for one task.

    High/critical-risk work that a user permits to run locally gets the
    strongest local loop and repository-inspection ceilings supported by the
    harness. Context volume is still tied to the configured model's declared
    window; patch policy, commands, state transitions, and completion authority
    are unchanged.
    """

    if specification.risk_level not in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
        return config
    local_model = config.models.local_coder or config.models.frontier
    context_tokens = local_model.context_window_tokens or 32_768
    context_chars = min(
        2_000_000, max(config.context.max_total_chars, context_tokens * 3)
    )
    agent = AgentLoopConfig(
        max_turns=50,
        max_patch_attempts=20,
        max_verification_runs=20,
        max_search_results=100,
        max_read_lines=2_000,
        max_observation_chars=1_000_000,
        max_transmitted_observation_chars=min(1_000_000, context_tokens * 3),
    )
    execution = config.execution.model_copy(update={"agent": agent})
    context = config.context.model_copy(
        update={
            "max_files": 100,
            "max_excerpt_lines": 1_000,
            "max_total_chars": context_chars,
            "match_context_lines": 200,
            "max_search_terms": 50,
            "max_import_depth": 10,
        }
    )
    return config.model_copy(update={"execution": execution, "context": context})
