# SOL Harness

SOL Harness is a local-first context, delegation, and verification layer for AI
coding agents. This repository currently contains the deterministic MVP
substrate: versioned schemas, an auditable SQLite workflow, Git worktree
isolation, and a configurable verification runner.

It intentionally does **not** generate or apply model-authored code yet.

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

See [ADR 0001](docs/adr/0001-mvp-deterministic-substrate.md) for the resolved
MVP design questions and explicit non-goals.

## Install for development

Requirements are Python 3.12+ and Git.

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
tests/               deterministic unit and integration tests
docs/adr/            architectural decisions
```
