from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, quote_plus

from apoapsis.config import GitHubResearchSourceConfig
from apoapsis.research.fetcher import FetchRequest, ResearchFetcher
from apoapsis.research.schemas import (
    LicenseClassification,
    ResearchQuery,
    ResearchSourceName,
    ResearchSourceType,
    RetrievedSource,
    SourceBudget,
    SourceCandidate,
    SourceLocator,
)


class GitHubSource:
    adapter_name = "github"
    adapter_version = "3"

    def __init__(
        self,
        fetcher: ResearchFetcher,
        config: GitHubResearchSourceConfig,
    ) -> None:
        self.fetcher = fetcher
        self.config = config
        self._headers: dict[str, str] | None = None
        self._repository_metadata_cache: dict[str, dict[str, Any]] = {}

    async def search(
        self, query: ResearchQuery, budget: SourceBudget
    ) -> list[SourceCandidate]:
        content_types = query.content_types or [
            ResearchSourceType.GITHUB_REPOSITORY,
            ResearchSourceType.GITHUB_FILE,
            ResearchSourceType.GITHUB_ISSUE,
            ResearchSourceType.GITHUB_PULL_REQUEST,
        ]
        candidates: list[SourceCandidate] = []
        for source_type in content_types:
            if len(candidates) >= budget.max_candidates:
                break
            remaining = budget.max_candidates - len(candidates)
            if source_type == ResearchSourceType.GITHUB_COMMENT:
                candidates.extend(
                    await self._search_pull_request_comments(query, remaining)
                )
                continue
            if source_type == ResearchSourceType.GITHUB_DISCUSSION:
                candidates.extend(await self._search_discussions(query, remaining))
                continue
            if source_type == ResearchSourceType.GITHUB_REPOSITORY:
                endpoint = "repositories"
                qualifier = f" language:{query.language}" if query.language else ""
            elif source_type == ResearchSourceType.GITHUB_FILE:
                endpoint = "code"
                qualifier = f" language:{query.language}" if query.language else ""
            elif source_type in {
                ResearchSourceType.GITHUB_ISSUE,
                ResearchSourceType.GITHUB_PULL_REQUEST,
            }:
                endpoint = "issues"
                qualifier = (
                    " is:pr"
                    if source_type == ResearchSourceType.GITHUB_PULL_REQUEST
                    else " is:issue"
                )
            else:
                continue
            per_page = min(30, budget.max_candidates - len(candidates))
            url = (
                f"https://api.github.com/search/{endpoint}?q="
                f"{quote_plus(query.query + qualifier)}&per_page={per_page}"
            )
            response = await self.fetcher.fetch(
                FetchRequest(url=url, headers=self._authentication_headers())
            )
            raw = json.loads(response.body)
            candidates.extend(
                self.parse_search_results(raw, source_type, query.query)
            )
        return candidates[: budget.max_candidates]

    async def fetch(self, candidate: SourceCandidate) -> RetrievedSource:
        if candidate.source_type == ResearchSourceType.GITHUB_DISCUSSION:
            raw = await self._fetch_discussion(candidate)
        else:
            url = candidate.api_url or candidate.url
            response = await self.fetcher.fetch(
                FetchRequest(url=url, headers=self._authentication_headers())
            )
            raw = json.loads(response.body)
        repository_metadata = await self._repository_metadata(
            candidate.repository,
            raw if candidate.source_type == ResearchSourceType.GITHUB_REPOSITORY else None,
        )
        candidate_metadata = {**candidate.metadata, **repository_metadata}
        if candidate.source_type == ResearchSourceType.GITHUB_FILE:
            candidate_metadata["blob_sha"] = raw.get("sha")
            candidate_metadata["commit_sha"] = await self._resolve_file_commit(
                candidate,
                raw,
                repository_metadata,
            )
        candidate_for_parsing = candidate.model_copy(
            update={"metadata": candidate_metadata}
        )
        content, locator, metadata = self.parse_retrieved(
            candidate_for_parsing, raw
        )
        metadata = self._merge_repository_metadata(
            repository_metadata, metadata
        )
        license_identifier = (
            metadata.get("license_spdx")
            or candidate.metadata.get("license_spdx")
        )
        return RetrievedSource(
            candidate_id=candidate.candidate_id,
            source=ResearchSourceName.GITHUB,
            source_type=candidate.source_type,
            title=candidate.title,
            locator=locator,
            content=content,
            metadata={**candidate.metadata, **metadata},
            license=LicenseClassification.IDEA_ONLY,
            license_identifier=license_identifier,
        )

    @staticmethod
    def _merge_repository_metadata(
        repository_metadata: dict[str, Any],
        source_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        combined = dict(repository_metadata)
        repository_fields = {
            "license_spdx",
            "stars",
            "forks",
            "language",
            "default_branch",
            "fork",
            "contributors_count",
        }
        for key, value in source_metadata.items():
            if (
                key in repository_fields
                and key in combined
                and value in {None, 0, False, ""}
            ):
                continue
            combined[key] = value
        return combined

    async def _resolve_file_commit(
        self,
        candidate: SourceCandidate,
        raw: dict[str, Any],
        repository_metadata: dict[str, Any],
    ) -> str | None:
        html_url = str(raw.get("html_url") or candidate.url)
        ref: str | None = None
        if "/blob/" in html_url:
            ref = html_url.split("/blob/", 1)[1].split("/", 1)[0]
        ref = str(candidate.metadata.get("ref") or ref or "") or None
        if ref and re.fullmatch(r"[0-9a-fA-F]{40,64}", ref):
            return ref.lower()
        ref = ref or repository_metadata.get("default_branch")
        if (
            ref == repository_metadata.get("default_branch")
            and repository_metadata.get("default_branch_commit_sha")
        ):
            return str(repository_metadata["default_branch_commit_sha"])
        if candidate.repository and ref:
            try:
                response = await self.fetcher.fetch(
                    FetchRequest(
                        url=(
                            f"https://api.github.com/repos/{candidate.repository}"
                            f"/commits/{quote(str(ref), safe='')}"
                        ),
                        headers=self._authentication_headers(),
                    )
                )
                commit = json.loads(response.body)
                return str(commit.get("sha") or "") or None
            except Exception:
                pass
        return (
            str(repository_metadata.get("default_branch_commit_sha") or "")
            or None
        )

    async def _repository_metadata(
        self,
        repository: str | None,
        repository_response: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not repository:
            return {}
        cached = self._repository_metadata_cache.get(repository)
        if cached is not None:
            return dict(cached)
        metadata: dict[str, Any] = {}
        raw_repository = repository_response
        if raw_repository is None:
            try:
                response = await self.fetcher.fetch(
                    FetchRequest(
                        url=f"https://api.github.com/repos/{repository}",
                        headers=self._authentication_headers(),
                    )
                )
                raw_repository = json.loads(response.body)
            except Exception as exc:
                metadata["repository_metadata_error"] = type(exc).__name__
        if isinstance(raw_repository, dict):
            metadata.update(self._candidate_metadata(raw_repository))
        default_branch = metadata.get("default_branch")
        if default_branch:
            try:
                response = await self.fetcher.fetch(
                    FetchRequest(
                        url=(
                            f"https://api.github.com/repos/{repository}/commits/"
                            f"{quote(str(default_branch), safe='')}"
                        ),
                        headers=self._authentication_headers(),
                    )
                )
                commit = json.loads(response.body)
                metadata["default_branch_commit_sha"] = commit.get("sha")
            except Exception as exc:
                metadata["default_branch_commit_error"] = type(exc).__name__
        try:
            response = await self.fetcher.fetch(
                FetchRequest(
                    url=(
                        f"https://api.github.com/repos/{repository}/contributors"
                        "?per_page=100&anon=1"
                    ),
                    headers=self._authentication_headers(),
                )
            )
            contributors = json.loads(response.body)
            if isinstance(contributors, list):
                metadata["contributors_count"] = len(contributors)
                metadata["contributors_count_is_lower_bound"] = (
                    len(contributors) == 100
                )
        except Exception as exc:
            metadata["contributors_metadata_error"] = type(exc).__name__
        self._repository_metadata_cache[repository] = dict(metadata)
        return metadata

    async def _search_pull_request_comments(
        self, query: ResearchQuery, limit: int
    ) -> list[SourceCandidate]:
        search_url = (
            "https://api.github.com/search/issues?q="
            f"{quote_plus(query.query + ' is:pr')}&per_page={min(3, limit)}"
        )
        response = await self.fetcher.fetch(
            FetchRequest(url=search_url, headers=self._authentication_headers())
        )
        pull_requests = (json.loads(response.body).get("items") or [])[:3]
        candidates: list[SourceCandidate] = []
        for pull_request in pull_requests:
            repository = self._repository_name(pull_request)
            number = int(pull_request.get("number") or 0)
            if not repository or not number:
                continue
            endpoints = [
                (
                    f"https://api.github.com/repos/{repository}/issues/"
                    f"{number}/comments?per_page={limit}",
                    "conversation",
                ),
                (
                    f"https://api.github.com/repos/{repository}/pulls/"
                    f"{number}/comments?per_page={limit}",
                    "review",
                ),
            ]
            for endpoint, comment_kind in endpoints:
                if len(candidates) >= limit:
                    break
                comments_response = await self.fetcher.fetch(
                    FetchRequest(
                        url=endpoint,
                        headers=self._authentication_headers(),
                    )
                )
                raw_comments = json.loads(comments_response.body)
                candidates.extend(
                    self.parse_comment_results(
                        raw_comments,
                        query.query,
                        repository,
                        number,
                        comment_kind,
                    )[: limit - len(candidates)]
                )
        return candidates[:limit]

    async def _search_discussions(
        self, query: ResearchQuery, limit: int
    ) -> list[SourceCandidate]:
        repository = re.search(
            r"(?:^|\s)repo:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
            query.query,
        )
        headers = self._authentication_headers()
        if repository is None or "Authorization" not in headers:
            return []
        owner, name = repository.groups()
        graphql = """query($owner: String!, $name: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    discussions(first: $first, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number
        title
        body
        url
        updatedAt
        isAnswered
        comments { totalCount }
      }
    }
  }
}"""
        raw = await self._graphql(
            graphql,
            {"owner": owner, "name": name, "first": min(25, limit)},
        )
        nodes = (
            (((raw.get("data") or {}).get("repository") or {}).get("discussions") or {})
            .get("nodes")
            or []
        )
        return self.parse_discussion_results(
            nodes, query.query, f"{owner}/{name}"
        )[:limit]

    async def _fetch_discussion(
        self, candidate: SourceCandidate
    ) -> dict[str, Any]:
        repository = candidate.repository or ""
        try:
            owner, name = repository.split("/", 1)
        except ValueError as exc:
            raise ValueError("GitHub discussion candidate has no repository") from exc
        number = int(candidate.metadata.get("number") or 0)
        graphql = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      number
      title
      body
      url
      updatedAt
      isAnswered
      comments { totalCount }
    }
  }
}"""
        raw = await self._graphql(
            graphql,
            {"owner": owner, "name": name, "number": number},
        )
        discussion = (
            ((raw.get("data") or {}).get("repository") or {}).get("discussion")
        )
        if not isinstance(discussion, dict):
            raise ValueError("GitHub discussion was unavailable")
        return discussion

    async def _graphql(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        headers = self._authentication_headers()
        if "Authorization" not in headers:
            raise RuntimeError("GitHub Discussions require authenticated API access")
        response = await self.fetcher.fetch(
            FetchRequest(
                url="https://api.github.com/graphql",
                method="POST",
                headers={**headers, "Content-Type": "application/json"},
                body=json.dumps({"query": query, "variables": variables}),
            )
        )
        raw = json.loads(response.body)
        if raw.get("errors"):
            raise RuntimeError("GitHub GraphQL returned an error")
        return raw

    def _authentication_headers(self) -> dict[str, str]:
        if self._headers is not None:
            return dict(self._headers)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "apoapsis-harness-research",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token: str | None = None
        if self.config.authentication in {"auto", "github_cli"}:
            try:
                result = subprocess.run(
                    ["gh", "auth", "token"],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    shell=False,
                )
                if result.returncode == 0:
                    token = result.stdout.strip() or None
            except (FileNotFoundError, subprocess.TimeoutExpired):
                token = None
        if token is None and self.config.authentication in {"auto", "token"}:
            token = os.environ.get("GITHUB_TOKEN")
        if self.config.authentication in {"github_cli", "token"} and not token:
            raise RuntimeError("configured GitHub authentication is unavailable")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._headers = headers
        return dict(headers)

    @classmethod
    def parse_search_results(
        cls,
        raw: dict[str, Any],
        requested_type: ResearchSourceType,
        query: str,
    ) -> list[SourceCandidate]:
        candidates: list[SourceCandidate] = []
        for item in raw.get("items") or []:
            source_type = requested_type
            if requested_type in {
                ResearchSourceType.GITHUB_ISSUE,
                ResearchSourceType.GITHUB_PULL_REQUEST,
            }:
                source_type = (
                    ResearchSourceType.GITHUB_PULL_REQUEST
                    if "pull_request" in item
                    else ResearchSourceType.GITHUB_ISSUE
                )
                if source_type != requested_type:
                    continue
            repository = cls._repository_name(item)
            title = str(
                item.get("name")
                or item.get("title")
                or item.get("path")
                or repository
                or "GitHub result"
            )
            url = str(item.get("html_url") or item.get("url"))
            api_url = str(item.get("url")) if item.get("url") else None
            snippet = str(item.get("description") or item.get("body") or "")[:1000]
            metadata = cls._candidate_metadata(item)
            identity = item.get("path") or item.get("number") or ""
            dedupe = f"{source_type.value}:{repository or url}:{identity}"
            candidate_id = "CAND-GH-" + hashlib.sha256(
                dedupe.encode("utf-8")
            ).hexdigest()[:16]
            candidates.append(
                SourceCandidate(
                    candidate_id=candidate_id,
                    source=ResearchSourceName.GITHUB,
                    source_type=source_type,
                    title=title,
                    url=url,
                    api_url=api_url,
                    snippet=snippet,
                    repository=repository,
                    metadata=metadata,
                    deterministic_score=cls._base_score(
                        query, title, snippet, metadata
                    ),
                    deduplication_key=dedupe.lower(),
                )
            )
        return candidates

    @classmethod
    def parse_comment_results(
        cls,
        raw: list[dict[str, Any]],
        query: str,
        repository: str,
        pull_request_number: int,
        comment_kind: str,
    ) -> list[SourceCandidate]:
        candidates: list[SourceCandidate] = []
        for item in raw:
            comment_id = str(item.get("id") or "")
            body = str(item.get("body") or "")
            url = str(item.get("html_url") or item.get("url") or "")
            if not comment_id or not body or not url:
                continue
            login = str((item.get("user") or {}).get("login") or "unknown")
            metadata = cls._candidate_metadata(item)
            metadata.update(
                {
                    "comment_id": comment_id,
                    "comment_kind": comment_kind,
                    "pull_request_number": pull_request_number,
                    "commit_sha": item.get("commit_id"),
                    "start_line": item.get("start_line"),
                    "line": item.get("line"),
                }
            )
            dedupe = f"github_comment:{repository}:{comment_id}"
            candidates.append(
                SourceCandidate(
                    candidate_id="CAND-GH-" + hashlib.sha256(
                        dedupe.encode("utf-8")
                    ).hexdigest()[:16],
                    source=ResearchSourceName.GITHUB,
                    source_type=ResearchSourceType.GITHUB_COMMENT,
                    title=f"PR #{pull_request_number} comment by {login}",
                    url=url,
                    api_url=str(item.get("url") or "") or None,
                    snippet=body[:1_000],
                    repository=repository,
                    metadata=metadata,
                    deterministic_score=cls._base_score(
                        query, f"PR {pull_request_number} comment", body, metadata
                    ),
                    deduplication_key=dedupe,
                )
            )
        return candidates

    @classmethod
    def parse_discussion_results(
        cls,
        raw: list[dict[str, Any]],
        query: str,
        repository: str,
    ) -> list[SourceCandidate]:
        candidates: list[SourceCandidate] = []
        for item in raw:
            number = int(item.get("number") or 0)
            title = str(item.get("title") or "")
            url = str(item.get("url") or "")
            if not number or not title or not url:
                continue
            body = str(item.get("body") or "")
            comments = int((item.get("comments") or {}).get("totalCount") or 0)
            metadata = {
                "number": number,
                "updated_at": item.get("updatedAt"),
                "state": "answered" if item.get("isAnswered") else "open",
                "comments": comments,
            }
            dedupe = f"github_discussion:{repository}:{number}"
            candidates.append(
                SourceCandidate(
                    candidate_id="CAND-GH-" + hashlib.sha256(
                        dedupe.encode("utf-8")
                    ).hexdigest()[:16],
                    source=ResearchSourceName.GITHUB,
                    source_type=ResearchSourceType.GITHUB_DISCUSSION,
                    title=title,
                    url=url,
                    api_url="https://api.github.com/graphql",
                    snippet=body[:1_000],
                    repository=repository,
                    metadata=metadata,
                    deterministic_score=cls._base_score(
                        query, title, body, metadata
                    ),
                    deduplication_key=dedupe,
                )
            )
        return candidates

    @staticmethod
    def parse_retrieved(
        candidate: SourceCandidate, raw: dict[str, Any]
    ) -> tuple[str, SourceLocator, dict[str, Any]]:
        metadata = GitHubSource._candidate_metadata(raw)
        if candidate.source_type == ResearchSourceType.GITHUB_FILE:
            encoded = str(raw.get("content") or "").replace("\n", "")
            content = base64.b64decode(encoded).decode("utf-8", errors="replace")
            lines = max(1, len(content.splitlines()))
            locator = SourceLocator(
                repository=candidate.repository,
                url=str(raw.get("html_url") or candidate.url),
                commit_sha=(
                    str(candidate.metadata.get("commit_sha") or "") or None
                ),
                path=str(raw.get("path") or candidate.metadata.get("path") or "") or None,
                start_line=1,
                end_line=lines,
            )
            metadata["path"] = locator.path
            metadata["blob_sha"] = raw.get("sha")
            metadata["commit_sha"] = locator.commit_sha
            return content, locator, metadata
        if candidate.source_type == ResearchSourceType.GITHUB_REPOSITORY:
            content = "\n".join(
                part
                for part in [
                    str(raw.get("full_name") or candidate.repository or ""),
                    str(raw.get("description") or ""),
                ]
                if part
            )
            locator = SourceLocator(
                repository=str(raw.get("full_name") or candidate.repository or "") or None,
                url=str(raw.get("html_url") or candidate.url),
                commit_sha=(
                    str(
                        candidate.metadata.get("default_branch_commit_sha")
                        or ""
                    )
                    or None
                ),
            )
            return content, locator, metadata
        if candidate.source_type == ResearchSourceType.GITHUB_DISCUSSION:
            number = int(raw.get("number") or candidate.metadata.get("number") or 0)
            content = "\n".join(
                part
                for part in [str(raw.get("title") or ""), str(raw.get("body") or "")]
                if part
            )
            locator = SourceLocator(
                repository=candidate.repository,
                url=str(raw.get("url") or candidate.url),
                discussion_number=number or None,
            )
            metadata.update(
                {
                    "number": number,
                    "updated_at": raw.get("updatedAt"),
                    "state": "answered" if raw.get("isAnswered") else "open",
                    "comments": int(
                        (raw.get("comments") or {}).get("totalCount") or 0
                    ),
                }
            )
            return content, locator, metadata
        number = int(raw.get("number") or candidate.metadata.get("number") or 0) or None
        content = "\n".join(
            part
            for part in [str(raw.get("title") or ""), str(raw.get("body") or "")]
            if part
        )
        locator = SourceLocator(
            repository=candidate.repository,
            url=str(raw.get("html_url") or candidate.url),
            commit_sha=(
                str(raw.get("commit_id") or candidate.metadata.get("commit_sha") or "")
                or None
            ),
            path=str(raw.get("path") or candidate.metadata.get("path") or "") or None,
            start_line=(
                int(raw.get("start_line") or raw.get("line") or 0) or None
                if candidate.source_type == ResearchSourceType.GITHUB_COMMENT
                and (raw.get("path") or candidate.metadata.get("path"))
                else None
            ),
            end_line=(
                int(raw.get("line") or raw.get("start_line") or 0) or None
                if candidate.source_type == ResearchSourceType.GITHUB_COMMENT
                and (raw.get("path") or candidate.metadata.get("path"))
                else None
            ),
            issue_number=(
                number
                if candidate.source_type == ResearchSourceType.GITHUB_ISSUE
                else None
            ),
            pull_request_number=(
                number if candidate.source_type == ResearchSourceType.GITHUB_PULL_REQUEST
                else int(candidate.metadata.get("pull_request_number") or 0) or None
                if candidate.source_type == ResearchSourceType.GITHUB_COMMENT
                else None
            ),
            comment_id=(
                str(raw.get("id"))
                if candidate.source_type == ResearchSourceType.GITHUB_COMMENT
                else None
            ),
        )
        return content, locator, metadata

    @staticmethod
    def _repository_name(item: dict[str, Any]) -> str | None:
        repository = item.get("repository") or {}
        return (
            item.get("full_name")
            or repository.get("full_name")
            or item.get("repository_url", "").removeprefix("https://api.github.com/repos/")
            or None
        )

    @staticmethod
    def _candidate_metadata(item: dict[str, Any]) -> dict[str, Any]:
        license_value = item.get("license") or {}
        raw_comments = item.get("comments") or 0
        comments = (
            int(raw_comments.get("totalCount") or 0)
            if isinstance(raw_comments, dict)
            else int(raw_comments)
        )
        return {
            "updated_at": item.get("updated_at"),
            "stars": int(item.get("stargazers_count") or 0),
            "forks": int(item.get("forks_count") or 0),
            "language": item.get("language"),
            "default_branch": item.get("default_branch"),
            "license_spdx": license_value.get("spdx_id"),
            "number": item.get("number"),
            "state": item.get("state"),
            "comments": comments,
            "contributors_count": item.get("contributors_count"),
            "path": item.get("path"),
            "sha": item.get("sha"),
            "fork": bool(item.get("fork", False)),
            "archived": bool(item.get("archived", False)),
            "topics": list(item.get("topics") or []),
        }

    @staticmethod
    def _base_score(
        query: str, title: str, snippet: str, metadata: dict[str, Any]
    ) -> float:
        terms = {word.lower() for word in query.split() if len(word) > 3}
        haystack = f"{title} {snippet}".lower()
        exact = sum(term in haystack for term in terms) / max(1, len(terms))
        score = 0.2 + 0.55 * exact
        if metadata.get("license_spdx"):
            score += 0.1
        if metadata.get("state") == "closed":
            score += 0.05
        if metadata.get("fork"):
            score -= 0.1
        if metadata.get("archived"):
            score -= 0.2
        path = str(metadata.get("path") or "").lower()
        if "test" in path:
            score += 0.04
        if any(marker in haystack for marker in ("generated", "tutorial", "demo")):
            score -= 0.08
        updated_at = metadata.get("updated_at")
        if updated_at:
            try:
                updated = datetime.fromisoformat(
                    str(updated_at).replace("Z", "+00:00")
                )
                age_days = (datetime.now(timezone.utc) - updated).days
                if age_days <= 730:
                    score += 0.05
                elif age_days > 1_825:
                    score -= 0.05
            except ValueError:
                pass
        stars = int(metadata.get("stars") or 0)
        score += min(0.05, stars / 200_000)
        return max(0.0, min(1.0, score))
