from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
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

# A cold llama-server has to read a multi-gigabyte GGUF off disk before it
# will answer /v1/models. Thirty seconds was never survivable for that.
_DEFAULT_SERVICE_WAIT_SECONDS = 300.0


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


@dataclass(frozen=True)
class OpenAICompatibleLocalTarget:
    base_url: str
    model: str
    context_window_tokens: int | None
    roles: tuple[str, ...]


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


def _is_loopback_base_url(value: str) -> bool:
    try:
        _require_loopback_base_url(value)
    except ModelLifecycleError:
        return False
    return True


def configured_ollama_targets(project_root: Path) -> list[OllamaModelTarget]:
    """Read and deduplicate local Ollama models from Apoapsis configuration."""

    payload = _read_config_payload(project_root)
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


def configured_openai_compatible_local_targets(
    project_root: Path,
) -> list[OpenAICompatibleLocalTarget]:
    """Read loopback OpenAI-compatible coding targets from configuration.

    Hosted OpenAI-compatible endpoints are intentionally ignored here. This
    lifecycle helper may only manage local loopback processes selected by the
    operator.
    """

    payload = _read_config_payload(project_root)
    models = payload.get("models")
    if not isinstance(models, dict):
        raise ModelLifecycleError("configuration has no [models] table")

    merged: dict[tuple[str, str], dict[str, object]] = {}
    for role in ("frontier", "local_coder", "frontier_coder"):
        section = models.get(role)
        if (
            not isinstance(section, dict)
            or section.get("provider") != "openai_compatible"
        ):
            continue
        base_url = str(section.get("base_url") or "").rstrip("/")
        if not base_url or not _is_loopback_base_url(base_url):
            continue
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
        key = (base_url, model)
        item = merged.setdefault(key, {"roles": [], "contexts": []})
        roles = item["roles"]
        contexts = item["contexts"]
        assert isinstance(roles, list) and isinstance(contexts, list)
        roles.append(role)
        if context is not None:
            contexts.append(context)

    targets: list[OpenAICompatibleLocalTarget] = []
    for (base_url, model), item in sorted(merged.items()):
        roles = tuple(str(role) for role in item["roles"])
        contexts = [int(value) for value in item["contexts"]]
        targets.append(
            OpenAICompatibleLocalTarget(
                base_url=base_url,
                model=model,
                context_window_tokens=max(contexts) if contexts else None,
                roles=roles,
            )
        )
    return targets


def _read_config_payload(project_root: Path) -> dict[str, object]:
    config_path = Path(project_root).resolve() / ".apoapsis" / "config.toml"
    if not config_path.is_file():
        raise ModelLifecycleError(
            f"Apoapsis is not initialized: configuration not found at {config_path}"
        )
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ModelLifecycleError(f"cannot read {config_path}: {exc}") from exc
    return payload


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


