# Slice 2A: the controller-owned model relay and in-container forwarder

Date: 2026-07-30  
Evidence class: **deterministic only.** The relay itself is exercised end to end
over real Unix sockets against a fake upstream. No container was started, no
Qwen CLI was run, and no live model was reached. Slice 2 remains
evidence-blocked.

## What this closes

Slice 2 specified the egress design and did not build it: `workcell-preflight`
validated pins and the runtime, and there was no way for anything inside the
container to reach a model. This adds the two halves.

| Piece | Module | Runs where |
| --- | --- | --- |
| Fixed-forwarding policy | `workcell/relay_policy.py` | Controller, pure |
| Unix-socket relay server | `workcell/relay.py` | Controller |
| Loopback forwarder | `workcell/forwarder.py` | Inside the workcell, read-only |
| Portability assessment | `workcell/platform_support.py` | Controller |
| End-to-end readiness | `workcell/relay_preflight.py` | Both |

`--network none` is unchanged and non-negotiable; it is a `Literal` in the
config so no file can set anything else.

## The relay is not a proxy

A proxy takes its destination from the client. This takes its destination from
`upstream_base_url` and ignores everything the client says about routing. That
distinction drives most of the policy:

- **`CONNECT` is refused** — it exists to open arbitrary tunnels.
- **Absolute-form request URIs are refused** — `GET http://evil/...` is a client
  choosing an upstream.
- **`upstream_base_url` must be an origin**, validated to carry no path, query,
  or fragment, so configuration cannot smuggle a different API prefix.
- **Routes are a module constant.** Configuration may *narrow*
  `ALLOWED_ROUTES` and validation rejects anything not already in it, so a
  permissive config file cannot widen the relay into a tunnel. The set is
  `POST /v1/chat/completions`, `POST /v1/completions`, `GET /v1/models`, and
  `GET /health`.
- **Traversal and empty path segments are refused, not normalised.**
  `/v1/../v1/models` is rejected rather than quietly rewritten.
- **Redirects are never followed**, and a `Location` pointing at any other
  origin is refused with 502 rather than handed back to be chased.

### One correction worth flagging

The first implementation *rejected* requests carrying `Host`,
`X-Forwarded-Host`, and friends. Every real request failed, because `Host` is
mandatory in HTTP/1.1.

The fix is not a narrower blocklist — it is that these headers are **stripped**
and never consulted. The safety property was never "the client did not send a
Host"; it is "the relay does not read it". `http.client` regenerates `Host` from
the connection the controller actually opened, so a client value cannot reach
the model server or change the destination. A test asserts an injected
`X-Forwarded-Host` does not appear upstream.

## Two real defects the tests found

**Streaming was buffered.** The pump used `response.read(65_536)`, which blocks
until the buffer fills or the upstream closes. For a token-by-token SSE stream
that means nothing reaches the CLI until 64 KiB has accumulated — the CLI's
incremental parsing would stall for the whole generation. Now `read1`, which
returns whatever has arrived.

**A vanished client could pin a worker for two minutes.** When the workcell
disappears mid-stream, the write does not always raise `EPIPE`; once the socket
buffer fills, `sendall` simply blocks, and it would have blocked for the full
120-second idle timeout while the model server kept generating for a reader
that no longer existed. There is now a separate, shorter
`stream_write_timeout_seconds` (default 15) and hitting it is recorded as the
cancellation it is. The test asserts the upstream observed the abort.

A third, smaller one: a client that reset while its rejected body was being
drained could kill the handler thread before the refusal was recorded. A
refusal nobody can see is indistinguishable from a request that was allowed, so
the record is now written in a `finally`.

## Portability: the Windows assumption is false

A Unix domain socket created on a **Windows host cannot be bind-mounted into a
Docker Desktop Linux container.** Windows has had `AF_UNIX` since 10 1803, so
`bind()` succeeds and the mount looks fine — but the shared filesystem
(9p/gRPC-FUSE/virtiofs) does not carry socket inodes, and the container's
`connect()` fails at the first model request. The failure is quiet and late.

