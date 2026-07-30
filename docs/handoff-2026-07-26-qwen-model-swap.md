# Handoff: swapping the local coder to Qwen 3.6 27B / 35B

**Date:** 2026-07-26
**Author:** Claude (Cowork session)
**Audience:** whoever performs the swap — assumes no prior context from the session that produced this
**Status:** ready to execute; nothing in this document has been run

---

## 1. Why this swap is being considered

Not preference. Measured failure.

The current local coder is `Laguna-S-2.1-UD-Q4_K_S`: **117.5B parameters, 68.6 GB
of weights, 64 GiB on disk across 3 GGUF shards**, served against a single
**RTX 4090 with 24,563 MiB VRAM**. The weights cannot fit in VRAM. Most of the
model runs on CPU out of the 58 GiB visible to WSL.

Observed on task `TASK-B0147B765CD5D95D07BEEA6A` (project `test project 4`,
slice SLICE-001):

| Measurement | Value | Source |
| --- | --- | --- |
| Prompt eval throughput | **1.21 tok/s** | `.apoapsis/runtime/llama-server.log` |
| Token generation | ~16.6 tok/s | same |
| Slowest single agent call | **577.9 s** | `call-011-telemetry.json` |
| Cold model load | ~5.5 min | `docs/evaluation/laguna-llama-server-live-startup-2026-07-26.md` |
| Turns to produce a broken 88-line file | 8 | `local-power-session.json` |

The decisive event: on turn 11 the model was asked to repair `src/app.py`. It
emitted the literal string `"""Application bootstrap and lifecycle management."""`
repeatedly for **41,626 characters** until it hit the 8,192-token output ceiling,
producing unterminated JSON that the action parser rejected. That is a
degenerate repetition loop — a well-known failure mode of aggressive quantization
on long structured output — and it burned ten minutes of wall clock to produce
nothing.

A 27B model at Q4_K_M is roughly **16–17 GB**, which fits in 24 GB VRAM alongside
a 32k KV cache. Expect prompt eval in the high hundreds of tok/s rather than 1.2:
a **two-to-three order of magnitude** difference on the phase that dominates
agent-loop latency. The smaller model may reason less well per token. It will
produce vastly more tokens per minute and is far less prone to the repetition
collapse documented above.

**Do not treat this as settled.** Section 8 defines how to actually decide.

---

## 2. How model serving works here (read before changing anything)

Three facts govern everything below.

**The harness never downloads models.** Per ADR 0062, Apoapsis launches only the
explicit command an operator supplies. There is no model registry, no auto-pull,
no fallback. If the command is wrong, startup fails loudly rather than
substituting something.

**One environment variable is the entire launch contract.**
`APOAPSIS_LLAMA_SERVER_COMMAND` holds a verbatim Windows command line.
`_launch_openai_compatible_service` in `src/apoapsis/operator_lifecycle.py`
hands it to `CreateProcess` **without re-quoting** on Windows — deliberately,
because splitting and re-quoting corrupts any command containing quotes (notably
`wsl.exe -d Ubuntu -- bash -lc "..."`). Windows native quoting rules are what you
are writing against.

**The model name must match in two places or requests silently address the wrong
thing.** `--alias` on the llama-server command sets the `id` returned by
`/v1/models`. That `id` must equal `models.local_coder.model` (and
`models.frontier.model`) in the project's `.apoapsis/config.toml`. ADR 0060 and
the startup evaluation both call this out; it was already the cause of one
incident.

Current live topology:

```
Windows                          WSL2 (Ubuntu-24.04)
-------                          -------------------
apoapsis UI (py 3.14) :7331  ->  llama-server :8000  (via wslrelay)
                                 /home/arya/llama.cpp/build/bin/llama-server
ollama :11434 (local_research, qwen3.6:27b) — separate, untouched by this swap
```

Note `models.local_research` in `test project 4` already points at
`qwen3.6:27b` **via Ollama**, not llama.cpp. That is a different serving path and
a different role. Do not confuse the two. This handoff changes the *coder*, which
is served by llama-server on port 8000.

---

## 3. Chosen approach: same port, swap the launch command

One model resident at a time on `127.0.0.1:8000`. Swapping means: stop
llama-server, change `APOAPSIS_LLAMA_SERVER_COMMAND`, change the model name in
`config.toml`, start again.

**Why not run both simultaneously on two ports.** It was considered and rejected
on arithmetic. A 27B Q4_K_M (~17 GB) plus a 32k KV cache leaves roughly 5–6 GB of
a 24 GB card. Laguna already cannot fit and is spilling to CPU; co-residency
would push it further onto the CPU and make *both* models slower. Two ports is
the right design on a 48 GB card, not this one.

