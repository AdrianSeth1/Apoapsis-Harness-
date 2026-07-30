# Live evidence: START lifecycle with Laguna via llama-server (WSL2)

Date: 2026-07-26
Operator: Arya
Scope: handoff items 1, 2 (partial), 5 — first live evidence that
`apoapsis.operator_lifecycle start` launches and warms the owner's real
Laguna `llama-server` from Windows against WSL2.

Status: **model startup path PROVEN. End-to-end task run BLOCKED** by a
credential check (see "Blockers found" below).

## Environment

| Item | Value |
| --- | --- |
| Host | Windows, 63.06 GiB RAM |
| Python | 3.14.5 (`py -3`) |
| Git | 2.54.0.windows.1 |
| WSL distribution | `Ubuntu-24.04` (also present: `docker-desktop`) |
| WSL RAM visible | 58 GiB |
| GPU | NVIDIA GeForce RTX 4090, 24564 MiB, driver 610.74 |
| llama.cpp build | `b10107-c0bc8591e`, CUDA-enabled, `/home/arya/llama.cpp/build/bin/llama-server` |
| Model file | `/home/arya/models/laguna-q4s/UD-Q4_K_S/Laguna-S-2.1-UD-Q4_K_S-00001-of-00003.gguf` (3 shards, 64 GiB on disk) |
| Model reported | 117,561,977,600 params, 68,585,600,000 bytes, ftype `Q4_K - Small`, `n_ctx` 32768 |

`--list-devices` reported `CUDA0: NVIDIA GeForce RTX 4090 (24563 MiB, 22988 MiB free)`.

## Operator command

Set via `setx APOAPSIS_LLAMA_SERVER_COMMAND`:

```
wsl.exe -d Ubuntu-24.04 -- /home/arya/llama.cpp/build/bin/llama-server
  -m /home/arya/models/laguna-q4s/UD-Q4_K_S/Laguna-S-2.1-UD-Q4_K_S-00001-of-00003.gguf
  --alias Laguna-S-2.1-UD-Q4_K_S
  --parallel 1 --ctx-size 32768 --flash-attn on
  --cache-type-k q8_0 --cache-type-v q8_0
  --fit on --fit-target 512 --load-mode none
  --jinja --reasoning off --reasoning-budget 0 --reasoning-format none
  --threads 16 --host 127.0.0.1 --port 8000
```

Two deviations from the owner's originally supplied command, both deliberate
and both recorded here rather than silently applied:

1. **`-m` points at shard `00001-of-00003`.** llama.cpp discovers the
   remaining shards itself. The originally supplied path
   (`/home/arya/models/laguna-s-2.1/IQ4_XS/laguna-s-2.1-IQ4_XS.gguf`) does not
   exist on this machine; no IQ4_XS quant is present. No model was downloaded.
2. **`--alias Laguna-S-2.1-UD-Q4_K_S` added.** Without it, the `id` returned by
   `/v1/models` is not guaranteed to equal `models.local_coder.model` in
   `.apoapsis/config.toml`. With it, the two match exactly, which is what makes
   the warm request and all later requests address the right model by name.

## Measured startup

| Event | Time (UTC) |
| --- | --- |
| `_launch_openai_compatible_service` spawned the command | 05:44:17 |
| `/v1/models` answered; warm request completed; lifecycle wrote its result | 05:49:46 |
| **Total** | **5 min 29 s** |

Resident set of `llama-server` inside WSL over the load, sampled:

| Elapsed | RSS | GPU used |
| --- | --- | --- |
| 02:03 | 26.1 GB | 22646 MiB |
| 03:29 | 35.3 GB | — |
| 04:44 | 46.3 GB | — |

Memory behaved as the owner described: weights split between VRAM (~22.6 GiB
on the 4090) and system RAM, with `available` never approaching exhaustion.

`.apoapsis/runtime/last-model-lifecycle.json` after the run:

```json
{
  "action": "start",
  "models": [
    {
      "base_url": "http://127.0.0.1:8000/v1",
      "context_window_tokens": 32768,
      "model": "Laguna-S-2.1-UD-Q4_K_S",
      "provider": "openai_compatible",
      "roles": ["frontier", "local_coder"],
      "status": "ready"
    }
  ],
  "recorded_at": "2026-07-26T05:49:46.585503+00:00",
  "research_included": false,
  "service_launched": true,
  "service_pids": [41704]
}
```

## Inference sanity check

```
POST /v1/chat/completions  model=Laguna-S-2.1-UD-Q4_K_S  max_tokens=16  temperature=0
prompt: "Reply with exactly: OK"
```

Response content `OK`, `finish_reason` `stop`. Server-reported timings:

- `predicted_per_second`: **15.12 tok/s**
- `prompt_per_second`: 8.89 tok/s
- `cached_tokens`: 33 of 48 prompt tokens

15 tok/s matches the owner's prior expectation for this configuration.

