# ADR 0100: Keep Unix runtime on ext4 and durable evidence on Windows

## Status

Accepted and implemented on 2026-08-01.

## Context

After the approved seed reached the product controller, live containment tried
to create `model.sock` below the task's durable evidence directory. That
directory is on Windows DrvFs. DrvFs cannot represent a Unix socket inode, so
the relay correctly refused before inference.

The controller is itself a container that launches sibling workcell
containers through the host Docker socket. A private path inside the controller
is therefore insufficient: sibling bind-mount sources must also exist at the
same path on the WSL host.

## Decision

The WSL launcher creates one short, fresh `/tmp/apx.XXXXXX` runtime directory
with `mktemp`, clones the approved seed there, mounts the runtime into the
controller at the identical absolute path, and passes a dedicated controller
runtime root. The launcher removes that exact generated directory on exit.

The runtime name and internal `r/p` and `r/s` roots are deliberately short so
every derived relay socket stays below Linux's AF_UNIX pathname limit. Before
the WSL-user cleanup trap runs, the root controller removes its exact runtime
subtree so directories intentionally owned by the non-root workcell user do
not survive or make cleanup fail.

The launcher also exposes a containment-preflight-only diagnostic mode. It
uses the same seed normalization, mounts, committed controller image, runtime
verification, and deterministic fake-provider containment path as production,
writes its gates to the requested response artifact, and exits before any
model server or Qwen execution. It grants no product/UI authority and exists
only to prove this platform boundary repeatably.

All socket directories, workcell workspaces, Qwen homes, forwarders, and other
ephemeral sibling-container inputs use this ext4 runtime. Durable authorization,
logs, observations, checkpoints, admitted snapshots, and the final response
remain under the project task's Windows audit directory. A completed admitted
snapshot is therefore available to the Windows promotion adapter after the
ephemeral runtime is removed.

## Consequences

- Unix sockets are never placed on DrvFs.
- The host Docker daemon can resolve every sibling-container bind source.
- Durable evidence remains visible and persistent in the project.
- Ephemeral model workspaces are removed even when the controller fails.
- No model, filesystem, command, or completion authority moves into the model.

## Observed verification

On 2026-08-01, the containment-only mode ran through the committed production
launcher and controller image against the actual failed Slice 1 task seed. The
runtime-identity gate observed all 26 bound tools with the expected schema
digest. The containment gate passed all 22 probes with zero breaches and zero
unproven results, including refused egress in every tested category. The mode
then exited successfully before starting or calling Qwen. Focused product
coverage passed 7/7; shell syntax, compileall, and `git diff --check` also
passed. Per owner direction, no full test suite or target-project verification
was run.