The cost of this choice is honest: a swap is not instant. It costs a
llama-server restart — seconds for a 27B (it fits in VRAM and loads from page
cache), versus the ~5.5 minutes Laguna needs. In practice, swapping *to* Qwen is
fast and swapping *back* to Laguna is slow.

---

## 4. Getting the GGUF (you run these)

Nothing here is executed on your behalf, consistent with ADR 0062.

### 4.1 Pick a quant

For a 24 GB 4090, targeting 32k context:

| Model | Quant | Approx. weights | Fits 24 GB w/ 32k KV? | Notes |
| --- | --- | --- | --- | --- |
| Qwen 3.6 27B | Q4_K_M | ~16.5 GB | **Yes**, comfortably | Recommended starting point |
| Qwen 3.6 27B | Q5_K_M | ~19.2 GB | Yes, tight | Better fidelity; less KV headroom |
| Qwen 3.6 27B | Q6_K | ~22.3 GB | Marginal | Likely forces KV to CPU; not worth it |
| Qwen 3.6 35B | Q4_K_M | ~21 GB | Marginal | Needs q8_0 KV cache and possibly <32k ctx |
| Qwen 3.6 35B | Q4_K_S | ~19 GB | Yes, tight | The realistic 35B option on this card |

KV cache at 32k with `q8_0` for both K and V is roughly 1.5–2.5 GB depending on
the model's head configuration — the existing Laguna command already uses
`--cache-type-k q8_0 --cache-type-v q8_0`, so keep that.

**Start with 27B Q4_K_M.** It is the only row above with real headroom, which
means you are measuring the model rather than measuring VRAM thrash. Move to 35B
Q4_K_S only if 27B is capable-but-slow-to-converge, which would be a quality
signal rather than a speed one.

### 4.2 Download

From inside WSL, matching the existing layout under `/home/arya/models/`:

```bash
# In WSL: wsl.exe -d Ubuntu-24.04
python3 -m pip install --user "huggingface_hub[cli]"

mkdir -p /home/arya/models/qwen3.6-27b

# Replace the repo id with the actual GGUF repo you intend to use --
# verify the exact repo and filename on Hugging Face first. Do not guess:
# a wrong-but-plausible filename is the single most common cause of a
# silent-looking startup failure here.
hf download <org>/<Qwen3.6-27B-GGUF> \
  --include "*Q4_K_M*.gguf" \
  --local-dir /home/arya/models/qwen3.6-27b
```

Then confirm what actually landed, because sharded downloads and single-file
downloads need different `-m` arguments:

```bash
ls -la /home/arya/models/qwen3.6-27b/
```

- **Single file** → point `-m` at it directly.
- **Sharded** (`...-00001-of-0000N.gguf`) → point `-m` at shard `00001` only.
  llama.cpp discovers the rest. This is exactly what the Laguna command does.

### 4.3 Confirm the tokenizer/template loads cleanly

Before wiring it into Apoapsis, run llama-server by hand once and read the log.
Watch specifically for `special_eos_id is not in special_eog_ids` style warnings
— the Laguna log emits those, and a model whose EOG tokens are misconfigured is a
model that will not reliably stop generating. Given that runaway generation is
the failure you are trying to escape, this check is not optional.

---

## 5. The swap procedure

### 5.1 Stop the current server

`STOP_APOAPSIS.cmd` releases *configured* model memory but is aimed at the
project's models. To stop llama-server specifically, kill it inside WSL — the
Windows-side PID is `wsl.exe`'s, not llama-server's (`operator_lifecycle.py`
documents this: it cannot see through the WSL process boundary, which is why
`service_pids` is empty in `last-model-lifecycle.json`).

```bash
# In WSL
pkill -f 'llama-server.*--port 8000'
```