## Defects found and fixed during this pass

Every one of these passed `tests/test_launcher.py` beforehand, because that
module asserts only that the launcher's *source text* contains certain
substrings. None of its tests execute the argument path or the llama-server
path. Passing meant "the file still mentions the right words."

1. **`%*` is unaffected by `SHIFT` (`START_APOAPSIS.cmd`).** The script did
   `set "APOAPSIS_LIFECYCLE_ARGS=%*"`, then `shift /1`, then re-read `%*`
   expecting the project folder to be gone. `SHIFT` renumbers `%1`/`%2` but
   leaves `%*` at the original full argument string, so passing a folder
   produced `... --project-root "C:\repo" C:\repo` and argparse rejected it.
   The folder-picker path worked; the documented argument path did not.
   Fixed by rebuilding the remaining arguments in an explicit loop.

2. **`shlex.split(command, posix=False)` corrupted quoted Windows commands.**
   On Windows the tokens retain their own quote characters and
   `subprocess.list2cmdline` then escapes them again, so
   `wsl.exe -d Ubuntu -- bash -lc "llama-server ..."` reached `CreateProcess`
   as `bash -lc "\"llama-server ...\""`. Fixed by handing the operator's
   command line to `CreateProcess` verbatim on Windows, which is the quoting
   convention the operator actually typed against.

3. **`service_wait_seconds` defaulted to 30 s and was not exposed on the CLI.**
   Measured cold start here was 5 min 29 s. The default is now 300 s and
   `--service-wait-seconds` exists. Without this fix this run could not have
   succeeded at all.

4. **The warm request sent `content: ""`.** Ollama's warm idiom is
   `/api/generate` with an empty prompt; OpenAI-compatible servers have no
   equivalent and chat templates may reject empty user content. Now sends one
   real token.

5. **`STOP_APOAPSIS.cmd` read the wrong project.** It passed
   `--project-root "%APOAPSIS_ROOT%."` — the Apoapsis install directory, not
   the project `START_APOAPSIS.cmd` opened. It now takes a project argument or
   prompts for one, symmetrically with START.

6. **Launched-service output was discarded entirely.** `.apoapsis/runtime/llama-server.log`
   was 0 bytes. Measured cause: `subprocess.Popen(..., creationflags=DETACHED_PROCESS)`
   breaks `wsl.exe`'s output relay. Probe results on this machine, launching
   `wsl.exe -d Ubuntu-24.04 -- bash -lc "echo STDOUT_OK; echo STDERR_OK 1>&2"`:

   | creationflags | captured |
   | --- | --- |
   | `NEW_PROCESS_GROUP \| DETACHED_PROCESS \| NO_WINDOW` | *(nothing)* |
   | `NEW_PROCESS_GROUP \| NO_WINDOW` | `STDERR_OK` |
   | `NEW_PROCESS_GROUP` | `STDERR_OK` |

   stdout does not survive the `wsl.exe` relay under any of these; stderr does,
   once `DETACHED_PROCESS` is dropped. llama.cpp logs to stderr, so dropping
   `DETACHED_PROCESS` recovers the diagnostic channel. The log now also records
   a timestamped header and the exact command line before spawning, and a
   readiness timeout now names the log path in its error message.

   Residual limitation: llama.cpp's stderr appears block-buffered through the
   relay, so the log went quiet mid-load and only the first ~1.3 KB was visible
   while loading. It is evidence, not a live progress indicator.

## Blockers found (not yet fixed)

1. **`apoapsis doctor` reports `overall_status: error` for a keyless local
   server.** `credential:APOAPSIS_LOCAL_CODER_API_KEY` is `status: error`
   because the variable is unset — but `llama-server` was started with no API
   key and requires none. A loopback OpenAI-compatible endpoint should not
   demand a credential. Compounding this, `START_APOAPSIS.cmd` never runs
   `doctor`, so it starts a 68 GB model and opens the UI before the operator
   can learn the project is in an error state. This is precisely the "feels
   buggy" complaint in the handoff.

2. **Context budget exceeds the coding context window.**
   `context_limits` is a warning: estimated ~45,000 tokens against the smallest
   configured coding window of 32,768. Left unaddressed, a real task risks
   failing on context rather than on capability, which would make any Local
   Power reliability measurement (handoff item 6) meaningless.

3. **Two pricing warnings** (`hosted_pricing:frontier`, `hosted_pricing:local_coder`)
   fire because `provider = "openai_compatible"` is treated as hosted. For a
   loopback endpoint the real rate genuinely is zero, so the warning is
   correct in letter and misleading in spirit.

4. **`ripgrep` not on PATH** — warning only; deterministic lexical fallback is used.

## Process ownership: deliberately not implemented

