"""Capture the run's identity from the CLI and the server, never by hand.

`WorkcellPin` requires `system_prompt_sha256`, `tool_schema_sha256`,
`chat_template_sha256`, and friends, and the Slice 2 live gate had to fill some
of them with "the installed CLI bundle hash" as a provisional stand-in. A
provisional identity is worse than a missing one: it validates, it looks
authoritative in evidence, and it does not actually pin the thing whose change
would invalidate the comparison.

This module removes the temptation by taking every value from the artefact
itself:

* the **prompt and tool schemas** come off the wire, from the request body the
  CLI really sent -- not from an Apoapsis-authored copy of what we believe Qwen
  Code sends. Slice 2's whole premise is the *default* CLI, so a hand-written
  prompt hash would be pinning the wrong experiment.
* the **chat template, context window, and server build** come from
  `llama-server`'s own `/props`, which reports what the server loaded rather
  than what its command line asked for.
* the **CLI bundle, model file, and image** are hashed where they live.

`/props` is deliberately *not* an allowlisted relay route. Pin capture is
controller work, done against the upstream directly; the workcell never needs
to ask what it is running inside.
"""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from pydantic import Field

from apoapsis.specification.schema import StrictModel

_SHA256_HEX = r"^[0-9a-f]{64}$"


class PinCaptureError(RuntimeError):
    """Raised when an identity value could not be observed at its source."""


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_tool_schema(tools: list[dict]) -> str:
    """One stable string for a tool schema list.

    Sorted by tool name and re-serialised with sorted keys. Ordering is part of
    the identity for prompt-cache locality (`AgentCliPin` enforces the same
    thing on `tool_names`), but the *hash* must not change merely because the
    CLI emitted its schemas in a different order on a different day.
    """

    return json.dumps(
        sorted(tools, key=_tool_name), sort_keys=True, separators=(",", ":")
    )


def _tool_name(tool: dict) -> str:
    function = tool.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    if isinstance(tool.get("name"), str):
        return tool["name"]
    raise PinCaptureError(f"a tool schema carries no name: {json.dumps(tool)[:200]}")


class PromptIdentity(StrictModel):
    """What the CLI actually put on the wire."""

    system_prompt_sha256: str = Field(pattern=_SHA256_HEX)
    tool_schema_sha256: str = Field(pattern=_SHA256_HEX)
    tool_names: list[str] = Field(min_length=1)
    system_prompt_chars: int = Field(ge=1)
    tool_count: int = Field(ge=1)


