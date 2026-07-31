# LIVE_ENGINEERING_SMOKE — NOT QUALIFICATION EVIDENCE

Date: 2026-07-31.

**This is not a Crisis Atlas arm.** It burns none of the six, it is not paired,
it has no repetition identity, and no manifest or lock binds it. It answers one
practical question only: *does the real model still do real work inside the new
boundary?* It does.

Nothing in this record may be cited as evidence about model quality, about
Apoapsis's detection capability, or about the Crisis Atlas case. The official
zero-token rehearsal and the six paired arms remain entirely ahead.

## Result

| Check | Observation |
| --- | --- |
| Qwen identity in the running container | `@qwen-code/qwen-code` **0.21.1** |
| Approval mode | `yolo`; computer-use and tool-search disabled in settings |
| Relay readiness | container → loopback forwarder → Unix socket → controller relay → model, **live** |
| Server | locked 64K argv, `n_ctx_slot = 65536`; `-n 4096` for this smoke |
| Worktree fingerprint | `d62f607c…` → `e7a467e9…` (**changed**) |
| Files Qwen created | `calc/arithmetic.py`, `tests/test_arithmetic.py` |
| **Controller ran the tests itself** | `Ran 3 tests — OK`, exit **0** |
| Relay requests during the task | **4** |
| Server telemetry | ~40 tok/s eval, `n_tokens = 20,620`, `truncated = 0`, clean stop |

The controller verified the change by running the suite itself rather than
accepting the agent's report. Qwen's own closing line was "Done. Both files
created and all 3 tests pass"; that claim is corroborated here, not trusted.

## Two gates that held under a real model

**Image attestation was not waived.** The controller was rebuilt from `b6063f2`
with `--no-cache` and had to attest before the run: observed
`org.apoapsis.source-commit` equal to the declared commit, build context
re-derived from `git archive`. The earlier `commit_mismatch` stayed a failure
until the image was actually rebuilt.

**The relay refused an over-budget request, live.** On an earlier attempt the
Qwen settings landed at `.qwen/settings.json` instead of the `QWEN_HOME` root,
so the CLI fell back to its own 64,000-token default:

> `400 — the request asked for 64,000 output tokens via 'max_tokens', above
> this run's pinned 16,384-token ceiling; the relay refuses rather than
> clamping so the disagreement is visible instead of becoming an unexplained
> early stop`

That is the containment boundary doing its job against a real model, which is
worth more than a clean first attempt would have been.

## Four setup defects, all in the harness scaffolding

1. `QWEN_HOME` expects `settings.json` at its root, not under `.qwen/`. The
   misplacement caused the 64k refusal above.
2. The controller creates the worktree as root; the workcell runs as uid 65532.
   **Qwen diagnosed this itself** — it tried shell redirection, `cp`, `touch`,
   Python writes and `nsenter`, then reported the ownership rather than
   claiming success. Handing the worktree over is the controller's job.
3. `--list-tools` is not a flag; asking for it prints help.
4. The static tool-surface probe matched vendored noise (`read_file` inside a
   bundled `miniaudio.h`, `edit` in a dependency LICENSE). It was **removed as
   a gate** and kept as labelled weak evidence. The capability proof here is
   behavioural: Qwen used write and shell tools, and the controller verified
   the outcome independently.

Point 4 is the one worth carrying forward. A string search that answers "yes"
for the wrong reason is exactly the kind of check that makes a harness look
green while proving nothing, and the official runner must not rely on it.

## Preserved evidence

`docs/evaluation/slice-7p4-live-smoke-evidence/`

- `smoke-result.json` — the structured result, including the bundle-match caveat
- `qwen-stdout.log`, `qwen-stderr.log` — the agent's raw streams
- `readiness.json`, `workcell-config.json` — the relay path and the exact config
- `produced-worktree/` — what Qwen actually wrote, plus the task it was given
- `llama-server.log`, `serve-smoke.sh` — server telemetry and the exact argv,
  with its single deliberate deviation (`-n 4096`) recorded in the script

`llama-server` was stopped immediately after the run; VRAM returned from
21,404 MiB to 2,932 MiB.

## What is still true

No manifest or lock binds the runner. `run_rehearsal` still passes
`session=None`, Stage 4 still applies candidates itself rather than letting Qwen
write them, several negative controls still pass through a declared-detector
fallback, and the pair scores are unpopulated. The rehearsal is incomplete and
this smoke does not change that; it only establishes that the path the
rehearsal must exercise is real and works.
