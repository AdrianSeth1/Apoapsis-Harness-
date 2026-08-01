# ADR 0091: Separate live-pilot authorization and operator launch

## Status

Accepted, 2026-08-01. Implementation prepared; no live arm has run.

## Context

The v8 zero-model rehearsal reached `PASS_LIVE_PREFLIGHT_AUTHORIZED`, but its
manifest and lock intentionally cannot authorize inference. The rehearsal
driver also always constructs a scripted provider. Repointing that driver at
`llama-server` would make new, unbound code decide a measured result and would
erase the distinction between validating the measurement system and spending
the six experimental arms.

## Decision

Live execution uses a separate authorization document. It binds the unchanged
v8 manifest and lock, the exact passing rehearsal report, a committed live
runner and every live module, and a provenance-labelled controller image built
from that commit. The old lock remains false for live inference and is not
edited or reinterpreted.

The operator must also pass the literal acknowledgement
`I-AUTHORIZE-SIX-LOCAL-INFERENCE-ARMS`. The supplied WSL launcher is the only
supported entry point. It selects the controller by immutable image id, mounts
only the Docker socket, the committed seed, durable evidence, the bound
qualification/evaluation inputs, and the pinned read-only model/server files,
and gives the controller host networking. The docs mount is data, not an
executable-source override; the runner modules remain the bytes baked into the
immutable image. Each agent workcell remains `--network none`; its only egress
remains the controller-owned Unix-socket relay.

Immediately before spend, the runner rehashes the GGUF, launcher, and complete
llama/ggml dependency closure, refuses an already-running server, and reobserves
the real CLI tool surface and all containment probes with a scripted provider.
It then runs exactly the frozen six-slot order. Every slot starts cold, makes
the declared one-token readiness request, runs from a fresh seed and Qwen home,
and stops the server afterward. The control gets Qwen Code's own uninterrupted
loop. The sandbox checkpoints the first proposal and may issue only the
controller-produced repair packet through Qwen's native `--continue`, within
the frozen two-continuation ceiling. First-proposal and final-checkpoint records
remain separate.

The live runner does not declare the pilot a success. It retains raw results as
`six_slots_complete_pending_independent_scoring`; proposal quality and harness
detection are scored separately after the owner returns the artifact.

## Consequences

No model starts while preparing or validating this change. A missing or altered
authorization, module, rehearsal, runtime file, live preflight, slot, telemetry
record, or operator acknowledgement fails closed. The v8 rehearsal remains
historical evidence for the zero-model system rather than being rewritten as a
live authorization.

## Observed verification

Focused runner coverage passed 14/14 and the final launcher-focused coverage
passed 6/6 on CPython 3.12. The canonical native-ext4 full suite at launcher
commit `1217243` passed 1,943 tests with 13 skips in 343.721 seconds. Earlier
invalid attempts are not evidence: one imported a
previously installed package, and one found the Linux `rg` binary through its
read-only Windows mount and failed before normal workflows; both causes were
corrected before the recorded run. A first final-tree suite attempt hit a
known-timing-shaped relay-test race; the exact test immediately passed alone,
and the complete clean rerun above is the recorded result. `compileall` and diff checking are recorded
with the final authorization commit. No `llama-server` process was started and
no inference occurred.

## First operator preflight correction

The first operator launch on 2026-08-01 refused before inference. Running the
controller inside a host-networked container exposed two assumptions that the
native-WSL rehearsal had not exercised. A fake provider served by the
controller process must be addressed through controller loopback: resolving
the container hostname produced Docker Desktop's host-gateway address, so the
controller-owned relay never reached the fake provider. Preflight scratch also
lived in the controller's private `/tmp`; the host Docker daemon resolves the
sibling workcell's bind sources and could not see those files. Scratch now
lives under the identically mounted evidence root. The root controller also
transfers ownership of the freshly created containment workspace to pinned
workcell UID 65532; without that transfer, the
capability-preserving writable-workspace probe correctly reported a breach.

The first correction's zero-model diagnostic then found two more consequences
of that boundary: an evidence-derived relay socket exceeded the kernel's Unix
socket path limit, and the manually constructed containment session still
named controller-private task and forwarder paths. Disposable workspaces,
mount sources, and sockets therefore use a separate short host-mounted runtime
root; durable evidence remains at its descriptive path. The containment task
and forwarder are copied into that runtime root before the sibling workcell is
created.

These corrections do not relax either gate. They make the observed topology
match the declared one and restore the workcell's intended editing capability.
Failed evidence is retained, and a retry must use new evidence/runtime roots
and a newly built and bound controller image.

The final corrected immutable image passed the complete live preflight with
scripted providers only: 26 tools and the exact schema were observed, read,
write, and shell were demonstrated, all 22 containment probes passed, the
inside-container mount set was correct, and real `web_fetch` egress was
refused. For that diagnostic only, the six-slot schedule was replaced at
runtime with an empty tuple; the runner therefore refused at 0/6 slots after
the gates passed and could not reach model startup. The corrected session
factory is now an explicitly required live-authorization module.

Runner commit `ec1c934` passed the canonical native-ext4 suite: 1,948 tests,
13 skipped, zero failures in 348.880 seconds. No live arm has run.

The next operator attempt passed that live preflight and reached the first
server process, which exited 127 before health or readiness. The pinned server
was built on Ubuntu 24.04, while the controller used Debian 12: `libgomp.so.1`
was absent, glibc 2.38 and GLIBCXX 3.4.32 symbols were unavailable, and the
host CUDA runtime was not mounted. The controller base is therefore
digest-pinned Ubuntu 24.04 with `libgomp1`; the launcher mounts `/usr/local/cuda`
read-only, and the runner supplies only the server and CUDA library directories
through `LD_LIBRARY_PATH`. Preflight now records `ldd` and refuses any missing
library or symbol-version error before server startup.

The owner explicitly directed that test suites be skipped for this correction.
That exception is recorded rather than disguised as verification; compile,
diff, immutable-image linkage, and zero-token server-start checks remain
required before reauthorization.
