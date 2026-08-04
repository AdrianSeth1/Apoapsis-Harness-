# ADR 0106: State inherited slice state instead of making the agent find it

## Status

Accepted and implemented on 2026-08-03.

## Context

Every slice runs in a fresh Qwen session. That is the right design — no
cross-slice contamination, and inherited state travels through *code* rather
than through a context window nobody can audit — but it has a cost that grows.

`CAP-4EE9F101146E4556` made 122 tool calls, 44 of them `read_file`. Most of
that was the agent rediscovering, from zero, files earlier slices had written
and the harness already had perfect deterministic knowledge of. Slice N's
context cost is therefore proportional to the size of the inherited codebase,
not to the size of the slice, which is the trajectory behind "16K of context by
slice 4": by slice 10–15 of a 40-slice plan, sessions cross the CLI's
compression threshold mid-work, and mid-slice compression is where quality
craters. The same waste shows in the budget — one slice already consumed ~2.01M
of a 2.5M per-arm allowance.

The harness does not need to ask a model any of this. It walked the tree to
build the delta. It wrote the checkpoint records. It knows which slice produced
which file and which additions its own witnesses proved were reached.

## Decision

An **orientation brief** is generated deterministically and appended to
`task.md` under "Inherited state - read before exploring", before the judgement
contract and before the instruction to implement. It contains: what each
completed earlier slice of this plan added, with the behaviour names its
checkpoint recorded; the plan's integration contracts for this slice; the
verification commands as the harness will run them; and the current file tree
with line counts.

**No model calls, and nothing inferred.** Every line is recomputable
byte-for-byte from the same inputs, and a test asserts reproducibility. A brief
assembled by a model would be a summary the harness could not vouch for, in a
place where being wrong is worse than being absent.

**It is built on the host, not in the controller.** Earlier slices' reports and
checkpoint records live in the project's audit tree; the controller sees only
the request. `product.py` builds it and passes it through, and the controller
appends it verbatim, so the brief that was built and the brief that was sent
cannot differ.

**It is bounded at ~2,500 tokens, enforced by test.** Over the cap it degrades
to the directory shape plus this slice's own declared paths, since those are
what the agent would otherwise spend its first turns locating. The directory
summary is itself bounded — a summary larger than what it summarises is not a
summary — and the count of what was omitted is stated rather than quietly
dropped.

**Exploration is made unnecessary, not forbidden.** The brief says "read files
when you need their contents; you should not need to go looking for what
exists". A prohibition would be unsafe: if the brief were ever wrong or
incomplete, an agent forbidden from checking has no recovery, and a test
asserts no prohibition appears.

**Completion is read from the task's own report, not the slice record's
status.** All four finished slices in `test project 6` sit at `approved` in
`plan-slice-executions.db` while their reports read `complete`. Gating on the
record status would have produced an empty brief on every real project, and
silently — the worst way for an optimisation to fail. (The record not advancing
to `COMPLETE` is a separate defect, recorded in NEXT_STEPS.)

**`.apoapsis` and `.sol` are excluded from the tree**, unlike in the delta,
where their presence must be *seen and refused*. They are harness state rather
than the product's code; describing them as inherited work would be false, and
in a real project they are thousands of audit files. The generated text is
ASCII, because it crosses Windows → WSL → container and a completed slice has
already been lost once to a decode error on that path.

## Consequences

Slice N > 1 opens knowing what exists. The expected effect — fewer `read_file`
calls, lower per-slice input tokens — is measurable rather than hoped for,
because ADR 0101 records per-call usage: compare the next live plan run against
CAP-4EE9's 44 `read_file` calls and ~2.01M cumulative input tokens.

The brief adds a few hundred tokens to the first prompt of each slice. On the
observed project that is ~390 tokens against the 44 file reads it aims to
replace, and it is paid once per slice rather than once per turn.

A wrong brief is now a way to mislead the agent that did not previously exist.
That is mitigated by construction — everything is read from artifacts rather
than inferred — and by never forbidding the agent from checking for itself.
