# First local Qwen smoke evaluation

## Environment

- Windows workstation with an RTX 4090 (24 GB VRAM) and 64 GB system RAM.
- Ollama 0.32.1.
- `qwen3-coder:30b` Q4_K_M (18 GB).
- `qwen3.6:27b` Q4_K_M (17 GB).
- Controlled `examples/download-service` baseline with one passing test and two
  intentionally failing resumable-download tests.

The task was run through the normal one-command SOL workflow with Research Mode
off so the patch proposer could be measured independently:

```text
Add resumable downloads.
Preserve the current public API.
Do not add runtime dependencies.
Existing clients must continue working.
```

## What the run established

The complete native Ollama path is operational. Real calls produced model
digests, prompt hashes, token counts, load/evaluation/generation durations,
latency, exact context provenance, original and canonicalized diffs, policy
decisions, isolated worktrees, normalized failures, bounded repairs, verification
results, and final reports.

The smoke run exposed and drove deterministic fixes for:

- Ollama use as the implementation and repair provider without a fake API key.
- Diff-only Markdown wrapper handling and unmarked blank context lines.
- Non-applying patch replacement within the single total repair budget.
- Default rejection of any model-proposed test-file modification.
- Plural/stem query ranking so verbose specifications still retrieve source and
  tests for terms such as `downloads` and `download`.
- Portable unittest discovery for repositories whose `tests` directory is not a
  Python package.
- Unique old-context hunk-coordinate rebasing and CRLF worktree normalization,
  with exact canonical diffs retained in the audit.

## Model observations

`qwen3-coder:30b` was interactive: warm implementation and repair calls were
generally about 1–6 seconds, with complete three-call workflows around 8–17
seconds. It reliably extracted all three verbatim constraints and sometimes
produced an applying source patch. On this task it did not reach a verified
solution within the one-repair budget. Observed failures included incomplete
append behavior, a logically insufficient repair, and attempted duplicate test
changes that policy rejected.

`qwen3.6:27b` produced a good structured specification but was less suitable as
the raw diff proposer. Thinking-enabled patch calls used a full 4,096-token
generation allowance on reasoning without returning final content and took
roughly 100 seconds each. With thinking disabled, a complete three-call attempt
took about 47 seconds, but the proposed resume logic still failed the controlled
behavior. It remains the better candidate for Research Mode planning and
synthesis, with thinking kept off for extraction and used cautiously for
synthesis.

These are smoke observations, not a statistically meaningful benchmark. The
harness behaved correctly by rejecting or failing unverified proposals instead
of declaring success.

## Representative local audits

The disposable evaluation repository is ignored by the main Git checkout:

```text
.sol/evaluations/download-service-local/
```

Representative task reports from the development run include:

```text
.sol/evaluations/download-service-local/.sol/tasks/TASK-804930F48EF5/report.json
.sol/evaluations/download-service-local/.sol/tasks/TASK-1D0535EFC4DA/report.json
.sol/evaluations/download-service-local/.sol/tasks/TASK-51D768D8C530/report.json
```

The first records a real test failure and targeted repair from the installed
coder. The second records the Qwen 3.6 thinking-budget failure. The third records
deterministic rejection of attempted test modifications.

## Next comparison

Keep `qwen3.6:27b` as the local research model and use `qwen3-coder:30b` as the
fast baseline proposer. The next meaningful model comparison is
`qwen3-coder-next` Q4, not Q5, because its published Q4 package is already about
52 GB and will require CPU/RAM offload on this workstation. Run the same task set
without changing SOL policy, context budgets, verification commands, or repair
limits.
