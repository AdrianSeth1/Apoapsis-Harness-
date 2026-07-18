from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


_MODEL_ROLES = ("frontier", "local_coder", "frontier_coder", "local_research")
_KEEP_ALIVE_PATTERN = re.compile(r"^(?:[1-9][0-9]*[smh]?|-1)$")


class ModelLifecycleError(RuntimeError):
    """Raised when the local model lifecycle cannot proceed safely."""


@dataclass(frozen=True)
class OllamaModelTarget:
    base_url: str
    model: str
    context_window_tokens: int | None
    roles: tuple[str, ...]

    @property
    def is_research_only(self) -> bool:
        return self.roles == ("local_research",)


def _require_loopback_base_url(value: str) -> str:
    parsed = urlparse(value.rstrip("/"))
    if parsed.scheme != "http" or not parsed.hostname:
        raise ModelLifecycleError(
            "configured Ollama lifecycle endpoints must be loopback HTTP URLs"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelLifecycleError(
            "configured Ollama lifecycle endpoints must not contain credentials, "
            "queries, or fragments"
        )
    hostname = parsed.hostname.lower()
    loopback = hostname == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise ModelLifecycleError(
            "refusing to manage a non-loopback Ollama endpoint"
        )
    return value.rstrip("/")


def configured_ollama_targets(project_root: Path) -> list[OllamaModelTarget]:
    """Read and deduplicate local Ollama models from Apoapsis configuration."""

    config_path = Path(project_root).resolve() / ".apoapsis" / "config.toml"
    if not config_path.is_file():
        raise ModelLifecycleError(
            f"Apoapsis is not initialized: configuration not found at {config_path}"
        )
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ModelLifecycleError(f"cannot read {config_path}: {exc}") from exc
    models = payload.get("models")
    if not isinstance(models, dict):
        raise ModelLifecycleError("configuration has no [models] table")

    merged: dict[tuple[str, str], dict[str, object]] = {}
    for role in _MODEL_ROLES:
        section = models.get(role)
        if not isinstance(section, dict) or section.get("provider") != "ollama":
            continue
        base_url = _require_loopback_base_url(
            str(section.get("base_url") or "http://127.0.0.1:11434")
        )
        model = str(section.get("model") or "").strip()
        if not model:
            raise ModelLifecycleError(f"models.{role}.model is missing")
        raw_context = section.get("context_window_tokens")
        try:
            context = int(raw_context) if raw_context is not None else None
        except (TypeError, ValueError) as exc:
            raise ModelLifecycleError(
                f"models.{role}.context_window_tokens must be an integer"
            ) from exc
        if context is not None and context <= 0:
            raise ModelLifecycleError(
                f"models.{role}.context_window_tokens must be positive"
            )
        key = (base_url, model)
        item = merged.setdefault(
            key,
            {"roles": [], "contexts": []},
        )
        roles = item["roles"]
        contexts = item["contexts"]
        assert isinstance(roles, list) and isinstance(contexts, list)
        roles.append(role)
        if context is not None:
            contexts.append(context)

    targets: list[OllamaModelTarget] = []
    for (base_url, model), item in sorted(merged.items()):
        roles = tuple(str(role) for role in item["roles"])
        contexts = [int(value) for value in item["contexts"]]
        targets.append(
            OllamaModelTarget(
                base_url=base_url,
                model=model,
                context_window_tokens=max(contexts) if contexts else None,
                roles=roles,
            )
        )
    return targets


def _request_json(
    base_url: str,
    path: str,
    payload: dict[str, object] | None = None,
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, object]:
    body = None
    method = "GET"
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        method = "POST"
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url}{path}", data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelLifecycleError(f"Ollama at {base_url} is unavailable: {exc}") from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelLifecycleError(
            f"Ollama at {base_url} returned invalid JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise ModelLifecycleError(f"Ollama at {base_url} returned a non-object response")
    return decoded


def _installed_models(tags: dict[str, object]) -> set[str]:
    names: set[str] = set()
    raw_models = tags.get("models")
    if not isinstance(raw_models, list):
        return names
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        for field in ("name", "model"):
            value = item.get(field)
            if isinstance(value, str) and value:
                names.add(value)
    return names


def _is_installed(model: str, installed: set[str]) -> bool:
    return model in installed or (":" not in model and f"{model}:latest" in installed)


def _is_default_ollama_endpoint(base_url: str) -> bool:
    parsed = urlparse(base_url)
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and (parsed.port or 80) == 11434
    )


