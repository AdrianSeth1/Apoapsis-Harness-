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

## 32K and 64K profile check

SOL 0.4.1 repeated the controlled task with the same installed
`qwen3-coder:30b` and the new explicit context profiles. The first two rows
transmitted all six relevant fixture files, so this small repository does not
measure the retrieval benefit of a larger budget.

| Profile | Task | Input / output tokens | Model latency | Result |
| --- | --- | ---: | ---: | --- |
| `32k` | `TASK-CD68B3934CB4` | 7,367 / 1,879 | 21.16 s | Failed policy after the repair attempted to modify tests |
| `64k` | `TASK-571F8C1896FB` | 7,568 / 1,292 | 13.55 s | Applied and verified twice; one resume test still failed after repair |
| `64k`, Qwen 3.6 | `TASK-FE104DC2242A` | 7,186 / 1,681 | 45.16 s | Replacement applied; two tests passed and one byte-count assertion failed |

During the Qwen 3 Coder 64K run, `ollama ps` reported a 65,536-token context,
100% GPU placement, and a 22 GB loaded allocation. The GPU reported approximately
23.1 GB used with 1.0 GB free. This confirms the installed Q4 model can run the
64K profile fully on this 24 GB GPU. It does not establish that 64K improved
quality: both profiles saw the same repository evidence, and the generated
specification and patch were separate model samples.

The net coding outcome remained unsuccessful. The 64K attempt was closer—it
fixed the server-ignores-range behavior during repair—but did not preserve the
partial file when resuming after a disconnect.

An initial Qwen 3.6 64K trial (`TASK-7EA45D04747A`) revealed that the term limit
could retain `downloads` while evicting the searchable singular `download`, and
that one-level import expansion stopped at a package `__init__.py`. SOL 0.4.2
now keeps plural/stem variants together and follows two bounded import levels.
The corrected rerun transmitted the test, package re-export, downloader, job
store, README, and project configuration. Qwen 3.6's first patch was rejected
for trailing whitespace; its one allowed replacement applied and passed two of
three tests. The remaining failure returned the existing six-byte offset plus
the fourteen-byte replacement response (`20`) instead of the replacement size
(`14`). The repair budget had already been spent replacing the rejected patch.

During the corrected Qwen 3.6 run, Ollama reported the 65,536-token context at
100% GPU placement. The loaded allocation was 18 GB and total GPU usage was
approximately 21.4 GB, leaving 2.7 GB free.

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
.sol/evaluations/download-service-local/.sol/tasks/TASK-CD68B3934CB4/report.json
.sol/evaluations/download-service-local/.sol/tasks/TASK-571F8C1896FB/report.json
.sol/evaluations/download-service-local/.sol/tasks/TASK-FE104DC2242A/report.json
```

The first records a real test failure and targeted repair from the installed
coder. The second records the Qwen 3.6 thinking-budget failure. The third records
deterministic rejection of attempted test modifications.

## Next comparison

Keep `qwen3.6:27b` as the local research model and use `qwen3-coder:30b` as the
fast baseline proposer. The next meaningful model comparison is
`qwen3-coder-next` Q4, not Q5, because its published Q4 package is already about
52 GB and will require CPU/RAM offload on this workstation. Run the same task set
at the explicit `16k`, `32k`, and `64k` profiles. Keep SOL policy, verification
commands, and repair limits fixed, and measure prompt evaluation speed, offload,
patch acceptance, and verified task success at each context size.

That comparison is now recorded in
[`qwen3-coder-next-smoke.md`](qwen3-coder-next-smoke.md).