Handoff item 4 asked for process ownership "only if lifecycle can prove it
started that exact process." It cannot, on this path. The PID recorded in
`service_pids` (41704) is the PID of `wsl.exe` on the Windows side. The actual
`llama-server` is PID 287 inside the `Ubuntu-24.04` namespace. There is no
supported way to prove those correspond from the Windows side, so any stop
implementation would be guessing — and guessing here means terminating an
unrelated process listening on port 8000.

`stop_local_models` therefore now *reports* loopback OpenAI-compatible
endpoints under `unmanaged_local_endpoints` with status
`running_not_managed_by_apoapsis` and tells the operator to stop it where they
started it. It kills nothing. This is a deliberate refusal, not an omission.

## End-to-end task run (handoff item 2)

Run against an isolated scratch repository (`C:\Users\aryam\apoapsis-live-test`,
`git init` + `apoapsis init`, containing `calc.py` with `add()` and one passing
test) rather than the harness repo itself, to honour "preserve user work".
`apoapsis init` inherited the llama-server model configuration unchanged.

```
apoapsis run --yes --agent-route local_only --research off
  "Add a subtract(a, b) function to calc.py that returns a minus b,
   and add a unit test for it in tests/test_calc.py"
```

Task `TASK-EF33C00E5BD4`. Result:

| Field | Value |
| --- | --- |
| `outcome` | `human_review_required` |
| `agent_stop_reason` | agent turn budget exhausted after 20 turns; automatic final deterministic verification did not pass |
| `agent_turns` | 20 / 20 |
| `agent_patch_attempts` | 13 |
| `agent_verification_runs` | 1 |
| `rejected_tool_requests` | 16 |
| `number_of_calls` | 21 |
| `input_tokens` / `output_tokens` | 96,866 / 2,393 |
| `latency_seconds` | 859.3 (14 min 19 s) |
| `escalation_triggered` | true |

**The harness behaved correctly.** Worktree isolation held
(`.apoapsis/worktrees/ef33c00e5bd4` on branch `apoapsis/ef33c00e5bd4`; the
main checkout was never touched). The authority boundary held: 16 model tool
requests were rejected. Most importantly, Apoapsis **refused to claim
completion** — the configured verification command passed, but it passed
having run only the pre-existing `test_add`, and the harness escalated to
`human_review_required` rather than reporting success.

The model produced a correct implementation:

```diff
+def subtract(a, b):
+    return a - b
+
+
 def add(a, b):
     return a + b
```

**Laguna failed the second half of the task.** It never successfully wrote the
test into `tests/test_calc.py`, burning 13 patch attempts and all 20 turns to
land one four-line function. That is a model capability/looping result, not an
Apoapsis defect, and it is the first real data point for handoff item 6.

### New defect found by this run

`files_changed` reports:

```
__pycache__/calc.cpython-314.pyc, calc.py, tests/__pycache__/test_calc.cpython-314.pyc
```

Compiled bytecode is being counted as task output. Two of the three "files
changed" by this task are `.pyc` artifacts created as a side effect of the
verification command. This corrupts the change surface a reviewer sees, and it
is the same class of generated clutter the handoff audit flagged as item 6.
The scratch repo's `.gitignore` did not cover `__pycache__`; `apoapsis init`
wrote a `.gitignore` but evidently not one that excludes it from this report.

### Suspicious ratio worth investigating

13 patch attempts against **1** verification run. The agent loop spent almost
its entire budget failing to produce an applicable patch and only reached
verification once. If patch application is failing for a mechanical reason
(diff format, path resolution) rather than a reasoning reason, that would be
an Apoapsis defect masquerading as a model-quality result, and it would
invalidate any reliability measurement built on top of it. This should be
diagnosed from the `call-*-response.json` artifacts before handoff item 6 is
attempted.

## What is still unproven

- No `START_APOAPSIS.cmd` double-click run was performed end to end; the
  lifecycle was invoked directly with the same arguments the launcher passes,
  and the task was submitted through `apoapsis run` rather than the browser UI.
  The UI's Local Power toggle path is therefore still unexercised live.
- The folder-picker path was not re-exercised after the argument-handling fix.
- `STOP_APOAPSIS.cmd` was not run after its project-selection fix.
- Handoff item 6 (Local Power reliability) has exactly one sample, and that
  sample is not trustworthy until the 13-attempts-to-1-verification ratio above
  is explained.

## Verification run

`python -m unittest tests.test_operator_lifecycle tests.test_launcher` — 26
tests, OK, on Python 3.14.5. Note that these tests would also have passed
before any of the six fixes above; they are not what established this evidence.

## Correction recorded

During this session the run was killed once at ~2.5 minutes on a mistaken
conclusion that a 64 GiB model could not load into 58 GiB of RAM. That analysis
cited Linux `free` as near zero, which is normal and not a scarcity signal;
`available` was 29 GiB and healthy, and GPU offload to the 4090 meant only
~41 GiB was ever destined for system RAM. The owner's account was correct and
the kill was unnecessary. Recorded so the mistake is not repeated by whoever
reads this next.
