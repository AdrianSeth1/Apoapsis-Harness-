# ADR 0077: Ephemeral capability inside a workcell, durable authority outside it

- Status: Accepted
- Date: 2026-07-30
- Supersedes the execution boundary of ADR 0059 and ADR 0071. Both remain
  decision history and ADR 0071's atomic change set remains a supported
  fallback experiment. Neither is edited retroactively.
- Foundations retained: ADRs 0072-0076.

## Context

The Crisis Atlas evidence is the reason this ADR exists, and it says something
uncomfortable: the harness made the model worse.

Two arms ran the same Qwen3.6-27B Q4_K_M against the same approved plan.

| Arm | Qwen calls | Input tokens | Result before independent review |
| --- | ---: | ---: | --- |
| 64K sliced Local Power | 19 | 258,632 | Slice 2 falsely completed after a partial wrong-path service; Slices 3-4 needed repair |
| Unrestricted Qwen CLI control | 62 of 63 attempted | 2,080,801 | Coherent whole product, 88 self-authored tests passing, but a broken status filter and strict-web warnings |

The sliced arm used about **eight times fewer** input tokens and was
**substantially worse at proposing a coherent product**. That is not a token
efficiency win. It is a capability regression that the old scorecard could not
see, because there was only one score and the cheap arm looked good on it.

### Slice 2 is the specific harness defect

The Slice 2 task asked for `IncidentService`, `ExportService`, and tests. Qwen's
first response said it would implement both services, then proposed one partial
file at `services/incident_service.py` — the wrong package path — with no export
service and no tests.

The response was short, so this was not output truncation. The prompt carried
relevant repository evidence, so this was not a missing-context failure. Qwen
made a poor first proposal, and models make poor first proposals.

What happened next is ours. Apoapsis applied that one change, ran inherited
checks that never imported the new file, observed green, and terminated the
session as `COMPLETE`. Qwen never received another turn in which it could
inspect its work, notice the omissions, or implement the rest of its own stated
plan.

> The model made the incomplete edit. The harness converted an incomplete first
> edit into a final result.

### Why the existing mitigations were not enough

1. **The tests were inherited, not slice-mapped.** They stayed green precisely
   because nothing imported the new service.
2. **Verification doubled as premature termination.** ADR 0069 correctly stopped
   redundant verification loops, but "all configured checks are green" is a
   weaker claim than "the active slice is implemented". The optimization ended
   productive model work.
3. **The interface taxed the model.** The control could inspect, edit, run
   commands, and repair in a persistent shell. Local Power required one typed
   JSON action per model call and often whole file contents in the response.
   ADR 0071 improved multi-file granularity and kept the artificial envelope.
4. **The prompt supplied volume instead of navigation.** Slice prompts
   front-loaded excerpts, inherited tests, contracts, and replayed history. The
   control chose what to inspect as it worked.
5. **The budget counted protocol turns, not engineering work.** Twelve Local
   Power turns are not twelve of the control's 62 model/tool cycles.

The conclusion the evidence supports is not "give the model more turns". It is
that the action protocol itself suppressed capability the model demonstrably
has.

### The conflated authority

The standing rule says the model may not have direct shell or filesystem
authority. That rule merges two things that are not the same:

1. **ephemeral capability** inside a disposable environment; and
2. **durable authority** over the owner's repository, network, credentials,
   workflow, evidence, and delivery.

Denying (2) is why Apoapsis exists. Denying (1) bought no safety the container
boundary does not already provide, and cost most of the model's engineering
ability. The unrestricted control already ran with a real shell inside a
disposable Docker container with no network, no Docker socket, no harness
checkout, and no credentials — and it was safe.

## Decision

### 1. The boundary moves from the action grammar to the operating system

> Qwen may act like a normal coding agent inside a disposable workcell.
> Apoapsis alone decides whether any resulting delta is valid, verified,
> checkpointed, promoted, or delivered.

The new mode is the **Capability Sandbox**. It does **not** authorize a model
shell on the Windows host, and it does not authorize network or credential
access by prompt instruction.

### 2. Layer 1 — the inner workcell provides the baseline interface

The workcell runs the default Qwen coding CLI, or a conformance-tested
equivalent using the same native tool loop, and must provide all eight
capabilities the control had:

