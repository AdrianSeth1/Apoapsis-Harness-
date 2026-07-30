# Slice 2B: live conformance, captured pins, and the Slice 3 gate

Date: 2026-07-30

Evidence class: **live** — real container, real `llama-server`, real Qwen
weights, real Qwen Code CLI. Every number below was observed in this session.
Deterministic coverage is labelled as such and never counted as live evidence.

Result: **conformance failed closed; the capability spike is `NOT_MEASURABLE`;
Slice 3 remains blocked.** No agent quality was measured, and no acceptance
repair was performed.

## Bound run

| Element | Value |
| --- | --- |
| Model file | `Qwen3.6-27B-Q4_K_M.gguf`, SHA-256 `5ed60d0af4650a854b1755bd392f9aef4872643dc25a254bc68043fa638392a0` (hashed this session) |
| Server | `llama-server` `b10107-c0bc8591e`, 65,536-token context, `-n 16384`, `--jinja`, temperature 0 |
| Server flags SHA-256 | `d381954139bda50d8cefb6b76ef54197b4949f9bb2ab577a92f903577664a06e` |
| Workcell image | `apoapsis-qwen-workcell@sha256:c19f73760b126b5af459870d9c8ebde1c78110370c8bc7e27ddfcea0debbb4ea` |
| Agent CLI | `@qwen-code/qwen-code` 0.21.1, bundle SHA-256 `4c6ff2aef38173d6aa5e0ee47e4a72cd401788b9435eab254e811c02597fd811` |
| Seed commit | `5458bc093ddd1076b132c04a245d5cb5918f8dd4` |
| Runtime | Docker 29.5.2; controller and clone on the Docker Desktop VM's ext4 volume |
| Workcell manifest digest | `14921cb7db7ccbff23eff6787ad9e11e1a0bd5b643b720d70dbec4bdf865a74a` |

The owner's two pre-existing containers were not touched. The workcell
container and the relay socket were destroyed at the end of each run.

## Pins actually captured

All four previously-missing identity fields are now **real**, taken from the
CLI and the server rather than written by hand. The Slice 2 gate had filled
three of them with the installed-bundle hash as an admitted stand-in; those
stand-ins are gone.

| Field | Value | How it was obtained |
| --- | --- | --- |
| `chat_template_sha256` | `55d4931433fe502b794226ee7f4d206a6bdd436ac9f80eb7d8ebb4c639f9ea0c` | `llama-server` `/props`, i.e. the template the server loaded |
| `system_prompt_sha256` | `dd7a7426bd704a06773a2cceb5c483abbfce3c32887b14763b0c0d5f5f61ae16` | off the wire, from the request body the real CLI sent (27,594 characters) |
| `tool_schema_sha256` | `c2da51e70e5dff233b3e5b583d01233133a244c7f7b4f0c14f5045b108b518a5` | off the wire, canonicalised by sorted tool name |
| `tool_names` | 13 names, sorted and unique | off the wire |

The 13 tool names are `agent`, `edit`, `glob`, `grep_search`, `list_agents`,
`list_directory`, `notebook_edit`, `read_file`, `run_shell_command`, `skill`,
`todo_write`, `tool_search`, `write_file`. `RelayPin.allowed_routes` is sorted.

Capture used `WireCaptureUpstream`, a throwaway recorder placed between the CLI
and the server for a single invocation. One limitation is worth recording: the
recorder buffers, so the CLI's streaming request returned a non-SSE response and
the CLI reported an API error. The *request* — which is all that pin capture
reads — was captured verbatim before forwarding, so the pins are sound, but the
recorder is not usable for anything that needs a working response.

## The nine conformance checks, live

Run inside the workcell, through the loopback forwarder, the controller-owned
Unix socket, and the relay — the same path the agent's requests take.

| Check | Result | Note |
| --- | --- | --- |
| role_round_trip | **passed** | system/user/assistant/tool-call/tool-result accepted and continued |
| single_tool_call | **passed** | `echo_value` round-tripped with its argument intact |
| parallel_tool_calls | **passed** | two calls, names, argument objects, distinct ids, in order |
| multiline_unicode_integrity | **failed** | 140 characters sent, 139 received — see below |
| thinking_block_handling | **passed** | natively supported and preserved |
| stop_reason_fidelity | **passed** | all six outcomes carry distinct provider signals |
| usage_accounting | **passed** | 24 prompt / 16 completion tokens against a 16-token cap, consistent with `length` |
| replay_non_idempotence_guard | **passed** | the replayed response did not re-execute the mutating tool |
| declared_limits_match_server | **failed** | CLI declares 1,000,000 / 64,000; server reports 65,536 / 16,384 |

Containment held **22/22** and relay readiness passed (health, model listing,
and a one-token completion) before any of these ran. The ordered gate is
enforced in code: containment, then readiness, then conformance, each stopping
the ones after it.

### The decisive failure: declared limits

This is a genuine defect and the most important finding of the slice. The CLI,
asked by executing its own token-limit module inside the image, believes
`qwen3.6-27b` has a **1,000,000-token context window** and a 64,000-token
output ceiling. The server reports **65,536**. The CLI's belief is not a
fallback guess — the model matches an entry in its known-model table
(`knownTokenLimit` returns 1,000,000), so the wrong number is authoritative to
it.

A CLI that thinks its window is fifteen times larger than it is will not
compact until long after the real window is gone. That is precisely the
failure mode the Crisis Atlas trial hit and could not explain, and this check
now names its root cause. It is an adapter/configuration defect, not model
behaviour, and per the ordered procedure no agent quality was measured.

### The other failure, and what it actually shows

