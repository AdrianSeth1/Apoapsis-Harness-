# Slice 2: Capability Sandbox workcell — implementation, no live spike yet

Date: 2026-07-30  
Evidence class: **deterministic only.** No container was started, no Qwen CLI
was run, no model was loaded, no containment probe was executed against a real
namespace, and no conformance check was run against a real chat template.

This record exists so that cannot be misread later.

## What Slice 2 asks for, and what exists

| Requirement | Implementation | Live evidence |
| --- | --- | --- |
| Run the real default Qwen CLI in the hardened workcell | `workcell/controller.py` (create/start/exec/freeze/destroy) | **Not run** |
| Use Qwen's native headless loop, not another action grammar | `workcell/events.py` — a one-way `stream-json` adapter with no scheduler | **Not run** |
| Pin CLI, model, server, prompt, tool-schema, container versions | `workcell/pins.py` — every identity field required, folded into one digest | Schema enforced; no real pins captured |
| Tool/chat-template and stop-reason conformance | `workcell/conformance.py` — nine checks, fails closed | **Not run** |
| Prove host paths, credentials, network, sockets, controller authority unreachable | `workcell/containment.py` — 22 probes across 7 categories | **Not run** |
| Record cold/warm timings, tokens, tool calls, cleanup | `WorkcellTimings`, `WorkcellSessionTrace`, `CleanupRecord` | **Not recorded** |
| Compare capability against the unrestricted control | `workcell/spike.py` against the frozen control record | **Not measured** |

Deterministic coverage: `tests/test_workcell.py`, 48 tests, passing. Those
tests prove the *scorer and the argv are correct*. They prove nothing about a
real container, and a passing suite here is not containment evidence.

## The design decision worth reviewing: how the model is reachable

The container runs with `--network none`. That is not compatible with a CLI
that needs to reach an OpenAI-compatible endpoint, and the obvious fixes are
both bad: a bridge network with a host route exposes every other host port, and
an egress allowlist is a policy that has to be right rather than a boundary
that cannot be wrong.

Instead, the only path out is a **Unix domain socket the controller creates,
owns, and bind-mounts in**. An in-container forwarder exposes it on a fixed
loopback port, and `OPENAI_BASE_URL` points at that port. Inside a
`--network none` namespace, loopback reaches the forwarder and nothing else:
there is no default route, no DNS, and nothing to reconfigure by prompt.

Three properties follow, and the containment suite checks all three:
`no-default-route`, `host-loopback-unreachable`, and
`model-socket-is-only-egress`. Every model request also crosses a boundary the
controller can count, meter, and stop, which is where `max_model_requests`
lives.

This is an implementation choice inside the boundary ADR 0077 already set, so
it does not get its own ADR. If a reviewer disagrees with it, this is the
paragraph to argue with.

## Choices that will look odd without the reasoning

**The task artifact is mounted read-only at `/task/task.md`, outside
`/workspace`.** `WorkcellConfig` rejects a configuration that puts it inside.
An approved task sitting in the project tree would show up in the computed
delta and could be committed as delivered content — and a writable one would
let the model edit its own objective.

**The container's PID 1 is `sleep infinity`.** The verification backend runs
one container per command and throws it away, which is right for verification
and fatal for a coding agent. Slice 2 needs a persistent shell and working
directory, so it is one container for the session with every action arriving as
an `exec`.

**Teardown does not delete the workspace.** Delta admission runs against it
after the container is gone; destroying it would destroy the candidate.
`CleanupRecord.workspace_retained_for_admission` records this deliberately
rather than leaving it looking like a leak.

**Capability is derived from observed behaviour, never from configuration.** A
workcell configured to allow a shell that never ran one records `UNPROVEN`, and
the paired scorer counts unproven as lost. In particular, the self-directed
test/debug loop is only credited when a command ran *after* an edit — which is
the precise thing ADR 0069 took away from Crisis Atlas Slice 2.

**Two probes fail when the box is too tight rather than too loose.**
`workspace-writable` and `non-root-execution` exist because a workcell with
perfect containment and no capability is the regression this whole slice is
meant to prevent.

**`CANCELLED` and `PROVIDER_ERROR` map to no ceiling condition.** They are not
ceilings and they are not model reasoning failures, and forcing them into
either bucket would corrupt the efficiency gate.

## Deliberate non-goals

No acceptance repair, no `SliceAcceptanceContract`, no structured witnesses, no
readiness-based completion. Those are handoff slices 4 and later. A spike that
changed both the interface and the acceptance rules could not say which one
moved the result, so `CapabilitySpikeReport.acceptance_repair_performed` is a
field on every report and is always `False`.

`ready_for_evaluation` is recognised by the adapter and recorded on the trace,
and it sets no task state. It is a request for inspection.

## What has to happen before Slice 3

Slice 3 is blocked. It needs evidence this record does not contain:

1. Capture real pins — CLI version and bundle hash, model file hash, server
   version and flag hash, chat template hash, the CLI's own system prompt and
   tool-schema hashes, and the image digest — into a `WorkcellConfig`, and run
   `apoapsis workcell-preflight` against it.
2. Run all 22 containment probes inside the started container. Any `BREACHED`,
   `INCONCLUSIVE`, or `NOT_RUN` result blocks the slice; an unproven boundary
   is not a closed one.
3. Run all nine conformance checks against the real endpoint and template. A
   failure here is an adapter defect and must be fixed before any quality
   number is recorded.
4. Run the CLI headless on a small corpus and record cold and warm timings
   separately, plus tokens, tool calls, and the cleanup record.
5. Produce a `CapabilitySpikeReport` and compare it against the frozen
   unrestricted control.

Until step 5 returns `CAPABILITY_PRESERVED` with `contained` and `conformant`
both true, the Capability Sandbox has demonstrated nothing, and this repository
should not describe it as working execution.

## Honest limitations

- Everything above is code and deterministic tests. The seven Slice 2 bullets
  are satisfied as *implementation*; none is satisfied as *evidence*.
- The in-container loopback forwarder is specified here and referenced by the
  egress policy, but the forwarder itself and the controller-side socket server
  are not implemented. `workcell-preflight` validates pins and the runtime; it
  does not stand up the socket.
- The probe argv assume a BusyBox/coreutils-ish image with `sh`, `ip`, `ss`,
  and `getent`. An image missing one of those will produce `INCONCLUSIVE`,
  which correctly fails the gate but points at the image rather than the
  boundary. Build the workcell image with those present.
- Test runs used CPython 3.10 with a sandbox-local `sitecustomize.py` shim for
  `enum.StrEnum`, `tomllib`, and `datetime.UTC`, because no 3.11+ interpreter
  was installable in the evaluation environment. The shim is not in the
  repository. Re-run on the project's real 3.12 interpreter.
- The full deterministic suite has still not been run; only
  `tests/test_workcell.py` (48), `tests/test_paired_scoring.py` (47), and
  `tests/test_cli.py` (10).
