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
