# ADR 0110: Stable-to-volatile prompt ordering, and a measured token ceiling

## Status

Accepted and implemented on 2026-08-03.

## Context

Two defects lived in the same place — the assembled step prompt — and neither
was visible from inside the code that caused it.

**The prefix cache was being defeated on every turn.** `agent_step_prompt`
(`models/prompts.py`) and `local_power_step_prompt` both emitted `TURN {n}`
and the remaining budgets immediately after the byte-stable static prefix.
llama-server reuses the KV cache of the longest common prefix between
consecutive requests, so the common prefix ended within the first one to two
thousand tokens and every turn re-prefilled the task specification, hard
constraints, repository evidence, and session history from scratch. Because
history grows with the session, the waste compounds: the cost of a session is
quadratic in its own length. `prompt_static_prefix()` exists, its docstring
says "for cache reuse", and segment ordering was throwing away everything it
bought.

**Nothing enforced prompt ≤ window.** `[context] max_total_chars = 180000` is
roughly 45K tokens under the project's own 4-chars/token heuristic, against a
`context_window_tokens` of 32,768 declared in the same config file. The two
numbers were never reconciled. `context/measurement.py` measures a compiled
`ContextPackage` and is explicitly documented as never influencing retrieval,
ranking, or truncation — accurate, and beside the point, because nothing else
measured either. The one component that did enforce a threshold and compact,
`workcell/session.py`'s `SessionCoordinator`, was unreferenced and has since
been removed (ADR 0109).

The failure mode this leaves is the worst one available. An oversized prompt
is not rejected; it is silently truncated by the server, from the front, which
is where the action protocol and the sandbox rules live. The harness then
records a turn as though the model had seen a prompt it never received, and
reasons about the resulting garbage action as a model failure.

## Decision

**One: step prompts are ordered most-stable to most-volatile, and that
ordering is an invariant, not a layout preference.**

Emission order for both step prompts:

1. static prefix (byte-stable, unchanged)
2. session-fixed: task specification, hard constraints, patch policy,
   configured command names, acceptance criteria, sandbox boundary
3. slowly changing: external research brief, repository evidence
4. per-turn: session history, refusals, harness-derived obligations,
   verification state
5. fully volatile: `TURN`, remaining budgets, next-action requirements,
   outstanding guidance

Every segment's *content* is byte-identical to what it was; only position
changed. The invariant is stated in a comment at each prompt builder and
asserted positionally in `tests/test_prompt_window.py`, including a direct
measurement that two consecutive turns share a prefix reaching at least the
history segment.

This is not purely a cache optimization. Volatile state now sits nearest the
generation point, which is where small models attend to it best — the same
reasoning ADR 0070 used when it moved the outstanding-command sentence next to
the machine-readable state rather than into the static rules.

**Two: the assembled prompt is measured against the model's declared window
before dispatch, and shrunk or refused.**

`apoapsis/context/window.py` measures the fully assembled prompt against
`context_window_tokens - max_output_tokens - safety_margin`, taken from the
*provider's* configuration rather than from the context compiler's char
budget. When the prompt is over, it reduces in a fixed, deterministic order:

1. observations, oldest first — the ledger view, already bounded elsewhere and
   the cheapest thing to lose;
2. evidence excerpts, lowest compiler priority first — reusing the compiler's
   own retrieval-reason ranking, now exported as
   `evidence_reason_priority`, so the least-justified excerpt goes first
   rather than whatever happens to be last in the list;
3. session history, oldest turn first, each dropped turn replaced by its
   one-line summary rather than deleted — a model that cannot see that it
   already tried something will try it again, which is the exact failure ADR
   0070 and the no-progress stop guard were both built to catch.

If the prompt still exceeds the ceiling after all three, **dispatch is
refused** and the session stops with a named outcome
(`prompt_window_exceeded` / `escalation_required`). What remains at that point
is the static protocol and the task specification; cutting either produces a
model that cannot emit a valid action, so there is nothing honest left to
trim. A named stop is strictly better evidence than a truncated send.

Both measurements — before and after — are recorded on the turn record
(`AgentTurnRecord.prompt_window_fit`). "We sent 3,000 tokens" and "we sent
3,000 after cutting 8,000" are different facts about the same turn, and only
the second one explains a degraded result.

**Three: an exact token count is used when one is cheaply available, and never
depended on.** When the coding provider is a loopback OpenAI-compatible
endpoint — that is, llama-server — `models/tokenize.py` offers its `/tokenize`
endpoint as the counter. Every failure path returns `None` and falls back to
the heuristic. A tokenizer that is slow, down, or newly incompatible must not
be able to stop a session the heuristic could measure perfectly well.

**Four: a provider that declares no window gets no enforcement.** `None` for
`context_window_tokens` means "we cannot make a claim about this provider",
not "this provider has an infinite window". Inventing a ceiling would refuse
dispatches against a limit nobody configured. Likewise a configuration whose
`max_output_tokens` already fills its own window is a configuration defect and
belongs to `doctor`, not to a per-turn refusal on every task.

## Consequences

- Sessions on the bounded and Local Power paths re-prefill only the volatile
  tail between turns instead of the whole prompt. The win scales with session
  length, so it is largest exactly where the old behavior was worst.
- The char budget and the token window are connected for the first time. A
  project configured with `max_total_chars` well above its window no longer
  silently loses the front of its prompt; it loses its least-justified
  evidence, visibly, on the record.
- A new stop reason exists. Operators can now see "the prompt did not fit"
  as a distinct outcome rather than as an inexplicable malformed action.
- The reduced `ContextPackage` — not the compiled one — is what reaches
  `model_call`, so context measurements and audit artifacts describe what was
  transmitted rather than what was retrieved.
- `ContextCompiler._priority` is now a thin wrapper over the module-level
  `evidence_reason_priority`. One ranking, two callers, no drift.
- The Capability Sandbox path is untouched. The Qwen CLI owns its own context
  inside the workcell and compresses against its own 65,536-token window; this
  ADR governs only prompts the harness itself assembles.

## Alternatives rejected

**Lower `max_total_chars` to fit the window and stop there.** It would remove
the arithmetic contradiction without removing the failure mode: a session's
prompt also carries history and observations that grow, so a compiler budget
that fits on turn one does not fit on turn twelve. The ceiling has to be
checked against the assembled prompt, not against one of its inputs.

**Truncate the prompt to fit and send it anyway.** This is what the server
already did, and doing it deliberately would only move the same corruption
inside the harness. The value of the boundary is that the harness knows what
the model saw.

**Require the exact tokenizer.** It would make every local session depend on
an HTTP call per turn to a component whose failure has nothing to do with the
task, in exchange for accuracy the safety margin already covers.
