from __future__ import annotations

import asyncio
import os
import unittest

from sol.config import (
    GitHubResearchSourceConfig,
    RedditResearchSourceConfig,
    ResearchSecurityConfig,
)
from sol.research.fetcher import ResearchFetchProcess
from sol.research.schemas import (
    ResearchQuery,
    ResearchSourceName,
    ResearchSourceType,
    SourceBudget,
)
from sol.research.sources.github import GitHubSource
from sol.research.sources.reddit import RedditSource


class OptionalLiveResearchTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("SOL_RUN_LIVE_GITHUB_TESTS") == "1",
        "set SOL_RUN_LIVE_GITHUB_TESTS=1 to run the bounded GitHub smoke test",
    )
    def test_live_github_search_and_fetch_with_strict_budget(self) -> None:
        security = ResearchSecurityConfig(
            allow_domains=["github.com", "api.github.com"],
            max_response_bytes=250_000,
            request_timeout_seconds=15,
            max_redirects=2,
        )
        process = ResearchFetchProcess(security)
        try:
            source = GitHubSource(
                process,
                GitHubResearchSourceConfig(
                    enabled=True,
                    authentication="anonymous",
                ),
            )
            query = ResearchQuery(
                query_id="QUERY-LIVE-GH",
                research_question_id="RQ-LIVE",
                source=ResearchSourceName.GITHUB,
                query="command line JSON report",
                content_types=[ResearchSourceType.GITHUB_REPOSITORY],
            )
            budget = SourceBudget(
                max_candidates=3,
                max_response_bytes=250_000,
                timeout_seconds=15,
            )
            candidates = asyncio.run(source.search(query, budget))
            self.assertLessEqual(len(candidates), 3)
            if candidates:
                retrieved = asyncio.run(source.fetch(candidates[0]))
                self.assertEqual(retrieved.source, ResearchSourceName.GITHUB)
                self.assertTrue(retrieved.locator.url.startswith("https://"))
        finally:
            process.close()

    @unittest.skipUnless(
        os.environ.get("SOL_RUN_LIVE_REDDIT_TESTS") == "1",
        "set SOL_RUN_LIVE_REDDIT_TESTS=1 to run the bounded Reddit smoke test",
    )
    def test_live_reddit_search_with_strict_budget(self) -> None:
        if not os.environ.get("REDDIT_CLIENT_ID") or not os.environ.get(
            "REDDIT_CLIENT_SECRET"
        ):
            self.skipTest("Reddit live test requires configured API credentials")
        security = ResearchSecurityConfig(
            allow_domains=["reddit.com", "www.reddit.com", "oauth.reddit.com"],
            max_response_bytes=250_000,
            request_timeout_seconds=15,
            max_redirects=2,
        )
        process = ResearchFetchProcess(security)
        try:
            source = RedditSource(
                process,
                RedditResearchSourceConfig(enabled=True),
            )
            query = ResearchQuery(
                query_id="QUERY-LIVE-RD",
                research_question_id="RQ-LIVE",
                source=ResearchSourceName.REDDIT,
                query="coding agent output verbosity",
                content_types=[ResearchSourceType.REDDIT_POST],
            )
            candidates = asyncio.run(
                source.search(
                    query,
                    SourceBudget(
                        max_candidates=2,
                        max_response_bytes=250_000,
                        timeout_seconds=15,
                    ),
                )
            )
            self.assertLessEqual(len(candidates), 2)
            self.assertTrue(
                all(item.source == ResearchSourceName.REDDIT for item in candidates)
            )
        finally:
            process.close()


if __name__ == "__main__":
    unittest.main()