def _launch_ollama_service(project_root: Path) -> int:
    executable = shutil.which("ollama")
    if executable is None:
        raise ModelLifecycleError(
            "Ollama is not running and the 'ollama' executable is not on PATH"
        )
    runtime = Path(project_root).resolve() / ".apoapsis" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    log_path = runtime / "ollama-serve.log"
    creationflags = 0
    popen_options: dict[str, object] = {}
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        popen_options["start_new_session"] = True
    with log_path.open("ab") as log:
        process = subprocess.Popen(  # noqa: S603 - fixed local executable/argv
            [executable, "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            **popen_options,
        )
    return process.pid


def _wait_for_tags(
    base_url: str,
    *,
    timeout_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_error: ModelLifecycleError | None = None
    while time.monotonic() < deadline:
        try:
            return _request_json(base_url, "/api/tags", timeout_seconds=2.0)
        except ModelLifecycleError as exc:
            last_error = exc
            sleep(0.25)
    raise ModelLifecycleError(
        f"Ollama did not become ready at {base_url}: {last_error or 'timeout'}"
    )


def _write_last_result(project_root: Path, result: dict[str, object]) -> None:
    runtime = Path(project_root).resolve() / ".apoapsis" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    target = runtime / "last-model-lifecycle.json"
    temporary = target.with_suffix(".tmp")
    payload = dict(result)
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(target)


def start_local_models(
    project_root: Path,
    *,
    include_research: bool = False,
    keep_alive: str = "30m",
    launch_service: bool = True,
    service_wait_seconds: float = 30.0,
) -> dict[str, object]:
    """Ensure the local service is ready and warm configured coding models."""

    if not _KEEP_ALIVE_PATTERN.fullmatch(keep_alive):
        raise ModelLifecycleError(
            "keep_alive must be a positive Ollama duration such as 30m, or -1"
        )
    configured = configured_ollama_targets(project_root)
    selected = [
        item for item in configured if include_research or not item.is_research_only
    ]
    if not selected:
        raise ModelLifecycleError("no configured local Ollama coding model was found")

    endpoint_tags: dict[str, dict[str, object]] = {}
    service_pid: int | None = None
    for base_url in sorted({item.base_url for item in selected}):
        try:
            endpoint_tags[base_url] = _request_json(base_url, "/api/tags")
        except ModelLifecycleError:
            if not launch_service or not _is_default_ollama_endpoint(base_url):
                raise
            if service_pid is None:
                service_pid = _launch_ollama_service(project_root)
            endpoint_tags[base_url] = _wait_for_tags(
                base_url, timeout_seconds=service_wait_seconds
            )

    for target in selected:
        installed = _installed_models(endpoint_tags[target.base_url])
        if not _is_installed(target.model, installed):
            raise ModelLifecycleError(
                f"configured model '{target.model}' is not installed at "
                f"{target.base_url}; run 'ollama pull {target.model}' explicitly"
            )

    warmed: list[dict[str, object]] = []
    for target in selected:
        options: dict[str, object] = {}
        if target.context_window_tokens is not None:
            options["num_ctx"] = target.context_window_tokens
        payload: dict[str, object] = {
            "model": target.model,
            "prompt": "",
            "stream": False,
            "keep_alive": keep_alive,
        }
        if options:
            payload["options"] = options
        _request_json(
            target.base_url,
            "/api/generate",
            payload,
            timeout_seconds=900.0,
        )
        warmed.append(
            {
                "model": target.model,
                "roles": list(target.roles),
                "base_url": target.base_url,
                "context_window_tokens": target.context_window_tokens,
                "keep_alive": keep_alive,
                "status": "ready",
            }
        )

    result: dict[str, object] = {
        "action": "start",
        "service_launched": service_pid is not None,
        "service_pid": service_pid,
        "research_included": include_research,
        "models": warmed,
        "note": (
            "The Ollama service is shared and remains running; use STOP_APOAPSIS.cmd "
            "to release configured model memory."
        ),
    }
    _write_last_result(project_root, result)
    return result


def stop_local_models(project_root: Path) -> dict[str, object]:
    """Explicitly unload every configured loopback Ollama model."""

    targets = configured_ollama_targets(project_root)
    results: list[dict[str, object]] = []
    reachable: dict[str, tuple[bool, set[str]]] = {}
    for base_url in sorted({item.base_url for item in targets}):
        try:
            tags = _request_json(base_url, "/api/tags")
            reachable[base_url] = (True, _installed_models(tags))
        except ModelLifecycleError:
            reachable[base_url] = (False, set())

    for target in targets:
        endpoint_reachable, installed = reachable[target.base_url]
        status = "service_unreachable_already_unloaded"
        if endpoint_reachable and not _is_installed(target.model, installed):
            status = "not_installed"
        elif endpoint_reachable:
            _request_json(
                target.base_url,
                "/api/generate",
                {
                    "model": target.model,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": 0,
                },
                timeout_seconds=60.0,
            )
            status = "unloaded"
        results.append(
            {
                "model": target.model,
                "roles": list(target.roles),
                "base_url": target.base_url,
                "status": status,
            }
        )

    result: dict[str, object] = {
        "action": "stop",
        "models": results,
        "service_stopped": False,
        "note": (
            "Configured model memory was released. The shared Ollama service was "
            "left running intentionally."
        ),
    }
    _write_last_result(project_root, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apoapsis-model-lifecycle",
        description="Start or stop Apoapsis's configured local Ollama models.",
    )
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--include-research",
        action="store_true",
        help="also warm models used only by local Research Mode",
    )
    parser.add_argument(
        "--keep-alive",
        default="30m",
        help="Ollama keep-alive for warmed models (default: 30m)",
    )
    parser.add_argument(
        "--no-launch-service",
        action="store_true",
        help="fail instead of launching `ollama serve` when the default endpoint is down",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "start":
            result = start_local_models(
                args.project_root,
                include_research=args.include_research,
                keep_alive=args.keep_alive,
                launch_service=not args.no_launch_service,
            )
        else:
            result = stop_local_models(args.project_root)
    except ModelLifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