`assess_socket_support` classifies this up front and refuses, with remedies:
run the controller inside WSL2 with the socket on the distro's own ext4, or on
a Linux host. It explicitly says **not** to substitute a TCP port, because that
would require giving the workcell a network route, which ADR 0077 forbids.

The same trap exists one level down: inside WSL2, a socket under `/mnt/c` is
DrvFs — the Windows filesystem again — and is refused for the same reason.

This is checked in `ModelRelay.start`, in `WorkcellController.preflight`, and
reported by `workcell-preflight` even when no container runtime is present.

## Mounts and tooling

Only a **dedicated socket directory** is mounted, never a broad writable host
path; `prepare_socket_directory` refuses to proceed if the directory contains
anything but the socket, because mounting a directory nobody has looked at
would hand the workcell a channel the relay does not mediate. A stale socket
file left by a crashed controller is removed so `bind()` does not fail with
`EADDRINUSE` against nothing.

The forwarder is mounted **read-only at `/opt/apoapsis/forwarder.py`**, outside
`/workspace`: the agent cannot edit it and it never enters the computed delta.
`RelayPin` rejects a forwarder path inside the worktree. Preflight hashes the
file and refuses to run if it does not match `forwarder_sha256`.

The forwarder applies **no policy at all** — no HTTP parsing, no route
knowledge, no imports from Apoapsis. Tests assert this. A forwarder that
understood requests would be a second place for policy to live, and the second
place is always the one that is wrong.

## Readiness is end to end, or it is nothing

`relay_preflight` runs three escalating steps inside the real container:
forwarder liveness, a health round trip, and a **one-token** chat completion
(`max_tokens: 1`). Only the third proves the route, chat template, and model
work together, and it stops at the first failure so a broken health route does
not spend a token to learn nothing.

It then cross-checks against the relay's own counter. **If every step passed but
the relay observed zero requests, the report is not ready** — the container
reached a model by some path other than the controller's socket, which is a
containment failure wearing a green tick.

## Verification

- `python -m compileall -q src tests`: clean.
- `tests/test_workcell_relay.py`: 54 tests, passing, stable over three
  consecutive runs. Covers unauthorized paths, absolute-form and `CONNECT`
  attempts, oversized request and response bodies, dropped streams, stale
  sockets, controller death, teardown, budget and concurrency limits, and the
  Windows refusal.
- `tests/test_workcell.py`: 53, `tests/test_paired_scoring.py`: 47 — passing.
- **Full deterministic suite: 65 modules run.** 12 failures, in
  `test_acceptance_coverage` (2), `test_desktop_import` (3),
  `test_diagnostic_probe` (2), `test_doctor` (2), and
  `test_planning_evaluation` (3). **All 12 reproduce identically at commit
  `0fb4e39`, before any of this work**, verified in a detached worktree. They
  are pre-existing or environmental (a Windows-targeted suite on Linux, and a
  3.10 interpreter). None is in a module this work touches.
- `git diff --check`: clean.

Runs used CPython 3.10 with a sandbox-local `sitecustomize.py` shim supplying
`enum.StrEnum`, `tomllib`, and `datetime.UTC`; no 3.11+ interpreter was
installable in the evaluation environment. The shim is not in the repository.
**Re-run on the project's real 3.12 interpreter before treating any of this as
a clean result**, including the 12 baseline failures, some of which may be
artefacts of the shim or the platform.

## Still outstanding — Slice 2 is not complete

Nothing here is live evidence. The ordered live sequence remains:

1. containment probes, no model spend;
2. relay readiness through the real container;
3. the nine conformance checks;
4. one tiny baseline-Qwen task;
5. the matched Capability Sandbox task;
6. cold/warm timing and cleanup verification.

I could not run any of these: the evaluation environment has no container
runtime, no GPU, no model weights, and no `llama-server`. They need the owner's
machine.

Slice 3 stays blocked until those artifacts exist.
