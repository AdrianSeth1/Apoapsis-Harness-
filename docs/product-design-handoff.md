# Claude Design handoff: Apoapsis application

## Assignment

Design a desktop-first application for **Apoapsis**, a local-first, auditable AI
coding harness. The result should feel like a calm, high-end engineering control
room: dark, precise, legible, and trustworthy. Use a black/orange/purple visual
language without turning it into neon cyberpunk or a generic chat application.

This assignment is product and interaction design. Do not redesign Apoapsis's
authority model or assume the model can directly edit files, execute arbitrary
commands, decide success, or hide its evidence.

## Product promise

A developer enters a coding request, approves an exact structured specification,
and watches Apoapsis deterministically retrieve repository evidence, ask a local
or frontier model for bounded proposals, validate changes, run verification, and
produce an inspectable report. Models are untrusted proposers. Apoapsis owns every
transition, tool action, safety decision, retry ceiling, verification result, and
audit record.

The interface should make that distinction obvious without constantly lecturing
the user. At any moment a user should be able to answer:

1. What state is this task in?
2. What is Apoapsis doing now, and what is the model merely proposing?
3. Which constraints are active and covered?
4. What code changed?
5. What passed, failed, or was rejected—and why?
6. How much local/hosted model usage occurred?
7. What can I safely do next?

## Primary user

A solo developer or small technical team that wants strong coding-model help
without giving a model unrestricted access. They are comfortable reviewing code
but should not need to understand Apoapsis internals to run a task safely.

## Core flow to design

```text
Project → New request → Extracted specification → User approval
→ Context/routing summary → Bounded implementation timeline
→ Diff + verification → Complete report or human-review decision
```

Design the complete happy path plus verification failure/repair, policy rejection,
frontier escalation, model unavailability, sandbox unavailability, human review,
and held-out-oracle false-success states.

## Information architecture

Use a compact left navigation and a persistent environment/status strip.

### 1. Home / Projects

- Recently opened repositories.
- Environment readiness: Git, ripgrep, Ollama, configured models, Docker sandbox,
  and credentials as safe present/absent indicators—never credential values.
- Local model controls backed by the existing Start/Stop lifecycle: coding model
  ready, research model lazy/loaded, memory-release action.
- Recent tasks with state, outcome, repository, duration, and changed-file count.
- A strong but restrained **New task** action.

### 2. New task

- Large natural-language request field, not a chat transcript.
- Repository selector and concise execution choices: agent/one-shot baseline,
  route, context profile, Research Mode, and sandbox backend.
- Explain cost or hosted-call implications before starting.
- Keep advanced settings collapsed. Show the deterministic ceilings that matter,
  but do not imply the model controls them.

### 3. Specification approval

- Original request beside the extracted structured specification.
- Goal, acceptance criteria, risk, and hard constraints in distinct sections.
- Every hard constraint shows its exact verbatim source highlight.
- Clear states for model proposal, schema validation, and user approval.
- Approve, revise request, or cancel. No ambiguous "continue" button.

### 4. Active task control room

- Persistent task header: repository, task ID, branch/worktree, current workflow
  state, route, provider/model, elapsed time, and sandbox status.
- A vertical event timeline for context compilation, model turns, requested typed
  actions, patch-policy decisions, checks, repairs, and escalation.
- Distinguish events visually:
  - purple = model proposal/inference;
  - orange = active Apoapsis control-plane work or user action;
  - neutral = repository/context evidence;
  - green/red = deterministic pass/fail outcomes.
- Live budgets: turns, patch attempts, verification runs, context utilization,
  transmitted files/lines, tokens, latency, cache use, and estimated cost.
- Expandable details rather than an unbounded terminal wall. Exact commands and
  normalized root errors remain available for inspection and copying.
- Never fake continuous activity. Use explicit queued/running/completed states.

### 5. Changes and verification

- High-quality unified/split diff viewer with file tree, additions/deletions,
  policy badges, and changed-test/dependency/config warnings.
- Constraint coverage alongside the diff: covered, uncovered, or requires review,
  with the evidence used to make that deterministic disposition.
- Verification cards show exact configured command, backend/sandbox, duration,
  result, and root error. Separate ordinary verification from the post-completion
  held-out oracle.
- A model saying "done" must never look like success; only verifier-owned COMPLETE
  receives the completion treatment.

### 6. Human review / resume

- Explain the exact stop reason in plain language.
- Show current diff, failures, policy rejections, spent budgets, and which options
  are actually authorized.
- Potential actions: inspect only, authorize a configured frontier stage, use a
  remaining deterministic retry, abandon/rollback, or export the handoff.
- Destructive choices require clear confirmation. Do not offer unavailable or
  policy-forbidden actions as decorative disabled controls without explanation.

### 7. Final report and audit

- Outcome and confidence should derive from verification/oracle facts, not model
  self-assessment.
- Constraint coverage, models/roles used, calls, input/output/cached tokens,
  estimated cost, latency, transmitted files/lines, files changed, verification,
  context measurements/attribution, and escalation history.
- Direct links to audit artifacts and worktree, plus copy/export actions.
- A compact "What happened" narrative generated from deterministic event data,
  clearly distinct from raw evidence.

### 8. Evaluations

- Compare local, hybrid, forced-escalation, frontier, and one-shot lanes.
- Context-profile comparison for 16k/32k/64k/128k/256k.
- Completion, human review, unsafe rejection, false success, latency,
  transmissions, and context-density charts.
- Unmeasured hosted metrics must literally say **Unmeasured** with the reason;
  never render missing data as zero.
- Clearly label deterministic fake, live local, and live hosted evidence.