Verify from Windows that the port is clear:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
```

### 5.2 Set the launch command

Windows, permanent (`setx` writes to the user environment; **open a new shell
afterwards** — `setx` does not affect the current one):

```
setx APOAPSIS_LLAMA_SERVER_COMMAND "wsl.exe -d Ubuntu-24.04 -- /home/arya/llama.cpp/build/bin/llama-server -m /home/arya/models/qwen3.6-27b/<ACTUAL-FILENAME>.gguf --alias qwen3.6-27b --parallel 1 --ctx-size 32768 --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 --n-gpu-layers 999 --jinja --threads 16 --host 127.0.0.1 --port 8000"
```

Differences from the Laguna command, and why:

| Flag | Laguna | Qwen | Reason |
| --- | --- | --- | --- |
| `--n-gpu-layers` | absent (used `--fit on --fit-target 512`) | `999` | The whole point: force every layer onto the GPU. `--fit` exists to cope with a model that *cannot* fit; a 27B Q4 can, so say so explicitly. |
| `--load-mode` | `none` | omitted | Was a Laguna-specific accommodation for a 64 GiB load. |
| `--reasoning off --reasoning-budget 0 --reasoning-format none` | present | **decide deliberately** | See below. |
| `--alias` | `Laguna-S-2.1-UD-Q4_K_S` | `qwen3.6-27b` | Must match `config.toml`. |

**On the reasoning flags.** The Laguna command disables reasoning output
entirely. Qwen 3.x models are typically hybrid-reasoning, and the project config
sets `think = false` for the coder. Keep reasoning off for the coder: the local
power loop expects one JSON action per turn, and reasoning tokens leaking into
the content stream is precisely the class of bug ADR 0058 exists about (there is
already `llama_cpp` tool-residue stripping in `parse_power_action` for this).
If the Qwen chat template requires the flags to be spelled differently, the
`--jinja` template will tell you — read the startup log.

### 5.3 Update the project config

In `C:\Users\aryam\coding stuff\test project 4\.apoapsis\config.toml`, change the
model name in **both** blocks that point at port 8000:

```toml
[models.frontier]
model = "qwen3.6-27b"     # was Laguna-S-2.1-UD-Q4_K_S

[models.local_coder]
model = "qwen3.6-27b"     # was Laguna-S-2.1-UD-Q4_K_S
```

Leave `base_url`, `context_window_tokens = 32768`, `max_output_tokens = 8192`,
`temperature = 0.0` alone for the first run — you want one variable changing.

Leave `[models.local_research]` (Ollama, `qwen3.6:27b`) untouched. Different
role, different server.

### 5.4 Start and verify

```powershell
# Confirm the alias the server actually advertises
(Invoke-WebRequest -Uri "http://127.0.0.1:8000/v1/models" -UseBasicParsing).Content
```

The `id` in that response **must** exactly equal the `model` value you wrote in
`config.toml`. If it does not, fix `--alias` — do not fix it by changing
`config.toml` to match a wrong alias, because `.apoapsis/runtime/last-model-lifecycle.json`
and every telemetry record will then disagree with the plan.

Then `apoapsis doctor --project-root "C:\Users\aryam\coding stuff\test project 4"`.

### 5.5 Restart the UI — this bites

The Apoapsis UI is a long-lived Python process that imports its modules once at
startup. **It does not notice edited source or, in some paths, edited config.**
During the session that produced this handoff, three consecutive review actions
failed against already-fixed code purely because the UI process predated the fix.

After any swap:

```powershell
# find it
Get-CimInstance Win32_Process -Filter "name like '%python%'" |
  Select-Object ProcessId, CommandLine | Format-List
# stop the apoapsis.cli.app ... ui process, then:
cd "C:\Users\aryam\local harness"
$env:PYTHONPATH="C:\Users\aryam\local harness\src"; $env:PYTHONUTF8="1"
Start-Process python -ArgumentList '-m','apoapsis.cli.app','--project-root','"C:\Users\aryam\coding stuff\test project 4"','ui' -NoNewWindow
```

Note the nested quoting on the project-root argument — PowerShell's
`-ArgumentList` will otherwise split on the spaces in `coding stuff\test project 4`
and argparse will reject `stuff\test` as an invalid subcommand. This was hit and
resolved during the session.

---

## 6. Keeping it hot-swappable

Store both commands so a swap is a copy-paste, not an act of recall. Suggested:
`docs/model-commands.md` in this repo, or a pair of `.cmd` files next to
`START_APOAPSIS.cmd`.

```
# LAGUNA (117B, CPU-bound, ~5.5 min cold load)
wsl.exe -d Ubuntu-24.04 -- /home/arya/llama.cpp/build/bin/llama-server -m /home/arya/models/laguna-q4s/UD-Q4_K_S/Laguna-S-2.1-UD-Q4_K_S-00001-of-00003.gguf --alias Laguna-S-2.1-UD-Q4_K_S --parallel 1 --ctx-size 32768 --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 --fit on --fit-target 512 --load-mode none --jinja --reasoning off --reasoning-budget 0 --reasoning-format none --threads 16 --host 127.0.0.1 --port 8000
config.toml model = "Laguna-S-2.1-UD-Q4_K_S"

