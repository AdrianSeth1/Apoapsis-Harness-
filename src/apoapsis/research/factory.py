from __future__ import annotations

from pathlib import Path

from apoapsis.config import ApoapsisConfig, FrontierProviderConfig
from apoapsis.models.frontier import OpenAICompatibleFrontierProvider
from apoapsis.models.local import OllamaProvider
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.research.engine import ResearchEngine
from apoapsis.research.fetcher import ResearchFetchProcess
from apoapsis.research.model import LocalResearchModelClient
from apoapsis.research.schemas import ResearchSourceName
from apoapsis.research.sources.github import GitHubSource
from apoapsis.research.sources.official import OfficialDocumentationSource
from apoapsis.research.sources.reddit import RedditSource


class ResearchConfigurationError(RuntimeError):
    """Research was requested without a usable local-research role."""


def build_research_engine(
    root: str | Path, config: ApoapsisConfig
) -> tuple[ResearchEngine, ResearchFetchProcess]:
    """Build the one quarantined Research Mode engine used by both coding
    execution and discovery/planning.

    Provider construction stays outside HTTP handlers.  The returned fetch
    process is owned by the caller and must be closed after the operation.
    """

    root_path = Path(root).resolve()
    local_config = config.models.local_research
    if local_config is None:
        raise ResearchConfigurationError(
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
        ResearchEngine(root_path, config.research, local_model, sources),
        fetch_process,
    )


__all__ = [
    "ResearchConfigurationError",
    "build_research_engine",
]
