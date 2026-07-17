from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any
from urllib.parse import quote_plus, urlencode, urlparse

from sol.config import RedditResearchSourceConfig
from sol.research.fetcher import FetchRequest, ResearchFetcher
from sol.research.schemas import (
    LicenseClassification,
    ResearchQuery,
    ResearchSourceName,
    ResearchSourceType,
    RetrievedSource,
    SourceBudget,
    SourceCandidate,
    SourceLocator,
)
from sol.research.security import validate_domain


class RedditSource:
    adapter_name = "reddit"
    adapter_version = "2"

    def __init__(
        self,
        fetcher: ResearchFetcher,
        config: RedditResearchSourceConfig,
    ) -> None:
        if not config.enabled:
            raise ValueError("Reddit research is disabled")
        self.fetcher = fetcher
        self.config = config
        self._token: str | None = None

    async def search(
        self, query: ResearchQuery, budget: SourceBudget
    ) -> list[SourceCandidate]:
        candidates = [self._candidate_from_url(url) for url in query.urls]
        remaining = budget.max_candidates - len(candidates)
        if remaining <= 0:
            return candidates[: budget.max_candidates]
        token = await self._access_token()
        url = (
            "https://oauth.reddit.com/search?"
            f"q={quote_plus(query.query)}&limit={min(remaining, 25)}&sort=relevance"
        )
        response = await self.fetcher.fetch(
            FetchRequest(url=url, headers=self._oauth_headers(token))
        )
        raw = json.loads(response.body)
        candidates.extend(self.parse_search_results(raw, query.query))
        return candidates[: budget.max_candidates]

    async def fetch(self, candidate: SourceCandidate) -> RetrievedSource:
        token = await self._access_token()
        response = await self.fetcher.fetch(
            FetchRequest(
                url=candidate.api_url or candidate.url,
                headers=self._oauth_headers(token),
            )
        )
        raw = json.loads(response.body)
        content, locator, metadata = self.parse_thread(raw, candidate)
        return RetrievedSource(
            candidate_id=candidate.candidate_id,
            source=ResearchSourceName.REDDIT,
            source_type=candidate.source_type,
            title=candidate.title,
            locator=locator,
            content=content,
            metadata={**candidate.metadata, **metadata},
            license=LicenseClassification.IDEA_ONLY,
        )

    async def _access_token(self) -> str:
        if self._token:
            return self._token
        client_id = os.environ.get(self.config.client_id_env)
        client_secret = os.environ.get(self.config.client_secret_env)
        if not client_id or not client_secret:
            raise RuntimeError("Reddit API credentials are not configured")
        encoded = base64.b64encode(
            f"{client_id}:{client_secret}".encode("utf-8")
        ).decode("ascii")
        response = await self.fetcher.fetch(
            FetchRequest(
                url="https://www.reddit.com/api/v1/access_token",
                method="POST",
                headers={
                    "Authorization": f"Basic {encoded}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": self.config.user_agent,
                },
                body=urlencode({"grant_type": "client_credentials"}),
            )
        )
        raw = json.loads(response.body)
        self._token = str(raw["access_token"])
        return self._token

    def _oauth_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "User-Agent": self.config.user_agent,
        }

    def _candidate_from_url(self, url: str) -> SourceCandidate:
        validate_domain(url, ["reddit.com"])
        segments = [item for item in urlparse(url).path.split("/") if item]
        try:
            marker = segments.index("comments")
            post_id = segments[marker + 1]
        except (ValueError, IndexError) as exc:
            raise ValueError(f"unsupported Reddit post URL: {url}")
        trailing = segments[marker + 2 :]
        comment_id = trailing[1] if len(trailing) >= 2 else None
        source_type = (
            ResearchSourceType.REDDIT_COMMENT
            if comment_id
            else ResearchSourceType.REDDIT_POST
        )
        identity = f"{post_id}-{comment_id}" if comment_id else post_id
        return SourceCandidate(
            candidate_id=f"CAND-RD-{identity}",
            source=ResearchSourceName.REDDIT,
            source_type=source_type,
            title=(
                f"Reddit comment {comment_id}"
                if comment_id
                else f"Reddit post {post_id}"
            ),
            url=url,
            api_url=f"https://oauth.reddit.com/comments/{post_id}?limit=20&depth=2",
            deterministic_score=0.5,
            deduplication_key=f"reddit:{identity}",
            metadata={"post_id": post_id, "comment_id": comment_id},
        )

    @classmethod
    def parse_search_results(
        cls, raw: dict[str, Any], query: str
    ) -> list[SourceCandidate]:
        results: list[SourceCandidate] = []
        for child in (raw.get("data") or {}).get("children") or []:
            data = child.get("data") or {}
            post_id = str(data.get("id") or "")
            if not post_id:
                continue
            permalink = str(data.get("permalink") or "")
            url = f"https://www.reddit.com{permalink}"
            title = str(data.get("title") or f"Reddit post {post_id}")
            selftext = str(data.get("selftext") or "")
            terms = {word.lower() for word in query.split() if len(word) > 3}
            exact = sum(term in f"{title} {selftext}".lower() for term in terms)
            score = min(1.0, 0.25 + 0.1 * exact)
            results.append(
                SourceCandidate(
                    candidate_id=f"CAND-RD-{post_id}",
                    source=ResearchSourceName.REDDIT,
                    source_type=ResearchSourceType.REDDIT_POST,
                    title=title,
                    url=url,
                    api_url=(
                        f"https://oauth.reddit.com/comments/{post_id}?limit=20&depth=2"
                    ),
                    snippet=selftext[:1000],
                    metadata={
                        "post_id": post_id,
                        "subreddit": data.get("subreddit"),
                        "created_utc": data.get("created_utc"),
                        "score": int(data.get("score") or 0),
                        "num_comments": int(data.get("num_comments") or 0),
                    },
                    deterministic_score=score,
                    deduplication_key=f"reddit:{post_id}",
                )
            )
        return results

    @staticmethod
    def parse_thread(
        raw: list[Any], candidate: SourceCandidate
    ) -> tuple[str, SourceLocator, dict[str, Any]]:
        post_children = (
            ((raw[0] or {}).get("data") or {}).get("children") or [{}]
        )
        post_data = post_children[0].get("data") or {}
        if candidate.source_type == ResearchSourceType.REDDIT_COMMENT:
            comment_id = str(candidate.metadata.get("comment_id") or "")
            comment_data = RedditSource._find_comment(raw, comment_id)
            body = str(comment_data.get("body") or "")
            if not body or body in {"[deleted]", "[removed]"}:
                raise ValueError("requested Reddit comment is deleted or unavailable")
            permalink = str(comment_data.get("permalink") or "")
            locator = SourceLocator(
                url=(
                    f"https://www.reddit.com{permalink}"
                    if permalink
                    else candidate.url
                ),
                comment_id=comment_id,
            )
            return body[:2_000], locator, {
                "post_id": post_data.get("id") or candidate.metadata.get("post_id"),
                "comment_id": comment_id,
                "subreddit": comment_data.get("subreddit") or post_data.get("subreddit"),
                "created_utc": comment_data.get("created_utc"),
                "score": int(comment_data.get("score") or 0),
            }
        parts = [
            str(post_data.get("title") or candidate.title),
            str(post_data.get("selftext") or ""),
        ]
        permalink = str(post_data.get("permalink") or "")
        locator = SourceLocator(
            url=(
                f"https://www.reddit.com{permalink}"
                if permalink
                else candidate.url
            )
        )
        metadata = {
            "post_id": post_data.get("id") or candidate.metadata.get("post_id"),
            "subreddit": post_data.get("subreddit"),
            "created_utc": post_data.get("created_utc"),
            "score": int(post_data.get("score") or 0),
            "num_comments": int(post_data.get("num_comments") or 0),
        }
        return "\n\n".join(part for part in parts if part), locator, metadata

    @staticmethod
    def _find_comment(raw: list[Any], comment_id: str) -> dict[str, Any]:
        if len(raw) < 2:
            return {}
        pending = list(
            ((raw[1] or {}).get("data") or {}).get("children") or []
        )
        while pending:
            child = pending.pop(0)
            data = child.get("data") or {}
            if str(data.get("id") or "") == comment_id:
                return data
            replies = data.get("replies")
            if isinstance(replies, dict):
                pending.extend(
                    ((replies.get("data") or {}).get("children") or [])
                )
        return {}
