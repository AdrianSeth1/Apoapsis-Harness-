# Slice 2D: execution-profile identity, and Slice 2 complete

Date: 2026-07-30  
Evidence class: **live.** Real Docker containers, real `llama-server` serving
Qwen3.6-27B Q4_K_M at 65,536/16,384, real `@qwen-code/qwen-code@0.21.1`, real
relay and forwarder. Both paired arms ran live.

## The failure this repairs

Slice 2C scored two arms and returned `CAPABILITY_REGRESSED`. The verdict was
worthless.

The binary was genuine: `/usr/local/bin/qwen` resolves to
`@qwen-code/qwen-code@0.21.1`, correct symlink, correct `package.json`, correct
repository, one `qwen` on `PATH`, and `write_file` (81 hits) and
`run_shell_command` (72) present in the bundle. **A binary-provenance check
would have passed cleanly and told us nothing.**

What was wrong was how it was launched. Bisected one mode at a time against the
real image, capturing the session banner with no inference:

| `tools.approvalMode` | `write_file` | `edit` | `run_shell_command` |
| --- | --- | --- | --- |
| `auto` — shipped default, what both 2C arms ran | absent | absent | absent |
| `auto-edit` | present | present | absent |
| `yolo` | present | present | present |

Qwen Code's own documentation, under "Run *headless* queries in Plan Mode",
says `-p`/`--prompt` runs read-only. `tools.computerUse.enabled` also defaults
to `true`, contributing 35 `computer_use__*` tools. So both arms ran a
**read-only planner with a desktop-automation toolbelt**, not the coding agent
under evaluation. The control arm was observed calling
`computer_use__launch_app` on a task asking it to edit `calc.py`.

The accurate name is an **execution-profile identity failure**: genuine Qwen
Code, launched as the wrong kind of agent.

## The coding profile

```jsonc
"tools": {
  "approvalMode": "yolo",
  "computerUse": { "enabled": false },
  "toolSearch":  { "enabled": false }
}
```

`yolo` is defensible only because ADR 0077 already confines the agent to a
disposable `--network none` container whose sole egress is a controller-owned
socket. The container is the safety boundary; the approval prompt never was.
`toolSearch` off keeps the prompt prefix stable, which the handoff wants for
KV-cache locality and which Qwen Code's own settings reference recommends.

## What ran, in order

| Step | Result |
| --- | --- |
| Containment probes | **22/22 contained**, 0 breached, 0 unproven |
| Relay readiness | **passed** — forwarder, health, one-token completion |
| Provider-protocol conformance | **9/9**, including `declared_limits_match_server` |
| Agent binary provenance | genuine `@qwen-code/qwen-code@0.21.1`, single `PATH` candidate |
| Execution profile, both arms | `permission_mode=yolo`, 26 tools, no `computer_use__*` |
| Capability readiness, both arms | **ready** — read, in-place edit, shell, host-visible, residue-free |
| Matched agent profiles | **identical** (`046370bf32c6`) |
| Matched tiny tasks | both arms **passed**; `run_tests.py` exit 0, `subtract` defined |
| Spike verdict | **`CAPABILITY_PRESERVED`** |
| Slice 3 gate | **allowed** |

The CLI now resolves 65,536/16,384, sourced from
`generationConfig.contextWindowSize` and `generationConfig.samplingParams` on
the `modelProviders` entry — read back from the CLI's own resolver, not from
the file we wrote.

Both arms: 8 model requests, ~84,800 input tokens, 2 edits, 1 shell call, and
an independent check passing. Compare Slice 2C's ~940,000 tokens across two
arms that changed nothing.

## A threshold I changed after seeing a result

This needs stating plainly, because it is the kind of move that quietly turns a
gate into a formality.

The first scoring of this run returned `CAPABILITY_REGRESSED` with exactly one
capability missing: `arbitrary_sandbox_commands`. My Slice 2 heuristic required
**more than one** shell call, on the theory that a single invocation might be a
configured command rather than a freely chosen one.

That reasoning was wrong about where the boundary is:

* every entry in `trace.shell_calls` is the **agent's own tool call**. The
  harness's configured verification commands run through `controller.exec` and
  never enter the trace at all, so the trace already contains only
  agent-chosen commands;
* under the legacy typed protocol the count is **structurally zero** — the
  action grammar has no shell action — so 0 versus 1 is exactly the capability
  boundary;
* requiring two made the measurement depend on task size, on a task
  deliberately kept tiny so the arms would not differ by luck. A run that
  correctly needed one command was recorded as having lost the ability to run
  any.

Two things constrain the change. The rule was corrected and then applied to the
**frozen traces from the completed run** — no new inference, so it could not be
tuned against a fresh outcome. And the evidence string still states the weaker
reading: *"One call proves the agent may issue a command of its choosing; it
does not by itself demonstrate variety."*

A reader who thinks this was too convenient should read
`test_one_agent_issued_shell_call_proves_arbitrary_commands` and the two
alongside it: no shell call, and a *failed* shell call, both still record
`UNPROVEN`.

## What is now gated, and why it is four things

`evaluate_slice3_gate` requires containment, provider-protocol conformance,
agent execution-profile identity, and capability readiness. None may be
inferred from another, and Slice 2C is why:

* **conformance** proves the relay and the OpenAI envelope. It passed 9/9 while
  the process on the other end had no `write_file`. `ConformanceReport.scope`
  is now `provider_protocol` so the result cannot be read as agent readiness.
* **agent profile** proves which program ran and how it was launched. A test
  asserts every binary-identity field of the bad run was correct and the gate
  refuses it anyway.
* **capability readiness** proves the tools work, by using them. A registered
  tool is a claim; `write_file` on a read-only mount is registered and useless.

`build_spike_report` now returns `NOT_MEASURABLE`, never
`CAPABILITY_REGRESSED`, when a prerequisite is unmet. A regression verdict says
the harness took something away, and may only be said when the thing was there
to take.

## Slice 2C's arms remain invalid

Both are `NOT_MEASURABLE`. Their **~940,000 input tokens are excluded** from
model-quality and efficiency scoring. They measured a read-only planner and
tell us nothing about the harness's effect on a coding agent.

## Honest limitations

- **One tiny task, one seed, one case.** `CAPABILITY_PRESERVED` here means the
  hardened workcell did not remove a capability on *this* task. The handoff's
  release gates need the full corpus at three seeds; nothing here promotes the
  Capability Sandbox to default.
- **No compaction fired.** `context_continuation_or_compaction` is recorded as
  *gained* on a resumable session id, not on an observed compaction event. The
  near-boundary question from Slice 2C is still open, and the limit mismatch
  remains **causally consistent** with the Crisis Atlas rollover rather than
  proven.
- **`arbitrary_sandbox_commands` rests on a single shell call**, as the
  evidence string says.
- **The two arms differ only in prompt framing**, not in harness supervision:
  the sandbox arm is pointed at the task artifact and told to run the tests
  itself. Admission, acceptance, and repair are Slices 3+ and were not
  exercised. `acceptance_repair_performed` is `False`.
- **`relay.py` cannot be imported on Windows** — it defines
  `socketserver.ThreadingUnixStreamServer` at module scope. The controller runs
  on Linux, so this is not fatal, but relay tests cannot run on the host. A
  Slice 2A defect, still unfixed.

## Slice 2 is complete

Containment, conformance, identity, readiness, and a matched paired run all
hold, and `slice3-gate.json` records `allowed: true`. Slice 3 — candidate delta
admission — may begin. It was not started.
