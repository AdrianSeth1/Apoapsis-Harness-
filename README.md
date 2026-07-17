# SOL Harness

SOL Harness is a local-first context, research, and verification layer for AI
coding agents. It contains the deterministic `substrate-v0.1` baseline, one
bounded frontier-model patch flow, and a quarantined local Research Mode.

## What works now

- Structured task, hard-constraint, context-evidence, model I/O, verification,
  and workflow-event schemas.
- Exact preservation of hard-constraint wording and fail-closed model-request
  constraint coverage.
- Atomic, optimistic SQLite workflow transitions with an append-only event log.
- Repository inspection and dedicated Git worktree/branch lifecycle.
- TOML-configured verification commands with timeouts, restricted environment,
  bounded logs, and structured results.
- A dependency-light CLI and standard-library test suite.
- Native loopback-only Ollama and authenticated OpenAI-compatible frontier
  adapters with token, cache, latency, and configured-price telemetry.
- Model-assisted specification extraction with exact hard-constraint source
  validation and explicit user approval.
- Reproducible Git/ripgrep/symbol/import/test context packages with line-level
  provenance.
- Unified-diff parsing, policy validation, safe worktree application, automatic
  verification, and at most one targeted frontier repair.
- A complete per-task audit directory and aggregate usage/outcome report.
- Deterministically triggered GitHub, official-documentation, and opt-in Reddit
  research planned and synthesized by a tool-free local model.
- Native Ollama structured output, thinking controls, model digest, token, and
  duration telemetry, with the OpenAI-compatible interface retained as fallback.
- Source provenance, license classification, content quarantine, injection
  warnings, bounded caching, comparative synthesis, and brief-only frontier
  handoff.

See [ADR 0001](docs/adr/0001-mvp-deterministic-substrate.md) for the substrate
and [ADR 0002](docs/adr/0002-frontier-vertical-slice.md) for the frontier flow.
[ADR 0003](docs/adr/0003-local-research-mode.md) records the Research Mode trust
boundary, [ADR 0004](docs/adr/0004-native-ollama-frontier.md) records the native
all-local proposal path, and [the Research Mode guide](docs/research-mode.md)
covers setup and operation.

## Install for development

Requirements are Python 3.12+, Git, and preferably ripgrep. The context compiler
has a deterministic lexical fallback when ripgrep is unavailable.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e .
```

On macOS or Linux, use `.venv/bin/python` instead.

Run the tests:

```bash
python -m unittest discover -s tests -v
```

## Current CLI workflow

Initialize SOL inside an existing Git repository:

```bash
sol init
```

Draft a task without model inference. Repeated flags preserve constraints and
criteria as separate source-backed records:

```bash
sol task "Add resumable downloads" \
  --constraint "Preserve the current public API." \
  --constraint "Do not add runtime dependencies." \
  --acceptance "Interrupted downloads resume from the persisted byte."
```

Review and approve the generated task ID:

```bash
sol inspect TASK-ABC123
sol approve TASK-ABC123 --version 2
```

The lower-level workflow APIs then support repository analysis, context
compilation, routing, patch readiness, and verification as later milestones are
added. Worktree and verification lifecycle commands already exist:

```bash
sol worktree-create TASK-ABC123
sol verify TASK-ABC123
sol rollback TASK-ABC123 --delete-branch
```

`verify` deliberately refuses to run until the persisted task state is
`PATCH_READY`. `rollback` is explicit and may discard uncommitted task-worktree
changes. Normal cleanup APIs refuse dirty worktrees unless force is requested.

## Complete all-local flow

`sol init` now creates a 32K working configuration for the models used by the
local evaluation:

```toml
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3-coder:30b"
timeout_seconds = 900
max_output_tokens = 8192
temperature = 0.0
context_window_tokens = 32768
think = false
specification_think = false

[models.frontier.pricing]
input_per_million_usd = 0
output_per_million_usd = 0
cached_input_per_million_usd = 0

[models.local_research]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3.6:27b"
timeout_seconds = 600
max_output_tokens = 8192
temperature = 0.0
context_window_tokens = 32768