def _launch_openai_compatible_service(project_root: Path, base_url: str) -> int:
    command = os.environ.get("APOAPSIS_LLAMA_SERVER_COMMAND", "").strip()
    if not command:
        raise ModelLifecycleError(
            f"local OpenAI-compatible model endpoint is unavailable at {base_url}; "
            "set APOAPSIS_LLAMA_SERVER_COMMAND to the explicit llama-server command "
            "Apoapsis should launch, or start the endpoint yourself"
        )
    runtime = Path(project_root).resolve() / ".apoapsis" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    log_path = runtime / "llama-server.log"
    # On Windows the operator's command line is handed to CreateProcess
    # verbatim. Splitting it here and letting subprocess re-quote the tokens
    # corrupts any command containing quotes -- notably
    #   wsl.exe -d Ubuntu -- bash -lc "llama-server ..."
    # which shlex(posix=False) turns into a token that still carries its own
    # quote characters, and list2cmdline then escapes them again. Windows
    # native quoting rules are what the operator typed against, so use them.
    launch_target: str | list[str]
    if os.name == "nt":
        launch_target = command
    else:
        argv = shlex.split(command)
        if not argv:
            raise ModelLifecycleError("APOAPSIS_LLAMA_SERVER_COMMAND is empty")
        launch_target = argv
    creationflags = 0
    popen_options: dict[str, object] = {}
    if os.name == "nt":
        # DETACHED_PROCESS must NOT be set here. Measured on Windows 11 with
        # WSL2: launching `wsl.exe ...` with DETACHED_PROCESS produces a
        # completely empty log, because wsl.exe's output relay needs the
        # console handles it is denied. Dropping it recovers the child's
        # stderr -- which is the stream llama.cpp actually logs to. stdout
        # still does not survive the wsl.exe relay, so stderr is the only
        # reliable evidence channel for a launched local model service.
        # CREATE_NO_WINDOW still suppresses a visible console and
        # CREATE_NEW_PROCESS_GROUP still shields the child from Ctrl+C.
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        popen_options["start_new_session"] = True
    with log_path.open("ab") as log:
        log.write(
            f"\n=== apoapsis launch {datetime.now(timezone.utc).isoformat()} ===\n"
            f"command: {command}\n".encode("utf-8")
        )
        log.flush()
        process = subprocess.Popen(  # noqa: S603 - explicit operator command
            launch_target,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=Path(project_root).resolve(),
            creationflags=creationflags,
            **popen_options,
        )
    return process.pid


