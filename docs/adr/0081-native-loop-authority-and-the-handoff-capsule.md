# ADR 0081: The native loop is authoritative; Apoapsis injects a handoff capsule

- Status: Accepted
- Date: 2026-07-30
- **Supersedes ADR 0080.** ADR 0080 is preserved unchanged as history: its
  three authority corrections (kernel provenance, provider-reported tokens,
  progress as state advancement) all stand. What it got wrong is *who manages
  the model's context*, and that is what this ADR replaces.
- Builds on ADR 0077 (workcell boundary) and ADR 0079 (readiness-based
  completion). Neither is modified.

## Context

ADR 0080 assumed Apoapsis could assemble the prompt, manage the conversation,
and compact it — while still running Qwen Code's native agent loop, which is
the thing Slice 2D proved preserves capability. Those two are incompatible
under the invocation shape the workcell actually uses. Every live call is a
single `qwen -o stream-json -p "<task>"`; Qwen builds its own prompt, owns its
own history, and Apoapsis observes the event stream. `SessionCoordinator`'s
`PromptLayout` was never sent to the model, and the `StateCapsule` had no
mechanism by which it could enter the model's context.

The Option C probe (`docs/evaluation/slice5c-native-session-probe-2026-07-30.md`,
no inference spent) then established what the pinned 0.21.1 does offer:
`qwen --resume <session-id> -p "<prompt>"` in headless mode, which
"restores conversation history, tool outputs, and chat-compression checkpoints
before sending the new prompt"; stable session ids under
`~/.qwen/projects/<sanitized-cwd>/chats`; and a daemon with per-session prompt
queues, `POST /session/:id/resume`, and `POST /session/:id/recap`.

It also established what does **not** exist: context replacement. No
`replaceContext`, `setContext`, `forkSession`, or `enqueueMidTurnMessage` in
the bundle. Turn-boundary injection: yes. Context surgery: no.

## Decision

### 1. Qwen owns the agent loop, the tool loop, and the live context

Apoapsis does not assemble the model's prompt and does not manage its history.
This preserves the exact configuration Slice 2D measured as
`CAPABILITY_PRESERVED`, and it declines to rebuild the harness that Crisis
Atlas showed was weaker than the CLI.

### 2. Apoapsis speaks between native invocations, as a user turn

At a checkpoint, Apoapsis runs the ADR 0079 path unchanged — freeze, delta,
admit, emit witnesses against the admitted snapshot, evaluate readiness — and
then injects a bounded **session-handoff capsule** with
`qwen --resume <id> -p "<capsule + what is still outstanding>"`.

The capsule's content is unchanged from Slice 5: outstanding obligations,
interface ledger, changed paths, witnesses already observed with staleness
labelled, latest failures, refused and no-progress actions, and the model's own
notes rendered as advisory. It carries no transcript.

No daemon and no open port. `--resume` runs through the existing
`controller.exec` shape, so ADR 0077's `--network none` plus controller-owned
relay is untouched.

### 3. Native context settings are pinned, not reimplemented

`NativeContextPin` records `context.autoCompactThreshold` (resolved default
**0.85** for 0.21.1), `model.chatCompression.maxRecentFilesToRetain` (default
5), and `maxRecentImagesToRetain` (default 3). `maxRecentFilesToRetain` is
pinned because it materially decides post-compaction continuity: it is how much
of the working set Qwen restores by itself, and therefore how much the capsule
does not need to carry.

They are pinned rather than modelled. `autoCompactThreshold` is documented as a
ceiling on a three-tier warn/auto/hard ladder computed internally by
`computeThresholds()`, firing earlier on small windows. A second Apoapsis-side
model of that ladder would drift from the real one silently.

`resolved_from_cli` on the pin distinguishes a value read back from the CLI
from this model's default. A run recorded on assumed values is not evidence
about that run.

### 4. `compaction.py` is capsule construction and simulation

It is no longer the live history manager, and it never was one in practice. Its
threshold logic remains useful for two things: bounding what goes into a
capsule, and reasoning about recorded runs.

**Corrected provenance.** ADR 0080, `compaction.py`, `ceilings.py` and the
README all stated that the 0.70 default "matches Qwen Code's default". That is
false for 0.21.1. The setting carrying a percentage threshold,
`model.chatCompression.contextPercentageThreshold`, is marked **REMOVED** and
"silently ignored (no startup warning)". 0.70 replicates nothing upstream.

It is retained as the *capsule* trigger with a different and honest
justification: it sits deliberately below the native 0.85 so a handoff capsule
exists **before** the model's own compaction fires, rather than after it.

## Consequences

### What must be measured before any context-safety claim

This ADR records a design and a corrected provenance. It records **no live
result.** The qualification is:

1. ordered containment, readiness, protocol, profile and capability gates;
2. a fresh `-p` turn capturing session id, resolved settings, and the 26-tool
   profile;
3. `--resume` in the same container, HOME and working directory;
4. the resumed profile still `yolo`, still holding native edit and shell, still
   with no computer-use or tool-search surface;
5. crossing the resolved native threshold and observing an **actual native
   compaction event**;
6. capsule injection on resume followed by a **dependent, verified edit**;
7. the stable-versus-perturbed kernel cache control from provider telemetry.

If cached-input telemetry is absent, efficiency stays `NOT_MEASURABLE` —
latency alone is insufficient. If compaction does not fire on a turn beyond the
resolved threshold, context safety stays unproven.

### What ADR 0080 keeps

Kernel stability as provenance rather than lexical shape; provider-reported
tokens as the only authority for ceilings; progress as authoritative state
advancement. `SessionCoordinator` remains the checkpoint/budget owner. What it
loses is the belief that it drives the model's turns.

### Rejected alternative

**Option A: Apoapsis owns the model and tool loop.** It would make the capsule
a real context manager. It also reintroduces precisely the architecture Crisis
Atlas showed weakens Qwen, and would require a fresh paired capability
qualification before any result from it could be trusted. Held as a last
resort, not taken.
