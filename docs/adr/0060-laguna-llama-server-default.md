# ADR 0060: Laguna llama-server local coding default

- Status: Accepted
- Date: 2026-07-26

## Context

The owner is now running Poolside Laguna S 2.1 locally through `llama.cpp`'s
OpenAI-compatible `llama-server` on `127.0.0.1:8000`, with a 32,768-token
context and Q8 key/value cache. The generated Apoapsis project configuration
still targeted an Ollama-served Qwen3-Coder-Next model on port 11434.

The existing OpenAI-compatible adapter also required an API key for every
endpoint, which is correct for hosted providers but unnecessary friction for a
loopback `llama-server`.

## Decision

Fresh `apoapsis init` projects now default both `models.frontier` and
`models.local_coder` to:

```toml
provider = "openai_compatible"
base_url = "http://127.0.0.1:8000/v1"
model = "Laguna-S-2.1-UD-Q4_K_S"
context_window_tokens = 32768
think = false
specification_think = false
```

The OpenAI-compatible adapter may omit the `Authorization` header only when the
configured endpoint host is loopback (`localhost`, `127.0.0.1`, or `::1`).
Non-loopback OpenAI-compatible endpoints still require the configured credential
environment variable before any request is sent.

## Consequences

The default coding lane now matches the live local model the owner is actually
testing. ADR 0062 later supersedes this ADR's original launcher limitation:
`START_APOAPSIS.cmd` can now start a loopback `llama-server` through an explicit
operator-provided `APOAPSIS_LLAMA_SERVER_COMMAND`.

This changes defaults for new projects only. Existing initialized projects keep
their current `.apoapsis/config.toml` until edited or reinitialized.

## Verification

Planned focused checks:

```powershell
python -m unittest tests.test_cli tests.test_provider_and_specification -v
python -m compileall -q src tests
git diff --check
```
