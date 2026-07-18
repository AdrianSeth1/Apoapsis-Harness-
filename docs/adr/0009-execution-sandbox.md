# ADR 0009: Execution sandbox (`ExecutionBackend`)

- Status: Accepted
- Date: 2026-07-18

## Context

Verification has always run configured commands directly on the host
(`subprocess.run(argv, shell=False, env=<allowlisted>, timeout=...)`), with
no process isolation, network denial, or CPU/memory/disk limits.
`HANDOFF.md` has named this the top open risk since ADR 0001: "the next
high-value safety increment is an execution sandbox." This ADR introduces a
narrow `ExecutionBackend` seam and a Docker-based sandbox backend without
changing what a model may request: it still only ever names one
preconfigured check; Apoapsis still owns which argv runs, where, and
whether it counts as passing.

The target environment is Windows with Docker Desktop. WSL was considered
first but rejected for this milestone: the only registered WSL distro on
the reference machine is Docker Desktop's own internal utility VM (no
`bash`, not a general-purpose Linux environment), so the sandbox targets
Docker Desktop's Linux-container engine through the `docker` CLI instead of
shelling into WSL directly.

## Decisions

1. `src/apoapsis/execution/backend.py` defines a narrow `ExecutionBackend`
   protocol (`prepare`, `run_command`, `finalize`) and its config
   (`ExecutionBackendConfig`, `DockerBackendConfig`). `VerificationConfig`
   gains a `backend` field defaulting to `host` — every existing
   configuration and test keeps working unchanged.
2. `HostExecutionBackend` (`execution/host_backend.py`) is the pre-0.9
   behavior, preserved exactly, kept only as an **explicitly selected**
   compatibility backend. Every result it produces carries
   `backend_metadata = {"sandboxed": False}` so it is always clearly
   reported as unsandboxed, in both audit artifacts and `apoapsis doctor`.
3. `DockerExecutionBackend` (`execution/docker_backend.py`) is the
   preferred sandbox. It **fails closed**: a missing Docker CLI, an
   unreachable engine, a non-Linux container mode, or a pinned image that
   isn't already present locally all raise `SandboxUnavailableError` with a
   precise diagnostic — including the exact `docker pull <image>@<digest>`
   command when the image is absent. **It never pulls an image itself**,
   and there is no code path from a failed Docker preflight into
   `HostExecutionBackend`; changing backends requires the caller to
   explicitly edit `[verification.backend]`.
4. Every container run applies the same fixed hardening:
   `--rm --network none --read-only --cap-drop ALL --security-opt
   no-new-privileges --pids-limit <N> --memory <N>m --cpus <N> --user
   <non-root numeric> --tmpfs /tmp:size=<N>m`, exactly one writable mount
   (`-v <workspace>:/workspace:rw -w /workspace`), and the configured
   verification `argv` passed directly to the pinned `image@digest` — never
   through a shell. `network` is a fixed `"none"` literal this milestone,
   not a runtime toggle. No `-e` flag is ever added for a host environment
   variable unless it is in the Docker-specific `environment_allowlist`
   (default empty) — Docker's own default behavior of not inheriting host
   environment already satisfies "remove host credentials and unrelated
   environment variables" for everything not explicitly listed.
5. Containers use a deterministic name derived from the task and attempt
   (`apoapsis-verify-<task-slug>-<attempt:03d>`). Before running, any
   same-named container from a prior crashed run is force-removed. If the
   host-side wait exceeds `min(command.timeout_seconds,
   docker.wall_clock_timeout_seconds)`, Apoapsis explicitly runs `docker
   kill` then `docker rm -f` on that exact container — a killed Docker CLI
   process can never leave sandboxed work running unattended.
6. Verification runs against a **temporary copy** of the task worktree
   under `.apoapsis/sandbox/<task>/attempt-<N>/workspace/` (existing,
   gitignored, Apoapsis-controlled state), not the real worktree, so a
   misbehaving command cannot mutate the actual task branch. The copy
   excludes `.git`/`.apoapsis`/`.sol` and **never follows or copies
   symlinks** — that omission is the entire containment guarantee against a
   symlink pointing outside the workspace. After the run, Apoapsis
   recomputes a content-hash manifest and compares it against the
   pre-run manifest, **restricted to paths that already existed before the
   run**: any changed pre-existing file is an "unexpected filesystem
   change" and forces the aggregate `VerificationResult.status` to
   `FAILED` (recorded in a new `integrity_violations` field); genuinely new
   files created during the run (build/test output) are not flagged. The
   temporary workspace is deleted unconditionally afterward — retaining it
   for audit is a future extension, not built here.
7. `apoapsis doctor` reuses the backend's own preflight so doctor and a
   real run can never disagree: Docker CLI present, engine responds, Linux
   containers, pinned image present locally, and one minimal sandbox
   self-test that runs a real (configurable, default `["true"]`) command
   through the full backend and checks it reports `sandboxed: True`. Doctor
   never pulls an image and never starts Docker Desktop. When `backend =
   "host"` (the default), doctor reports a single `WARNING` noting
   verification is unsandboxed, with remediation pointing at
   `[verification.backend]`.
8. `VerificationCommandResult` and `VerificationResult` gained additive
   fields (`backend`, `backend_metadata`, `integrity_violations`) with safe
   defaults; `FailureNormalizer` and every existing caller are unaffected,
   since both backends still produce the same `stdout`/`stderr`/`exit_code`
   shape "structured command results and failure normalization" is
   preserved exactly, regardless of which backend produced them.

## Threat model

**What this defends against:** a verification command reading host
credentials or unrelated environment variables (none are forwarded by
default); a verification command reaching the network (fixed `--network
none`); a verification command consuming unbounded CPU, memory, or
processes (hard `--cpus`/`--memory`/`--pids-limit`); a verification command
mutating the real task worktree, `.git`, or other host paths (only a
throwaway copy is mounted, and it is the *only* writable mount); a
verification command persisting after Apoapsis gives up on it (deterministic
naming plus explicit kill-and-remove on timeout).

**What this does not defend against, and is not claimed to:** a kernel or
container-runtime vulnerability that breaks out of the container namespace
—**Docker containers materially improve isolation but are not a perfect
defense against container-runtime or kernel vulnerabilities.** The
integrity check is a heuristic (pre-existing-file content hash comparison),
not an exhaustive taint-tracking system, and does not currently retain a
forensic copy of a flagged run. There is no live proof yet that a real
`docker run` under this exact flag set behaves as designed on this
machine — Docker Desktop's engine was not running when this ADR was
written; the fail-closed "engine unreachable" path is proven for real, the
success path is proven only via injected-process unit tests plus a
live-gated integration test that will run once a user starts Docker
Desktop and pins a real image.

## Consequences

Apoapsis can now run verification either exactly as before (`host`,
explicit, clearly marked unsandboxed) or inside a hardened, network-denied,
resource-limited, throwaway Linux container (`docker`), without granting a
model any new authority — it still only ever names one preconfigured
check. This does not add a WSL integration, a general-purpose shell sandbox,
model-selected commands, or a persisted forensic-retention policy for
flagged runs; those remain explicitly out of scope.
