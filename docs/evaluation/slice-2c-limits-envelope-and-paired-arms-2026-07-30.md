# Slice 2C: the limits override, the envelope check, and the paired arms

Date: 2026-07-30

Evidence class, stated once and precisely:

- **Live** — real container, real `llama-server`, real Qwen3.6-27B Q4_K_M
  weights, real agent CLI. The nine conformance checks, the containment
  probes, the relay observations, and the paired arms are all live.
- **Deterministic** — in-process tests over fixed inputs and a local echo
  provider. Named as such wherever it appears, and never counted as live
  evidence for a property only a live run can establish.

Slice 3 is **not** unblocked. See the verdict section; the reason is recorded
plainly rather than worked around.

## Bound run

| Element | Value |
| --- | --- |
| Model file | `Qwen3.6-27B-Q4_K_M.gguf`, SHA-256 `5ed60d0af4650a854b1755bd392f9aef4872643dc25a254bc68043fa638392a0` |
| Server | `llama-server` `b10107-c0bc8591e`, 65,536-token context, `-n 16384`, `--jinja`, temperature 0 |
| Server argv | recorded verbatim in `.apoapsis-eval/slice2c-2026-07-30/serve.sh` |
| Server flags SHA-256 | `f5967deb61bac1c32140610ca825a4223d2fb75da59a1a9f5466585eb7fa59b9` |
| Effective CLI config SHA-256 | `516caa2a92c3f090d8c314b208b558be77c990de89dfe27d795c64bd79612833` |
| Workcell manifest digest | `05b7ceca94b9b9f8b461094b87406ac103fa34a0f4762a4964ee849aa0e162af` |
| Evidence | `.apoapsis-eval/slice2c-2026-07-30/evidence/` and `paired-evidence/` |

Slice 2B recorded only the SHA-256 of its server argv, not the argv, so its
server invocation was not reproducible. `serve.sh` exists so this one is.

## 1. The declared-limits failure is fixed, and fixed where the owner said

The override is on the selected `modelProviders` entry, exactly as specified:

```json
"generationConfig": {
  "contextWindowSize": 65536,
  "samplingParams": { "max_tokens": 16384 }
}
```

Qwen's bundled model table was **not** patched, the model was **not** renamed,
and the server window was **not** raised. The evidence that the table is
untouched is that it still reports the wrong numbers: `declared-cli-limits.json`
records the token-limit module returning a **1,000,000**-token context window
and a 64,000-token output ceiling for `qwen3.6-27b`, unchanged from 2B.

What changed is what the CLI *resolves*. From `resolved-cli-limits.json`:

| Field | Value | Source, as the CLI reported it |
| --- | --- | --- |
| `context_window_size` | **65,536** | `modelProviders` → `generationConfig.contextWindowSize` |
| `max_output_tokens` | **16,384** | `modelProviders` → `generationConfig.samplingParams` |

This was read back by executing the CLI's own `loadSettings` and
`resolveCliGenerationConfig` inside the workcell image — the functions the CLI
itself calls on startup — not by re-reading the file Apoapsis had just written.
That distinction is the whole point of the deliverable: writing a config and
asserting the file's contents proves nothing about what the CLI resolved. The
per-field provenance above comes from the CLI's own `sources` map, so the
override is not merely present but demonstrably *winning* over the bundled
table.

## 2. The effective configuration is pinned and hashed

`pin_capture.py` captures the CLI's whole merged settings object and the
resolved generation config, redacts any credential-shaped value by replacing it
with its own SHA-256, and hashes the result into
`AgentCliPin.effective_config_sha256`, which is part of the run manifest
digest. A run whose effective config differs is therefore a different
experiment by construction.

`sources` and `warnings` are deliberately outside the digest: they are
provenance *about* the configuration, and a CLI release that reworded a warning
must not read as a configuration change.

### A bookkeeping error of mine, recorded rather than tidied away

The first gate run reported `effective_config_pin_mismatch`: observed
`516caa2a…`, pinned `7aeefaea…`. The configuration had not changed — the two
captures are byte-identical. The pinned constant had been computed by an ad-hoc
phase-A command over a *different serialisation* of the same output, so it
never agreed with `canonical_effective_config`. Recomputing the recorded raw
capture under the canonical form reproduces `516caa2a…` exactly.

