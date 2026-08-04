# ADR 0109: One local execution path, and the phantom one removed

## Status

Accepted and implemented on 2026-08-03.

## Context

Four local-coding pathways existed in the tree:

1. **Bounded agent protocol** — stateless, one JSON action per turn, prompt
   rebuilt every turn.
2. **Local Power** — the same shape with whole-file writes and a mediated
   shell.
3. **Capability Sandbox** — the pinned Qwen CLI inside a `--network none`
   workcell, with checkpoint admission and witness-gated completion. This is
   what actually ran every slice in `test project 6`.
4. **`SessionCoordinator`** — a harness-owned loop with a stable kernel,
   budget clock, tiered compaction and spill directory, referenced by nothing
   except its own test.

The project's own evidence already settled the architecture: the unrestricted
CLI outperformed the bounded protocol, and the winning shape is *native agent
inside, deterministic gate outside*. The Capability Sandbox is that shape. What
remained was a tree that did not say so — four pathways to maintain, test,
explain and keep prompts current for, three of which nothing runs.

## Decision

**The Capability Sandbox is the single local execution path for approved plan
slices, and the default in code as well as in the template.**
`CapabilitySandboxConfig.enabled` was `False` in the class while `apoapsis
init` wrote `true` and config loading migrated a missing table to `true`. A
library caller or a test constructing `ApoapsisConfig()` therefore got a
*different execution path* from every real project — the same class-versus-
template drift ADR 0104 fixed for patch ceilings, in a far more consequential
field. Both now say `true`, asserted together in one test.

**Ordinary (non-plan) tasks keep the bounded path, deliberately.** The
reviewer's recommendation was to route them through the sandbox too. That was
weighed and declined: the sandbox refuses any task without an approved slice
contract, so this would mean synthesising a contract from a bare
`TaskSpecification`, and it would make a one-line quick change depend on WSL,
Docker and a pinned 16.8 GB model. A harness whose smallest useful action
requires the whole container stack is worse for the operator than one with two
honestly-labelled paths. The one-path claim is therefore scoped to what the
evidence actually covers: plan slices.

**Local Power is named as legacy and never selected for you.** It is reachable
only by explicit opt-in, its config field says so, and `apoapsis init` does not
write an `[execution.local_power]` table at all — a fresh project's
configuration does not present it as an option to toggle.

**`SessionCoordinator` is deleted, and its design is recorded here.** It was
the best context-management code in the project and it was wired to nothing:
one rendered-once kernel artifact hashed and re-read every turn (drift is a
provenance question, so the control is provenance, not pattern-matching on
"volatile-looking" text); provider-reported tokens only, never the controller's
estimate, for both compaction and ceilings, because a session stopped on an
estimate is governed by the estimator's error rather than the owner's budget;
and every ending routed through one `_stop` that appends a transition, so a
reader sees the state machine's path and not just its verdict. Its four
components remain as modules with their own tests — `workcell.context`,
`workcell.compaction`, `workcell.budgets`, `workcell.checkpoint` — so anyone
rebuilding a harness-driven mode assembles those rather than starting over.
`SessionOutcome` also remains: it names how a contained session ends and the
operator renderings in `reporting.operator` are keyed on it.

Deleting a working implementation is uncomfortable, and the alternative was
worse. A module that looks live in the tree and is reached by nothing costs a
reader their trust in everything next to it.

**`doctor` checks the default path.** It previously reported cheerfully on a
machine where the default local path could not start at all, and the operator
found out at the first slice. It now reports whether the sandbox is enabled,
whether the launcher and pinned manifest are present, and whether the
Ubuntu-24.04 WSL distribution answers — shallow and read-only on purpose, since
a doctor that spent two minutes building a container image would stop being
run.

## Consequences

The maintenance surface loses one whole pathway and its test file. Three remain
in the tree and only two are reachable without an explicit opt-in: the sandbox
for slices, and the bounded protocol for ordinary tasks.

Flipping the class default immediately surfaced a real caller: the diagnostic
probe measures the bounded loop's own prompts and had been relying on the old
default to get it. It now names the legacy path explicitly, which is exactly
the behaviour this ADR asks of an operator, and is a fair demonstration that
the default actually changed something.

Making the sandbox the default also made one pre-existing validation rule
wrong. "Capability Sandbox enabled + `frontier_only` route" was refused,
because when enabling the sandbox was an explicit act the combination was
evidence of operator confusion. As a default it fired on a perfectly coherent
configuration -- send everything to the frontier coder -- where the operator
had touched nothing. The refusal is removed: the setting means "when a local
slice runs, run it contained", and `frontier_only` means none runs. The
equivalent Local Power rule stays, because Local Power is still an explicit
act.

The `execution.mode = "legacy-bounded" | "legacy-power"` renaming the reviewer
proposed is *not* done. `ExecutionMode` distinguishes one-shot from the bounded
loop and is orthogonal to which local pathway runs; renaming its members would
break every existing config and every evaluation lane to express something the
enum does not mean. The deprecation is expressed where the pathway is actually
chosen instead.
