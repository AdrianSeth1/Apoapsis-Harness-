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

For the all-local run, use the generated native Ollama configuration with
`qwen3-coder-next:q4_K_M`; it requires no API key. Set
`[models.local_research].model` to `qwen3.6:27b`. For the hybrid comparison, add
`[models.frontier_coder]` with the same endpoint and model used by the direct
run and enter all three pricing rates.
Set the verification command's first argument to the desired Python executable
if `python` is not on `PATH`.

Export the configured credential environment variable, then run one command:

```bash
sol run "Add resumable downloads.
Preserve the current public API.
Do not add runtime dependencies.
Existing clients must continue working." --context-profile 32k
```

SOL prints the extracted specification and pauses for approval. After approval,
it creates a task worktree and runs the bounded local inspect-edit-test loop. Run
the local-only lane with `--agent-route local_only`; run the automatic escalation
lane with `--agent-route local_then_frontier`. The latter writes a reproducible
escalation package and continues in the same worktree only when the local stage
requests escalation or exhausts its budget.

Inspect the result with the task ID printed in the report:

```bash
sol inspect TASK-XXXXXXXXXXXX
```

The exact prompts, context packages, diffs, policy findings, verification logs,
telemetry, and final report are under `.sol/tasks/<task-id>/`. Source changes are
under `.sol/worktrees/<task-slug>/`; the original checkout remains unchanged.

Use `--context-profile 16k`, `32k`, and `64k` for controlled context scaling.
Keep the selected profile identical between repeated model comparisons and
record GPU/CPU offload separately; the task report already captures transmitted
files and lines, prompt tokens, generation tokens, and latency.

## Comparison

Compare the same outcome fields for both runs:

| Measure | Direct frontier | SOL local-only | SOL local + frontier |
| --- | ---: | ---: | ---: |
| Accepted without human edits | | | |
| Verification passed | | | |
| Local calls | | | |
| Frontier calls | | | |
| Input tokens | | | |
| Output tokens | | | |
| Cached input tokens | | | |
| Estimated cost | | | |
| Total model latency | | | |
| Files and lines transmitted | | | |
| Constraint violations | | | |
| Human review time | | | |

The primary comparison is cost per accepted, non-regressing patch—not token count
alone.

See [`local-qwen-smoke.md`](local-qwen-smoke.md) for the first measured run on
the controlled repository and the model-specific findings it exposed.