The constant was corrected and the gate re-run; the second run reports no
mismatch. This is recorded because the mismatch field did its job — it caught a
hash that had been carried by hand — and because the failure mode it nearly
produced (a plausible-looking digest nobody could re-derive) is precisely what
the pins exist to prevent.

## 3. The nine conformance checks, live: 9/9

Containment held **22/22** and relay readiness passed before any check ran; the
ordering is enforced in code.

| Check | Result | Note |
| --- | --- | --- |
| role_round_trip | **passed** | 55 characters continued across all five roles |
| single_tool_call | **passed** | `echo_value` round-tripped with its argument |
| parallel_tool_calls | **passed** | two calls, names, argument objects, distinct ids, in order |
| multiline_unicode_integrity | **passed** | 158 bytes byte-for-byte through relay, forwarder, and tool-call envelope |
| thinking_block_handling | **passed** | natively supported and preserved |
| stop_reason_fidelity | **passed** | all six outcomes carry distinct provider signals |
| usage_accounting | **passed** | 24 prompt / 16 completion against a 16-token cap, consistent with `length` |
| replay_non_idempotence_guard | **passed** | the replayed response did not re-execute the mutating tool |
| declared_limits_match_server | **passed** | both report 65,536 / 16,384; CLI values from its own resolver |

The two 2B failures are the two that changed. The other seven were passing
before and still pass, on the same evidence path.

Two provocations remain uninformative and are still recorded as observations
rather than passes: `llama-server` reports nothing distinguishable for an
unknown model name (`provider_error_unreported`) or for a deliberately
oversized prompt (`context_limit_unreported`). The check passes on signal
*distinctness*; these two are distinct but uninformative, and that remains
worth the owner's attention.

## 4. Outbound requests respected the cap, observed at the relay

From `outbound-output-budget.json`, for the conformance run:

| Observation | Value |
| --- | --- |
| Requests across the relay | 16 |
| Requests carrying an explicit output budget | 14 |
| Largest budget any request asked for | **4,096** |
| Configured cap | 16,384 |
| Requests refused for exceeding the cap | 0 |

The peak is reported alongside the count of requests that carried a budget at
all, because "nothing exceeded the cap" is vacuous if nothing was inspected.
The honest scope of this claim: these sixteen requests are *conformance probe*
traffic. Agent traffic is covered by the paired run, which drives the real CLI.

A request that names no output budget is governed by the server's own `-n`
flag, which is pinned separately. That is the real limit of relay-side
enforcement and it is why the config override, not the relay, is the primary
fix.

## 5. Relay enforcement: refuse, do not clamp

`classify_request_body` in `relay_policy.py` refuses any request whose
`max_tokens`, `max_completion_tokens`, or `max_new_tokens` exceeds the run's
pinned ceiling, with `RelayRejection.OUTPUT_BUDGET_ABOVE_CAP` and a 400.

The reasoning is in the code and is deliberate. Clamping is tempting because it
always "works", and that is the objection to it: a clamped request succeeds
while the client still believes it asked for 64,000 tokens, reproducing the
Slice 2B defect one layer lower — two components disagreeing about the output
budget with nothing failing. The disagreement would then surface as a response
that stopped early for no visible reason, indistinguishable in the transcript
from a model that chose to stop. Refusing turns a silent measurement error into
a loud transport error that `conformance.py` can see.

The relay is explicitly *not* a schema validator: it reads three integer keys
on two routes and forwards anything it cannot parse. The guarantee is bounded
and stated as such — no request carrying an explicit budget above the cap
crosses the relay.

## 6. ADR 0078: the Unicode check measured the envelope, not the model

Written and accepted as `docs/adr/0078-envelope-integrity-versus-model-transcription.md`.

The old check asked Qwen to retype a payload and attributed any difference to
the adapter. On 2B's evidence it was measuring model transcription: the emoji,
its variation selector, the em-dash, and both CJK characters all survived
byte-exact, and what changed was the model retyping curly quotes as ASCII.

