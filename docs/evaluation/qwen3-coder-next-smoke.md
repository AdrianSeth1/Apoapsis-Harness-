# Qwen3-Coder-Next Q4 local evaluation

## Installed model and hardware

The official Ollama model `qwen3-coder-next:q4_K_M` was downloaded and retained
locally. Ollama reports digest
`ca06e9e4087c714d44355bf954099187890e63084b4a632b8e9956c4b9492074`,
51 GB on disk, 79.7B parameters, Q4_K_M quantization, and a 262,144-token native
context. The model is non-thinking-only. See the
[Ollama model entry](https://ollama.com/library/qwen3-coder-next:q4_K_M) and
[Qwen GGUF model card](https://huggingface.co/Qwen/Qwen3-Coder-Next-GGUF).

At both 32K and 64K, Ollama placed approximately 43% on the RTX 4090 and 57% in
CPU/system memory. GPU usage was approximately 23.7 GB. Roughly 19 GB of the
machine's 64 GB system RAM remained free while loaded. The model therefore runs
reliably at 64K on this machine, but it is materially slower than the fully
GPU-resident 18 GB coder baseline.

## Controlled task

Every run used the same repository, natural-language request, six transmitted
files, test-change prohibition, verification command, and single total repair
budget. Research Mode was off. The request was:

```text
Add resumable downloads.
Preserve the current public API.
Do not add runtime dependencies.
Existing clients must continue working.
```

## Results

| Profile | Temperature | Task | Tokens in / out | Model latency | Outcome |
| --- | ---: | --- | ---: | ---: | --- |
| 32K | 0.0 | `TASK-87C7DA93782B` | 7,635 / 1,577 | 77.31 s | Initial patch passed 2/3 tests; repair did not apply |
| 64K | 0.0 | `TASK-51800DB4E8BC` | 7,496 / 1,179 | 69.83 s | Same resume failure; repair did not apply |
| 64K | 1.0 | `TASK-835FA3F05D57` | 7,311 / 976 | 36.80 s | Both patches applied; the same byte-count test failed twice |
| 64K | 1.0 | `TASK-8B15BEB16E82` | 8,206 / 2,479 | 70.08 s | Initial and replacement proposals modified protected tests and were rejected |

An earlier temperature-1 diagnostic (`TASK-5D93D3719E30`) exposed a safe patch
normalization gap: the old side matched once, but the model omitted unchanged
edge context and Git rejected it. Apoapsis 0.4.3 now adds an immediately adjacent
unchanged line only after an exact unique old-side match and audits the rebased
patch. It does not enable Git's global zero-context mode.

The best post-fix run reached verification twice. New-download behavior and
resume-after-disconnect passed, but when a server ignored `Range` and returned a
full `200` response the patch reported `20` bytes—the prior six-byte offset plus
the fourteen-byte replacement—instead of `14`. The targeted repair did not
change that logic.

## Conclusion

The model installs, loads, and runs acceptably at 64K with predictable CPU/GPU
offload. Apoapsis's workflow and safety boundary also behave correctly. However,
Coder-Next Q4 produced no accepted, verified solution in these controlled
**one-shot** samples. More context did not help this small repository because
all relevant files already fit in every request. Recommended sampling increased
variation but did not produce a successful patch.

These results are retained as the one-shot baseline. Apoapsis 0.5 adds a bounded
inspect-edit-test action loop specifically because this experiment did not test
Coder-Next in its intended agentic operating mode. The identical fixture was
therefore rerun through `--execution-mode agent` to test whether iterative
evidence requests and exact failure feedback improve the accepted patch rate.

## Bounded-agent results

Apoapsis 0.5 reran the identical fixture at 64K and temperature 1.0. The model could
request bounded reads and checks, inspect the current worktree diff, and propose
either unified diffs or exact text replacements. Apoapsis converted replacements to
unified diffs and retained the same patch policy and verifier-owned completion.

| Task | Agent turns | Patch attempts | Verification runs | Tokens in / out | Model latency | Outcome |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `TASK-7A745ADAD346` | 12 | 3 | 4 | 75,679 / 2,633 | 210.41 s | Escalated at turn limit; one test remained failing |
| `TASK-74ED212F94BB` | 10 | 3 | 3 | 51,909 / 1,945 | 149.46 s | Complete; all 3 tests passed |

In the successful run, Coder-Next first ran the baseline test, read the target
source, and made an exact replacement. The next test exposed resume and
server-ignore failures. One repair was rejected for whitespace on blank lines;
the model reread the current file, inspected the diff, made a second exact
replacement, and requested the configured test again. All three tests passed.
Only `src/download_service/downloader.py` changed; dependencies, tests, public
signatures, and verification configuration were untouched.

An earlier diagnostic task (`TASK-C91B0A7ED1E6`) failed before the first agent
turn because Ollama rejected Pydantic's discriminated-union JSON Schema. The
wire schema is now a flat conservative JSON object, while Apoapsis still performs
strict discriminated per-action validation after generation.

The successful result changes the interpretation of the one-shot failures:
Coder-Next Q4 is usable on this fixture when given a constrained agent loop.
This is one accepted task, not evidence of broad capability or cost advantage;
the next evaluation phase must repeat runs across multiple task categories and
compare accepted-patch cost and time with direct frontier execution.

These immutable audits predate the Apoapsis namespace migration. Their legacy
`.sol` paths and embedded hashes are intentionally preserved. Reports remain in
the ignored controlled repository under:

```text
.sol/evaluations/download-service-local/.sol/tasks/TASK-87C7DA93782B/report.json
.sol/evaluations/download-service-local/.sol/tasks/TASK-51800DB4E8BC/report.json
.sol/evaluations/download-service-local/.sol/tasks/TASK-835FA3F05D57/report.json
.sol/evaluations/download-service-local/.sol/tasks/TASK-8B15BEB16E82/report.json
```
