# ADR 0071: Atomic slice proposals in Local Power

- Status: Accepted
- Date: 2026-07-27

## Context

Live task `TASK-A0E17C03D69B` ran the Focus Orbit build on
`openai_compatible/qwen3.6-27b` through Local Power with ADR 0069 and ADR 0070
in place. Initial operation `EXOP-FE26BA8810574A6F9C9F3888`, continuation
`RVOP-37315CE9A0184ACC8E77DC03`, completed after 13 total turns and 3
verification runs.

The shape of the transcript is the finding:

- Qwen spent its **first six turns replacing `index.html`**, and only
  `index.html`.
- The initial eight-turn session ended with **no `app.js` at all**.
- Once ADR 0070's normalized failure evidence reached the continuation, Qwen
  created `app.js` and passed both required commands.
- The final product worked, but was visually basic and carried behavioral and
  accessibility defects that the configured verification did not detect.

The same Qwen endpoint was then given the same natural-language product brief
directly, with no turn/action protocol at all. It produced all three files in
one response, and the result was more coherent than the harnessed one: a
working timer, an animated progress ring, mode switching, correct radio state.
`web-product-integrity` passed. The owner's tests passed 5 of 7 — it missed
repository-specific element ids and used class methods a brittle test did not
recognize — and its requested JSON envelope was malformed enough to need
mechanical extraction.

The tempting reading is "the harness makes Qwen worse." That is not what the
evidence shows, and this ADR does not accept it. The direct run had no
repository contract, no element-id awareness, no verification, no audit trail,
and could not have completed a task under any definition Apoapsis uses. What
it did have was **permission to produce a coherent whole in one act**.

The defect is proposal granularity. Local Power serializes a naturally
multi-file implementation into one action per turn. For files as tightly
coupled as `index.html`, `styles.css`, and `app.js`, that forces the model to
hold two of the three in its head while regenerating the first, and the
observable consequence is partial regeneration, context drift, and turns spent
rewriting a file that was already adequate. ADR 0059 removed a protocol
mechanic the model was bad at (diff syntax). It left in place a different one:
stating a coherent change one file at a time.

## Decision

### 1. One turn may propose an entire slice

A new Local Power action, `propose_change_set`, carries:

- `summary` — what the slice does.
- `changes` — one or more `write` (whole-file create or replace) or `delete`
  operations.
- `verification_commands` — an optional **request** naming commands from the
  configured catalog. It cannot define a command.
- `base_worktree_digest` — an optional optimistic-concurrency claim.

There is deliberately **no patch operation**. The handoff that prompted this
ADR listed patches among the possible operations; adding one would reintroduce
the exact failure mode ADR 0059 removed, into the one mode built to avoid it.
Whole-file authorship is the premise of Local Power, and a change set is that
premise applied to more than one file at once.

Nesting stops at one level. The models this mode targets fumble deeply
structured JSON, so `changes` is a flat list of three-field objects and
nothing inside it nests further.

### 2. All of it applies, or none of it does

`LocalPowerSession._apply_change_set` validates the complete proposal before
writing a byte:

- every path through the same `SandboxGuard` that governs a single
  `write_file` — traversal, drive letters, symlink escape, forbidden globs;
- no path named twice, because a proposal must state one final intent per file;
- a `delete` must name an existing file, and may not name a path a configured
  verification command points at — checks are not removed to make them pass;
- content size and binary content per the existing per-file limits;
- requested verification commands must exist in the catalog;
- at most `min(max_change_set_files, max_changed_files)` files, so lowering the
  session ceiling always lowers the per-proposal ceiling and the two cannot be
  configured into disagreeing;
- `base_worktree_digest`, when supplied, must equal the current worktree
  fingerprint (ADR 0017).

Every problem is reported at once rather than the first one found. Since the
whole proposal is rejected anyway, the complete list costs nothing and is the
difference between one repair turn and four.

Only the changed-line ceiling cannot be known before writing, because it is
measured from the computed diff. That path writes, measures, and on violation
restores every touched file byte-for-byte in reverse order before recording a
refusal. A directory created to hold a new file is left in place: it is empty
and invisible to Git, and removing it would mean deciding whether an empty
directory that existed beforehand was ours to remove.

The invariant a reviewer should hold onto: **no partial filesystem mutation**.
An invalid proposal leaves the sandbox exactly as it was, so the next turn
repairs one coherent slice rather than reasoning about a half-applied one.

### 3. The harness verifies the slice itself

Once a change set applies, the harness runs the required configured commands
(plus any the proposal legitimately requested) without being asked, and the
existing ADR 0069 sufficiency check ends the session the moment every required
command has passed for the resulting fingerprint. Making the model spend a
second turn asking whether its increment worked is the granularity problem in
miniature, and the answer was never the model's to give.

Finalization still reuses that pass rather than re-running it, so a
change-set session that succeeds performs exactly one verification run.

### 4. Repair prompts are delta-oriented

When the sandbox already contains work, the prompt says so, lists
`CURRENT_CHANGED_PATHS_JSON`, and states plainly: do not regenerate this from
the objective; read the `<verification:NAME>` output and propose one atomic
repair change set containing only the files the repair needs.