The new check routes a payload to a `DeterministicEchoProvider` through the
*real* relay, the real Unix socket, and the real in-container forwarder, and
compares **captured request bytes against parsed response bytes**. No model, no
sampling, no chat template. Bytes rather than `str` because two of the three
named corruptions are invisible after decoding.

The ADR states the losses rather than hiding them. The most significant is that
the echo provider does not exercise `llama-server`'s Jinja template at all;
what still gates the template is `single_tool_call` and `parallel_tool_calls`,
which assert exact argument values. Template corruption of a *long multiline
payload specifically* is now observed by the non-gating transcription metric
rather than gated. That narrowing is accepted and argued for in the ADR.
Absence of the echo path is `NOT_RUN`, which fails; there is no fallback to the
weaker evidence source.

## 7. Model transcription accuracy, preserved and non-gating

`workcell/transcription.py` keeps the original probe — real server, real
template, real model — and reports `TranscriptionFidelity` with an
`attribution` field fixed to `MODEL_BEHAVIOUR` and a `gating` field fixed to
`False`, as data rather than prose. It has no `ConformanceStatus`, is not a
`CheckResult`, and nothing in `evaluate_conformance` or `evaluate_slice3_gate`
can reach it.

This run's measurement: **not exact.** 140 characters sent, 139 received; two
substitutions, both punctuation:

| Index | Sent | Received |
| --- | --- | --- |
| 30 | `U+2018` LEFT SINGLE QUOTATION MARK | `'` APOSTROPHE |
| 37 | `U+2019` RIGHT SINGLE QUOTATION MARK | `'` APOSTROPHE |

The same finding as 2B, now correctly attributed and blocking nothing.

## 8. The paired arms: `CAPABILITY_REGRESSED`, and why that verdict is not
about capability

Both arms ran live, same seed, same weights, same server flags, same 16,384
cap, same 900-second wall-clock budget, same tiny task (add `subtract(a, b)` to
`calc.py` and extend `run_tests.py`). Two `PairedArmRecord`s were produced and
scored with `score_paired_corpus`. **`acceptance_repair_performed` is `False`**
and no acceptance repair of any kind was performed.

| | control (`default_qwen_control`) | candidate (`capability_sandbox`) |
| --- | --- | --- |
| CLI exit | 0 | 1 |
| Model requests | 37 | 26 |
| Tool calls | 20 | 13 |
| `subtract` defined afterwards | **no** | **no** |
| Case outcome | failed | failed |

`CapabilitySpikeReport.verdict` is **`capability_regressed`**;
`evaluate_slice3_gate` returns `allowed = False`. **Slice 3 is not unblocked.**

That verdict is reported as the gate produced it. It should not, however, be
read as evidence about the Capability Sandbox design or about Qwen. The five
capabilities the spike recorded as lost are:

`persistent_shell`, `ordinary_file_editing`, `arbitrary_sandbox_commands`,
`self_directed_test_debug_loop`, `multi_file_change_without_json_serialization`

and those are exactly the five that require a write, edit, or shell tool. The
agent CLI inside the workcell image **exposes none of them.** From the session
banner both arms emitted, the CLI offered 57 tools:

```
tool_search, read_mcp_resource, agent, list_agents, task_stop, send_message,
skill, list_directory, read_file, zoom_image, grep_search, glob, todo_write,
enter_worktree, exit_worktree, web_fetch, record_artifact, cron_create,
cron_list, cron_delete, loop_wakeup, create_sub_session
```

plus 35 `computer_use__*` tools. There is no `write_file`, no `edit`, and no
`run_shell_command`. Both arms spent their turns on `tool_search`, `read_file`,
`skill`, and `agent`, searching for an editing tool that was not there, and
neither ever modified `calc.py`.

This directly contradicts the pin. `AgentCliPin.tool_names` records 13 tools
captured off the wire in Slice 2B — including `edit`, `write_file`, and
`run_shell_command`. The workcell's CLI and the pinned CLI do not agree about
what the agent can do, and the presence of `computer_use__*` and `cron_*` tools
in a sanitized offline workcell is separately worth the owner's attention.

**This is an adapter/image defect, and it invalidates the paired measurement as
a capability comparison.** An arm that cannot write a file cannot demonstrate
file editing, so the comparison measured tool availability, not capability. The
correct reading is that quality was **not validly measured**, the verdict is
`CAPABILITY_REGRESSED` on its face, and Slice 3 stays blocked either way. No
attempt was made to reinterpret the verdict into a pass.

