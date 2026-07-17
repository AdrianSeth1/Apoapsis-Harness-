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
- One OpenAI-compatible frontier adapter with token, cache, latency, and
  configured-price telemetry.
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
boundary, and [the Research Mode guide](docs/research-mode.md) covers setup and
operation.

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
python -m unittest discover -s tests -t . -v
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

## Complete frontier flow

After `sol init`, configure `.sol/config.toml`:

```toml
[models.frontier]
provider = "openai_compatible"
base_url = "https://api.openai.com/v1"
model = "your-frontier-model"
api_key_env = "OPENAI_API_KEY"
timeout_seconds = 120

[models.frontier.pricing]
input_per_million_usd = 0
output_per_million_usd = 0
cached_input_per_million_usd = 0
```

Use the provider's current pricing for the three rates. Then run one command:

```bash
sol run "Add resumable downloads without changing the public API"
```

SOL displays the extracted Pydantic specification and waits for approval. The
`--yes` flag is available for controlled non-interactive evaluation. Approval
does not grant the model workflow authority: SOL deterministically selects
context, records the exact request package, validates the returned diff, applies
it only in the task worktree, runs configured checks, and allows one repair.

Every task writes `.sol/tasks/<task-id>/report.json`. `sol inspect <task-id>`
returns the persisted state/events and embeds that report when present.

The controlled download-service fixture and direct-versus-SOL procedure are in
[`examples/download-service`](examples/download-service) and
[`docs/evaluation/direct-vs-sol.md`](docs/evaluation/direct-vs-sol.md).

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
argv = ["python", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"]
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