This is the same lesson as ADR 0070 applied one level up. A model told only
what the product should be will re-derive the product — which is precisely
what six consecutive `index.html` rewrites look like. A model told what
exists, what failed, and what remains unproven has a repair to make.

`WORKTREE_DIGEST` is stated in the prompt so a model that wants concurrency
protection can echo it back.

### 5. The one-action protocol remains a real comparison arm

`atomic_change_sets = false` restores the pre-0071 behavior exactly: the
grammar schema omits `changes` and omits `propose_change_set` from the action
enum, the prompt never mentions the action, and the harness refuses it if one
arrives anyway. That makes the three-way evaluation below a comparison between
two protocols rather than between one protocol and a discouraging sentence.

It defaults to `true` because Local Power is itself opt-in and disabled by
default; this widens an experiment, not the product's default path.

## Authority: unchanged

A change set is a **proposal**. Apoapsis parses, validates, applies, verifies,
records, and terminates. The model gains no authority over direct filesystem
mutation, shell execution, Git, network, verification results, workflow
transitions, retry or budget ceilings, completion, or audit records. Every
boundary in `apoapsis.agent.sandbox` applies to every operation in a change
set exactly as it applies to a single `write_file`; the only thing that
changed is how many operations one refusal or one acceptance covers.

## Alternatives considered

**Remove the harness for generation and re-attach it for verification.** The
direct-baseline result invites this. Rejected: the direct run's advantages
(coherence) and its failures (wrong element ids, malformed envelope, no
repository contract) both come from the same absence of repository-specific
feedback. Atomic slices are an attempt to keep the contract and recover the
coherence, not to trade one for the other.

**Let a change set request arbitrary verification, including new commands.**
Rejected outright. The catalog is harness-owned; a proposal may name from it
and nothing else.

**Include a patch operation.** Rejected; see decision 1.

**Refuse a no-op change set that rewrites identical content.** Not done. The
fingerprint is unchanged, so the existing ADR 0069 cache returns the recorded
result and the model sees a state that did not move. Adding a refusal would
spend a rule on a case the existing machinery already handles honestly.

## Consequences

- A model may state a coherent multi-file increment in one turn, which is the
  unit a product increment actually has.
- An invalid proposal is a clean no-op with a complete problem list, rather
  than a partially-built sandbox.
- `local-power-change-set-NNN.json` joins the audit record, and
  `LocalPowerReviewPackage.change_sets` carries every proposal — applied or
  refused — with the digest the harness observed next to the digest the model
  claimed.
- A successful change-set session runs verification once.
- None of this makes Qwen better at building web applications. It removes a
  constraint that the transcript shows was actively working against it.
  Whether it helps is the question the evaluation below exists to answer, and
  this ADR makes no claim about the outcome.

## Evaluation plan (not yet run)

Repeat the exact Focus Orbit challenge across three arms — current one-action
Local Power (`atomic_change_sets = false`), atomic-slice Local Power, and
direct one-shot generation — and record for each: model identity, total turns
and calls, files rewritten more than once, verification runs and refusals,
time to a first complete three-file implementation, owner-test results,
`verify-web-product` results, browser behavior and console errors, a visual
quality review, and the acceptance criteria left unproven.

Success is atomic-slice mode retaining repository-contract compliance while
approaching the coherence and visual quality of direct generation. Live Qwen
results must be recorded separately from the fake-provider evidence below.

## Evidence

- Deterministic fake-provider coverage: `tests.test_local_power_session`
  (`AtomicChangeSetTests`, 23 tests) — multi-file create and replacement,
  delete-plus-write in one proposal, the audit artifact, forbidden-path atomic
  rejection, no partial mutation when a later operation is invalid, all
  problems reported at once, duplicate paths, deleting a verification-named
  path, an unconfigured verification request, both file ceilings, the
  changed-line rollback restoring a replaced file byte-for-byte, stale and
  current worktree digests, harness-run verification of an applied proposal,
  automatic termination, no redundant final verification, a failing proposal
  followed by an atomic repair, the delta-oriented repair prompt, a resumed
  session repairing a prior stage atomically, and the disabled comparison arm
  removing the action from both the prompt and the grammar. Plus 4 new
  `ActionProtocolTests` for parsing, the write/delete content invariants, and
  tool-residue stripping inside a change set.
- `python -m unittest tests.test_local_power_session` passed **93/93 with 1
  expected symlink-permission skip** on 2026-07-27 (Python 3.14.5), up from
  ADR 0070's 66. `python -m compileall -q src tests` passed and
  `git -c core.whitespace=blank-at-eol,space-before-tab,cr-at-eol diff --check`
  passed on the changed paths.
- **No live model run was performed for this ADR.** Everything above is
  fake-provider evidence. The Qwen transcripts in Context are prior evidence
  that motivated the change, not evidence that it works.
- The full suite was not run, at the owner's explicit request.

## Related defect (tracked separately)

The `TASK-A0E17C03D69B` continuation completed while `report.json` and the
Report page retained the original `human_review_required` headline. That is a
reporting-consistency defect in the continuation path, independent of proposal
granularity, and is recorded in `NEXT_STEPS.md` rather than fixed here.