`multiline_unicode_integrity` failed, but the diagnosis does **not** support
calling the transport lossy. A targeted follow-up probe showed the astral-plane
emoji `U+1F6F0` with its `U+FE0F` variation selector, the em-dash, and both CJK
characters all survived byte-exact. What changed was the model retyping the
typographic quotes `U+2018`/`U+2019` as ASCII `'`.

So this check, as written, asks the model to transcribe a string and then
attributes any difference to the adapter. On this evidence it is measuring
model transcription, not envelope integrity. **The check has been left failing
and unmodified.** Loosening a gate to make a run pass is the one move this
codebase exists to prevent, and the honest report is that the check does not
currently isolate what it claims to isolate. Reworking it to compare what the
relay carried against what the relay received — rather than what the model
retyped — is recommended follow-up work for the owner, not something to do
silently inside a failing run.

### A defect in this slice's own driver, found and fixed

The first live conformance run reported `stop_reason_fidelity` as failed, with
`length` collapsing normal completion, the output limit, and tool calls into one
signal. That was **my driver's fault, not the adapter's**. The probes capped
output at 8–256 tokens, and a reasoning model spends its budget on reasoning
before emitting content or a tool call, so every probe terminated at the cap.

The provocations now carry `REASONING_HEADROOM_TOKENS` (2,048) except the two
that deliberately want the cap. On the re-run, the six outcomes came back
distinct — `stop`, `tool_calls`, `length`, and separate cancellation and error
signals — and the check passed. Reporting the first run's result as an adapter
defect would have been a false positive of exactly the kind this suite exists
to prevent, so it is recorded here rather than quietly overwritten.

Two provocations remain unresolved and are recorded as observations, not
passes: `llama-server` reported nothing distinguishable for an unknown model
name (`provider_error_unreported`) and nothing for a deliberately oversized
prompt (`context_limit_unreported`). The check passed on signal *distinctness*;
these two signals are distinct but uninformative, which is worth the owner's
attention.

## Sanitized clone automation

`create_sanitized_clone` now builds the disposable workcell tree end to end, and
the containment probes were re-run against its output to prove it: shallow clone
of the seed commit, `origin` removed, no `.apoapsis`, no `.sol`, no credential
material, no local Git config naming a network or identity, and the task
artifact written **outside** the clone. It fails closed — an audit finding
destroys the clone rather than returning it with a warning.

One defect was found by the probes rather than by inspection. The first run
produced a root-owned clone, and `workspace-writable` correctly reported a
breach: the workcell user could not edit its own worktree. Ownership is now
handed to the workcell's numeric uid:gid, symlinks are not followed while doing
so, and the directory stays owned by the controller. Containment went from
21/22 to 22/22.

A second controller defect surfaced at readiness: the socket directory was
created with the controller's group, so the relay propagated group 0 to the
socket and the first workcell connection failed with `EACCES`. The session now
grants the directory the workcell's group and sets setgid before the relay
binds. This is the same class of bug the owner's `platform_support.py` edit
addresses, at the one layer above it; `platform_support.py` itself was not
modified.

## Paired tasks: deliberately not run

The tiny matched `default_qwen_control` / `capability_sandbox` tasks were
**not** run, and no `PairedArmRecord` or scorecard exists. Conformance failed,
and measuring agent quality through an adapter whose declared context window is
wrong by a factor of fifteen would produce numbers that look real and mean
nothing. `CapabilitySpikeReport.acceptance_repair_performed` is `False`.

## Spike verdict and the Slice 3 gate

`build_spike_report` returned **`not_measurable`**: "this run is not a valid
capability experiment." The session trace is genuinely empty, because no agent
session was run; it was not populated with a placeholder, which would have let
`observe_capabilities` credit capabilities nobody demonstrated.

`evaluate_slice3_gate` returns `allowed: false` with three blockers: conformance
did not hold, the workcell demonstrated none of the seven baseline capabilities,
and the verdict is not `capability_preserved`.

**Slice 3 (candidate delta admission) is blocked and was not begun.**

## Verification

- Live: containment 22/22, readiness passed, nine conformance checks executed
  against the real endpoint through the real relay path.
- Deterministic: `tests/test_workcell_conformance_driver.py`, 36 new tests,
  passing on Windows CPython 3.12 and in the Linux sandbox.
- Linux sandbox, combined focused suites
  (`test_workcell`, `test_workcell_relay`, `test_workcell_conformance_driver`,
  `test_paired_scoring`): 192 tests. One error appeared in
  `test_an_oversized_body_is_refused_without_reaching_upstream` only in the
  combined run; `test_workcell_relay` passes 57/57 in isolation and `relay.py`
  was not modified in this slice, so this is recorded as a cross-test
  interaction on the sandbox's Python 3.10 shim rather than a regression.
- `python -m compileall -q src tests` — passed.
- The full suite was not run to completion here and no full-suite result is
  claimed. The standing baseline of 12 pre-existing failures was not added to.

Raw evidence: `.apoapsis-eval/slice2b-runtime-2026-07-30/evidence/`
(`containment.json`, `readiness.json`, `conformance.json`, `stop-signals.json`,
`declared-cli-limits.json`, `clone.json`, `spike-report.json`,
`slice3-gate.json`, `workcell-config.json`).

## What the owner should decide next

1. Reconcile the declared limits. Either correct the CLI's table entry for this
   model or serve the window the CLI expects. Until then no paired measurement
   through this CLI is trustworthy.
2. Decide what `multiline_unicode_integrity` should compare. It currently fails
   on model transcription, which is not what it claims to test.
3. Consider whether `llama-server` silently accepting an unknown model name is
   acceptable at the relay boundary.