### 9. Models, environment, and settings

- Model role mapping: specification/legacy frontier, local coder, frontier coder,
  and local research.
- Provider endpoint, model, context window, thinking mode, and price configuration.
- Never display or store secret values; show only credential-variable status.
- Doctor results and actionable remediation.
- Sandbox selection with a strong unsandboxed warning for host execution.
- Start/Stop controls manage only configured loopback Ollama models; Stop releases
  model memory but leaves the shared Ollama service running.

## Visual direction

### Character

Dark, focused, architectural, and slightly cinematic. Think precision instrument,
not gamer dashboard. Use generous negative space, crisp 1px borders, subtle depth,
and restrained glow only for focus/active-state communication.

### Suggested palette

| Role | Color | Use |
| --- | --- | --- |
| Canvas | `#07070A` | App background |
| Primary surface | `#101016` | Panels and navigation |
| Elevated surface | `#181820` | Modals, active cards, diff header |
| Border | `#2A2933` | Dividers and inactive controls |
| Primary text | `#F4F1F7` | Main text |
| Secondary text | `#A9A4B3` | Metadata and supporting labels |
| Apoapsis orange | `#FF7A1A` | Primary actions, current state, user authority |
| Proposal purple | `#8B5CF6` | Model activity, context, research, intelligence |
| Soft purple | `#B69CFF` | Purple text/highlight on dark surfaces |
| Verified green | `#32D583` | Deterministic pass/complete |
| Failure red | `#FF5C6C` | Failed checks, policy rejection, destructive action |
| Warning amber | `#F5B942` | Unsandboxed, cost, incomplete evidence |

Orange and purple are brand/interaction colors, not substitutes for success and
failure semantics. Never rely on color alone; pair it with text, iconography, and
shape. Verify contrast to WCAG AA.

### Type and iconography

- Use a highly legible modern sans for UI and a restrained monospace for diffs,
  commands, IDs, paths, tokens, and telemetry.
- Prefer tabular numerals in metrics.
- Icons should be geometric and quiet. Avoid robot heads, magic wands, brains,
  sparkles, and chat bubbles as the core brand language.
- Explore a restrained orbital/apsis motif: one controlled object passing through
  checkpoints, useful for the logo, workflow state, or loading indicator.

### Motion

- Short transitions that communicate state ownership and sequence.
- A model proposal can pulse purple; deterministic validation should snap into a
  stable pass/fail state.
- Respect reduced-motion preferences. Avoid decorative continuous animation.

## Component vocabulary

- Workflow state pill
- Authority badge: User / Apoapsis / Model proposal
- Constraint card with verbatim-source reveal
- Provider/model identity chip
- Sandbox status shield
- Budget meter with hard ceiling
- Context utilization ring or compact bar
- Event timeline row with expandable artifact
- Patch policy finding
- Verification command/result card
- Diff file tree and code viewer
- Oracle result card, visually distinct from normal verification
- Telemetry stat and measured/unmeasured state
- Audit artifact link/copy control
- Human-review decision panel

## Content style

Use calm, specific language:

- "Verification failed" rather than "Something went wrong."
- "Model proposed a patch" rather than "Apoapsis changed your code."
- "Waiting for your specification approval" rather than "Thinking..."
- "Hosted metrics are unmeasured: no live hosted run exists" rather than `0`.
- "Model memory released; Ollama service remains available" after Stop.

Avoid anthropomorphism, hype, fake certainty, and vague agent language.

## Accessibility and interaction requirements

- Full keyboard navigation and visible focus states.
- Do not encode state with color alone.
- Resizable diff/telemetry panes and readable code at 125–150% scaling.
- Screen-reader labels for state, authority, verification, and destructive actions.
- Copyable paths, commands, task IDs, hashes, errors, and artifact locations.
- Dense information must progressively disclose; the default view should remain
  understandable to a developer seeing Apoapsis for the first time.

## Prototype scenarios

Produce high-fidelity desktop screens and a clickable flow for:

1. Local happy path: request → approval → two model turns → verified completion.
2. Verification failure → targeted repair → completion.
3. Unsafe patch rejection followed by a safe proposal.
4. Local exhaustion → deterministic hosted-frontier escalation.
5. Human-review stop with explicit resume choices.
6. Normal verification passes but the held-out oracle fails: false success.
7. Environment not ready: Ollama unavailable and Docker sandbox unavailable.
8. Evaluation comparison showing measured local data and unmeasured hosted data.

Show empty, loading, partial, error, stopped, and completed states—not only the
ideal populated dashboard.

## Deliverables requested from Claude Design

1. Sitemap and primary user-flow diagram.
2. Desktop design system: tokens, type, spacing, elevation, icon approach, states.
3. High-fidelity screens for every prototype scenario above.
4. Clickable primary flow at 1440px desktop width and a compact 1100px layout.
5. Component/state inventory suitable for engineering implementation.
6. Notes explaining how the design exposes authority, verification, cost, and
   measured-versus-unmeasured evidence.
7. A short recommendation for the future local desktop technology surface, but
   no implementation and no assumption that a web server is already authorized.

## Hard constraints

- The UI never calls a model provider directly.
- The UI never constructs or executes arbitrary shell commands.
- The UI cannot mark a task complete or bypass verification/policy.
- Model output is always labeled as an untrusted proposal until accepted by the
  deterministic pipeline.
- Audit history is append-only and cannot be cosmetically rewritten.
- Hosted calls/cost require explicit configuration and disclosure.
- The design must work with no hosted provider configured.
- Do not turn the product into a general-purpose chat or autonomous-agent swarm.
