# Apoapsis Harness: Current Architecture and Handoff

Read this before changing the project. This document is the canonical map of
the system as it exists now. ADRs in `docs/adr/` preserve decision history,
dated files in `docs/evaluation/` preserve live evidence, `README.md` is the
user guide, and `NEXT_STEPS.md` contains only active priorities.

When documentation and code differ, implementation plus deterministic tests are
operational truth and the documentation must be corrected in the same change.

**The section rule.** Sections here describe the system *as it is*. Anything
phrased as "on date X, Y happened" — what was implemented when, which suite was
run in which session, what a live attempt exposed — belongs in an ADR or in
[`docs/history/handoff-archive-2026-08.md`](docs/history/handoff-archive-2026-08.md),
never here. This file had grown to 179 KB of accreted session narrative, and
`AGENTS.md` makes every coding agent read it, so each stray paragraph was a tax
on every future session. Add a row to the Snapshot with a one-line status and
put the story where stories go.

## Snapshot

Current status only, one line each. The full narrative for every row lives in
[`docs/history/handoff-archive-2026-08.md`](docs/history/handoff-archive-2026-08.md);
the decision itself lives in its ADR. Newest first.

| Item | Status | Date | Full record |
| --- | --- | --- | --- |
| ADR 0115 stop releases model memory | Implemented; 10 focused tests green and **verified live: 17.3 GB of VRAM freed** (19,924 -> 2,653 MiB used). Stop now stops a loopback llama-server, identifying it by the model file the endpoint itself reports, never by port. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0115-stop-releases-model-memory) · [ADR](docs/adr/0115-stop-releases-model-server-memory.md) |
| ADR 0114 the hiring package | Implemented; 9 drift-guard tests green. Public README (200 lines), Crisis Atlas writeup, demo script and publication checklist. Every public figure is asserted against the dated evidence file it came from. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0114-the-hiring-package) · [ADR](docs/adr/0114-the-hiring-package.md) |
| ADR 0113 warm the controller image | Implemented and **verified against real Docker**: the launcher warms the per-commit controller image (34.3s measured; 0.4s no-op when present) instead of the next slice paying for it invisibly. The build now reports itself as a status stage. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0113-warm-the-controller-image) · [ADR](docs/adr/0113-warm-the-controller-image.md) |
| ADR 0112 live run status | Implemented; 25 focused tests green. A run now writes an append-only progress journal while it works; one polled page shows stage, calls, prompt-vs-window and the last check in operator language. Launcher image prebuild deferred. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0112-live-run-status) · [ADR](docs/adr/0112-live-run-status.md) |
| ADR 0111 green suite for strangers | Suite green on a clean checkout: 2,075 tests, 0 failures, 0 errors, 48 skips, ~975s (py3.14.5/Windows). The 78-problem baseline was five causes; one was a real product defect. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0111-green-suite-for-strangers) · [ADR](docs/adr/0111-a-green-suite-for-strangers.md) |
| ADR 0110 prompt ordering and token ceiling | Implemented; focused coverage green. Step prompts ordered stable-to-volatile for KV reuse; assembled prompt measured against the window, shrunk in a fixed order or refused with a named stop. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0110-prompt-ordering-and-token-ceiling) · [ADR](docs/adr/0110-prompt-ordering-and-token-ceiling.md) |
| ADR 0109 one local execution path | Sandbox default in code as well as template; Local Power named legacy; SessionCoordinator deleted; doctor checks the default path. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0109-one-local-execution-path) · [ADR](docs/adr/0109-one-local-execution-path.md) |
| ADR 0108 parity guard sampling policy | Implemented; focused coverage green. Default pairs the first slice and every 4th instead of every slice. Escalation unchanged. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0108-parity-guard-sampling-policy) · [ADR](docs/adr/0108-parity-guard-sampling-policy.md) |
| ADR 0107 model-server lease across arms | Implemented; focused coverage green. One verified load per run instead of one per arm per attempt. Live timing not yet measured. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0107-model-server-lease-across-arms) · [ADR](docs/adr/0107-model-server-lease-across-arms.md) |
| ADR 0106 deterministic orientation brief | Implemented; focused coverage green. Slice N is told what earlier slices built instead of rediscovering it. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0106-deterministic-orientation-brief) · [ADR](docs/adr/0106-deterministic-orientation-brief.md) |
| ADR 0105 packet dedup and operator-readable stops | Implemented; focused coverage green. Repair rules stated once; every stop renders as attempted/refused/next action. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0105-packet-dedup-and-operator-readable-stops) · [ADR](docs/adr/0105-one-rule-per-packet-and-operator-readable-stops.md) |
| ADR 0104 patch changed-line ceiling | `[patch] max_changed_lines` default 500 to 5000, matching live projects; `max_files` stays 20. Drift-guard test added. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0104-patch-changed-line-ceiling) · [ADR](docs/adr/0104-patch-changed-line-ceiling-for-slice-sized-work.md) |
| ADR 0103 stated judgement contract | Implemented; focused coverage green. The slice brief and every repair packet now state how proof is established. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0103-stated-judgement-contract) · [ADR](docs/adr/0103-state-the-judgement-contract.md) |
| ADR 0102 repository metadata excluded at the walk | Implemented; focused coverage green. `.git` can no longer reach a reviewer-facing surface. Live confirmation pending. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0102-repository-metadata-excluded-at-the-walk) · [ADR](docs/adr/0102-vcs-metadata-excluded-at-the-walk.md) |
| MH-3 HANDOFF diet | Snapshot, ADR-index commentary and the ADR 0050 spike moved verbatim to the archive; 179 KB to 72 KB. Further passes listed in NEXT_STEPS. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#mh-3-handoff-diet) |
| ADR 0101 relay-observed model usage | Implemented; deterministic coverage green on Windows and WSL. Not yet exercised on a live run. | 2026-08-03 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0101-relay-observed-model-usage) · [ADR](docs/adr/0101-relay-observed-model-usage.md) |
| ADR 0100 ext4 runtime / Windows audit separation | Implemented, focused-tested, live-containment-proven, and the reported task reset ready | 2026-08-01 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0100-ext4-runtime--windows-audit-separation) · [ADR](docs/adr/0100-ext4-runtime-windows-audit-separation.md) |
| ADR 0099 exact controller-copy Git trust | Implemented, focused-tested, container-proven, and the reported task reset ready | 2026-08-01 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0099-exact-controller-copy-git-trust) · [ADR](docs/adr/0099-exact-controller-base-safe-directory.md) |
| ADR 0098 zero-session Capability Sandbox retry | Implemented, focused-tested, live-preflighted, and the reported task reset ready | 2026-08-01 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0098-zero-session-capability-sandbox-retry) · [ADR](docs/adr/0098-zero-session-capability-sandbox-retry.md) |
| ADR 0097 approved-plan package binding | Implemented, focused-tested, and the reported project repaired | 2026-08-01 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0097-approved-plan-package-binding) · [ADR](docs/adr/0097-hash-bound-approved-plan-in-slice-packages.md) |
| ADR 0096 imported plan-response transfer recovery | Implemented, corrected against the reported real project, and focused-tested in the working tree | 2026-08-01 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0096-imported-plan-response-transfer-recovery) · [ADR](docs/adr/0096-exact-imported-plan-response-transfer-recovery.md) |
| ADR 0095 Slice 8 product rollout | Implemented, committed, and verified; live ordinary-task evidence pending. | 2026-08-01 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0095-slice-8-product-rollout) · [ADR](docs/adr/0095-capability-sandbox-product-rollout-and-fallback.md) |
| ADR 0094 frontier-plan validation and auto-run UX | Implemented and verified in the working tree | 2026-08-01 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0094-frontier-plan-validation-and-auto-run-ux) · [ADR](docs/adr/0094-auto-validate-frontier-plans-and-explicit-auto-run-state.md) |
| ADR 0093 friendly launcher setup | Implemented and focused-tested in the working tree | 2026-08-01 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0093-friendly-launcher-setup) · [ADR](docs/adr/0093-friendly-launcher-owned-project-setup.md) |
| ADR 0092 plan auto mode | Implemented, focused-tested, and browser-exercised | 2026-08-01 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0092-plan-auto-mode) · [ADR](docs/adr/0092-one-authorization-for-controller-owned-plan-progression.md) |
| Crisis Atlas live pilot v4 | Six live slots complete and independently scored | 2026-08-01 | [narrative](docs/history/handoff-archive-2026-08.md#crisis-atlas-live-pilot-v4) |
| ADR 0091 live Crisis Atlas runner | Minimal evaluator ownership correction rebuilt, checked, and rebound; ready for a fresh v4 six-slot run | 2026-08-01 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0091-live-crisis-atlas-runner) · [ADR](docs/adr/0091-separate-live-pilot-authorization-and-operator-launch.md) |
| ADR 0077 Slice 2 live gate | Partially proven, blocked at conformance | 2026-07-30 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0077-slice-2-live-gate) · [ADR](docs/adr/0077-capability-sandbox-authority-boundary.md) |
| ADR 0077 Slice 2C conformance and paired arms | Conformance fully proven live, quality measurement invalid, Slice 3 still blocked | 2026-07-30 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0077-slice-2c-conformance-and-paired-arms) · [ADR](docs/adr/0077-capability-sandbox-authority-boundary.md) |
| Crisis Atlas unrestricted Qwen CLI control | Live local evidence | 2026-07-30 | [narrative](docs/history/handoff-archive-2026-08.md#crisis-atlas-unrestricted-qwen-cli-control) |
| Qwen baseline-preserving superiority handoff | Design assignment, not implemented | 2026-07-30 | [narrative](docs/history/handoff-archive-2026-08.md#qwen-baseline-preserving-superiority-handoff) |
| Crisis Atlas 64K Codex-frontier trial | Live local evidence–30. | 2026-07-29 | [narrative](docs/history/handoff-archive-2026-08.md#crisis-atlas-64k-codex-frontier-trial) |
| Last verified | Through ADR 0037 on, 61 focused tests and the full 722-test deterministic suite passed with 10 expected skips, plus compileall and `git d... | 2026-07-21 | [narrative](docs/history/handoff-archive-2026-08.md#last-verified) |
| ADR 0059 status | Deterministic fake-provider boundary suite verified on: `python -m unittest tests.test_local_power_session -v` passed 35/35 with 1 expect... | 2026-07-26 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0059-status) · [ADR](docs/adr/0059-local-power-sandbox-execution-mode.md) |
| ADR 0060 status | Deterministic config/provider coverage verified on: `python -m unittest tests.test_cli tests.test_provider_and_specification -v` passed 2... | 2026-07-26 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0060-status) · [ADR](docs/adr/0060-laguna-llama-server-default.md) |
| ADR 0061 status | Deterministic UI toggle coverage verified on: `python -m unittest tests.test_ui -v` passed 27/27 and `python -m unittest tests.test_execu... | 2026-07-26 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0061-status) · [ADR](docs/adr/0061-local-power-ui-toggle.md) |
| ADR 0062 status | Deterministic launcher/lifecycle coverage verified on: `python -m unittest tests.test_operator_lifecycle tests.test_launcher -v` passed 2... | 2026-07-26 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0062-status) · [ADR](docs/adr/0062-start-launcher-and-llama-server-lifecycle.md) |
| ADR 0063 status | Fixes the two harness-side defects found in live task `TASK-EF33C00E5BD4` (): reviewer-facing `files_changed` included Python bytecode wr... | 2026-07-26 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0063-status) · [ADR](docs/adr/0063-changed-path-classification-and-structured-edit-eof-normalization.md) |
| ADR 0064 status | Found during the live run: discovery session `DISC-7D87B2379D8E` dead-ended at step 3 with a bare `Failed to fetch`. Root cause was a pro... | 2026-07-26 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0064-status) · [ADR](docs/adr/0064-legible-unborn-head-errors-and-no-silent-ui-dead-ends.md) |
| ADR 0065 status | Found during the live run: importing a real frontier plan (10 components, 8 integration contracts, 17 slices) failed with `request body s... | 2026-07-26 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0065-status) · [ADR](docs/adr/0065-per-route-request-body-ceilings-for-pasted-model-responses.md) |
| ADR 0066 status | Found during the live run: a frontier plan was rejected with eight `extra_forbidden` errors confined to `delivery_contract` and `verifica... | 2026-07-26 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0066-status) · [ADR](docs/adr/0066-literal-response-shape-in-the-frontier-planning-handoff.md) |
| ADR 0067 status | Third failed round-trip on the same planning handoff: the pasted response began with a `` `json `` Markdown fence, so `json.loads` failed... | 2026-07-26 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0067-status) · [ADR](docs/adr/0067-bounded-normalization-of-operator-pasted-json.md) |
| ADR 0068 status | Live Qwen discovery responses included a leading `<think>...</think>` wrapper before otherwise valid JSON. Discovery now strips only that... | -- | [narrative](docs/history/handoff-archive-2026-08.md#adr-0068-status) · [ADR](docs/adr/0068-local-reasoning-wrapper-normalization.md) |
| ADR 0069 status | Two independent defects found in live task `TASK-33E0EB6476C4` (, Local Power, Laguna S 2.1). (1) A passing verification did not end the... | 2026-07-27 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0069-status) · [ADR](docs/adr/0069-verification-sufficiency-termination-and-contract-strength.md) |
| ADR 0070 status | Found in live task `TASK-E01762481075` (), the ADR 0069 rerun. The harness performed correctly — 6 turns, 3 writes, 2 verification runs,... | 2026-07-27 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0070-status) · [ADR](docs/adr/0070-failure-evidence-reaches-the-local-power-repair-model.md) |
| ADR 0071 status | Live task `TASK-A0E17C03D69B` (, Local Power, `openai_compatible/qwen3.6-27b`; `EXOP-FE26BA8810574A6F9C9F3888` then `RVOP-37315CE9A0184AC... | 2026-07-27 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0071-status) · [ADR](docs/adr/0071-atomic-slice-proposals-in-local-power.md) |
| ADR 0076 status | Crisis Atlas remediation slice D. The approved plan named a launch command and required a README, and neither was proven: the delivered g... | 2026-07-29 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0076-status) · [ADR](docs/adr/0076-structured-operability-contract.md) |
| ADR 0075 status | Closes ADR 0074's remaining implementation gap. The contradiction check needs a planner to populate `IntegrationContract.runtime_boundary... | 2026-07-29 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0075-status) · [ADR](docs/adr/0075-the-planner-handoff-asks-for-runtime-boundary.md) |
| Full suite, ADRs 0075-0076 | Run to completion on | 2026-07-29 | [narrative](docs/history/handoff-archive-2026-08.md#full-suite-adrs-0075-0076) |
| Full suite, ADRs 0072-0074 | Run to completion on | 2026-07-29 | [narrative](docs/history/handoff-archive-2026-08.md#full-suite-adrs-0072-0074) |
| ADR 0072 status | Fixes the stale-delivery defect found in the preserved Crisis Atlas run (`PLAN-E1B90639E58D`, `qwen3.6-27b` at 32K). Slice 4 (`TASK-5494B... | 2026-07-29 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0072-status) · [ADR](docs/adr/0072-current-task-evidence-projection.md) |
| ADR 0073 status | Crisis Atlas remediation slice B. `verify-web-product` treated every `fetch`/`XMLHttpRequest`/`WebSocket`/`EventSource` as forbidden unde... | 2026-07-29 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0073-status) · [ADR](docs/adr/0073-same-origin-requests-and-web-check-evidence.md) |
| ADR 0074 status | Crisis Atlas remediation slice C. Every slice was verified in isolation and nothing ever executed against the combined result: `prepare_p... | 2026-07-29 | [narrative](docs/history/handoff-archive-2026-08.md#adr-0074-status) · [ADR](docs/adr/0074-final-integrated-project-verification.md) |
| Slice 7P.1a/7P.1b qualification packages | Deterministic only, one case authored and validated, no inference. 7P.1a `2ee8afd`; 7P.1b this commit. | 2026-07-29 | [narrative](docs/history/handoff-archive-2026-08.md#slice-7p1a7p1b-qualification-packages) |
| Slice 7P.1c real qualification | Deterministic only, real evidence, no inference. This commit. | 2026-07-31 | [narrative](docs/history/handoff-archive-2026-08.md#slice-7p1c-real-qualification) |
| Slice 7P.2 Crisis Atlas pilot freeze | Identity and configuration capture only. No `llama-server` start, no model load, no readiness request, no inference, no rehearsal. | 2026-07-31 | [narrative](docs/history/handoff-archive-2026-08.md#slice-7p2-crisis-atlas-pilot-freeze) |
| Slice 7P.3 rehearsal | BLOCKED at the executable-provenance gate. The rehearsal did not execute. | 2026-07-31 | [narrative](docs/history/handoff-archive-2026-08.md#slice-7p3-rehearsal) |
| Slice 7P.2S supersession | No `llama-server`, no GGUF load, no readiness call, no inference, no rehearsal. | 2026-07-31 | [narrative](docs/history/handoff-archive-2026-08.md#slice-7p2s-supersession) |
| Version/state | Committed implementation through ADR 0095 plus working-tree ADR 0096, including plan auto-run, friendly setup, auto-validation UX, Slice... | -- | [narrative](docs/history/handoff-archive-2026-08.md#versionstate) |
| Branch | `codex/slice2-live-gate` | -- | [narrative](docs/history/handoff-archive-2026-08.md#branch) |
| Preserved tag | `substrate-v0.1` at `4c2e735`; never move or delete it. | -- | [narrative](docs/history/handoff-archive-2026-08.md#preserved-tag) |
| Live local coding | Qwen3-Coder-Next Q4 has completed controlled tasks, but reliability is not established. A six-run planning comparison reached 0/6; two la... | -- | [narrative](docs/history/handoff-archive-2026-08.md#live-local-coding) |
| Live hosted coding | Not run. Hosted paths have deterministic fake-provider coverage only. | -- | [narrative](docs/history/handoff-archive-2026-08.md#live-hosted-coding) |
| Live browser | Task intake/execution, review, plans/slices, discovery/manual frontier, launcher, and guided-workflow surfaces have each been exercised i... | 2026-08-01 | [narrative](docs/history/handoff-archive-2026-08.md#live-browser) |
| Live Docker | The pinned `python:3.12-slim` sandbox success path and isolation checks passed on See `docs/evaluation/apoapsis-d5a-live-docker-evidence-... | 2026-07-20 | [narrative](docs/history/handoff-archive-2026-08.md#live-docker) |

## Product thesis

Apoapsis is a local-first, auditable control plane for verified AI coding. It
makes bounded local or hosted models useful by giving them typed opportunities
to inspect, propose edits, and request configured checks. Apoapsis—not a
model—owns context selection, action execution, patch validation, retry ceilings,
workflow transitions, verification, completion, and audit records.

The intended primary flow is:

```text
request -> structured specification -> user approval -> deterministic context
-> deterministic route -> bounded coding proposals -> patch policy
-> configured verification -> COMPLETE or HUMAN_REVIEW_REQUIRED
```

Larger work may first pass through discovery, optional planning research, a
frontier/manual architecture plan, automatic deterministic validation, explicit
human approval, and then one controller-authorized automatic run or one slice
at a time.

## Non-negotiable authority boundary

| Capability | Authority |
| --- | --- |
| Interpret a request or propose a plan | Model proposes typed data; schemas and user approval govern acceptance. |
| Preserve hard constraints | Deterministic validation retains exact user source text. |
| Select repository context | Context compiler. |
| Choose local/frontier/human route | Deterministic routing from risk and configuration. |
| Read/search/edit/request checks | Model requests one typed action; Apoapsis validates and performs it. |
| Access external research | Restricted source adapters fetch allowlisted sources; models receive sanitized evidence only. |
| Apply a patch | Unified-diff parser, policy validator, and Git applier. |
| Decide pass/fail | Verification runner. |
| Retry, escalate, or complete | Fixed controller rules and configured ceilings. |
| Record audit history | Append-only harness stores. |

Models never receive direct shell, filesystem, Git, network, credential,
workflow-transition, retry-limit, verification, completion, or audit authority.
Changing this boundary requires an explicit ADR before implementation.

### Split ephemeral capability from durable authority (ADR 0077 and ADR 0095)

The rule above conflates two different things: *ephemeral capability* inside a
disposable environment, and *durable authority* over the owner's repository,
network, credentials, workflow, evidence, and delivery. The Crisis Atlas
unrestricted control showed that denying the first bought no safety the
container boundary does not already provide, and cost most of the model's
engineering ability — the same Qwen used eight times *fewer* input tokens
through the typed loop and produced a materially worse product.

ADR 0077 keeps the second denial exactly as written above and permits the first
inside a disposable workcell:

> Qwen may act like a normal coding agent inside a disposable workcell.
> Apoapsis alone decides whether any resulting delta is valid, verified,
> checkpointed, promoted, or delivered.

This does **not** authorize a model shell on the Windows host, network or
credential access by prompt instruction, model-selected verification or
acceptance policy, or model-owned completion, Git promotion, plan approval, or
delivery.

The `apoapsis.workcell` package implements this boundary. The locked Crisis
Atlas live pilot has now exercised the pinned native Qwen CLI through the real
workcell, relay, containment checks, controller checkpoint, and continuation
seam. Six fresh slots completed; all three matched pairs scored 1.0/1.0 on
first-proposal quality, and the separate zero-model rehearsal retained 17/17
mapped detector controls. ADR 0095 connects that path to ordinary approved
plan-slice execution through `workcell/product.py` and
`workcell/product_live.py`: `VerticalSliceRunner` consumes the same
`AgentSessionResult` boundary as its older local stages, but only the external
controller can produce COMPLETE after admission, structured witnesses and
readiness. The typed Local Power route is retained as an explicitly selected
compatibility mode, never renamed. Live evidence for the new ordinary-task
adapter is still pending and must not be inferred from the earlier pilot.

| Layer | Module | Enforced by |
| --- | --- | --- |
| Pinned identity | `workcell/pins.py` | Every field required; one manifest digest per experiment |
| Container lifecycle | `workcell/controller.py` | One persistent container per session; run-id label ownership; fail-closed teardown |
| Containment | `workcell/containment.py` | 22 probes over host filesystem, credentials, network, container control, controller authority, privilege, and ceilings |
| Provider conformance | `workcell/conformance.py` | Nine checks; a malformed tool envelope is an adapter defect until they pass |
| Native loop | `workcell/events.py` | One-way `stream-json` adapter; no second model-action scheduler |
| Capability spike | `workcell/spike.py` | Observed behaviour vs the frozen control; no acceptance repair |

The container runs `--network none`. The model endpoint is reached only through
a Unix domain socket the controller creates, owns, meters, and deletes, exposed
inside the namespace on a loopback port by a read-only forwarder mounted
outside `/workspace`. There is no default route and no DNS, so egress is a
boundary rather than a policy.

| Egress layer | Module | Property |
| --- | --- | --- |
| Forwarding policy | `workcell/relay_policy.py` | Not a proxy: the upstream comes from configuration, never from the client. `CONNECT`, absolute-form URIs, and cross-origin redirects are refused; routes are a constant config can narrow but never widen. |
| Relay server | `workcell/relay.py` | Unix socket, streaming forwarded not buffered, cancellation propagated upstream, request/response ceilings, concurrency and session budgets, refusals always recorded. |
| Forwarder | `workcell/forwarder.py` | Read-only, outside the worktree, hashed into the manifest, and deliberately policy-free. |
| Portability | `workcell/platform_support.py` | Refuses a Windows-host or DrvFs socket path up front: Docker Desktop cannot carry a socket inode from a Windows filesystem into the Linux VM. |
| Readiness | `workcell/relay_preflight.py` | One-token request through the whole path, cross-checked against the relay's own counter — steps passing with zero relay traffic is a containment failure, not readiness. |

`Host` and `X-Forwarded-*` are stripped rather than refused: `Host` is
mandatory in HTTP/1.1, and the safety property is that the relay never consults
it, not that the client omitted it.

### Local Power Sandbox (ADR 0059, explicit compatibility mode)

`[execution.local_power]` adds a second, opt-in execution path for *local*
models only. It widens the action protocol — whole-file `write_file`,
`delete_file`, an atomic multi-file `propose_change_set` (ADR 0071), and a
mediated `run_shell` instead of hand-authored unified diffs — inside the
disposable per-task worktree. It does not move the boundary above:

| Capability | Under the sandbox mode |
| --- | --- |
| Edit project files | Model sends whole-file content; the harness computes the diff. |
| Change several files at once | Proposal only (ADR 0071). `propose_change_set` is validated in full before a byte is written and applies completely or not at all; there is no patch operation and no partial mutation. |
| Reach Apoapsis internals, `.git`, `.env*`, keys, home, system paths | Refused by `SandboxGuard` on every read, write, delete, and shell argument. |
| Run a command | Only allowlisted program prefixes, never through a shell, always with the sandbox as cwd, a scrubbed secret-free environment, a hard timeout, and capped audited output. |
| Network | Denied by default (`allow_network = false`). |
| Mutate workflow state or audit | Never. |
| Decide completion | Never. `finish` ends the model's turns; configured verification decides the outcome. |
| Decide *when to stop* | Never (ADR 0069). The harness ends the session itself as soon as every required command has passed for the current worktree fingerprint. An identical model-requested re-run at an unchanged state is refused, and the recorded pass is reused at finalization rather than re-run. |
| End the session having ignored a failing check | Bounded (ADR 0070). `finish` is refused — at most twice — while a required command has no result for the current state and nothing has been edited since the last check. Any edit, or actually running that command, satisfies the gate; succeeding is not required. |

Every failing check is normalized into `<verification:NAME>` evidence and an
audit artifact, and a resumed session is seeded with the prior stage's
unresolved failure, so a repair continuation can see what it is repairing
(ADR 0070). The prompt states per-command verification state as four distinct
values — passing for the current code, failed, passed before an edit
invalidated it, never run — because collapsing the middle two into "passed"
is how a stale result comes to look like proof.

A slice is a coherent, independently verifiable increment, not a file (ADR
0071). One `propose_change_set` may create, replace, and delete several files
in one turn; the harness validates every operation against the same
`SandboxGuard`, reports every problem at once, applies all of it or none of
it, then runs the required checks itself. Ceilings are
`min(max_change_set_files, max_changed_files)` per proposal plus the existing
session changed-line budget, whose violation rolls the whole set back
byte-for-byte. Every proposal — applied or refused — is recorded in
`local-power-change-set-NNN.json` and `LocalPowerReviewPackage.change_sets`
with the worktree digest the harness observed beside the one the model
claimed. Setting `atomic_change_sets = false` removes the action from the
prompt and the grammar, which is what makes the one-action protocol a real
comparison arm rather than differently-worded advice.

Capability Sandbox is the default local plan-slice mode. The strict one-action
loop remains the path for quick-change tasks, which do not yet have an approved
slice-readiness contract. See ADR 0095 and ADR 0059.
The UI exposes one **Local coding mode** card on Models & environment. Its
confirmed compatibility selection atomically disables Capability Sandbox and
enables Local Power; its rollback does the inverse. The browser never writes
arbitrary TOML: `ApoapsisUIService.set_capability_sandbox()` edits only known
execution fields, corrects an incompatible route, reloads the full Pydantic
config, and restores the previous bytes if validation fails. The older Local
Power endpoint remains compatible but performs the same mutually exclusive
edit.

## Current architecture

### Entry points and configuration

- `src/apoapsis/cli/app.py` owns the CLI, default project configuration, and
  command dispatch.
- `src/apoapsis/config.py` contains strict Pydantic configuration. Unknown keys
  fail closed.
- Fresh projects default to baseline completion: required verification remains
  mandatory, while strict per-criterion acceptance mapping is opt-in. Patch
  changed-path accounting expands untracked directories to individual files,
  and verification/acceptance Human Review stops can return to a bounded local
  repair continuation (ADR 0042).
- Plan validation and failed-verification repair are UI-first actions: **Verify
  plan** persists the deterministic validation result, and **Repair and verify**
  submits the existing bounded local review continuation (ADR 0043).
- A completed continuation that returns to Human Review renders as **Repair
  incomplete**; freshest failed verification keeps the repair action available.
  Test-authoring guidance requires concrete mock interfaces and isolated
  filesystem effects plus ignore rules for credential/token files; identical
  replacements are rejected clearly, and changed paths enumerate untracked files
  individually. Terminal repair polling opens a
  completed task's report instead of refetching a now-invalid review case (ADR
  0044).
- Turn-budget exhaustion triggers one harness-owned final full verification only
  when current edits are newer than the recorded command results and verification
  budget remains. Pass/fail, acceptance, completion, and audit authority remain
  entirely deterministic (ADR 0045).
- Plan-slice tasks retain the complete approved work brief, interfaces,
  exclusions, assumptions, stop conditions, and advisory paths/symbols as
  traceable context. Older slice repairs recover that context from their exact
  approved package. Repeated unchanged diff/file observations are rejected, and
  three consecutive violations stop early as no progress (ADR 0046).
- Every Human Review case with an eligible local continuation presents the same
  **Repair and verify** action, including budget-exhausted implementation stops;
  the underlying service and authorization checks are unchanged (ADR 0046).
- A deterministic routing stop that occurred before any worktree or agent session
  offers **Run locally**. Explicit confirmation creates a fresh normal execution
  with an operation-scoped `local_only` route override; project configuration is
  unchanged, and execution authorization, isolation, patch policy, verification,
  reporting, and audit remain mandatory (ADR 0047).
- AUTO high-risk execution uses a maximum finite local profile first and
  escalates to frontier when configured. Critical risk still requires an explicit
  choice; routing review offers fresh local or frontier execution as available.
  Authorization packages record the effective profile, and local continuations
  retain it (ADR 0048).
- Manual frontier repair packages include cloud-safe repository excerpts, full
  verification evidence, prior agent sessions, and the exact approved slice
  package when present (ADR 0048).
- `START_APOAPSIS.cmd` is the primary Windows entry point: it accepts or prompts
  for an existing Git project or empty folder, safely prepares first-time
  project state, then starts configured loopback local coding
  service targets (Ollama or OpenAI-compatible `llama-server`), and opens the
  UI for that project. `OPEN_APOAPSIS.cmd` remains a UI-only fallback for an
  already-running local model service. `STOP_APOAPSIS.cmd` releases configured
  Ollama model memory and leaves shared services running.
- `.apoapsis/` is runtime state and must be Git-ignored. Explicit CLI
  initialization updates `.gitignore`; friendly launcher setup uses the local
  Git exclude file. Launcher setup may create a repository only in an empty
  selected folder and never adds or commits pre-existing user files.

### Specification and workflow persistence

- `specification/` turns natural language into a typed proposal and enforces
  exact hard-constraint provenance.
- `workflow/engine.py` is the SQLite task/event state machine.
- `workflow/vertical_slice.py` is the primary execution controller.
- Browser and CLI paths call the same services; browser code does not infer
  authoritative state or construct provider/shell actions.

Core task states are persisted and optimistic-versioned. Only valid state
transitions may append events. A model response is never itself a transition.

### Durable operations

Long-running intake, execution, review, discovery, and planning-research work is
represented by durable SQLite operation records and background workers.
Operations use leases and heartbeats, re-read authoritative state at execution
time, and fail terminally on stale versions, changed repository state, lost
leases, or authorization drift.

Execution authorization captures task/version, repository HEAD and fingerprint,
effective config, model identities/roles, budgets, policy, verification catalog,
and hashes before provider construction or worktree mutation.

### Repository context and isolation

- The context compiler deterministically selects bounded files/excerpts and
  records attribution plus measurements.
- Secret-like paths and `.apoapsis/**` are excluded from cloud transmission.
- Agent inspection is read-only and bounded. Repository search has a pure-Python
  fallback when ripgrep cannot launch.
- Execution requires a clean parent repository, then creates an isolated Git
  worktree. Apoapsis never stashes, resets, commits, merges, or discards user
  work automatically.
- Session-patched compile-time excerpts are labeled stale in transmitted context
  when a fresh worktree version also exists.

### Bounded coding protocol

`agent/` accepts exactly one typed model action per turn:

- `search_repository`
- `read_file`
- `inspect_diff`
- `propose_patch`
- `replace_text`
- `create_file`
- `run_check`
- `submit_for_verification`
- `request_escalation`

The loop has separate turn, patch, verification, search/read, observation, and
transmission ceilings. Defaults are 20 local turns with 14 patch attempts and
14 frontier turns with 9 patch attempts (ADR 0049); the slice
`max_criteria_per_slice` ceiling (`[architect.ceilings]` in
`.apoapsis/config.toml`) is 20, paired with a `max_work_brief_chars` of 3,500
so the larger work brief stays consistent with the larger criterion budget.
Raising a ceiling changes configuration, not model authority.

Patch attempts are incremental against the current worktree. Dependency, test,
verification-config, binary, secret, metadata, and out-of-root changes are
governed by `PatchPolicyConfig`. New configurations allow non-deleting test-file
changes and dependency-manifest edits by default so from-scratch work can create
its verification suite and declare required libraries; owners may explicitly
disable either. Test deletion and verification-config changes remain forbidden.
Policy decisions and
rebased applied patches are audited.

For llama.cpp-served local GGUF models that echo chat-template tool-call
fragments, `parse_agent_action` performs one narrow recovery for `create_file`:
known closing tool-call markers are trimmed from the literal content and a
cross-action `command_name` field is discarded before the usual strict
discriminated-union validation. Unknown authority fields and every non-`create_file`
action remain fail-closed (ADR 0058).

For a single-hunk text new-file diff from `/dev/null`, the parser
deterministically restores missing outer addition markers and recomputes the
new-side hunk count before policy validation. The original and normalized forms
are audited. Existing-file edits and other diff shapes remain strict. Agent
prompts receive the effective dependency/test edit flags so instructions agree
with enforced policy.

Structured edit actions never make the model author diff syntax, and Apoapsis
will not synthesize a patch that Git's own whitespace policy must reject:
`replace_text` and `create_file` normalize end-of-file blank content the edit
introduced, and an edit that reduces to only added EOF blank content is refused
outright (ADR 0063). Files whose end-of-file shape was already unusual are left
alone. The no-progress guard for `git apply` whitespace rejections applies to
every edit action, not only `propose_patch`, and its next-action guidance names
the failure without asserting which action the model took. Three same-class
failures stop the session rather than draining the patch and turn budgets.

Reviewer-facing changed files answer one question -- what did the model change?
`apoapsis.repository.changed_paths` classifies a changed-path listing into
reviewable work and generated byproducts (`__pycache__/**`, `*.py[cod]`,
`.pytest_cache/**`, coverage/build caches). Classification is name-based and
deliberately independent of `.gitignore`, so output is correct for repositories
Apoapsis did not initialize; a path already tracked in Git is always reviewable
regardless of its name. `AgentSessionResult` and `LocalPowerReviewPackage`
report `generated_byproducts` separately, and
`RepositoryInspector.changed_paths()` remains the raw audit view.

Known-impossible verification contracts fail before model spend. Currently this
includes required Python unittest discovery from a missing start directory when
test edits are forbidden. CLI and browser submissions surface the actionable
preflight error without creating an execution operation; the browser handler
returns a structured conflict response rather than dropping the connection.
Ordinary failing tests still become repair evidence; Apoapsis does not guess
that they are impossible.

When test edits are allowed, a missing required unittest discovery directory is
instead a live implementation obligation transmitted on every agent turn. After
the matching real verification failure, escalation for that missing scaffold is
rejected and audited so the model must propose meaningful tests within its existing
budgets. Other escalation paths and all verification authority remain unchanged.

### Verification and completion

Only configured command names may run. Commands execute through the configured
host or Docker backend with bounded time/output and a restricted environment.
Failure normalization records root errors and useful locations as repair
evidence. Identical verification on an unchanged worktree is rejected.

Apoapsis-owned verification runs with `PYTHONDONTWRITEBYTECODE=1` beneath the
allowlisted host environment, so a Python check does not write `__pycache__`
into the worktree it is measuring (ADR 0063). An explicitly configured command
can override it through the existing per-command `environment` field and no
other path.

Python dependency bootstrap is harness-selected from `requirements*.txt` or
`pyproject.toml` and runs before configured checks by default. Pip installs into a
task-scoped target, which is added to `PYTHONPATH`; package build/install scripts
are explicitly allowed. The bounded installer result is a required audited
verification command. Models still cannot submit raw install commands. Host mode
therefore executes model-influenced package code without isolation; prefer Docker
when that risk is unacceptable.

Under `baseline` (the initialized-project default), all required checks passing
is enough for completion. Under `strict`, every active acceptance criterion
must additionally map to an explicitly owner-designated acceptance command and
be deterministically proven on the current worktree fingerprint. Acceptance is
never auto-designated. A model saying “done” has no effect.

`apoapsis.verification.contract` grades the configured contract's evidence
structure — `none`, `development_only`, `acceptance_designated`,
`criterion_mapped` — from required/acceptance designation and criterion
mapping only (ADR 0069). It never inspects argv to guess whether a command
exercises the product, because nothing in argv can answer that. The assessment
appears in Doctor, in the hashed `ExecutionAuthorizationPackage`, in
`FinalTaskReport.verification_contract`, in the Local Power review package and
its own audit artifact, and in the UI beside both the start confirmation and
the outcome. It reports; it never blocks, and baseline semantics are unchanged.

`apoapsis verify-web-product` is an owner-configurable check, not a harness
default: it cross-references a dependency-free browser product's HTML, CSS, and
JavaScript against each other (unresolved element lookups, dead style rules,
duplicate ids, duplicate top-level functions, missing local assets, optional
external-resource and network prohibitions) and exits non-zero on error
findings. Behavioral in-browser verification is a defined seam with no provider
implemented; requesting it fails rather than passing.

Held-out evaluation oracles are separate from development verification and
never become repair context.

### Routing, providers, and spend

- `models.frontier` remains the specification/legacy provider.
- `models.local_coder` is the local coding role.
- `models.frontier_coder` is optional and separately authorized.
- `models.local_research` is a tool-less local synthesis/extraction role.
- Provider calls pass through instrumentation for tokens, latency, cache use,
  model/role identity, and configured cost estimates.

Hosted calls require explicit provider configuration. Evaluation lanes that can
use hosted models also require an aggregate maximum hosted-spend ceiling and
fail before any call if the pessimistic allowance exceeds it.

Fresh `apoapsis init` projects now default the coding/specification roles to
Laguna S 2.1 served by a local OpenAI-compatible `llama-server` endpoint:
`provider = "openai_compatible"`, `base_url = "http://127.0.0.1:8000/v1"`,
`model = "Laguna-S-2.1-UD-Q4_K_S"`, and
`context_window_tokens = 32768` (ADR 0060). The OpenAI-compatible adapter may
omit the `Authorization` header only for loopback endpoints (`localhost`,
`127.0.0.1`, or `::1`); non-loopback hosted endpoints still require the
configured credential environment variable before a request is sent.
`START_APOAPSIS.cmd`/`operator_lifecycle.py` can now health-check and warm such
loopback OpenAI-compatible targets. If the endpoint is down, it launches only an
explicit operator-provided `APOAPSIS_LLAMA_SERVER_COMMAND`; it never downloads,
installs, initializes a repository, or manages hosted endpoints (ADR 0062).

### Research Mode

`research/` is advisory and quarantined. The model proposes a typed plan;
Apoapsis validates allowed sources and budgets. Restricted GitHub, official-doc,
and optional Reddit adapters perform network access. Content is fetched with
domain/content/size/redirect/time limits, sanitized for prompt injection,
license-classified, provenance-bound, cached, and audited before a tool-less
local model extracts evidence or synthesizes patterns.

Candidate capacity is distributed across planned queries. One available source
may fill the fetch budget; diversity limits apply when multiple sources exist,
and, since ADR 0055, a per-research-question cap in `SourceRanker` also
prevents one broad query from consuming the entire fetch allowance meant to
be shared across every viable research question. Sources with no extracted
findings appear in `rejected-evidence.jsonl`.

ADR 0055 fixes a reproduced failure (operation
`DISCOP-796622810B804FE59E87536D`) where an empty `evidence.jsonl` always
raised the same generic "no provenance-valid research evidence remained"
error regardless of cause. `ResearchEngineError` now carries a
`ResearchFailureReason` (no source candidates; a planned source unusable for
its adapter; sources retrieved but nothing relevant extracted; findings
rejected by provenance validation; insufficient source diversity) and a
structured `detail`; the discovery operation service appends a
reason-specific recommended operator action to the persisted failure.
Every planned query is checked for feasibility before retrieval --
concretely, an `official_docs` query with no URLs and no configured search
provider, or whose URLs are all outside `allowed_domains`, is recorded in a
new `unusable-queries.jsonl` audit file and excluded rather than silently
contributing nothing; if literally no query is viable, research fails fast
with that reason instead of continuing on an unrelated adapter. When
retrieval produces sources but the first extraction pass finds nothing
relevant, the engine runs exactly one bounded, audited recovery pass over
the same retrieved sources (no new fetch, no larger budget, never a second
round) before giving up; `recovery.json` always records whether it ran.
`OfficialDocumentationSource` gained an optional
`OfficialDocumentSearchProvider` seam (`sources/search_provider.py`) for
real official-document discovery -- query proposal, then a deterministic
search provider, then domain filtering, then the existing ranker, then the
existing restricted fetcher. ADR 0056 records the owner's explicit
authorization of Tavily as the one concrete provider implemented behind
that seam (`TavilyOfficialDocumentSearchProvider`, `sources/tavily.py`;
Brave Search was the initial pick but was dropped after its free tier
turned out to require a credit card and metered billing, unlike Tavily's
no-card free tier): `search_provider = "tavily"` plus
`search_credentials_env` (default `TAVILY_API_KEY`) enables it, and
`api.tavily.com` must be added to `[research.security].allow_domains`. Any
other provider name still fails clearly (ADR 0055) rather than guessing at
Bing/Brave/Serper/etc. Direct-URL official-doc research is unchanged and
still needs no provider (`search_provider = "none"` remains the default).
The Tavily integration has deterministic fake-fetcher test coverage only --
no live call to the real Tavily API has been made or verified in this
session; no API key was available.

Research never writes project files, executes downloaded code, sees project
secrets, approves a plan, creates a coding task, or authorizes a slice. Coding
agents do not receive general internet access.

### Discovery and Architect Mode

- `discovery/` supports bounded local clarification questions, verbatim user
  answers, one typed `IdeaBrief`, explicit user approval, optional research,
  and an immutable frontier planning package.
- Harmless bullet/case/whitespace noise in a proposed constraint quote is
  resolved back to the exact characters from the user's idea/answer; paraphrase
  still fails.
- Frontier planning may use an explicitly configured API with spend controls or
  a manual ChatGPT/Claude subscription export/import. Subscription sites are
  never automated.
- `architect/` validates a typed plan, verification names, constraints,
  dependencies, paths, and ceilings before explicit approval.
- A plan may execute under one explicit, durable auto-mode authorization bound
  to its exact approved version and effective configuration digest. Apoapsis
  packages, approves, and runs only one dependency-ready slice at a time and
  advances only from authoritative COMPLETE; every other outcome stops. Its derived task preserves the full approved
  slice execution contract rather than only the objective and inherited
  constraints. Completion does not commit or merge; the operator does that in
  normal Git before dependent slices become ready.
- After every slice is COMPLETE, **Prepare finished project** checkpoints the
  final integrated task branch, writes a tracked-source ZIP with a usage guide,
  records the plan as EXECUTED, and emits a whole-project frontier-review handoff.
  It never moves or merges the user's checked-out branch (ADR 0048).

### Finished-plan delivery

`architect/delivery.py` checkpoints the integrated tip, refuses any slice
whose current evidence is unreadable (ADR 0072), then runs
`architect/final_verification.py` — the plan's own
`whole_project_verification_commands` against the exact integrated commit,
bound to that commit and to the worktree fingerprint captured before the run
(ADR 0074). Only a passing, matching record permits delivery; anything else
leaves the plan APPROVED with no archive and no `delivery.json`, and the
refusal's reasoning is persisted at
`.apoapsis/plans/<plan-id>/final-project-verification.json`.

`PlanDelivery` (schema 1.1) keeps two evidence fields that make different
claims and must never be conflated: `verification_summary` is per-slice
history, scoped to one task each and carrying no commit or fingerprint
binding; `final_project_verification` is the integrated run. The frontier
handoff and the ZIP usage guide preserve the same separation.

Plan validation enforces the structural preconditions: a plan must name a
whole-project command, every integration contract must be assigned to a
slice, every required delivery artifact must be produced by one, every
end-to-end scenario must be proven by a whole-project command, and a
contract's declared `runtime_boundary` must not be forbidden by the flags of
a command that governs it.

ADR 0076 adds the operability half. `PlanDeliveryContract` carries
`launch_verification_command` (the name of a configured command, never a
shell string) or `launch_not_runnable_reason`, and validation requires
exactly one. `primary_documentation_path` must be set, safe, and assigned to
a slice. At delivery, `assess_delivered_operability` compares the contract to
the integrated commit's file inventory and refuses a plan whose required
artifacts are not actually shipped; the resulting `DeliveredOperability`
record on `PlanDelivery` separates "artifact present", "launch exercised by
this command", and "launch explicitly unmeasured for this reason". The ZIP
usage guide renders the plan's own structured install/launch/test text and
labels its filename heuristics as inference rather than documentation.

### Human review and manual frontier repair

Budget exhaustion, unavailable escalation, policy stops, provider failures, and
verification/acceptance gaps become deterministic Human Review cases. Review
actions are computed from persisted state. A continuation requires explicit
authorization and has additive bounded budgets recorded in its package.

A pre-agent deterministic routing stop is not a continuation. **Run locally** is
an explicit user authorization for one fresh local execution and is only eligible
while no worktree or local session exists. A failed start returns to the same
routing-review class so the operator can inspect or retry without an unknown-state
dead end.

Manual frontier repair exports one hash-bound Markdown package, imports a typed
response, requires explicit approval, applies through normal patch policy, runs
normal verification, and records subscription usage as unmeasured.

### Local operator interface

`ui/application.py` is the server-side authority boundary and
`ui/static/app.js` is a state renderer/action client. The loopback API uses a
capability token. The UI covers Home, New Task, specification approval, task
control/changes/review/report, Plans and slices, Discovery, Evaluations, and
Models & environment.

The interface must distinguish user authority, model proposals, control-plane
actions, repository evidence, and deterministic results. Missing measurements
say `Unmeasured`, never zero. Detail-route errors must clear stale prior content.

### Audit and reports

Task audit directories preserve prompts, requests/responses, telemetry, context
measurements, turn actions, policy decisions, normalized failures, patches,
verification results, research provenance, routing, authorization, continuation,
and final reports. SQLite operation/event databases are authoritative for live
state; JSON/Markdown artifacts are immutable evidence and handoff material.

`report.json` is written exactly once, at the task's first stop, and is never
updated. `reporting/current_state.py` (ADR 0072) is the single read-only
projection of a task's *current* outcome, verification, acceptance coverage,
and stop/completion reason, computed from persisted task state, the
append-only event history, and the operation artifact the deciding stage
wrote. Every surface that labels a task outcome reads it: the Report page and
task list (`ui/application.py`), review-case construction (`review/case.py`),
finished-plan delivery and the whole-project frontier handoff
(`architect/delivery.py`), plan slice status
(`architect/slice_service.py`), and `apoapsis inspect`.

When the event history names an evidence generation whose artifact is missing
or malformed, the projection reports empty results with an explicit integrity
flag and never falls back to `report.json`. `prepare_plan_delivery` gates on
that: a slice that is persistently COMPLETE but no longer evidenced raises
`SlicePackagingError`, the plan stays APPROVED, and no ZIP or `delivery.json`
is written. Delivery's per-slice section is labelled per-slice history and is
not presented as integrated-project verification.

## Operating the project

Requirements: Python 3.12+, Git, and the declared project dependencies. Ripgrep
is recommended. Ollama and Docker are optional unless selected by configuration.

Typical development checks:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
```

For a target repository:

```powershell
apoapsis init
apoapsis doctor
apoapsis ui --project-root .
```

`apoapsis init` writes an example configuration, not a universal verification
contract. Replace its verification command with a real project check before
execution. In strict mode, explicitly designate acceptance commands and map
criteria only when their pass genuinely proves the criterion.

## Evidence index

- Slice 7P.1c real qualification of the Crisis Atlas package, and the
  correction of 7P.1b's registerability claim (real clones, real commands,
  real witnesses; no model, server or network):
  `docs/evaluation/slice-7p1c-real-qualification-2026-07-31.md`
- Slice 7P.1b Crisis Atlas pilot case package authorship (**orchestration
  evidence only** — the seed clone and object types are real, the checkpoint
  half was an injected probe, and its "registerable" claim is corrected by
  7P.1c above):
  `docs/evaluation/slice-7p1b-crisis-atlas-pilot-package-2026-07-31.md`
- Context profiles: `docs/evaluation/apoapsis-1.0-profile-evidence-2026-07-18.md`
- Strict live rounds: `docs/evaluation/apoapsis-strict-live-evaluation-2026-07-18.md`
  and `apoapsis-strict-live-evaluation-2026-07-19.md`
- Planning comparison: `docs/evaluation/apoapsis-planning-comparison-2026-07-20.md`
- D4c diagnosis/probes: `docs/evaluation/apoapsis-d4c-forensic-diagnosis-2026-07-19.md`
- Docker proof: `docs/evaluation/apoapsis-d5a-live-docker-evidence-2026-07-20.md`
- ADR 0072 current-evidence projection (deterministic only; the Crisis Atlas
  run is cited as motivating live-local input, not as evidence for the fix):
  `docs/evaluation/adr-0072-current-evidence-projection-2026-07-29.md`
- ADR 0073 request-policy split and web-check evidence (deterministic plus
  hand-run CLI transcripts against two constructed products; no live run):
  `docs/evaluation/adr-0073-request-policy-and-web-evidence-2026-07-29.md`
- ADR 0074 final integrated-project verification and plan cross-consistency
  (deterministic only, against real git worktrees; no live run):
  `docs/evaluation/adr-0074-final-integrated-verification-2026-07-29.md`
- ADR 0075/0076 planner handoff and operability contract (deterministic only;
  no live run):
  `docs/evaluation/adr-0075-0076-operability-and-planner-handoff-2026-07-29.md`
- Crisis Atlas 64K sliced Qwen plus Codex checkpoints (live local inference plus
  direct Codex inspection):
  `docs/evaluation/crisis-atlas-64k-codex-frontier-trial-2026-07-30.md`
- Crisis Atlas unrestricted Qwen CLI control (live local inference plus
  independent host/browser verification):
  `docs/evaluation/crisis-atlas-qwen-cli-control-2026-07-30.md`
- ADR 0077 paired scorer and frozen Crisis Atlas facts (deterministic rescore of
  the two records above; no new inference):
  `docs/evaluation/adr-0077-paired-scorer-and-frozen-arms-2026-07-30.md`
- Slice 2 Capability Sandbox workcell (deterministic implementation record):
  `docs/evaluation/slice-2-workcell-conformance-spike-2026-07-30.md`
- Slice 5C live Option B qualification -- compaction observed, continuation
  verified, cache measured at 2,173 tokens:
  `docs/evaluation/slice-5c-live-qualification-2026-07-30.md`
- Slice 5C native session interface probe (no inference):
  `docs/evaluation/slice5c-native-session-probe-2026-07-30.md`
- Slice 5B session coordinator and the three corrections of authority
  (deterministic only; two of seven exit criteria unmet, both live):
  `docs/evaluation/slice-5b-session-coordinator-2026-07-30.md`
- Slice 5 task kernel, state capsule, two-tier compaction, and budgets
  (deterministic only; not wired into a session loop):
  `docs/evaluation/slice-5-context-compaction-and-budgets-2026-07-30.md`
- Slice 4/4B readiness, structured witnesses, and the checkpoint loop
  (deterministic only; the two-turn CONTINUE-then-COMPLETE integration runs
  through `run_checkpoint`):
  `docs/evaluation/slice-4-slice-readiness-and-witnesses-2026-07-30.md` and
  `docs/evaluation/slice-4b-witness-emitters-and-checkpoint-loop-2026-07-30.md`
- Slice 3 candidate delta admission (deterministic plus a live demonstration
  against the real Slice 2D clones):
  `docs/evaluation/slice-3-candidate-delta-admission-2026-07-30.md`
- Slice 2A model relay and forwarder (relay exercised end to end over real Unix
  sockets against a fake upstream; **no container, no live model**), including
  the full-suite baseline of 12 pre-existing failures:
  `docs/evaluation/slice-2a-model-relay-2026-07-30.md`
- Slice 2 live gate (22/22 containment and one-token relay readiness passed;
  stopped with all nine conformance checks `NOT_RUN` because their live driver
  is absent):
  `docs/evaluation/slice-2-live-gate-2026-07-30.md`
- Slice 2B live conformance and captured pins (**live**: real container, real
  `llama-server`, real Qwen weights and CLI). All nine checks executed;
  containment 22/22; seven passed. Failed on `declared_limits_match_server` —
  the CLI declares a 1,000,000-token window against a 65,536-token server,
  which is the Crisis Atlas failure mode's root cause — and on
  `multiline_unicode_integrity`, which was diagnosed as model transcription
  rather than transport corruption and left failing rather than loosened. The
  prompt, tool-schema, and chat-template pins are now captured from the CLI's
  wire traffic and the server's `/props`. No paired task run, no acceptance
  repair, spike verdict `NOT_MEASURABLE`, **Slice 3 still blocked**:
  `docs/evaluation/slice-2b-live-conformance-and-pins-2026-07-30.md`
- Earlier local smoke records: remaining files in `docs/evaluation/`

Use these dated records for exact setups and observed results. Keep new live
claims there; the Snapshot above should contain only a short current summary.

## Known limitations and active risks

- Live local coding reliability is not established; run-to-run sensitivity in
  planned slice execution remains unexplained.
- No live hosted coding call has been made.
- The default initialized verification command is only an example and may not
  fit blank/non-Python projects; the known impossible unittest case now fails
  fast, but general project-check selection remains operator configuration.
- Research quality depends on allowed domains, source configuration, query
  quality, upstream search behavior, and available authentication. It is
  advisory, not proof.
- Real official-document web search has a secure provider seam (ADR 0055)
  and one implemented provider, Tavily (ADR 0056), but no live call has
  been made or verified against the real Tavily API in any session; treat
  it as untested until an API key is configured and a real run is recorded.
- Browser JavaScript still relies heavily on static regression tests; important
  flows need periodic real-browser checks.
- A passing verification contract is still not a working product. ADR 0069
  narrows the gap for browser products and makes the gap visible everywhere,
  but `verify-web-product` proves that a product's files agree with each
  other, not that the product behaves. The behavioral seam
  (`run_behavioral_probe`) has no provider and fails loudly rather than
  passing; no in-browser behavioral verification exists.
- ADR 0069's dead-style-rule warning suppresses any class name that appears as
  a word anywhere in a script, to avoid firing on client-rendered products
  whose DOM is built in template literals. That is a deliberate trade of
  recall for signal quality at warning level; error-level checks do not use it.
- ADR 0073's `CRITERION_ASKS_FOR_BEHAVIOR` reads owner criterion text against
  an explicit word table and will produce false positives; it is a WARNING
  that never changes `evidence_level` and never blocks. Its request-target
  classifier is lexical: a URL assembled from variables is reported as
  unproven rather than resolved, so a product whose API base is configured at
  runtime gets a warning under `--forbid-external-resources`, not a pass.
- `verify-web-product` still executes nothing. ADR 0073 makes the ceiling of
  a static run explicit and warns when a run cross-checked nothing, but a
  criterion about persistence, browser/API integration, or interaction
  behavior still needs a project-specific acceptance command that the harness
  cannot supply.
- Packaging a later plan slice checkpoints completed prior work on isolated task
  branches and records the exact inherited base commit. The user's checked-out
  branch remains untouched; incomplete slices and divergent histories fail closed.
- Native desktop packaging remains a disposable, unbuilt spike (ADR 0050,
  `spikes/native-shell-tauri/`); it has never been compiled or run on real
  Windows hardware. Live hosted evidence also remains deferred.

- ADR 0074 runs the plan's whole-project commands against the integrated
  commit before delivery, but what that proves is exactly what those
  commands prove. It is not a behavioral check, and the evidence-strength
  reporting of ADR 0069/0073 applies to it unchanged. A plan whose
  whole-project command is a static file check still delivers on static
  evidence; the difference is that the evidence is now about the combined
  result and is labelled as such.
- ADR 0074's contradiction detection only fires when a planner populates
  `IntegrationContract.runtime_boundary`. `unspecified` is the default and
  asserts nothing, so a plan that describes a browser/API integration only in
  prose is still not checked against a forbidding verification flag. ADR 0075
  makes the handoff ask for the field explicitly and stops the literal example
  from suggesting the default, which raises the odds a planner fills it in but
  cannot guarantee it. No live evidence yet says whether the prose is
  sufficient in practice.
- ADR 0076 enforces that a launch command exists and ran, or that the owner
  wrote down why it cannot, and that required artifacts are in the shipped
  tree. It does **not** read the README's content: a slice is responsible for
  it and its presence is checked, but whether it describes the shipped
  behaviour is a human judgement. Seed data, demo-only paths, and offline-mode
  fallbacks remain statically undetectable; the structural lever is
  `INTEGRATION_WITHOUT_END_TO_END_PROOF`, which forces a behavioural
  acceptance command to exist for a networked contract rather than detecting
  the smell itself.
- Eight tests skip on a host that cannot carry a relay socket (a Windows host,
  or a WSL2 socket on DrvFs): the Stage 3 relay-fault tests, the real
  containment test, and two shared-session lifecycle tests that reach Stage 3.
  They are not known failures — they are capability gaps, they name the gap in
  their own skip reason, and they execute under WSL2 or Linux. Until someone
  runs the suite there, those paths have deterministic coverage that this
  machine has never observed (ADR 0111).

See `NEXT_STEPS.md` for the prioritized actionable list only.

## Suite status

`python -m unittest discover -s tests` exits 0 on a clean checkout.

| | |
| --- | --- |
| Last full green run | 2026-08-03 |
| Result | 2,141 tests, 0 failures, 0 errors, 49 skipped |
| Duration | ~1,150 s (~19 min) |
| Environment | Python 3.14.5, Windows 11; Docker, the pilot image and the Crisis Atlas seed absent |

There is no known-failure inventory. A red suite is now news, not a
pre-existing condition (ADR 0111). The 49 skips are all capability gaps —
absent Docker, Node, a pilot image, a seed, or a POSIX relay socket — and each
states which capability is missing.

## Architecture decision index

Every ADR, by number. The ADR file is the canonical decision; the commentary
that used to be summarised here (including which suites were run in which
session) is preserved verbatim in
[`docs/history/handoff-archive-2026-08.md`](docs/history/handoff-archive-2026-08.md#architecture-decision-index-as-it-stood-on-2026-08-03).

| ADR | Decision |
| --- | --- |
| 0001 | [mvp deterministic substrate](docs/adr/0001-mvp-deterministic-substrate.md) |
| 0002 | [frontier vertical slice](docs/adr/0002-frontier-vertical-slice.md) |
| 0003 | [local research mode](docs/adr/0003-local-research-mode.md) |
| 0004 | [native ollama frontier](docs/adr/0004-native-ollama-frontier.md) |
| 0005 | [bounded coding agent loop](docs/adr/0005-bounded-coding-agent-loop.md) |
| 0006 | [deterministic frontier escalation](docs/adr/0006-deterministic-frontier-escalation.md) |
| 0007 | [apoapsis namespace](docs/adr/0007-apoapsis-namespace.md) |
| 0008 | [evaluation and diagnostic tooling](docs/adr/0008-evaluation-and-diagnostic-tooling.md) |
| 0009 | [execution sandbox](docs/adr/0009-execution-sandbox.md) |
| 0010 | [context measurement and wider profiles](docs/adr/0010-context-measurement-and-wider-profiles.md) |
| 0011 | [deterministic context quality](docs/adr/0011-deterministic-context-quality.md) |
| 0012 | [held out oracles and evaluation aggregation](docs/adr/0012-held-out-oracles-and-evaluation-aggregation.md) |
| 0013 | [local model operator lifecycle](docs/adr/0013-local-model-operator-lifecycle.md) |
| 0014 | [local operator interface](docs/adr/0014-local-operator-interface.md) |
| 0015 | [verification layers and acceptance coverage](docs/adr/0015-verification-layers-and-acceptance-coverage.md) |
| 0016 | [acceptance catalog stale proof and strict default](docs/adr/0016-acceptance-catalog-stale-proof-and-strict-default.md) |
| 0017 | [worktree fingerprint and explicit acceptance designation](docs/adr/0017-worktree-fingerprint-and-explicit-acceptance-designation.md) |
| 0018 | [acceptance failure evidence and bounded specification correction](docs/adr/0018-acceptance-failure-evidence-and-bounded-specification-correction.md) |
| 0019 | [architect mode planning foundation](docs/adr/0019-architect-mode-planning-foundation.md) |
| 0020 | [deterministic human review and resume](docs/adr/0020-deterministic-human-review-and-resume.md) |
| 0021 | [review resume integrity hardening](docs/adr/0021-review-resume-integrity-hardening.md) |
| 0022 | [authorized fresh frontier stage](docs/adr/0022-authorized-fresh-frontier-stage.md) |
| 0023 | [durable new task intake](docs/adr/0023-durable-new-task-intake.md) |
| 0024 | [durable post approval task execution](docs/adr/0024-durable-post-approval-task-execution.md) |
| 0025 | [operation lease and recovery integrity](docs/adr/0025-operation-lease-and-recovery-integrity.md) |
| 0026 | [immutable execution authorization and truthful live ui](docs/adr/0026-immutable-execution-authorization-and-truthful-live-ui.md) |
| 0027 | [approved plan to single slice execution](docs/adr/0027-approved-plan-to-single-slice-execution.md) |
| 0028 | [planning comparison framework](docs/adr/0028-planning-comparison-framework.md) |
| 0029 | [d4c diagnostic probe infrastructure](docs/adr/0029-d4c-diagnostic-probe-infrastructure.md) |
| 0030 | [hosted spend ceiling](docs/adr/0030-hosted-spend-ceiling.md) |
| 0031 | [manual subscription frontier handoff](docs/adr/0031-manual-subscription-frontier-handoff.md) |
| 0032 | [discovery and frontier planning handoff](docs/adr/0032-discovery-and-frontier-planning-handoff.md) |
| 0033 | [manual frontier and discovery local ui](docs/adr/0033-manual-frontier-and-discovery-local-ui.md) |
| 0034 | [browser launcher and native wrapper deferral](docs/adr/0034-browser-launcher-and-native-wrapper-deferral.md) |
| 0035 | [guided workflows and planning research](docs/adr/0035-guided-workflows-and-planning-research.md) |
| 0036 | [operational hardening and documentation compaction](docs/adr/0036-operational-hardening-and-documentation-compaction.md) |
| 0037 | [test authoring default](docs/adr/0037-test-authoring-default.md) |
| 0038 | [deterministic new file diff reconstruction](docs/adr/0038-deterministic-new-file-diff-reconstruction.md) |
| 0039 | [default dependency authoring and slice inheritance](docs/adr/0039-default-dependency-authoring-and-slice-inheritance.md) |
| 0040 | [required verification scaffolding is implementation work](docs/adr/0040-required-verification-scaffolding-is-implementation-work.md) |
| 0041 | [harness controlled dependency installation](docs/adr/0041-harness-controlled-dependency-installation.md) |
| 0042 | [untracked test patches and verification repair](docs/adr/0042-untracked-test-patches-and-verification-repair.md) |
| 0043 | [ui first plan validation and repair](docs/adr/0043-ui-first-plan-validation-and-repair.md) |
| 0044 | [truthful repair results and test side effect guidance](docs/adr/0044-truthful-repair-results-and-test-side-effect-guidance.md) |
| 0045 | [automatic final verification](docs/adr/0045-automatic-final-verification.md) |
| 0046 | [complete slice contract and no progress guard](docs/adr/0046-complete-slice-contract-and-no-progress-guard.md) |
| 0047 | [explicit local execution after routing review](docs/adr/0047-explicit-local-execution-after-routing-review.md) |
| 0048 | [risk aware local power frontier handoffs and plan delivery](docs/adr/0048-risk-aware-local-power-frontier-handoffs-and-plan-delivery.md) |
| 0049 | [coupled ceiling coder budget bump](docs/adr/0049-coupled-ceiling-coder-budget-bump.md) |
| 0050 | [native desktop shell and project management](docs/adr/0050-native-desktop-shell-and-project-management.md) |
| 0051 | [native project registry and safe import](docs/adr/0051-native-project-registry-and-safe-import.md) |
| 0052 | [reference projects and desktop home menu](docs/adr/0052-reference-projects-and-desktop-home-menu.md) |
| 0053 | [privileged desktop local ipc channel](docs/adr/0053-privileged-desktop-local-ipc-channel.md) |
| 0054 | [native picker wiring and phase7 coverage](docs/adr/0054-native-picker-wiring-and-phase7-coverage.md) |
| 0055 | [research failure classification and official doc search seam](docs/adr/0055-research-failure-classification-and-official-doc-search-seam.md) |
| 0056 | [tavily search official document provider](docs/adr/0056-tavily-search-official-document-provider.md) |
| 0057 | [create file action and malformed diff no progress guard](docs/adr/0057-create-file-action-and-malformed-diff-no-progress-guard.md) |
| 0058 | [local tool template noise normalization](docs/adr/0058-local-tool-template-noise-normalization.md) |
| 0059 | [local power sandbox execution mode](docs/adr/0059-local-power-sandbox-execution-mode.md) |
| 0060 | [laguna llama server default](docs/adr/0060-laguna-llama-server-default.md) |
| 0061 | [local power ui toggle](docs/adr/0061-local-power-ui-toggle.md) |
| 0062 | [start launcher and llama server lifecycle](docs/adr/0062-start-launcher-and-llama-server-lifecycle.md) |
| 0063 | [changed path classification and structured edit eof normalization](docs/adr/0063-changed-path-classification-and-structured-edit-eof-normalization.md) |
| 0064 | [legible unborn head errors and no silent ui dead ends](docs/adr/0064-legible-unborn-head-errors-and-no-silent-ui-dead-ends.md) |
| 0065 | [per route request body ceilings for pasted model responses](docs/adr/0065-per-route-request-body-ceilings-for-pasted-model-responses.md) |
| 0066 | [literal response shape in the frontier planning handoff](docs/adr/0066-literal-response-shape-in-the-frontier-planning-handoff.md) |
| 0067 | [bounded normalization of operator pasted json](docs/adr/0067-bounded-normalization-of-operator-pasted-json.md) |
| 0068 | [local reasoning wrapper normalization](docs/adr/0068-local-reasoning-wrapper-normalization.md) |
| 0069 | [verification sufficiency termination and contract strength](docs/adr/0069-verification-sufficiency-termination-and-contract-strength.md) |
| 0070 | [failure evidence reaches the local power repair model](docs/adr/0070-failure-evidence-reaches-the-local-power-repair-model.md) |
| 0071 | [atomic slice proposals in local power](docs/adr/0071-atomic-slice-proposals-in-local-power.md) |
| 0072 | [current task evidence projection](docs/adr/0072-current-task-evidence-projection.md) |
| 0073 | [same origin requests and web check evidence](docs/adr/0073-same-origin-requests-and-web-check-evidence.md) |
| 0074 | [final integrated project verification](docs/adr/0074-final-integrated-project-verification.md) |
| 0075 | [the planner handoff asks for runtime boundary](docs/adr/0075-the-planner-handoff-asks-for-runtime-boundary.md) |
| 0076 | [structured operability contract](docs/adr/0076-structured-operability-contract.md) |
| 0077 | [capability sandbox authority boundary](docs/adr/0077-capability-sandbox-authority-boundary.md) |
| 0078 | [envelope integrity versus model transcription](docs/adr/0078-envelope-integrity-versus-model-transcription.md) |
| 0079 | [readiness based completion and controller owned evidence](docs/adr/0079-readiness-based-completion-and-controller-owned-evidence.md) |
| 0080 | [context authority and the session coordinator](docs/adr/0080-context-authority-and-the-session-coordinator.md) |
| 0081 | [native loop authority and the handoff capsule](docs/adr/0081-native-loop-authority-and-the-handoff-capsule.md) |
| 0082 | [pinned runtime thresholds are measured not derived](docs/adr/0082-pinned-runtime-thresholds-are-measured-not-derived.md) |
| 0083 | [advisory diagnostics and one pinned runtime profile](docs/adr/0083-advisory-diagnostics-and-one-pinned-runtime-profile.md) |
| 0084 | [one authoritative repair checkpoint](docs/adr/0084-one-authoritative-repair-checkpoint.md) |
| 0085 | [the slice 7 qualification manifest is frozen before inference](docs/adr/0085-the-slice-7-qualification-manifest-is-frozen-before-inference.md) |
| 0090 | [credential exclusion not absence of authentication shaped configuration](docs/adr/0090-credential-exclusion-not-absence-of-authentication-shaped-configuration.md) |
| 0091 | [separate live pilot authorization and operator launch](docs/adr/0091-separate-live-pilot-authorization-and-operator-launch.md) |
| 0092 | [one authorization for controller owned plan progression](docs/adr/0092-one-authorization-for-controller-owned-plan-progression.md) |
| 0093 | [friendly launcher owned project setup](docs/adr/0093-friendly-launcher-owned-project-setup.md) |
| 0094 | [auto validate frontier plans and explicit auto run state](docs/adr/0094-auto-validate-frontier-plans-and-explicit-auto-run-state.md) |
| 0095 | [capability sandbox product rollout and fallback](docs/adr/0095-capability-sandbox-product-rollout-and-fallback.md) |
| 0096 | [exact imported plan response transfer recovery](docs/adr/0096-exact-imported-plan-response-transfer-recovery.md) |
| 0097 | [hash bound approved plan in slice packages](docs/adr/0097-hash-bound-approved-plan-in-slice-packages.md) |
| 0098 | [zero session capability sandbox retry](docs/adr/0098-zero-session-capability-sandbox-retry.md) |
| 0099 | [exact controller base safe directory](docs/adr/0099-exact-controller-base-safe-directory.md) |
| 0100 | [ext4 runtime windows audit separation](docs/adr/0100-ext4-runtime-windows-audit-separation.md) |
| 0101 | [relay observed model usage](docs/adr/0101-relay-observed-model-usage.md) |
| 0102 | [vcs metadata excluded at the walk](docs/adr/0102-vcs-metadata-excluded-at-the-walk.md) |
| 0103 | [state the judgement contract](docs/adr/0103-state-the-judgement-contract.md) |
| 0104 | [patch changed line ceiling for slice sized work](docs/adr/0104-patch-changed-line-ceiling-for-slice-sized-work.md) |
| 0105 | [one rule per packet and operator readable stops](docs/adr/0105-one-rule-per-packet-and-operator-readable-stops.md) |
| 0106 | [deterministic orientation brief](docs/adr/0106-deterministic-orientation-brief.md) |
| 0107 | [model server lease across arms](docs/adr/0107-model-server-lease-across-arms.md) |
| 0108 | [parity guard sampling policy](docs/adr/0108-parity-guard-sampling-policy.md) |
| 0109 | [one local execution path](docs/adr/0109-one-local-execution-path.md) |
| 0110 | [prompt ordering and token ceiling](docs/adr/0110-prompt-ordering-and-token-ceiling.md) |
| 0111 | [a green suite for strangers](docs/adr/0111-a-green-suite-for-strangers.md) |
| 0112 | [live run status](docs/adr/0112-live-run-status.md) |
| 0113 | [warm the controller image](docs/adr/0113-warm-the-controller-image.md) |
| 0114 | [the hiring package](docs/adr/0114-the-hiring-package.md) |
| 0115 | [stop releases model server memory](docs/adr/0115-stop-releases-model-server-memory.md) |

## Maintenance contract

For changes affecting architecture, workflow behavior, configuration, model
roles, context, patch policy, verification, audit artifacts, tests, or evidence:

1. Update this current-state map in the same change.
2. Update `README.md` for user-visible behavior.
3. Add an ADR for a new architectural decision; never rewrite accepted history.
4. Add deterministic fake-provider coverage for model-driven branches.
5. Run focused tests, the full suite, `python -m compileall -q src tests`, and
   `git diff --check`.
6. Update Snapshot only with results actually observed and label fake, live
   local, and live hosted evidence distinctly. One row, one line, 140 characters
   at most: item, status, date, and links to the ADR and to the archive. The
   narrative goes in `docs/history/handoff-archive-2026-08.md` (start a new
   dated archive file when the month changes), not in the table.
6b. Keep this file under 25 KB. If a section is growing, it is almost always
   accreting history that belongs in the archive -- check before adding prose.
7. Update `NEXT_STEPS.md` only when active priority/order changes; remove done
   items instead of appending milestone essays.
8. Put detailed live observations in a dated `docs/evaluation/` file and link it.
9. Preserve uncommitted user work and the `substrate-v0.1` tag.

Before handoff, verify source, tests, README, this file, the relevant ADR, and
active priorities agree. Do not declare success from model output or from a
partial test run.