def extract_prompt_identity(request_body: dict) -> PromptIdentity:
    """Derive the prompt and tool-schema pins from one captured request.

    Every leading `system` message is concatenated, because a CLI that splits
    its preamble across two system turns still has one system prompt, and
    hashing only the first would miss a change to the second.
    """

    messages = request_body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise PinCaptureError("the captured request carried no messages")
    system_parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            break
        content = message.get("content")
        if isinstance(content, str):
            system_parts.append(content)
        elif isinstance(content, list):
            # Typed content blocks: only the text carries the prompt.
            system_parts.extend(
                block["text"]
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
    if not system_parts:
        raise PinCaptureError(
            "the captured request had no leading system message, so the CLI's "
            "system prompt could not be pinned from the wire"
        )
    system_prompt = "\n".join(system_parts)

    tools = request_body.get("tools")
    if not isinstance(tools, list) or not tools:
        raise PinCaptureError(
            "the captured request declared no tools; a capture taken from a "
            "turn without the tool schemas would pin the wrong prefix"
        )
    names = sorted({_tool_name(tool) for tool in tools})
    return PromptIdentity(
        system_prompt_sha256=sha256_text(system_prompt),
        tool_schema_sha256=sha256_text(canonical_tool_schema(tools)),
        tool_names=names,
        system_prompt_chars=len(system_prompt),
        tool_count=len(tools),
    )


class ServerIdentity(StrictModel):
    """What `llama-server` says about itself, not what its flags requested."""

    server_name: str = Field(min_length=1)
    server_version: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    model_path: str = Field(min_length=1)
    quantization: str = Field(min_length=1)
    chat_template_sha256: str = Field(pattern=_SHA256_HEX)
    context_limit_tokens: int = Field(ge=1)
    supports_tool_calls: bool
    supports_parallel_tool_calls: bool
    supports_preserve_reasoning: bool


def extract_server_identity(props: dict, models: dict) -> ServerIdentity:
    """Fold `/props` and `/v1/models` into the server half of the pin."""

    template = props.get("chat_template")
    if not isinstance(template, str) or not template:
        raise PinCaptureError(
            "the server reported no chat template; the template governs whether "
            "tool calls survive at all and cannot be left unpinned"
        )
    build = props.get("build_info")
    if not isinstance(build, str) or not build:
        raise PinCaptureError("the server reported no build info")
    alias = props.get("model_alias")
    model_path = props.get("model_path")
    if not isinstance(alias, str) or not alias:
        raise PinCaptureError("the server reported no model alias")
    if not isinstance(model_path, str) or not model_path:
        raise PinCaptureError("the server reported no model path")

    settings = props.get("default_generation_settings")
    context_limit = settings.get("n_ctx") if isinstance(settings, dict) else None
    if not isinstance(context_limit, int) or context_limit < 1:
        # Fall back to the model listing, which reports the same window from a
        # different code path. Two disagreeing sources would be a real finding.
        context_limit = _model_context_limit(models, alias)
    listing_limit = _model_context_limit(models, alias, required=False)
    if listing_limit is not None and listing_limit != context_limit:
        raise PinCaptureError(
            f"the server reports two different context windows for {alias!r}: "
            f"{context_limit} from /props and {listing_limit} from /v1/models"
        )

    caps = props.get("chat_template_caps")
    caps = caps if isinstance(caps, dict) else {}
    return ServerIdentity(
        server_name="llama-server",
        server_version=build,
        model_name=alias,
        model_path=model_path,
        quantization=str(props.get("model_ftype") or "unknown"),
        chat_template_sha256=sha256_text(template),
        context_limit_tokens=context_limit,
        supports_tool_calls=bool(caps.get("supports_tool_calls")),
        supports_parallel_tool_calls=bool(caps.get("supports_parallel_tool_calls")),
        supports_preserve_reasoning=bool(caps.get("supports_preserve_reasoning")),
    )


def _model_context_limit(
    models: dict, alias: str, *, required: bool = True
) -> int | None:
    for entry in models.get("data") or []:
        if not isinstance(entry, dict) or entry.get("id") != alias:
            continue
        meta = entry.get("meta")
        value = meta.get("n_ctx") if isinstance(meta, dict) else None
        if isinstance(value, int) and value >= 1:
            return value
    if required:
        raise PinCaptureError(
            f"neither /props nor /v1/models reported a context window for {alias!r}"
        )
    return None


def server_flags_sha256(argv: list[str]) -> str:
    """Hash the exact argv the server was launched with.

    Hashed as JSON rather than as a joined string so that a flag containing a
    space cannot collide with two separate flags.
    """

    if not argv:
        raise PinCaptureError("the server argv is required and was empty")
    return sha256_text(json.dumps(argv, separators=(",", ":")))


Opener = Callable[[str], bytes]


def _default_opener(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def capture_server_identity(
    upstream_base_url: str, *, opener: Opener | None = None
) -> ServerIdentity:
    """Read `/props` and `/v1/models` from the configured upstream."""

    fetch = opener or _default_opener
    base = upstream_base_url.rstrip("/")
    try:
        props = json.loads(fetch(f"{base}/props"))
        models = json.loads(fetch(f"{base}/v1/models"))
    except (OSError, ValueError) as exc:
        raise PinCaptureError(
            f"could not read the server's own identity from {base}: {exc}"
        ) from exc
    return extract_server_identity(props, models)


#: Shell pipeline that hashes an installed CLI bundle reproducibly. Sorted by
#: path with a NUL separator so a filename containing a newline cannot reorder
#: the digest, and the per-file hashes are folded into one value.
CLI_BUNDLE_DIGEST_COMMAND = (
    "find {path} -type f -print0 | LC_ALL=C sort -z | "
    "xargs -0 sha256sum | sha256sum | cut -d' ' -f1"
)


def cli_bundle_argv(bundle_path: str) -> list[str]:
    """The argv that hashes the CLI bundle *inside* the image.

    Hashed where it runs, not on the host: the pin has to identify the CLI the
    workcell actually executes, and the host may not have it installed at all.
    """

    return ["sh", "-c", CLI_BUNDLE_DIGEST_COMMAND.format(path=bundle_path)]


def parse_bundle_digest(stdout: str) -> str:
    """Validate the digest the in-image hash command produced."""

    value = stdout.strip().split("\n")[-1].strip() if stdout.strip() else ""
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise PinCaptureError(
            f"the CLI bundle hash command produced no usable digest: {stdout[:200]!r}"
        )
    return value


class CapturedCliLimits(StrictModel):
    """What the CLI believes the window and output cap are, and how we know.

    Separate from `conformance_driver.DeclaredCliLimits` on purpose: this is the
    *capture* record, carrying the raw table values that produced the answer, so
    a later reader can see whether the model matched the CLI's known-model table
    or fell through to its defaults. The check only needs the two numbers.
    """

    model: str = Field(min_length=1)
    context_limit_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    #: `None` when the model is absent from the CLI's table and the defaults
    #: applied. That distinction matters: a model the CLI has never heard of is
    #: being sized by a generic guess, which is a different defect from a stale
    #: table entry.
    known_context_limit: int | None = None
    known_output_limit: int | None = None
    default_context_limit: int = Field(ge=1)
    output_ceiling: int = Field(ge=1)
    source: str = Field(min_length=1)


#: Asks the CLI's own token-limit module what it believes, by importing and
#: calling it. Reading the numbers out of the minified bundle with a regular
#: expression would pin *our reading* of the CLI; calling the function pins the
#: answer the CLI itself would use when it decides whether to compact.
CLI_LIMITS_SCRIPT = r"""
import * as t from "{module}";
// Last argument, not a fixed index: `node -e` does not put a script path in
// argv the way a file invocation does, and an off-by-one here reads
// `undefined` as the model name -- which silently returns the CLI's *default*
// limits instead of the ones for this model, i.e. a wrong pin that validates.
const model = process.argv[process.argv.length - 1];
console.log(JSON.stringify({{
  model,
  known_context_limit: t.knownTokenLimit(model, "input") ?? null,
  known_output_limit: t.knownTokenLimit(model, "output") ?? null,
  default_context_limit: t.DEFAULT_TOKEN_LIMIT,
  output_ceiling: t.OUTPUT_TOKEN_CEILING,
  context_limit_tokens: t.tokenLimit(model, "input"),
  max_output_tokens: t.defaultOutputCeiling(model),
}}));
"""

#: Locates the chunk that exports `tokenLimit` and runs the script above against
#: it. The chunk filename is content-hashed by the CLI's bundler and changes
#: between releases, so it must be discovered rather than hard-coded.
CLI_LIMITS_COMMAND = (
    'set -e; m=$(grep -rl "function tokenLimit" {path} | head -1); '
    '[ -n "$m" ] || {{ echo "no tokenLimit module found" >&2; exit 3; }}; '
    "node --input-type=module -e \"$(printf '%s' '{script}' | sed \"s#__MODULE__#$m#\")\" -- {model}"
)


def cli_declared_limits_argv(bundle_path: str, model: str) -> list[str]:
    """The argv that asks the in-image CLI for its own declared limits."""

    script = CLI_LIMITS_SCRIPT.format(module="__MODULE__").replace("'", "'\\''")
    return [
        "sh",
        "-c",
        CLI_LIMITS_COMMAND.format(path=bundle_path, script=script, model=model),
    ]


def parse_declared_limits(stdout: str, *, source: str) -> CapturedCliLimits:
    """Validate what the CLI's limit module reported."""

    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    if not lines:
        raise PinCaptureError(
            "the CLI's token-limit module produced no output, so the limits it "
            "believes in could not be observed"
        )
    try:
        payload = json.loads(lines[-1])
    except ValueError as exc:
        raise PinCaptureError(
            f"the CLI's token-limit module emitted unparseable output: "
            f"{lines[-1][:200]!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise PinCaptureError("the CLI's token-limit module did not emit an object")
    return CapturedCliLimits(source=source, **payload)


class EffectiveCliConfig(StrictModel):
    """The merged, resolved configuration the CLI actually runs with.

    Slice 2B pinned the CLI *bundle* and the CLI's *declared limits* and still
    had no pin on the thing that produced those limits. A settings file can be
    edited between two runs, change which model entry is selected, change the
    context window the CLI compacts against, and leave every existing pin
    identical. That is the same class of defect as the Crisis Atlas sliced
    arm's unrecorded seed commit: the arms differ, the manifest says they do
    not, and the comparison is quietly worthless.

    So the whole effective configuration is hashed into
    `AgentCliPin.effective_config_sha256`, and the hash is taken over what the
    CLI's *own* resolver returned -- not over the file Apoapsis wrote. Those are
    different objects whenever a settings file elsewhere on the machine, an
    environment variable, or a bundled default has a say, which is exactly the
    case this is meant to catch.

    Secrets never enter this record. `redacted_keys` names every field whose
    value was replaced by its own SHA-256, so a rotated key still changes the
    digest without the key ever reaching an evidence file.
    """

    effective_config_sha256: str = Field(pattern=_SHA256_HEX)
    #: The CLI's own merged settings, secrets redacted.
    merged_settings: dict
    #: What `resolveCliGenerationConfig` returned, secrets redacted.
    resolved_generation_config: dict
    #: Per-field provenance from the CLI: `settings`, `env`, `default`, and
    #: which key supplied each one. This is what makes the pin diagnosable
    #: rather than merely comparable.
    sources: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    redacted_keys: list[str] = Field(default_factory=list)
    source: str = Field(min_length=1)


#: Asks the CLI to merge and resolve its own configuration, then prints it.
#:
#: `loadSettings` is the CLI's settings loader and `resolveCliGenerationConfig`
#: is the function the CLI itself calls on startup to turn settings, argv, and
#: the environment into the generation config it runs with. Re-implementing
#: either in Python would pin Apoapsis's model of the CLI rather than the CLI,
#: which is the mistake the whole module exists to avoid.
CLI_EFFECTIVE_CONFIG_SCRIPT = r"""
import {{ loadSettings }} from "__SETTINGS_MODULE__";
import {{ resolveCliGenerationConfig }} from "__RESOLVE_MODULE__";
import {{ createHash }} from "node:crypto";

const workspace = process.argv[process.argv.length - 1];
const redacted = [];

// Replaced by its own digest rather than dropped: a rotated credential must
// still move the pin, or "the config did not change" would be a lie.
function scrub(value, path) {{
  if (Array.isArray(value)) return value.map((item, i) => scrub(item, path + "[" + i + "]"));
  if (value && typeof value === "object") {{
    const out = {{}};
    for (const key of Object.keys(value).sort()) {{
      const here = path ? path + "." + key : key;
      if (/apikey|token|secret|password/i.test(key) && typeof value[key] === "string") {{
        redacted.push(here);
        out[key] = "sha256:" + createHash("sha256").update(value[key]).digest("hex");
      }} else {{
        out[key] = scrub(value[key], here);
      }}
    }}
    return out;
  }}
  return value;
}}

const loaded = loadSettings(workspace);
const settings = loaded.merged;
const authType =
  settings?.security?.auth?.selectedType ?? settings?.selectedAuthType ?? null;
const resolved = resolveCliGenerationConfig({{
  selectedAuthType: authType,
  argv: {{}},
  settings,
  env: process.env,
}});

console.log(JSON.stringify({{
  merged_settings: scrub(settings, ""),
  resolved_generation_config: scrub(
    {{
      model: resolved.model,
      baseUrl: resolved.baseUrl,
      generationConfig: resolved.generationConfig,
    }},
    ""
  ),
  sources: resolved.sources ?? {{}},
  warnings: resolved.warnings ?? [],
  redacted_keys: redacted.sort(),
}}));
"""

#: Finds the chunk that *exports* a symbol, not merely one that mentions it.
#:
#: Chunk filenames are content-hashed by the bundler and change between
#: releases, so they must be discovered. `grep` is not used for this: these
#: files are multi-megabyte single-line bundles and GNU grep declines to match
#: on them, which produced a confident match on the wrong chunk -- one that
#: contains `loadSettingsCached` but exports no `loadSettings`, so `node`
#: failed at import with a message that looked like a missing dependency.
#: Matching the ESM export list is both correct and unambiguous.
CLI_EXPORT_DISCOVERY = (
    "import pathlib,re,sys\n"
    "root=pathlib.Path(sys.argv[1]); want=sys.argv[2]\n"
    "for path in sorted(root.rglob('*.js')):\n"
    "    text=path.read_text(errors='replace')\n"
    "    if any(want in [n.strip().split(' as ')[-1].strip() for n in block.split(',')]\n"
    "           for block in re.findall(r'\\nexport \\{([^}]*)\\}', text)):\n"
    "        print(path); break\n"
)

def cli_export_discovery_argv(bundle_path: str, symbol: str) -> list[str]:
    """The argv that names the chunk exporting `symbol`.

    A separate call rather than a shell pipeline inside the node invocation.
    Two reasons, both learned rather than assumed: a multi-line Python program
    nested inside a shell command inside an argv has three layers of quoting to
    get wrong, and a discovery that fails deserves its own exit code instead of
    surfacing as a `node` import error that reads like a missing dependency.
    """

    return ["python3", "-c", CLI_EXPORT_DISCOVERY, bundle_path, symbol]


def parse_discovered_module(stdout: str, *, symbol: str) -> str:
    path = stdout.strip().splitlines()[-1].strip() if stdout.strip() else ""
    if not path:
        raise PinCaptureError(
            f"no module in the CLI bundle exports {symbol!r}, so the CLI's own "
            "configuration resolver could not be called"
        )
    return path


def cli_effective_config_argv(
    *, settings_module: str, resolve_module: str, workspace: str
) -> list[str]:
    """The argv that asks the in-image CLI to resolve its own configuration."""

    script = (
        CLI_EFFECTIVE_CONFIG_SCRIPT.format()
        .replace("__SETTINGS_MODULE__", settings_module)
        .replace("__RESOLVE_MODULE__", resolve_module)
    )
    return ["node", "--input-type=module", "-e", script, "--", workspace]


def canonical_effective_config(payload: dict) -> str:
    """One stable string for an effective configuration.

    Sorted keys and no whitespace, so a re-serialisation with different
    formatting cannot move the digest while a changed *value* always does.
    """

    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def parse_effective_config(stdout: str, *, source: str) -> EffectiveCliConfig:
    """Validate and hash what the CLI's own resolver reported."""

    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    if not lines:
        raise PinCaptureError(
            "the CLI's configuration resolver produced no output, so the "
            "configuration it actually runs with could not be pinned"
        )
    try:
        payload = json.loads(lines[-1])
    except ValueError as exc:
        raise PinCaptureError(
            f"the CLI's configuration resolver emitted unparseable output: "
            f"{lines[-1][:200]!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise PinCaptureError("the CLI's configuration resolver did not emit an object")
    for required in ("merged_settings", "resolved_generation_config"):
        if not isinstance(payload.get(required), dict):
            raise PinCaptureError(
                f"the resolved configuration is missing {required!r}; an "
                "effective-config pin taken from a partial answer would pin "
                "less than it claims"
            )
    digest = sha256_text(
        canonical_effective_config(
            {
                "merged_settings": payload["merged_settings"],
                "resolved_generation_config": payload["resolved_generation_config"],
            }
        )
    )
    # `sources` and `warnings` are deliberately outside the digest: they are
    # provenance *about* the configuration, and a CLI release that reworded a
    # warning must not read as a configuration change.
    return EffectiveCliConfig(
        effective_config_sha256=digest,
        merged_settings=payload["merged_settings"],
        resolved_generation_config=payload["resolved_generation_config"],
        sources=payload.get("sources") or {},
        warnings=[str(item) for item in (payload.get("warnings") or [])],
        redacted_keys=[str(item) for item in (payload.get("redacted_keys") or [])],
        source=source,
    )


class ResolvedCliLimits(StrictModel):
    """The two numbers the CLI resolved, with the provenance of each.

    Distinct from `CapturedCliLimits`, which asks the token-limit *table* what
    it would say about a model name. This asks the assembled configuration what
    the CLI will actually use, which is the only question
    `declared_limits_match_server` was ever really about: after Slice 2C the
    table still answers 1,000,000 for `qwen3.6-27b`, and the run is correct
    anyway because the provider entry overrides it.
    """

    context_window_size: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    #: Where each value came from, in the CLI's own words.
    context_window_source: str = Field(min_length=1)
    max_output_source: str = Field(min_length=1)


def extract_resolved_limits(config: EffectiveCliConfig) -> ResolvedCliLimits:
    """Read the resolved window and output cap out of the CLI's own answer.

    Raises rather than defaulting. A missing value here means the override did
    not take effect, and a default substituted at this point would produce a
    pin that agrees with the server for the wrong reason -- the precise failure
    mode that `NOT_RUN`-is-not-a-pass exists to prevent.
    """

    generation = config.resolved_generation_config.get("generationConfig")
    if not isinstance(generation, dict):
        raise PinCaptureError(
            "the CLI's resolved configuration carried no generationConfig"
        )
    window = generation.get("contextWindowSize")
    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        raise PinCaptureError(
            "the CLI resolved no contextWindowSize, so it is still sizing this "
            "model from its bundled table rather than from the provider entry"
        )
    sampling = generation.get("samplingParams")
    sampling = sampling if isinstance(sampling, dict) else {}
    output = sampling.get("max_tokens")
    if not isinstance(output, int) or isinstance(output, bool) or output < 1:
        raise PinCaptureError(
            "the CLI resolved no samplingParams.max_tokens, so its output "
            "ceiling is still whatever defaultOutputCeiling() returns"
        )
    return ResolvedCliLimits(
        context_window_size=window,
        max_output_tokens=output,
        context_window_source=_source_of(config, "contextWindowSize"),
        max_output_source=_source_of(config, "samplingParams"),
    )


def _source_of(config: EffectiveCliConfig, field: str) -> str:
    entry = config.sources.get(field)
    if isinstance(entry, dict):
        return json.dumps(entry, sort_keys=True, separators=(",", ":"))
    if isinstance(entry, str) and entry:
        return entry
    return "unreported by the CLI resolver"


class CapturedRequest(StrictModel):
    """One request body the capture upstream saw, with no response attached."""

    path: str = Field(min_length=1)
    body_sha256: str = Field(pattern=_SHA256_HEX)
    body: dict


class WireCaptureUpstream:
    """A recording stand-in for the model server, used only to capture pins.

    The relay forwards to *one* configured upstream and refuses to let a client
    choose another. That is exactly the property that makes pin capture easy:
    point `upstream_base_url` at this recorder for a single throwaway CLI
    invocation, and every request the CLI makes is observed verbatim on its way
    to the real server.

    It is not a second relay and must never be used for a measured run. It
    applies no policy, keeps whole request bodies in memory, and exists for the
    handful of seconds it takes to learn what the CLI sends.
    """

    def __init__(self, *, upstream_base_url: str, host: str = "127.0.0.1", port: int = 0) -> None:
        self._upstream = upstream_base_url.rstrip("/")
        self.captured: list[CapturedRequest] = []
        self._lock = threading.Lock()
        recorder = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                # Silent: the capture is the evidence, not stderr noise.
                return

            def do_GET(self) -> None:  # noqa: N802
                self._relay("GET", None)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self._relay("POST", self.rfile.read(length) if length else b"")

            def _relay(self, method: str, payload: bytes | None) -> None:
                if payload:
                    recorder._record(self.path, payload)
                request = urllib.request.Request(
                    recorder._upstream + self.path, data=payload, method=method
                )
                request.add_header("Content-Type", "application/json")
                try:
                    with urllib.request.urlopen(request, timeout=600) as response:
                        body = response.read()
                        status = response.status
                except urllib.error.HTTPError as exc:
                    body = exc.read()
                    status = exc.code
                except OSError as exc:
                    body = json.dumps({"error": str(exc)}).encode("utf-8")
                    status = 502
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._thread: threading.Thread | None = None

    def _record(self, path: str, payload: bytes) -> None:
        try:
            body = json.loads(payload)
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        with self._lock:
            self.captured.append(
                CapturedRequest(
                    path=path,
                    body_sha256=hashlib.sha256(payload).hexdigest(),
                    body=body,
                )
            )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def first_chat_request(self) -> dict:
        with self._lock:
            for item in self.captured:
                if item.path.rstrip("/").endswith("/chat/completions"):
                    return item.body
        raise PinCaptureError(
            "the CLI never sent a chat completion, so nothing was captured to "
            "pin its prompt and tool schemas from"
        )

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def __enter__(self) -> WireCaptureUpstream:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
