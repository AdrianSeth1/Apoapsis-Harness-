# ADR 0115: Stop actually releases model memory

## Status

Accepted and implemented on 2026-08-03. Verified live: 17.3 GB of VRAM freed.

## Context

`STOP_APOAPSIS.cmd` printed "Apoapsis model memory has been released" and left
the largest consumer running.

The two configured local providers free memory in completely different ways.
Ollama unloads a model on request (`keep_alive: 0`) and keeps its service
alive, which `stop_local_models` did correctly. A llama-server holds its
weights until the process exits — there is no unload request — and
`stop_local_models` deliberately refused to touch it, with this reasoning:

> Apoapsis launches them through an operator-supplied command line that may
> cross a process boundary it cannot see through — `wsl.exe ...` yields the PID
> of wsl.exe, not of llama-server inside the distribution. Without proof that a
> specific PID is that exact server, terminating anything would mean guessing,
> and guessing here means killing a stranger's process on port 8000.

That reasoning is correct and worth keeping. Its conclusion was too broad.

Observed on the owner's machine while diagnosing exactly this complaint: an
RTX 4090 at **19,924 MiB of 24,564 MiB used, 4,215 MiB free**, held by

```
/home/arya/llama.cpp/build/bin/llama-server
  -m /home/arya/models/qwen3.6-27b/Qwen3.6-27B-Q4_K_M.gguf
  --n-gpu-layers 999 --ctx-size 32768 ... --port 8000
```

running inside WSL. It was invisible to Task Manager, to `ollama ps`, to
`lms ps`, and to per-process attribution in Windows `nvidia-smi` — so the
operator could see that 20 GB was gone and had no way to learn what held it.
The only remedy was to find and kill, by hand and inside a WSL shell, a process
Apoapsis had started for them.

## Decision

**Stop releases the loopback model server too, and proves its identity before
signalling anything.**

The missing ingredient was never force, it was identity — and the server
supplies it. Three facts, each checked rather than assumed:

1. the endpoint is one this project is **configured** to use, and it is
   loopback;
2. that server, asked directly, reports the model file it currently holds
   (`GET /props` → `model_path`);
3. the process signalled is the one whose **own command line** names that exact
   file.

Killing by *port* remains refused, exactly as the original comment demanded.
What is stopped is the process demonstrably serving the model file the
configured endpoint just named. Two copies of the same weights cannot both be
resident, so a process whose command line names this model *is* the one serving
it; a llama-server holding different weights is never matched.

This is the same discipline `workcell/product.py` has applied since ADR 0095
before a sandbox run, through the same shell tool
(`tools/resident_model_server.sh`). The capability existed and was trusted; the
shutdown path simply never used it. `workcell/resident_server.py` now holds
that logic as one implementation with two callers.

**Every refusal states its reason, on the record.** An endpoint that reports no
model file, a model file no local process names (a server on another machine),
a tool that cannot run — each returns a `StoppedServer` carrying why, which the
summary note reports. Silence is never allowed to look like success.

**`--keep-loopback-servers` opts out**, for the case where something else is
deliberately sharing that server. The note then says the weights still occupy
VRAM, rather than repeating "memory has been released".

**Releasing may never break the shutdown.** The call is wrapped: a failure to
free VRAM must not stop the rest of the shutdown from completing and being
reported.

## Consequences

- Clicking stop frees the GPU. Measured on the reported machine: **19,924 MiB →
  2,653 MiB used; 4,215 MiB → 21,486 MiB free — 17.3 GB returned**, with
  `pgrep llama-server` confirming the process gone.
- The claim in the summary note is now true, and when it is not true it says
  which endpoint and why.
- Starting Apoapsis again reloads the model, which is the intended cycle. The
  controller-image warm from ADR 0113 happens in the same start.
- **Host RAM is reported, because Apoapsis cannot reclaim it.** The server's
  own ~1.3 GB resident set goes with the process, and inside the distribution
  the memory is genuinely free. Windows still attributes ~1.9 GB to
  `vmmemWSL`, for two reasons worth naming rather than leaving as a mystery:
  reading a 16.8 GB GGUF fills the page cache (measured after the stop: 1,702
  MB used against **1,949 MB buff/cache**), and WSL2 only hands freed pages
  back to Windows when `autoMemoryReclaim` is configured — which the reported
  machine's `.wslconfig` did not set.

  Apoapsis can fix neither. Dropping caches needs root inside the
  distribution, and `sudo` there requires a password (verified: `id -u` is
  1000, `sudo -n` refused). `wsl --shutdown` reclaims everything at the cost
  of stopping Docker Desktop's backend and any other distribution work.

  So the stop result carries a `host_memory` block with what the distribution
  actually holds, whether `autoMemoryReclaim` is set, and the one-line remedy
  (`autoMemoryReclaim=gradual` under `[wsl2]`). An operator who frees 17 GB of
  VRAM and sees Task Manager barely move otherwise concludes the stop did
  nothing.

## Alternatives rejected

**Kill whatever listens on the configured port.** One line shorter and the
thing the original comment correctly refused. A configured port is not proof of
identity, and this runs on a developer machine where port 8000 is popular.

**Track the PID at launch instead.** It is the obvious answer and it does not
work here: Apoapsis starts the server through an operator-supplied command line
that usually crosses `wsl.exe`, so the PID it can observe belongs to the bridge
process, not the server. Asking the server what it is holding works regardless
of how it was started — including for a server the operator started themselves,
which is the common case.

**Have `doctor` report the resident server instead of stopping it.** Worth
doing as well, and it does not solve the complaint: the operator clicked the
button named stop.
