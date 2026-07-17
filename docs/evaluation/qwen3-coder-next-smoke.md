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
edge context and Git rejected it. SOL 0.4.3 now adds an immediately adjacent
unchanged line only after an exact unique old-side match and audits the rebased
patch. It does not enable Git's global zero-context mode.

The best post-fix run reached verification twice. New-download behavior and
resume-after-disconnect passed, but when a server ignored `Range` and returned a
full `200` response the patch reported `20` bytes—the prior six-byte offset plus
the fourteen-byte replacement—instead of `14`. The targeted repair did not
change that logic.

## Conclusion

The model installs, loads, and runs acceptably at 64K with predictable CPU/GPU
offload. SOL's workflow and safety boundary also behave correctly. However,
Coder-Next Q4 produced no accepted, verified solution in these controlled
samples. More context did not help this small repository because all relevant
files already fit in every request. Recommended sampling increased variation
but did not produce a successful patch.

Reports remain in the ignored controlled repository under:

```text
.sol/evaluations/download-service-local/.sol/tasks/TASK-87C7DA93782B/report.json
.sol/evaluations/download-service-local/.sol/tasks/TASK-51800DB4E8BC/report.json
.sol/evaluations/download-service-local/.sol/tasks/TASK-835FA3F05D57/report.json
.sol/evaluations/download-service-local/.sol/tasks/TASK-8B15BEB16E82/report.json
```