`persistent_shell`, `repository_wide_inspection`, `ordinary_file_editing`,
`arbitrary_sandbox_commands`, `self_directed_test_debug_loop`,
`multi_file_change_without_json_serialization`, `persistent_working_directory`,
and `context_continuation_or_compaction`.

These are enumerated as `BaselineCapability` in
`apoapsis/evaluation/paired.py`, observed per arm in `PairedRunManifest`, and
enforced by the capability-preservation gate. A capability that is merely
*unproven* counts as lost. An unobserved interface is not a preserved one.

The workcell also receives the approved plan as a read-only artifact mounted
**outside** the delivered project tree, and exposes exactly one signal back:
`ready_for_evaluation`.

That signal is a **request for inspection, not a completion decision**. Qwen
cannot set task state, approve a plan, promote a commit, or create a delivery.

The typed Local Power action grammar survives as an explicitly
lower-capability compatibility mode. It must not be the primary path.

### 3. Layer 2 — the container controller enforces containment

The prompt enforces nothing. The controller enforces:

| Boundary | Rule |
| --- | --- |
| Mounts | Only the disposable project clone and explicit read-only task artifacts. No host paths. |
| Container control | No Docker, Podman, or WSL control socket. |
| Credentials | No owner credentials, SSH agent, cloud metadata, browser profile, or package tokens. |
| Network | Disabled by default. Future egress allowlisting is owned by the controller, never the prompt. |
| Privilege | Non-root execution, dropped capabilities, `no-new-privileges`. |
| Resources | Process, memory, CPU, disk, output, and wall-clock ceilings. |
| Lifetime | A bounded process tree killed on stop; a filesystem destroyable without touching the source repository. |
| Audit | Immutable capture of model requests, tool calls, command output, and the final worktree fingerprint. |

Git inside the workcell is permitted **because the clone is sacrificial**.
Remotes are removed or sanitized and no network credentials exist. Apoapsis
computes the authoritative final delta and ancestry outside the model's trust
domain, so a model that rewrites its own history changes nothing that counts.

### 4. Layers 3-5 — admission, independent verification, and repair

At a checkpoint Apoapsis freezes the workcell, computes the complete delta
against the approved base, classifies changed/added/deleted/generated/test/
dependency/forbidden paths, applies patch policy to the **whole** delta,
records all findings at once, and rejects or snapshots atomically.

This keeps the useful atomicity of ADR 0071 without forcing Qwen to express its
engineering work as an atomic JSON payload. Forbidden changes never reach the
owner branch; a policy rejection returns a compact list of *all* violations.

Verification runs in a separate clean verifier workcell built from the approved
base plus the admitted delta. It must not trust Qwen's claim that tests passed,
Qwen-authored test names, a configured command's friendly label, artifacts
generated in the coding workcell, or a server Qwen left running. ADR 0074 final
integrated verification and ADR 0076 operability checks remain part of this
layer.

Repair escalates only under the approved routing policy, and a frontier role
must name a **genuinely stronger** model. Using the same Qwen endpoint with a
larger turn budget is a continuation, not frontier review; there is no silent
fallback from frontier to local, and if the stronger model is unavailable the
run stops for human review.

Every accepted repair — local, frontier, or human — becomes an authoritative
`PlanCheckpoint` carrying its parent, admitted delta, resulting commit and
fingerprint, actor class, commands and structured witnesses, obligations proved
and still open, and the transition it authorizes. Direct repair commits outside
the plan graph are evaluation evidence, not a deliverable plan. This is the gap
the Crisis Atlas Codex repairs left open.

### 5. Green tests stop being a completion decision

`verify_after_change_set` may remain a diagnostic option, but a pass cannot end
a session unless the slice's acceptance contract is also ready. A new
production component cannot complete solely because inherited tests remain
green; at least one current-state witness must prove the new path is reached,
or an owner-approved reason must explain why it is intentionally unmeasured —
which itself prevents automatic `COMPLETE`.

The implementation of that rule is Slice 4 of the handoff and is not part of
this ADR. What this ADR settles is that the harness may not treat
`ready_for_evaluation` as `COMPLETE`.

### 6. Measurement is separated, and no gate may be averaged away

`apoapsis/evaluation/paired.py` keeps three things apart:

* **Model proposal quality** — what the inner model produced before any
  external repair. A frontier repair is recorded on the delivered result and
  can never improve this.
* **Harness defect-detection quality** — what the outer system caught,
  refused, or let escape.
