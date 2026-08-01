# Slice 7 Phase 1B: blocked at the corpus seeds

Date: 2026-07-30. **No inference, no server launch, no manifest finalized.**

Phase 1B requires the manifest to be finalized **atomically** — every
placeholder replaced, `unresolved_hashes()` empty, `ready_for_inference()` true,
digest recomputed, lock written. It cannot be finalized, so nothing was
finalized. `cfe7df7` / digest `8c374827…` remains the draft, and **no manifest is
yet authorized for qualification.**

## The blocking condition, in the instruction's own terms

> If any corpus repository or seed does not yet exist, Phase 1B remains blocked
> rather than hashing a label.

Of the eight required corpus kinds, **one has a real seed repository.**

| Required case kind | Seed repository | Status |
|---|---|---|
| Crisis Atlas | `.apoapsis-eval/slice-e-crisis-atlas-seed-2026-07-29` (git) | **exists** |
| Focus Orbit | — | **does not exist anywhere in the tree** |
| Small backend change | `examples/download-service` (candidate) | **not declared as a seed** |
| Cross-file refactor | — | **does not exist** |
| Test repair | `examples/download-service-v2` (candidate) | **not declared as a seed** |
| Launch / operability | — | **does not exist** |
| Misleading inherited suite | — | **does not exist** |
| Held-out repository | — | **does not exist** |

`examples/download-service` and `download-service-v2` plausibly *could* serve two
of these. "Plausibly could serve" is not a seed identity. Item 4 requires every
case **and every repetition** to name its exact repository, seed commit, initial
tree digest, task hash, plan/contract hash, and deterministic repetition seed.

**Concrete seed identities available: 3 of 24 pairs** (Crisis Atlas × 3
repetitions). The remaining 21 have no repository to point at.

This is exactly the defect Phase 1 shipped and this phase was meant to fix: the
draft manifest's `seed_tree_sha256`, `task_text_sha256`,
`acceptance_criteria_sha256` and `verification_commands_sha256` are
`sha256("slice7::<case-id>::…")` — **hashes of labels**, structurally valid and
referring to nothing. Replacing eight placeholders while leaving 21 label-hashes
in place would produce a manifest that reports `ready_for_inference() == true`
and still cannot seed a run. That is a worse state than being blocked, because
the falsity would be machine-endorsed.

## What is resolvable now, and was deliberately not committed

Four of the eight placeholders have real artifacts behind them, located and
verified to exist:

| Placeholder | Artifact | Status |
|---|---|---|
| `model_file_sha256` | `/home/arya/models/qwen3.6-27b/Qwen3.6-27B-Q4_K_M.gguf` (WSL `Ubuntu-24.04`) | present, hashable |
| `llama_server_binary_sha256` | `/home/arya/llama.cpp/build/bin/llama-server` | present, hashable |
| `llama_server_argv_sha256` | derivable once the canonical argv object is authored | authorable |
| `qwen_package_sha256` | pinned image `apoapsis-qwen-workcell:0.21.1` | present, hashable |

They were **not** captured and committed, because Phase 1B's finalization is
atomic by instruction. A commit resolving four of eight placeholders would leave
`ready_for_inference()` false and produce a second draft digest that is neither
the authorized manifest nor the previous baseline — more state to reconcile, no
progress toward the gate.

The remaining four (`worktree_seed`, `mount_policy`, `verification_config`,
`repair_policy`) all depend on artifacts that do not exist yet: per-case seeds,
and the structured mount/verification/repair policy objects the instruction
requires be hashed as canonical objects rather than names.

## Three code identities, as far as they are known

| Role | Commit |
|---|---|
| Subject implementation | `ad13cf0` |
| Draft qualification framework / schema | `cfe7df7` |
| Final qualification framework / manifest | **not created** |

## What unblocks this

Bounded and concrete, in order:

1. **Author the seven missing corpus seeds** as real repositories with real
   initial commits — Focus Orbit, cross-file refactor, launch/operability,
   misleading inherited suite, held-out repository, plus explicit declaration of
   whichever repositories serve small-backend-change and test-repair.
2. **Author each case's task text, acceptance criteria, verification commands
   and plan/contract** as real artifacts, so their hashes describe content.
3. **Author the canonical structured objects** for mount policy, verification
   configuration, server argv and repair policy — objects, not names.
4. Then Phase 1B resolves all eight placeholders in one atomic finalization.

Step 1 is the large one and it is genuine engineering work: seven seed
repositories, each needing a task that actually exercises the capability its
case name claims. It is not a hashing exercise.

## Status

`unresolved_hashes()` — **8** (unchanged).
`ready_for_inference()` — **false** (unchanged).
Manifest digest — **`8c374827aa4ace9576ed9d2d2f0db04747f3b4fb05d425b10e6fc770454f3762`**, still the draft.
Lock artifact — **not written.**
Seeds with concrete identity — **3 of 24 pairs.**

No model calls were made. `llama-server` was not started. No test or source file
changed in this phase.