def _openai_models_path(base_url: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/models"


def _openai_chat_path(base_url: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/chat/completions"


def _request_absolute_json(
    url: str,
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
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelLifecycleError(f"local model endpoint {url} is unavailable: {exc}") from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelLifecycleError(f"local model endpoint {url} returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ModelLifecycleError(f"local model endpoint {url} returned a non-object response")
    return decoded


def _wait_for_openai_compatible(
    base_url: str,
    *,
    timeout_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_error: ModelLifecycleError | None = None
    while time.monotonic() < deadline:
        try:
            return _request_absolute_json(
                _openai_models_path(base_url), timeout_seconds=2.0
            )
        except ModelLifecycleError as exc:
            last_error = exc
            sleep(0.25)
    raise ModelLifecycleError(
        f"local OpenAI-compatible endpoint did not become ready at {base_url}: "
        f"{last_error or 'timeout'}"
    )


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
    service_wait_seconds: float = _DEFAULT_SERVICE_WAIT_SECONDS,
) -> dict[str, object]:
    """Ensure the local service is ready and warm configured coding models."""

    if not _KEEP_ALIVE_PATTERN.fullmatch(keep_alive):
        raise ModelLifecycleError(
            "keep_alive must be a positive Ollama duration such as 30m, or -1"
        )
    configured_ollama = configured_ollama_targets(project_root)
    selected_ollama = [
        item
        for item in configured_ollama
        if include_research or not item.is_research_only
    ]
    selected_openai = configured_openai_compatible_local_targets(project_root)
    if not selected_ollama and not selected_openai:
        raise ModelLifecycleError("no configured loopback local coding model was found")

    endpoint_tags: dict[str, dict[str, object]] = {}
    service_pids: list[int] = []
    for base_url in sorted({item.base_url for item in selected_ollama}):
        try:
            endpoint_tags[base_url] = _request_json(base_url, "/api/tags")
        except ModelLifecycleError:
            if not launch_service or not _is_default_ollama_endpoint(base_url):
                raise
            if not service_pids:
                service_pids.append(_launch_ollama_service(project_root))
            endpoint_tags[base_url] = _wait_for_tags(
                base_url, timeout_seconds=service_wait_seconds
            )

    for target in selected_ollama:
        installed = _installed_models(endpoint_tags[target.base_url])
        if not _is_installed(target.model, installed):
            raise ModelLifecycleError(
                f"configured model '{target.model}' is not installed at "
                f"{target.base_url}; run 'ollama pull {target.model}' explicitly"
            )

    warmed: list[dict[str, object]] = []
    for target in selected_ollama:
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

    for base_url in sorted({item.base_url for item in selected_openai}):
        try:
            _request_absolute_json(_openai_models_path(base_url))
        except ModelLifecycleError:
            if not launch_service:
                raise
            service_pids.append(_launch_openai_compatible_service(project_root, base_url))
            try:
                _wait_for_openai_compatible(
                    base_url, timeout_seconds=service_wait_seconds
                )
            except ModelLifecycleError as exc:
                log_path = (
                    Path(project_root).resolve()
                    / ".apoapsis"
                    / "runtime"
                    / "llama-server.log"
                )
                raise ModelLifecycleError(
                    f"{exc}\nThe launched service's stderr was captured at "
                    f"{log_path} -- read it before assuming the command is wrong. "
                    f"Large models legitimately take minutes to load; raise "
                    f"--service-wait-seconds if the log shows loading still in "
                    f"progress."
                ) from exc

    for target in selected_openai:
        payload: dict[str, object] = {
            "model": target.model,
            # Ollama warms on /api/generate with an empty prompt; OpenAI-
            # compatible servers have no such idiom and several chat templates
            # reject empty user content outright. Send one real token instead.
            "messages": [{"role": "user", "content": "ping"}],
            "temperature": 0,
            "max_tokens": 1,
            "stream": False,
        }
        _request_absolute_json(
            _openai_chat_path(target.base_url),
            payload,
            timeout_seconds=900.0,
        )
        warmed.append(
            {
                "model": target.model,
                "roles": list(target.roles),
                "base_url": target.base_url,
                "context_window_tokens": target.context_window_tokens,
                "status": "ready",
                "provider": "openai_compatible",
            }
        )

    result: dict[str, object] = {
        "action": "start",
        "service_launched": bool(service_pids),
        "service_pids": service_pids,
        "research_included": include_research,
        "models": warmed,
        "note": (
            "Local model services are shared and remain running; use "
            "STOP_APOAPSIS.cmd to release configured model memory where the "
            "provider supports it."
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

    # Loopback OpenAI-compatible servers (llama-server) are reported, never
    # killed. Apoapsis launches them through an operator-supplied command line
    # that may cross a process boundary it cannot see through -- `wsl.exe ...`
    # yields the PID of wsl.exe, not of llama-server inside the distribution.
    # Without proof that a specific PID is that exact server, terminating
    # anything would mean guessing, and guessing here means killing a stranger's
    # process on port 8000. So: tell the truth and stop.
    unmanaged: list[dict[str, object]] = []
    for target in configured_openai_compatible_local_targets(project_root):
        try:
            _request_absolute_json(_openai_models_path(target.base_url))
            status = "running_not_managed_by_apoapsis"
        except ModelLifecycleError:
            status = "not_running"
        unmanaged.append(
            {
                "model": target.model,
                "roles": list(target.roles),
                "base_url": target.base_url,
                "provider": "openai_compatible",
                "status": status,
            }
        )

    note = (
        "Configured model memory was released. The shared Ollama service was "
        "left running intentionally."
    )
    if any(item["status"] == "running_not_managed_by_apoapsis" for item in unmanaged):
        note += (
            " A loopback OpenAI-compatible server is still running and was NOT "
            "stopped: Apoapsis cannot prove which process it is, so it will not "
            "terminate anything on that port. Stop it where you started it."
        )

    result: dict[str, object] = {
        "action": "stop",
        "models": results,
        "unmanaged_local_endpoints": unmanaged,
        "service_stopped": False,
        "note": note,
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
    parser.add_argument(
        "--service-wait-seconds",
        type=float,
        default=_DEFAULT_SERVICE_WAIT_SECONDS,
        help=(
            "how long to wait for a launched local model service to answer "
            f"(default: {_DEFAULT_SERVICE_WAIT_SECONDS:g}s). A cold llama-server "
            "loading a multi-gigabyte GGUF routinely needs minutes, not seconds."
        ),
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