Note that conformance passed 9/9 immediately before this. That is not a
contradiction — it is a coverage gap worth naming. The nine checks exercise the
provider envelope (roles, tool-call encoding, Unicode, stop reasons, usage,
limits). None of them asserts that the CLI's *installed toolset* matches the
pinned one, which is why a CLI with no editing tools passed every check.

## 9. The near-boundary rerun: no rollover, but no compaction either

A near-boundary condition was reached, incidentally rather than by
construction: the control arm's largest single request carried **58,038 input
tokens against the 65,536-token window — 88.6% of it.**

What was observed at that point:

- **No context rollover.** No ceiling event of any kind was recorded in either
  arm, no provider error followed, and the control arm's session ended
  normally with exit 0.
- **No compaction fired.** Zero compaction events in either arm. The event
  adapter recorded zero unrecognised event types, so this is not a parsing
  gap; the string `compress` appears in these transcripts only as the name of
  a manual `/compress` slash command in the CLI's banner.

The honest conclusion, stated as the brief requires:

> The declared-limit mismatch remains a **causally consistent** explanation for
> the Crisis Atlas rollover. It is **not proven**, and it is **not** described
> here as the root cause.

The reasoning is that this run establishes the negative half only. Under the
corrected configuration the CLI computes its budget against a 65,536-token
window and a session reaching 88.6% of that window did not roll over. Under the
2B configuration the CLI believed the window was 1,000,000, and a threshold
computed against that number would not be crossed until far past the real
window — which is the shape of the Crisis Atlas failure. But because compaction
did not actually fire in this run, the positive half — *compaction firing in
time* — has **not** been demonstrated. A run that crosses the threshold and
compacts is still owed.

## 10. Defects found in this slice's own work

Recorded because the useful thing about 2B's report was that it named its own
false positive.

1. **A wrong effective-config pin constant.** Carried by hand from an ad-hoc
   computation over a different serialisation. Caught by the mismatch field,
   corrected, re-run. Section 2.
2. **The event adapter could not read the CLI's event stream.** It expected
   flat top-level `tool_use`/`tool_result` events; the CLI nests them inside
   `message.content`. The first paired run therefore recorded **zero** tool
   calls across a 158-event session containing forty-four of them, with zero
   malformed lines and zero unrecognised types — a parser that failed by
   finding nothing without ever saying so. The spike read that empty trace as
   **seven** lost capabilities. Fixed, covered deterministically in
   `NestedEventEnvelopeTests`, and re-run; the list dropped to five, and the
   remaining five are the toolset defect in section 8. Had this not been
   chased, Slice 2C would have reported a capability regression that was
   entirely an artefact of its own parser.
3. **The control arm never started in the first paired run.** Its prompt was
   interpolated into an `sh -c` string with `json.dumps`, which is not shell
   quoting; the task text contains backticks around `calc.py` and
   `python3 run_tests.py`, so the shell attempted command substitution and
   exited 2 before `qwen` ran. The supervised arm's prompt happened to contain
   no backticks, so only one arm was silent — which looked exactly like a
   capability difference. Fixed with `shlex.quote`.
4. **Slice 2B's server argv was not reproducible** (only its SHA-256 was
   recorded). Fixed forward by `serve.sh`; not a defect introduced here, but it
   cost time in this slice.

## Verdict

- Conformance: **9/9 live.** Both Slice 2B failures fixed, at the source the
  owner specified.
- Outbound cap: **respected**, observed at the relay, with the scope of the
  claim stated.
- Relay enforcement: **added**, refusing rather than clamping, with the
  argument recorded in the code.
- ADR 0078: **accepted**, with its losses stated as losses.
- Transcription accuracy: **preserved, non-gating, and still inexact.**
- Paired spike: **`CAPABILITY_REGRESSED`**, explained by a workcell toolset
  defect that makes the measurement invalid as a capability comparison.
- Near-boundary: **no rollover at 88.6% of the window; no compaction observed.**
  Causally consistent, not proven.

**Slice 3 is blocked.** Slice 3 work was not begun.