# QWEN 27B (fits VRAM, fast load)
wsl.exe -d Ubuntu-24.04 -- /home/arya/llama.cpp/build/bin/llama-server -m /home/arya/models/qwen3.6-27b/<FILE>.gguf --alias qwen3.6-27b --parallel 1 --ctx-size 32768 --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 --n-gpu-layers 999 --jinja --threads 16 --host 127.0.0.1 --port 8000
config.toml model = "qwen3.6-27b"
```

A swap is then exactly four steps: `pkill` → `setx` (new shell) → edit two
`model =` lines → restart UI.

**If you later want true zero-restart swapping**, the clean version is a
`[models.profiles.*]` table in `config.toml` plus a `apoapsis model use <name>`
subcommand that rewrites the active block and bounces llama-server. That is a
real feature with real tests, not a config trick, and it should be its own ADR.
Do not bolt it on informally.

---

## 7. What might break, ranked by likelihood

1. **Alias/config mismatch** (most likely). Symptom: requests fail or address a
   model you did not intend. Check `/v1/models` first, always.
2. **`setx` not picked up.** The variable exists but the running shell — and any
   process launched from it, including the UI — still has the old value. Open a
   new shell. Verify with `$env:APOAPSIS_LLAMA_SERVER_COMMAND`.
3. **Stale UI process.** Covered in 5.5. It cost three failed operations in one
   session; assume it will cost you one.
4. **Wrong shard argument.** Pointing `-m` at shard 2 of N, or at a directory.
   Log will say so plainly.
5. **Chat template mismatch under `--jinja`.** Qwen templates differ from
   Laguna's. If actions come back malformed in a *new* way (as opposed to the
   repetition loop), suspect this before suspecting the model's competence.
6. **Reasoning tokens in content.** If `<think>` blocks appear inside the JSON
   action, the reasoning flags are wrong for this template.

---

## 8. How to judge whether the swap actually helped

Do not judge on feel. There is a ready-made benchmark sitting in the repo right
now: **`TASK-B0147B765CD5D95D07BEEA6A` is a task Laguna demonstrably failed.**

The task's `src/app.py` has exactly four defects, and `tests/test_app.py`
(10 tests) defines done precisely:

1. Unterminated module docstring — `"""..."` closed with one quote. The file does
   not parse.
2. Imports `Config` from `src.config`, which defines `AppConfig`.
3. Calls `self._config_loader.load(config_path)`; `ConfigLoader.load()` takes no
   argument.
4. Calls `configure_logging(self._config.logging)`; `AppConfig` has no `.logging`
   and `configure_logging` wants a level string.

`tests/test_config.py` and `tests/test_logging.py` (33 tests) already pass, so a
regression there is a clear signal too.

**Procedure.** Swap to Qwen, authorize a `local_continuation`, and record:

| Metric | Laguna baseline | Where to read it |
| --- | --- | --- |
| Prompt eval tok/s | 1.21 | `runtime/llama-server.log` |
| Worst single call latency | 577.9 s | `call-NNN-telemetry.json` |
| Invalid/unparseable actions | 1 in 11 turns | `local-power-turn-*.json`, `accepted=false` |
| Defects repaired | 0 of 4 | `python -m unittest discover -s tests` in the worktree |
| Verification passed | no | `report.json` |

If Qwen 27B repairs all four and turns the suite green, the swap is settled.
If it repairs two or three, that is still a large improvement over zero and
argues for 35B or for a frontier escalation on the remainder. If it also
collapses into repetition, the problem is not model size and you should look at
the prompt and at `max_output_tokens` instead.

**A caveat worth stating.** The worktree now contains a `tests/` directory that
did not exist during Laguna's original 8-turn run, and the task specification now
carries test obligations it previously dropped. The comparison is therefore not
perfectly like-for-like — Qwen is being given a better-specified problem. That
mainly threatens the "defects repaired" row; the throughput and
invalid-action rows remain directly comparable, since they measure the model and
the server rather than the task framing.

---

## 9. Open state at the time of writing

- A `local_continuation` (`RVOP-A5A6ACD5AF2849CFAC288FDB`) was running on Laguna
  when this was written — 11 turns consumed of an 18-turn ceiling, turn 12 in
  flight. Its outcome is not yet known and should be read from
  `.apoapsis/tasks/TASK-B0147B765CD5D95D07BEEA6A/report.json` before starting a
  swap. **Do not swap models mid-continuation**; let it stop first, or the
  session record will straddle two models and the comparison in section 8 is
  ruined.
- Related harness changes from the same session — `LocalPowerSession.resume`,
  local-power session discovery in review, and the architect's
  `MISSING_ACCEPTANCE_CRITERIA` / `MISSING_TEST_OBLIGATIONS` validation —
  are **uncommitted**. Full suite: 940 tests, 9 failures, all 9 reproducing at
  HEAD (pre-existing).
- `test project 4` has a stuck `.git/index.lock`. Clear it before relying on any
  Git-dependent slice machinery.
