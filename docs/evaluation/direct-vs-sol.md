# Comparing a direct frontier task with SOL Harness

Use two independent copies of the controlled
[`examples/download-service`](../../examples/download-service) repository so
both runs begin at the same commit.

## Shared task

```text
Add resumable downloads.
Preserve the current public API.
Do not add runtime dependencies.
Existing clients must continue working.
```

Initialize and commit each copy, then record the commit hash:

```bash
git init -b main
git add .
git commit -m "Controlled download-service baseline"
git rev-parse HEAD
```

The baseline has two intentionally failing resumable-download acceptance tests.
The existing new-download test must remain passing.

## Direct frontier run

Give the shared task to the frontier coding tool without SOL. Record:

- Every prompt and response.
- Model name and provider.
- Input, output, and cached tokens.
- Estimated cost and total latency.
- Files or repository context transmitted.
- Number of repair turns.
- Final diff and human edits.

Apply the returned patch in a dedicated branch and run:

```bash
python -m unittest discover -s tests -v
```

Keep the model/provider and pricing identical to the SOL run. Do not give the
direct run extra repository context unless it is counted as transmitted input.

## SOL run

Install SOL Harness, initialize metadata, and edit `.sol/config.toml`:

```bash
sol init
```

For the first all-local run, use the generated native Ollama configuration with
`qwen3-coder:30b`; it requires no API key. Set `[models.local_research].model`
to `qwen3.6:27b`. For a hosted comparison, set `[models.frontier]` to the same
endpoint and model used by the direct run and enter all three pricing rates.
Set the verification command's first argument to the desired Python executable
if `python` is not on `PATH`.

Export the configured credential environment variable, then run one command:

```bash
sol run "Add resumable downloads.
Preserve the current public API.
Do not add runtime dependencies.
Existing clients must continue working."
```

SOL prints the extracted specification and pauses for approval. After approval,
it creates a task worktree, requests a unified diff, validates and applies it,
runs verification, and permits at most one focused frontier repair.

Inspect the result with the task ID printed in the report:

```bash
sol inspect TASK-XXXXXXXXXXXX
```

The exact prompts, context packages, diffs, policy findings, verification logs,
telemetry, and final report are under `.sol/tasks/<task-id>/`. Source changes are
under `.sol/worktrees/<task-slug>/`; the original checkout remains unchanged.

## Comparison

Compare the same outcome fields for both runs:

| Measure | Direct | SOL |
| --- | ---: | ---: |
| Accepted without human edits | | |
| Verification passed | | |
| Frontier calls | | |
| Input tokens | | |
| Output tokens | | |
| Cached input tokens | | |
| Estimated cost | | |
| Total model latency | | |
| Files and lines transmitted | | |
| Constraint violations | | |
| Human review time | | |

The primary comparison is cost per accepted, non-regressing patch—not token count
alone.

See [`local-qwen-smoke.md`](local-qwen-smoke.md) for the first measured run on
the controlled repository and the model-specific findings it exposed.
