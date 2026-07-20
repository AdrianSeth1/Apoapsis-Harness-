# D5a: live Docker sandbox evidence — 2026-07-20

Authorized by the user (`LIVE DOCKER AUTHORIZED`, image `python:3.12-slim`,
explicit authorized/not-authorized action lists). This document records
exactly what was run, in what order, and what was observed. Raw artifacts
(`doctor-output.json`, `live-docker-test-run.log`) are preserved locally
under `.apoapsis-eval/d5a-docker-live-project/` (gitignored, matching this
project's existing convention for raw evaluation evidence); this file is
the committed record of the same run.

## Environment at the time of this run

- `docker version`: Client `29.5.2` / Server `29.5.2`
- `docker info`: `OSType=linux Arch=x86_64
  KernelVersion=6.6.114.1-microsoft-standard-WSL2` (Docker Desktop's WSL2
  backend, Linux containers)
- Docker Desktop's engine was unreachable during the D5a readiness pass
  earlier the same day; it was running by the time this authorization was
  given, confirmed independently before taking any authorized action:
  `docker info --format '{{.ServerVersion}} {{.OSType}}'` returned
  `29.5.2 linux` with exit code 0.

## Image: inspect, pull, resolve digest

```
$ docker images python:3.12-slim
IMAGE   ID             DISK USAGE   CONTENT SIZE   EXTRA
(none -- image was not present locally)

$ docker image inspect python:3.12-slim --format '{{json .RepoDigests}}'
Error response from daemon: No such image: python:3.12-slim
```

Absent locally, so (per authorization) pulled exactly once:

```
$ docker pull python:3.12-slim
...
Digest: sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
Status: Downloaded newer image for python:3.12-slim
```

Resolved and independently re-verified digest:

```
$ docker image inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

$ docker image inspect "python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de" --format 'OK: {{.Id}}'
OK: sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
```

**Immutable digest used for every step below:**
`sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`

Image metadata: `Size=43200635` bytes, `Created=2026-07-14T02:11:29Z`. No
other image was pulled, tagged, or referenced at any point in this run.

## Disposable project configuration (`--pull=never`, pinned digest)

A disposable project was initialized under
`.apoapsis-eval/d5a-docker-live-project/` (its own Git repository,
`apoapsis init`) with `[verification.backend]` set to:

```toml
[verification.backend]
backend = "docker"

[verification.backend.docker]
image = "python:3.12-slim"
image_digest = "sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
cpu_limit = 2.0
memory_limit_mb = 2048
pids_limit = 256
tmpfs_size_mb = 256
wall_clock_timeout_seconds = 300
```

`--pull=never` itself is not a config field -- it is unconditionally part
of `DockerExecutionBackend`'s fixed `docker run` argv (ADR 0009, decision
4) for every invocation, regardless of configuration. Nothing in this run
changed that, added a config toggle for it, or exercised any code path
that could disable it.

## `apoapsis doctor` (real result, not injected)

```
$ apoapsis doctor   # run from .apoapsis-eval/d5a-docker-live-project
```

Relevant checks from the real, unmodified `run_doctor()`:

```json
{
  "name": "docker_sandbox",
  "category": "verification",
  "status": "ok",
  "detail": "Docker CLI/engine/image preflight passed for python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
},
{
  "name": "docker_self_test",
  "category": "verification",
  "status": "ok",
  "detail": "self-test container ran in 0.41s"
}
```

`overall_status` for the run was `warning`, entirely from two pre-existing,
Docker-unrelated checks (ripgrep not on this machine's `PATH` from a plain
Python subprocess -- the same known defect ADR 0029 already documents --
and the disposable project's `completion_policy = "strict"` having no
acceptance-designated command yet, expected for a project that was only
just initialized). Both Docker checks are unambiguously `ok`.

## Gated live Docker integration tests (real result, not injected)

Per authorization, only the gated live-Docker test class was run -- not
the full suite:

```
$ APOAPSIS_RUN_LIVE_DOCKER_TESTS=1 \
  APOAPSIS_SANDBOX_TEST_IMAGE=python:3.12-slim \
  APOAPSIS_SANDBOX_TEST_DIGEST=sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de \
  python -m unittest tests.test_docker_backend.DockerLiveIntegrationTest -v

test_network_access_is_denied ... ok
test_root_filesystem_outside_workspace_is_read_only ... ok
test_timeout_triggers_verified_removal_confirmed_via_docker_ps ... ok
test_trivial_command_passes_and_reports_sandboxed ... ok
test_worktree_copy_mutation_is_detected_by_finalize ... ok

----------------------------------------------------------------------
Ran 5 tests in 7.438s

OK
```

Each of these five tests, restructured from one generic smoke test during
the D5a readiness pass earlier the same day (ADR 0009's D5a amendment),
now has a real, live-proven result rather than only a deterministic
fake-process proof:

- **`test_trivial_command_passes_and_reports_sandboxed`**: a real
  container ran `true`, exited 0, and `backend_metadata["sandboxed"]` was
  `True`. The pre-run/post-run worktree content-hash manifests matched
  exactly (`changed == []`).
- **`test_network_access_is_denied`**: a real Python process inside the
  container attempted `socket.connect(('1.1.1.1', 80))` and received a
  real `OSError` (no route -- `--network none` genuinely has no interface
  to route through, not merely a configured flag that was never
  exercised). Confirms the container has no network reachability, matching
  ADR 0009's threat model exactly.
- **`test_root_filesystem_outside_workspace_is_read_only`**: a real
  attempt to `open('/apoapsis-readonly-probe', 'w')` inside the container
  raised (nonzero exit), confirming `--read-only` genuinely blocks writes
  outside the one `/workspace` mount, not merely that the flag was passed.
- **`test_worktree_copy_mutation_is_detected_by_finalize`**: a real
  in-container process rewrote `/workspace/marker.txt` (a pre-existing
  file in the mounted worktree copy); `finalize()`'s real post-run
  content-hash comparison against the pre-run manifest correctly flagged
  exactly `["marker.txt"]` as changed -- the integrity check works against
  a real container's real filesystem effects, not only a synthetic
  before/after manifest in a unit test.
- **`test_timeout_triggers_verified_removal_confirmed_via_docker_ps`**: a
  real `sleep 999` was started with a 2-second command timeout; the host
  wait timed out, `backend_metadata["timeout_cleanup"]` read `"removed"`,
  and -- independently of trusting that self-report -- a real
  `docker ps -a --filter name=<container> --format {{.Names}}` immediately
  afterward returned empty output, confirming the container was actually
  killed and removed from the real engine, not merely marked as such
  internally.

## Post-run container state

```
$ docker ps -a --filter "label=apoapsis.managed=true" --format "{{.Names}}\t{{.Status}}"
(empty)
```

No container created by any of the runs above (doctor's self-test or the
five live tests) was left running or stopped on the host. Every container
this run created was named uniquely per invocation and removed by the
backend itself (`--rm` on normal completion; verified, ownership-checked
`docker kill`/`docker rm -f` on the one deliberate timeout).

## What this evidence does and does not establish

**Confirms, for the first time with a real Docker engine** (rather than
only injected-process unit tests): the fail-closed preflight, the fixed
hardening flag set, the temporary-worktree-copy integrity check, the
unique-name/ownership-verified timeout cleanup, and `apoapsis doctor`'s
own reuse of the identical preflight all behave on a real engine exactly
as ADR 0009 specified and as the deterministic test suite already
asserted against injected process behavior.

**Does not** establish anything about container-runtime or kernel-level
isolation guarantees beyond what Docker itself provides -- ADR 0009's
threat model already states this is not a claim this milestone makes.
Does not run any verification command other than the test suite's own
minimal probes (`true`, a Python socket/file-write probe, `sleep`) -- a
real project's actual configured verification commands were not exercised
inside the sandbox in this pass. Does not touch Docker Desktop settings,
install or upgrade Docker, pull any image other than the one named, or
make any hosted-model call.
