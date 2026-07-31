# Slice 7P.2: freezing the Crisis Atlas pilot

Date: 2026-07-31. **No `llama-server` start, no model load, no readiness
request, no inference, no rehearsal.** The eight-case draft manifest is
untouched.

## What this phase is

Identity and configuration capture for one case, three repetitions, two arms.
Two commits, deliberately: a manifest commit, then a lock commit that names the
manifest's commit hash. A single commit could not do this truthfully, because
the lock would have to contain the hash of the commit containing it.

## Three findings that changed what may be claimed

### 1. The declared sampling seeds reach nothing

Audited statically on three independent paths, no inference:

| Path | Source | Finding |
| --- | --- | --- |
| Apoapsis provider request body | `models/local.py`, `models/frontier.py` | payload is `model`, `messages`, `stream`, `options{temperature,num_predict,num_ctx}` — **no seed field exists** |
| `llama-server` launch argv | `.apoapsis-eval/slice2c-2026-07-30/serve.sh` | **no `--seed`** |
| Qwen resolved generation config | `.apoapsis-eval/slice2c-2026-07-30/effective-raw.json` | `samplingParams` is `{"max_tokens": 16384}`; the string `seed` occurs **zero** times |

`sampling_seed` existed as a field on `ModelPin`, `RuntimeProfile` and
evaluation records, and was never transmitted by anything. So the three
repetitions are **repetition identities**, model sampling is **stochastic**,
and comparison is **paired within a repetition only** — valid inside a matched
pair sharing byte-identical configuration, never averaged across pairs into a
determinism claim. At n=3 the variance is expected and unquantified.

No seed was added to the request path. Inventing one would have made the pilot
look more controlled than the environment actually is.

**Terminology corrected where it could make new evidence claim determinism.**
The reissued package uses `repetition_identity`; `RuntimeProfile` moves to
schema **1.1** with `repetition_identity`, `sampling_seed_transmitted=False`,
and a nullable `temperature`. Schema 1.0 recorded `temperature=0.0` and
`sampling_seed=0`; neither was ever observed, and both are marked superseded
rather than rewritten. Historical records are preserved untouched.

**Temperature is `null`, not `0.0`.** The resolved configuration carries no
temperature at all, so the manifest records `temperature_state:
unset_provider_default`. A model that refuses to let an unset temperature carry
a number enforces this, and a test asserts it: writing `0.0` would translate an
absence into a setting nobody made.

### 2. A 17,920-byte launcher does not identify the inference implementation

`llama-server` is dynamically linked. Hashing it identifies a launcher; the
arithmetic lives elsewhere and can be swapped without that digest moving.

| Library | Size | SHA-256 |
| --- | --- | --- |
| `libllama-server-impl.so` | 7,200,464 | `f08d3f6eb650cf4f…` |
| `libggml-cuda.so.0.17.0` | 63,403,272 | `07d5fd499edaab1e…` |
| `libllama.so.0.0.10107` | 4,136,032 | `d51c1a492bfdae3f…` |
| `libllama-common.so.0.0.10107` | 5,866,896 | `cd73ef37e0e99bde…` |
| `libggml-base.so.0.17.0` | 918,056 | `bc747da6b4308ac6…` |
| `libggml-cpu.so.0.17.0` | 1,362,752 | `66808a6fee4bb2de…` |
| `libggml.so.0.17.0` | 56,016 | `7ee33932f726f6cf…` |
| `libmtmd.so.0.0.10107` | 1,432,176 | `a25a0be6899a49cd…` |

llama/ggml-owned libraries are hashed because we own their identity. System
libraries are recorded by package version instead — hashing glibc would bind
the run to a patch level nobody controls and would change on an unrelated
security update, which is noise rather than identity.

Host: glibc 2.39-0ubuntu8.8, libstdc++6/libgomp1 14.2.0, CUDA SDK 13.3.1
(cudart 13.3.29, cuBLAS 13.6.0.2), NVIDIA RTX 4090 driver 610.74, 24,564 MiB,
Ubuntu 24.04.4 on kernel 6.6.114.1-microsoft-standard-WSL2, x86_64. `libcuda`
comes from the Windows host via `/usr/lib/wsl/lib` and is not a file this
repository can pin.

The closure is a **static** claim; `LD_LIBRARY_PATH` at run time decides what
actually loads, so live preflight must recheck it. A stop condition exists for
a closure that differs.

### 3. Image ids are not provenance

`apoapsis-live-controller:slice5c` carries **no labels at all** and was built
`FROM …:slice2c` plus `COPY src` from a working tree. Its id is real; no commit
can be attributed to it.

A committed, reproducible build context now exists at
`docker/pilot-controller/`. The context is a `git archive` of a pinned commit,
so an uncommitted edit has no route into the image, and the commit is written
into the image as a label so the artefact answers "which source?" itself.

| Field | Value |
| --- | --- |
| Image | `apoapsis-pilot-controller:ad13cf0` |
| Image id | `sha256:d997bd0101a8f55c…` |
| Source commit | `ad13cf0f6b4013aa5b014394fc6d33f88b29312b` |
| Source tree | `53bbf5d196cd32f0a5b8416ae933fd51cee176bb` |
| Build context SHA-256 | `324731292cf3fd37…` |
| Dockerfile SHA-256 | `934c36b09275d213…` |

