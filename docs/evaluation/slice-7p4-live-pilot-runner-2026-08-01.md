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
