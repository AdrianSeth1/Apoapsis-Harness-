# Slice 2 live gate: containment and relay readiness

Date: 2026-07-30

Evidence class: live Docker containment plus one-token local inference

Result: **stopped at conformance; Slice 2 and Slice 3 remain blocked**

## Bound run

- Workcell image: `apoapsis-qwen-workcell@sha256:c19f73760b126b5af459870d9c8ebde1c78110370c8bc7e27ddfcea0debbb4ea`
- Qwen Code: official `@qwen-code/qwen-code` 0.21.1
- Model: Qwen3.6-27B Q4_K_M, file SHA-256
  `5ed60d0af4650a854b1755bd392f9aef4872643dc25a254bc68043fa638392a0`
- Server: `llama-server` `10107-c0bc8591e`, 65,536-token context,
  16,384-token requested output ceiling, temperature 0
- Seed: `197b3610e5720cf36718c548fa19c05fe784a978`
- Task artifact: approved `PLAN-C672117CD8F5/plan-v1.json`
- Runtime: Docker Desktop 29.5.2; controller and worktrees on the Docker
  Desktop VM's native ext4 volume

The two pre-existing Docker containers were not restarted or modified. The
trial-owned 64K model service and WSL keepalive were stopped afterward, and no
managed workcell container remained.

## Ordered gate result

| Gate | Observed result |
| --- | --- |
| Containment, first attempt | **Failed:** 19/22 held; project `.apoapsis`, a Git remote, and a dummy `OPENAI_API_KEY` environment entry were visible |
| Containment, sanitized rerun | **Passed:** 22/22 held; no model request |
| Relay readiness, first attempts | **Failed before inference:** Unix socket was `root:root` while the workcell ran as `65532:65532` |
| Relay readiness, corrected rerun | **Passed:** health, model listing, and one-token completion; relay observed exactly 3/3 requests |
| Nine-check conformance | **Failed closed:** 9/9 `NOT_RUN`; no live driver exists |
| Tiny baseline-Qwen task | Not run; the ordered procedure stops at the first failed gate |
| Matched Capability Sandbox task | Not run |
| Capability verdict | Not measurable |

The successful readiness run recorded a 0.726-second one-token completion, no
relay refusal, no upstream failure, and clean container/socket teardown. The
socket was observed inside the workcell as `0660`, owner `0`, group `65532`.

## Defects found and corrected

1. `check_relay_readiness` accepted an already-evaluated “after” count, so an
   honest caller necessarily supplied the count from before the helper ran its
   probes. It now accepts a counter callback and reads it after the probes.
2. Socket preparation erased setgid inheritance, and Docker Desktop did not
   preserve the intended group on the socket. Preparation now preserves
   setgid, and the trusted relay explicitly assigns the socket to the dedicated
   directory's group before making it available.
3. The live image initially placed a dummy local API key in the environment.
   The corrected image supplies no token-like environment variable.

The first two corrections are product code with deterministic Linux coverage.
The image and sacrificial-clone corrections are live-run setup evidence, not a
checked-in production image builder.

## Blocking gaps

`conformance.py` contains result models and pure classifiers, but nothing
executes the actual Qwen CLI/provider interactions required to produce its nine
observations. Calling the fail-closed evaluator with the observations that
exist therefore returns all nine checks as `NOT_RUN`. Starting the two quality
tasks anyway would measure an unproven adapter and violate the mandated order.

Two additional identity gaps remain:

- the controller does not create and sanitize the sacrificial clone; the
  operator had to remove project `.apoapsis` state and the Git remote before
  the 22-probe rerun;
- the required prompt, tool-schema, and chat-template hash fields have no
  capture/provenance mechanism. This run used the exact installed CLI-bundle
  hash as a provisional source identity, not as proof of the wire values.

Implement a live conformance driver and fail-closed pin capture before rerunning
the tiny paired tasks. Slice 3 must not begin from this result.

Raw evidence is retained under
`.apoapsis-eval/live-slice2-runtime-2026-07-30/evidence/`.

## Deterministic verification after the live fixes

- Linux CPython 3.12:
  `tests.test_workcell_relay tests.test_workcell tests.test_paired_scoring` —
  156/156 passed.
- `python -m compileall -q src tests` — passed.
- `git diff --check` — passed.
- The full suite was started, then stopped at the owner's request because the
  owner will run it separately. No full-suite result is claimed for this
  change.
