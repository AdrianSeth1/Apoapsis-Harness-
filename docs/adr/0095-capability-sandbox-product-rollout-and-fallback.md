# ADR 0095: Capability Sandbox product rollout and explicit fallback

Date: 2026-08-01

## Status

Accepted and implemented. Live product execution has not yet
been observed from an ordinary task, so deterministic coverage and the earlier
locked Crisis Atlas live pilot remain separately labelled.

## Context

The locked Crisis Atlas pilot cleared the owner-selected release gate: all
three matched default-Qwen/Capability-Sandbox pairs were non-inferior at
1.0/1.0 first-proposal quality, while the zero-model rehearsal fired all 17
mapped defect controls. The qualified runner was still evaluation-only.
Ordinary plan slices continued to select either the cognitively restrictive
typed loop or ADR 0059 Local Power, and the UI showed no usable Slice 8 mode
selection.

Renaming Local Power would be false. The product route has to launch the same
native Qwen CLI in the hardened network-none workcell, reobserve the runtime
and containment gates, admit the complete candidate outside the workcell, run
current-state witnesses, and let the normal task workflow consume only the
controller result.

## Decision

Capability Sandbox is the recommended local plan-slice mode and is written
`enabled = true` in every new project configuration. Loading an older config
with no Slice 8 table selects it automatically unless that config explicitly
had Local Power enabled. The UI presents one primary `Local coding mode` card:
Capability Sandbox or `Local Power compatibility`. Switching either way is one
confirmed action which atomically makes the two flags mutually exclusive. No
runtime failure silently changes the selection.

The ordinary `VerticalSliceRunner` selects the product adapter only for a task
derived from an explicitly approved plan slice. Quick-change tasks retain the
existing strict typed loop until they have an equally precise readiness
contract. The execution authorization records the profile, the pinned
`qwen3.6-27b` identity, the effective configuration digest, and whether the
high-assurance parity guard was selected.

The Windows product adapter invokes Ubuntu 24.04 through a fixed argument
vector. Its Linux launcher:

1. refuses uncommitted verdict-deciding Apoapsis source;
2. builds or reuses a controller image from the exact committed Git archive;
3. re-hashes the v8 model/server dependency closure;
4. reobserves the genuine Qwen tool schema and the full containment gate with
   a fake provider before starting the model;
5. starts the pinned local model, then the genuine Qwen CLI inside the
   network-none workcell;
6. runs the authoritative admission/readiness checkpoint after each native
   turn and supplies its repair packet for at most the configured continuation
   limit;
7. promotes only the controller-admitted snapshot into the normal task
   worktree and reruns the project's configured verification there.

The first product witness adapter supports Python `unittest` commands using a
controller-owned standard-library trace runner. A required command without a
structured witness adapter routes to Human Review; its reassuring name or exit
code cannot substitute for evidence.

Changed-behaviour analysis does not invent an impossible coverage obligation
for an added Python package marker containing only comments or a module
docstring: such a file has no executable line. Any executable statement,
including one in `__init__.py`, remains a behaviour unit that must be reached.

The optional high-assurance parity guard starts a fresh matched default-Qwen
control, checkpoints it independently, and refuses the supervised candidate if
its proved-obligation count is lower. An unavailable or unscoreable control is
also a stop, never a pass. This mode approximately doubles local inference and
model-memory time and is off by default.

## Consequences

- Normal approved plan execution now has a real native-Qwen workcell path; the
  selection is visible before authorization and in the authorization artifact.
- Capability Sandbox is on by default for new and legacy projects, while the
  lower-capability Local Power route remains recoverable in one UI action.
- The model has a shell and filesystem only inside its disposable workcell.
  It still has no host, credential, network, workflow, verification,
  completion, promotion, or audit authority.
- Product code must be committed before it can decide a task. The product
  launcher enforces that source-to-controller binding before a live ordinary
  slice can use the route.
- Live product evidence is still pending. The deterministic tests prove
  selection, admission, incomplete-green refusal, forbidden-delta refusal,
  and state-machine consumption; they do not claim a new live model run.
