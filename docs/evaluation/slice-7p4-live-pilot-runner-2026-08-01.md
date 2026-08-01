# Slice 7P.4 live-pilot runner preparation — 2026-08-01

## Outcome

The operator-launched six-arm Crisis Atlas path is prepared and bound. No live
arm has run. No `llama-server` process was started, no GGUF was loaded, and no
readiness or inference request was sent while preparing this path.

The live authorization binds:

- manifest v8 digest `f369760e438b6866b59fbecafa4b1d63cb8523aa726b3c9ba20a9699c26e4798`;
- lock v8 digest `61b36743c43ec3afc932d956ad76fe1f2a320d41cff52356ab749746b6112867`;
- the passed rehearsal report SHA-256 `7a97a927038b894cae3015aa78031df7b02ab4c8f21079fa3dee3ff54eaa800e`;
- live runner commit `ef2905bd5be5b00d7e209654475ba55a0fdbd01d`;
- controller image `sha256:41f592a7ee680358c177b4eda7fd9c4382d737120bcac268da608f53325b5b0f`;
- the exact live-runner and slot-driver module hashes.

The image was built from a `git archive` of the bound commit. Its source tree is
`c34eb7106f53521e3b1db5b9a3b7f0e8e72f1234` and build-context SHA-256 is
`d75209400ac2bca0227258076ca2d3447f0ef738fad7312e813f3735409a3d73`.

## Observed deterministic verification

- Focused live-runner tests: 14/14 passed on CPython 3.12; the final
  launcher-focused file passed 6/6 after the read-only docs mount was added.
- Canonical native-ext4 full suite at launcher commit `1217243`: 1,943 passed,
  13 skipped, zero failures in 343.721 seconds. A preceding attempt hit one
  relay-test timing race; that exact test passed immediately alone, and only
  the subsequent complete green run is recorded as verification.
- `python -m compileall -q src tests`: passed.
- `git diff --check`: passed.
- The immutable controller image imported the live entry point, exposed the
  pinned server mount, and observed the frozen GPU identity through
  `nvidia-smi`: NVIDIA GeForce RTX 4090, 24,564 MiB, driver 610.74.

The full-suite log is retained outside the repository at
`/home/arya/apoapsis-live-runner-1217243-full-suite-final.log`.

## Operator handoff

The supported entry point is `tools/run_crisis_atlas_live_pilot.sh` from
Ubuntu-24.04 under WSL2. It refuses an existing evidence directory and requires
the literal acknowledgement `I-AUTHORIZE-SIX-LOCAL-INFERENCE-ARMS`. The image
contains the committed executable runner; the launcher supplies its locked
qualification/evaluation inputs through a read-only docs mount. It runs the
frozen six slots and stops at `six_slots_complete_pending_independent_scoring`;
the resulting proposal-quality and harness-detection scores remain independent
review work.

## First operator attempt: refused before inference

The first operator attempt wrote
`/home/arya/crisis-atlas-live-pilot-v1/live-preflight/` and exited with
`identity=unrun, containment=failed`. Runtime rehashing completed. The fake
provider transcript was empty, the realised tool surface contained no tools,
and containment recorded only `workspace-writable` as breached. Inspection
after exit observed no controller, workcell, `llama-server`, or GPU compute
process; there was no readiness record and no `live-arms/` evidence. Therefore
zero model requests and zero arms were consumed.

The direct causes were controller-container topology, not the model: the local
fake-provider route resolved to Docker Desktop's host gateway instead of
controller loopback; private controller `/tmp` was invisible to the host
daemon resolving sibling-workcell mounts; and the root controller did not
transfer the fresh workspace to UID 65532. The remediation binds loopback
explicitly, places scratch under the identically mounted evidence root,
transfers that ownership before containment, and changes the default evidence location
from the root-owned rehearsal directory to an operator-writable native-ext4
path. The failed v1 evidence is retained and must not be reused.

The first corrected image was not authorized. A scripted-provider-only
diagnostic retained at `/home/arya/apoapsis-live-preflight-diagnostic-v2`
proved all 22 containment probes, including writable workspace, but refused
the overall preflight. Stage 1's relay socket path exceeded the Linux Unix
socket limit, and mount observation correctly rejected the containment
session's controller-private task and forwarder sources; its egress probe was
therefore also unobserved. No server or model request occurred. The next
correction separates durable evidence from a short host-mounted runtime root
and copies both manual-session mount inputs into that root.

The next scripted-only diagnostic reached the corrected stage-1 topology and
then refused before containment because `session_factory_from_manifest` did
not expose the `forwarder_path` and `task_artifact_path` arguments already
supported by `build_workcell_config`. The public factory now passes those
host-visible sources through explicitly. This diagnostic also stopped before
server startup, readiness, or any arm.

