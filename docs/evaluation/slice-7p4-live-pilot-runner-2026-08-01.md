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