* **Four release gates** — capability preservation, proposal non-inferiority,
  delivered superiority, and efficiency, each reported on its own.

`PairedCorpusReport` deliberately has **no** overall score field. An
`UNMEASURED` gate never counts as a pass, and `recommended_for_default`
requires all four to pass independently.

## Consequences

### What becomes possible

A local model that can inspect, edit, run commands, and debug in a persistent
shell — the interface that produced the materially better control arm — while
Apoapsis keeps sole authority over what is admitted, verified, checkpointed, and
delivered.

### What becomes required

**A container runtime is now on the critical path for the recommended local
mode.** The legacy typed mode remains available for environments without one,
and is labelled lower-capability rather than equivalent.

**Every evaluation must record its controlled variables.** `PairedRunManifest`
treats an unrecorded variable as disqualifying, not as a match. Rescoring the
two historical Crisis Atlas arms with this rule produces `INCOMPARABLE`, because
the sliced arm's seed commit was never written down and its output cap changed
mid-run. That is the correct answer, and it is the first thing the new scorer
demonstrates.

**A capability must be observed to count.** Arms that record no capability
observations produce an `UNMEASURED` capability gate rather than a pass.

### What this does not do

It does not make the Capability Sandbox the default. The handoff's release
gates require a corpus — Crisis Atlas, Focus Orbit, a small backend change, a
cross-file refactor, a test-repair task, a launch/operability task, a task with
a deliberately misleading inherited suite, and at least one unseen repository —
with at least three seeds per task. One Crisis Atlas run promotes nothing.

It does not give the model a shell on the host, network access, credentials,
verification authority, acceptance policy, Git promotion, plan approval, or
delivery. Those denials are the durable authority this ADR keeps outside.

It does not claim superiority has been achieved. It defines the boundary inside
which superiority could be measured honestly.

### Rejected alternatives

**Keep narrowing the action grammar and add more turns.** The control had 62
model/tool cycles and a real shell; the sliced arm had a narrow protocol. More
turns through a narrow protocol was already tried in the 32K and 64K autonomous
arms and did not produce a coherent product.

**Trust the container and drop independent verification.** The control is the
counter-evidence: 88 self-authored passing tests, and a broken status filter it
never noticed. Capability inside the box does not substitute for authority
outside it.

**Let the model's `ready_for_evaluation` set task state.** This is Slice 2's
false completion with a new name.

**Call the same Qwen endpoint "frontier" when a stronger model is
unavailable.** More turns from the same model are not an independent capability
tier, and recording them as one would corrupt every escalation measurement.

**Report one combined score.** The Crisis Atlas sliced arm would have looked
good on it. The absence of the field is the enforcement.

## Verification

`tests/test_paired_scoring.py`:

* `CeilingClassificationTests` — the control's 64,409 + 1,127 = 65,536 rollover
  classifying as `INPUT_CONTEXT_EXHAUSTED` rather than an output-cap hit; the
  sliced arm's 8,192-token truncations classifying as
  `OUTPUT_CEILING_TRUNCATION`; a post-rollover provider error attributed to the
  window; a provider error *without* rollover not being a ceiling at all;
  advisory pressure kept out of both failure classes.
* `PairedCaseComparisonTests` — parity, proposal regression by case and by
  rate, superiority, capability loss including the unproven case, additional
  versus shared delivered regressions, missing evidence accepted as complete, a
  defect escaping acceptance, and mismatched or unrecorded controlled variables
  producing `INCOMPARABLE` while differing prompts and CLI versions do not.
* `ReleaseGateTests` — all four gates passing together; a cheap arm failing
  proposal non-inferiority while passing efficiency, with neither cancelling the
  other; parity without a detection advantage failing delivered superiority; an
  empty corpus producing four `UNMEASURED` gates and no recommendation; and the
  absence of any combined score field.
* `CrisisAtlasFactsTests` — Slice 2 labelled both a proposal miss and a
  detection miss, Slice 1 labelled a proposal miss only, the frozen arms
  rescoring with no provider, the historical pair resolving to `INCOMPARABLE`
  on the unrecorded seed commit, and the published telemetry preserved exactly.
* `PairedReportTests` and `PairedCliTests` — every gate rendered separately,
  `unmeasured` explicitly labelled as not a pass, and `apoapsis eval-paired`
  rescoring the frozen arms with no arguments.
