"""An exact token count from a local llama-server, when one is reachable.

`apoapsis.context.window` enforces a token ceiling using the project-wide
4-chars/token heuristic. That heuristic is honest but coarse, and it is
coarsest exactly where these prompts spend their budget: indented JSON, where
punctuation and whitespace tokenize very differently from prose. When the
configured coding model is a local llama-server, it already exposes
`POST /tokenize`, so an exact count is available for the price of one loopback
request.

This is deliberately optional and non-fatal. A tokenizer that is slow, down,
or newly incompatible must never be able to stop a session the heuristic could
have measured perfectly well, so every failure path here returns `None` and
the caller falls back. The guard is only ever made *more* accurate by this
module, never dependent on it.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

#: Kept short on purpose: this call sits in front of every model dispatch, and
#: a tokenizer that needs more than a couple of seconds on loopback is one the
#: caller is better off not waiting for.
DEFAULT_TOKENIZE_TIMEOUT_SECONDS = 2.0


def is_loopback_url(base_url: str) -> bool:
    """Whether `base_url` points at this machine.

    Used only to decide whether offering a `/tokenize` probe is worth trying:
    a remote OpenAI-compatible endpoint is not llama.cpp and would just cost a
    failed request per turn. This is a heuristic for an optional optimisation,
    never a security boundary -- `apoapsis.config` owns the loopback rules
    that actually constrain where traffic may go.
    """

    hostname = urllib.parse.urlparse(base_url).hostname
    if hostname is None:
        return False
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def count_tokens_via_llama_server(
    base_url: str,
    prompt: str,
    *,
    timeout_seconds: float = DEFAULT_TOKENIZE_TIMEOUT_SECONDS,
) -> int | None:
    """Return llama-server's own token count for `prompt`, or `None`.

    `None` means "no exact count is available" for any reason at all --
    unreachable server, non-llama.cpp endpoint, unexpected payload shape. It
    never means "zero tokens".
    """

    endpoint = f"{base_url.rstrip('/')}/tokenize"
    body = json.dumps({"content": prompt}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, list):
        return None
    return len(tokens)


def llama_server_token_counter(
    base_url: str,
    *,
    timeout_seconds: float = DEFAULT_TOKENIZE_TIMEOUT_SECONDS,
) -> Callable[[str], int | None]:
    """Bind `count_tokens_via_llama_server` to one endpoint.

    Shaped as `apoapsis.context.window.TokenCounter` so it can be handed
    straight to `fit_prompt_to_window(count_tokens=...)`.
    """

    def count(prompt: str) -> int | None:
        return count_tokens_via_llama_server(
            base_url, prompt, timeout_seconds=timeout_seconds
        )

    return count


__all__ = [
    "DEFAULT_TOKENIZE_TIMEOUT_SECONDS",
    "is_loopback_url",
    "count_tokens_via_llama_server",
    "llama_server_token_counter",
]