## Corrected live preflight passed

The final corrected image is
`sha256:b9b1d440fb502a1625649b37fc7e72d6c51452af12d19835c5fe268ccdb5f174`,
built from runner commit `ec1c9342f5a13db2100fb297dce2f77d05dbec1b`,
source tree `2e9db606c42bb4f24ee27c27e26a8bf8091e6d2b`, and build-context
SHA-256 `ce426f9c065af749179b53ad3c120b232b10b07a61e4fcaf9e30b8ef1be17e4b`.
The authorization now binds `live_pilot.py`, `slot_driver.py`, and the changed
`session_factory.py`.

A scripted-provider-only diagnostic in the actual controller-container
topology passed both live gates:

- 26 tools matched the bound set and schema digest
  `d0d3891aa074f6efb9c7026cf83a5a4632ec8e7341dc40ec707d9145352e701d`;
- read, write, and shell were each demonstrated behaviorally;
- 22/22 containment probes passed with zero breaches and zero unproven;
- mount observation and real `web_fetch` egress refusal passed.

The diagnostic monkeypatched the schedule to empty, so after the gates passed
the runner refused with `only 0 of six slots ran`. This proves the corrected
preflight without permitting server startup. Post-run inspection found no
`llama-server`, GPU compute process, workcell, readiness record, or arm record.
Evidence is retained at
`/home/arya/apoapsis-live-preflight-diagnostic-v4`.

The final canonical native-ext4 suite at runner commit `ec1c934` passed 1,948
tests with 13 skips and zero failures in 348.880 seconds. Its log is retained at
`/home/arya/apoapsis-live-runner-ec1c934-full-suite.log`.

## First server start: ABI refused before readiness

The next operator attempt retained v2 evidence at
`/home/arya/apoapsis-live-evidence/crisis-atlas-live-pilot-v2`. Live preflight
passed both gates, then the first cold `llama-server` process exited 127. Its
captured log reported missing `libgomp.so.1`. A complete loader inspection also
found that the Debian 12 controller lacked the server build's glibc 2.38 and
GLIBCXX 3.4.32 symbol versions and could not see `libcudart.so.13` or
`libcublas.so.13`. There was no health/readiness record, model request, arm
result, surviving server, or GPU compute process; zero arms were consumed.

The direct correction uses digest-pinned Ubuntu 24.04, installs `libgomp1`,
mounts the WSL host CUDA runtime read-only, sets the server/CUDA dynamic-loader
directories explicitly, and makes unresolved `ldd` output a pre-start failure.
Per the owner's explicit instruction, test suites are skipped for this
correction; the record must not claim focused or full-suite verification for
the new commit.

The corrected controller image is
`sha256:ddc7e09ce5fc3c7a12c1a5f99d4a6bf7c2d14ff69d9f8a749d190f55929788d1`,
built from runner commit `603e68fdb62ba81f698ec663ec8b7ef149cb1533`,
source tree `daf407395561127b423ed7576fbf0970b1e78d91`, build-context SHA-256
`23b941560319d0e561e12769230017d8851ebd7a8cc5dbde8e6f90e99bf3c2bf`,
and Dockerfile SHA-256
`8215b364ffc9264c2b780a94855f6c3de132b2c9a150411f9aa34390b0112e7a`.

Observed without model loading or inference:

- all 22 dynamic-linkage entries resolved inside the `--gpus all` container;
- `llama-server --version` reported `10107 (c0bc8591e)`;
- the runner's complete runtime rehash/linkage/GPU check passed and observed
  NVIDIA GeForce RTX 4090, 24,564 MiB, driver 610.74;
- `python -m compileall -q src` and `git diff --check` passed.

Per the owner's instruction, focused and full test suites were not run for
commit `603e68f`. Fresh retry defaults are v3 evidence and runtime paths; v2 is
retained as failed evidence.

## V3 first control completed; evaluator clone refused

V3 passed preflight, server readiness, and the first control proposal. Its raw
trace is complete: 21 model requests, 201,429 input tokens, 2,834 output
tokens, 177,949 cached input tokens, 13 tool calls, normal end, zero adapter
errors, and no compaction. The independent evaluator then failed before
writing the slot result because Git rejected the root controller's clone of
the UID-1000 owner-mounted seed as dubious ownership.

The produced worktree and raw telemetry are retained. With the exact seed
`.git` path added to global `safe.directory` inside an ephemeral controller,
the same checkpoint ran successfully and reported `COMPLETE`, all three
criteria satisfied, unit tests exit 0, and fingerprint
`e0aa0d4dd6e23e10b9091dc7c65b6b74c6747bfc508ba2abb4eefe0bdb1b7b7f`.
The correction adds only that exact trust entry. At the owner's direction no
resume mechanism is introduced; fresh v4 paths rerun the normal six slots.