Two things were learned building it. The archive pathspec is **declared**:
`spikes/native-shell-tauri` holds ~800MB of committed Rust build artifacts, and
including them made the context digest mostly bytes nobody reads. And the build
runs `--no-cache`, because a cached `LABEL` layer retains the build args of
whichever build created it — observed here, with the image's label naming a
build-context digest from an earlier, different context. A validator now
refuses an image whose label disagrees with its recorded context.

**The Qwen workcell image also has no labels**, and that is recorded honestly
rather than papered over: `provenance_proven: false` with a written reason.
Both arms use the same image, so it cannot bias the comparison, and its
interior is pinned by entry-point and package-metadata digests. Rebuilding it
with provenance is later work.

## Captured identities

| Item | Value |
| --- | --- |
| Model | `Qwen3.6-27B-Q4_K_M.gguf`, 16,817,244,384 B, `5ed60d0af4650a85…`, GGUF v3, 851 tensors, 51 KV |
| Server | `llama-server` 17,920 B, `e864afb983444e2b…`, build `10107 (c0bc8591e)`, GCC 13.3.0, BuildID `f567a72d…` |
| Server argv digest | `f5967deb61bac1c3…` |
| Qwen | `@qwen-code/qwen-code` 0.21.1, entry `cli-entry.js` 13,219 B `1db9709bf1753611…`, metadata `acc4c718b6a414aa…` |
| Tool surface | 13 names, digest over the sorted list; wire-captured, **must be reobserved at live preflight** |
| Ladder | warn 12,536 / auto 32,536 / hard 42,536, effective window 45,536, ratio 0.4965, governed by the absolute ceiling |

The reconstructed argv digests to **`f5967deb61bac1c32140610ca825a4223d2fb75da59a1a9f5466585eb7fa59b9`**, which
is byte-identical to the server-flags SHA-256 recorded independently in the
Slice 2C evaluation record. That agreement is the check that the argv in this
manifest is the argv that actually ran.

The ladder is bound, never derived. `0.85 × 65,536` is 55,706; the real trigger
is 32,536 because `computeThresholds` returns the minimum of a proportional
term and an absolute ceiling, and at this window the ceiling governs. A model
validator recomputes it and refuses a ladder contradicting its own numbers.

## Package re-issue

Renaming `sampling_seed` changed the package bytes, so the package was
re-issued and **re-qualified**. All eight real proofs pass again.

| | Before | After |
| --- | --- | --- |
| Package digest | `993e7a5610f09f0e…` | **`d7c4b195ef505975c90f21892a17f633dce6d943dc4224ef3fd01010aef25d22`** |
| Evidence digest | — | **`d6c67ce643977c938c9486069100f2b3d02f12c8e49b1c983ae65176f6da52fa`** |
| `registerable` | true | true |

The 7P.1c evaluation record's digests are amended with a note; its findings are
unchanged.

## Pair schedule, frozen before results exist

| Repetition | First arm | Second arm |
| --- | --- | --- |
| `crisis-atlas-rep-1` | control | sandbox |
| `crisis-atlas-rep-2` | sandbox | control |
| `crisis-atlas-rep-3` | control | sandbox |

Each arm gets a fresh clone of the same seed commit, byte-identical task
information, identical model and server configuration, identical spend limits,
identical arm-visible mounts, and separate worktrees, Qwen homes and evidence
directories. A pair sharing any of those three will not construct.

## What is prohibited, structurally

`broad_non_inferiority_claimed` and `held_out_qualification_claimed` are
`Literal[False]`; `default_rollout_prohibited` is `Literal[True]`. A manifest
saying otherwise does not construct. `combined_score_defined` is false: one
number combining proposal and detection quality would let either hide the
other, which is how "COMPLETE with four green commands" happened in the first
place. Eighteen stop conditions are enumerated, and omitting any one is
refused; none is convertible into a pass.

## A pre-existing flaky test, recorded rather than waived

The canonical suite at this tree ran **1,819 tests, 20 skipped, 1 failure**:
`test_workcell_relay.RelayEndToEndTests.test_a_dropped_stream_is_recorded_and_upstream_is_released`,
asserting `self.upstream.stream_aborted`.

It is intermittent and it predates this slice. Repeated five times in each tree
on the canonical Linux/ext4 environment:

| Tree | Result |
| --- | --- |
| 7P.2 working tree | 4 pass, 1 fail |
| `918bc82` baseline | 4 pass, 1 fail |

Same failure, same rate, in a module this slice does not touch. So it is not a
7P.2 regression, and it is also not nothing: a stream-abort path that reports
release only four times in five is a real defect in the relay's teardown
observation, and it sits on the egress path both pilot arms depend on. It is
recorded here and in `NEXT_STEPS.md` as work to do before live execution rather
than quietly excluded, because a test that fails one run in five will fail
during the pilot too, and an unexplained red at that point would be
indistinguishable from a containment finding.

The 20 skips are the 12 long-standing ones plus the 8 lock-gated tests in
`test_qualification_pilot.py`, which skip in the manifest commit by design and
execute in the lock commit.

## Status

`unresolved_hashes()` — **0**.
`ready_for_inference()` — **true**, which means the manifest is complete, not
that anything may run.
Manifest digest — **`0f4b0fd5930846841dae90dc4c517141bf98366886f58de55a10528d042019bc`**.

**No model service was started and no inference occurred in this phase.** The
readiness request is written into the cold/warm protocol precisely so it is
visible that it has not been executed.

Crisis Atlas remains the only qualified pilot case. This is not held-out
evidence, no model-quality claim exists, default rollout remains prohibited,
and the remaining seven corpus cases are deferred, not deleted.
