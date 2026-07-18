from __future__ import annotations

import ipaddress
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator

from apoapsis.research.schemas import ResearchBudget, ResearchMode
from apoapsis.specification.schema import StrictModel
from apoapsis.verification.runner import VerificationConfig


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


class AgentLoopConfig(StrictModel):
    max_turns: int = Field(default=12, ge=1, le=50)
    max_patch_attempts: int = Field(default=4, ge=1, le=20)
    max_verification_runs: int = Field(default=4, ge=1, le=20)
    max_search_results: int = Field(default=20, ge=1, le=100)
    max_read_lines: int = Field(default=240, ge=1, le=2_000)
    max_observation_chars: int = Field(
        default=48_000, ge=1_000, le=1_000_000
    )
    max_transmitted_observation_chars: int = Field(
        default=24_000, ge=1_000, le=1_000_000
    )


class ExecutionConfig(StrictModel):
    mode: ExecutionMode = ExecutionMode.ONE_SHOT
    route: AgentRoute = AgentRoute.AUTO
    agent: AgentLoopConfig = Field(default_factory=AgentLoopConfig)
    frontier_agent: AgentLoopConfig = Field(
        default_factory=lambda: AgentLoopConfig(
            max_turns=8,
            max_patch_attempts=3,
            max_verification_runs=3,
            max_search_results=20,
            max_read_lines=240,
            max_observation_chars=48_000,
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
    max_changed_lines: int = Field(default=500, ge=1, le=100_000)
    max_files: int = Field(default=20, ge=1, le=1000)
    allow_dependency_changes: bool = False
    allow_test_changes: bool = False
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


class GitHubResearchSourceConfig(ResearchSourceConfig):
    authentication: Literal["auto", "github_cli", "token", "anonymous"] = "auto"
    require_license_for_code_reuse: bool = True


class RedditResearchSourceConfig(ResearchSourceConfig):
    client_id_env: str = "REDDIT_CLIENT_ID"
    client_secret_env: str = "REDDIT_CLIENT_SECRET"
    user_agent: str = "apoapsis-harness-research/0.7"
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
    minimum_distinct_sources: int = Field(default=3, ge=1, le=20)
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


class ApoapsisConfig(StrictModel):
    models: ModelsConfig
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    context: ContextCompilerConfig = Field(default_factory=ContextCompilerConfig)
    patch: PatchPolicyConfig = Field(default_factory=PatchPolicyConfig)
    verification: VerificationConfig
    research: ResearchConfig = Field(default_factory=ResearchConfig)

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
            )
            if key in raw
        }
        return cls.model_validate(selected)
