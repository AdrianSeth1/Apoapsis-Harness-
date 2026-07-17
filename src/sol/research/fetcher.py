from __future__ import annotations

import asyncio
import concurrent.futures
import multiprocessing
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urljoin, urlparse

from pydantic import Field

from sol.config import ResearchSecurityConfig
from sol.research.security import ResearchSecurityError, validate_domain
from sol.specification.schema import StrictModel


class FetchRequest(StrictModel):
    url: str
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None


class FetchResponse(StrictModel):
    requested_url: str
    final_url: str
    status: int
    content_type: str
    body: str
    byte_count: int = Field(ge=0)
    redirects: int = Field(default=0, ge=0)


class ResearchFetcher(Protocol):
    async def fetch(self, request: FetchRequest) -> FetchResponse: ...


class ResearchFetchError(ResearchSecurityError):
    """A restricted research HTTP request failed policy or transport checks."""


class _RestrictedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_domains: list[str], max_redirects: int) -> None:
        super().__init__()
        self.allow_domains = allow_domains
        self.max_redirects = max_redirects
        self.last_redirect_count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        validate_domain(target, self.allow_domains)
        prior_count = getattr(req, "_sol_redirect_count", 0)
        count = int(prior_count) + 1
        if count > self.max_redirects:
            raise ResearchFetchError("research redirect limit exceeded")
        redirected = super().redirect_request(req, fp, code, msg, headers, target)
        if redirected is not None:
            setattr(redirected, "_sol_redirect_count", count)
        self.last_redirect_count = count
        return redirected


class SafeHttpFetcher:
    _blocked_suffixes = {
        ".7z",
        ".dmg",
        ".dll",
        ".exe",
        ".gz",
        ".jar",
        ".msi",
        ".pkg",
        ".rar",
        ".tar",
        ".tgz",
        ".whl",
        ".zip",
    }

    def __init__(
        self,
        config: ResearchSecurityConfig,
        *,
        opener: Any | None = None,
    ) -> None:
        self.config = config
        self.redirect_handler: _RestrictedRedirectHandler | None = None
        if opener is None:
            self.redirect_handler = _RestrictedRedirectHandler(
                config.allow_domains, config.max_redirects
            )
            self.opener = urllib.request.build_opener(self.redirect_handler)
        else:
            self.opener = opener

    def fetch_sync(self, request: FetchRequest) -> FetchResponse:
        validate_domain(request.url, self.config.allow_domains)
        suffix = Path(urlparse(request.url).path).suffix.lower()
        if suffix in self._blocked_suffixes:
            raise ResearchFetchError(f"blocked research artifact type: {suffix}")
        body = request.body.encode("utf-8") if request.body is not None else None
        http_request = urllib.request.Request(
            request.url,
            data=body,
            method=request.method,
            headers=request.headers,
        )
        if self.redirect_handler is not None:
            self.redirect_handler.last_redirect_count = 0
        try:
            response = self.opener.open(
                http_request, timeout=self.config.request_timeout_seconds
            )
            with response:
                final_url = response.geturl()
                validate_domain(final_url, self.config.allow_domains)
                final_suffix = Path(urlparse(final_url).path).suffix.lower()
                if final_suffix in self._blocked_suffixes:
                    raise ResearchFetchError(
                        f"blocked research artifact type: {final_suffix}"
                    )
                content_type = (
                    response.headers.get_content_type()
                    if hasattr(response.headers, "get_content_type")
                    else str(response.headers.get("Content-Type", "")).split(";", 1)[0]
                )
                if content_type not in self.config.allowed_content_types:
                    raise ResearchFetchError(
                        f"research content type is not allowed: {content_type}"
                    )
                raw = response.read(self.config.max_response_bytes + 1)
                if len(raw) > self.config.max_response_bytes:
                    raise ResearchFetchError("research response size limit exceeded")
                redirects = (
                    self.redirect_handler.last_redirect_count
                    if self.redirect_handler is not None
                    else 0
                )
                return FetchResponse(
                    requested_url=request.url,
                    final_url=final_url,
                    status=int(getattr(response, "status", 200)),
                    content_type=content_type,
                    body=raw.decode("utf-8", errors="replace"),
                    byte_count=len(raw),
                    redirects=redirects,
                )
        except ResearchSecurityError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ResearchFetchError(f"research fetch failed: {exc}") from exc


def _worker_initializer() -> None:
    retained = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
    }
    os.environ.clear()
    os.environ.update(retained)
    isolated_directory = tempfile.mkdtemp(prefix="sol-research-fetch-")
    os.chdir(isolated_directory)


def _fetch_in_worker(
    request_json: str, config_json: str
) -> dict[str, Any]:
    request = FetchRequest.model_validate_json(request_json)
    config = ResearchSecurityConfig.model_validate_json(config_json)
    return SafeHttpFetcher(config).fetch_sync(request).model_dump(mode="json")


class ResearchFetchProcess:
    """Single-purpose network process with a scrubbed environment and no tools."""

    def __init__(self, config: ResearchSecurityConfig) -> None:
        self.config = config
        context = multiprocessing.get_context("spawn")
        self._executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=1,
            mp_context=context,
            initializer=_worker_initializer,
        )

    async def fetch(self, request: FetchRequest) -> FetchResponse:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(
            self._executor,
            _fetch_in_worker,
            request.model_dump_json(),
            self.config.model_dump_json(),
        )
        return FetchResponse.model_validate(raw)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self) -> ResearchFetchProcess:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