[context]
max_files = 16
max_excerpt_lines = 160
max_total_chars = 72000
max_import_depth = 2
```

Both native Ollama endpoints must be loopback URLs. No fake API key is needed.
For a hosted model, switch `provider` back to `openai_compatible`, configure the
base URL and `api_key_env`, and enter the provider's current pricing. Then run
one command:

```bash
sol run "Add resumable downloads without changing the public API"
```

The default is the `32k` working profile. A run can select a reproducible
comparison profile without editing the project configuration:

| Profile | Ollama window | Files | Lines per excerpt | Total excerpt characters |
| --- | ---: | ---: | ---: | ---: |
| `16k` | 16,384 | 10 | 100 | 24,000 |
| `32k` | 32,768 | 16 | 160 | 72,000 |
| `64k` | 65,536 | 24 | 240 | 180,000 |

```bash
sol run "Add resumable downloads without changing the public API" --context-profile 64k
```

Profiles affect frontier coding calls and deterministic repository retrieval;
Research Mode retains its separately configured budget. SOL records the active
window and generation settings in every frontier request package and the exact
retrieval limits in every context package.

The installed Coder-Next Q4 model can be selected explicitly:

```toml
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3-coder-next:q4_K_M"
temperature = 1.0
context_window_tokens = 65536
think = false
specification_think = false
```

Temperature is configurable for native Ollama and hosted providers and is
recorded in each request package. Zero remains the generated deterministic
sampling default; Coder-Next's published model settings recommend `1.0`.

SOL displays the extracted Pydantic specification and waits for approval. The
`--yes` flag is available for controlled non-interactive evaluation. Approval
does not grant the model workflow authority: SOL deterministically selects
context, records the exact request package, validates the returned diff, applies
it only in the task worktree, runs configured checks, and allows one repair.

Every task writes `.sol/tasks/<task-id>/report.json`. `sol inspect <task-id>`
returns the persisted state/events and embeds that report when present.

The controlled download-service fixture and direct-versus-SOL procedure are in
[`examples/download-service`](examples/download-service) and
[`docs/evaluation/direct-vs-sol.md`](docs/evaluation/direct-vs-sol.md). The first
measured local Qwen smoke results are in
[`docs/evaluation/local-qwen-smoke.md`](docs/evaluation/local-qwen-smoke.md).
The installed Coder-Next Q4 evaluation is in
[`docs/evaluation/qwen3-coder-next-smoke.md`](docs/evaluation/qwen3-coder-next-smoke.md).

## Research Mode

Research runs only after specification approval. In `auto` mode, deterministic
rules activate it for research, precedent, product/UX, public API, CLI, report,
dashboard, and similar judgment-heavy work, while localized mechanical work is
skipped. Explicit modes are also available:

```bash
sol run "Improve the task report UX" --research auto
sol run "Add resumable downloads" --research github
sol run "Why do users dislike coding-agent logs?" --research community
sol run "Research and improve the onboarding report" --research full
```

Configure `[models.local_research]` in `.sol/config.toml` with a locally
available Ollama model. GitHub and configured official documentation are enabled
by default. Reddit remains disabled until its approved API credentials and
applicable terms are configured.

Research can also be run independently for an already approved task:

```bash
sol research TASK-ABC123 --mode full
sol research inspect TASK-ABC123
sol research refresh TASK-ABC123 --mode full
sol research cache inspect
sol research cache clear
```

The deterministic harness owns URLs, network access, budgets, provenance,
license classification, caching, and audit writes. The local model receives no
tools and may only propose structured questions, rankings, evidence, and
synthesis. Retrieved text is sanitized and marked `UNTRUSTED_EXTERNAL_CONTENT`.
The frontier model receives only the compact research brief and evidence IDs,
never full threads or fetched pages. External sources remain advisory: only the
approved task, repository policy, patch validation, and verification authorize
a change.

Research artifacts are written below
`.sol/tasks/<task-id>/research/`; the final `report.json` includes the selected
mode, patterns, evidence IDs, local-model calls, tokens, latency, and whether the
brief influenced the proposed plan.

## Verification configuration

`sol init` creates `.sol/config.toml`. Commands are argument arrays, not shell
snippets:

```toml
[verification]
stop_on_failure = false
output_limit_chars = 100000

[[verification.commands]]
name = "unit-tests"
category = "tests"
argv = ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
timeout_seconds = 120
required = true
```

The runner is deterministic but is not yet a container sandbox. Network denial,
CPU/memory enforcement, secret redaction, and approval gates must be supplied by
the future sandbox adapter before untrusted or model-selected commands are run.

## Repository layout

```text
src/sol/
  cli/              CLI entry points
  context/          provenance-aware evidence schemas
  execution/        managed Git worktrees
  models/           provider-neutral model request/response schemas
  repository/       deterministic Git inspection
  specification/    task and constraint schemas
  verification/     command runner and results
  workflow/         persisted state machine and events
  audit/            reproducible per-call and per-task artifacts
  patches/          unified-diff parsing, policy, and application
  reporting/        aggregate outcome and usage reports
tests/               deterministic unit and integration tests
docs/adr/            architectural decisions
```
