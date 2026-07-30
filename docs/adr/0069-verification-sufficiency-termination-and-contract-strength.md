# ADR 0069: Verification sufficiency ends a Local Power session, and contract strength is reported

- Status: Accepted
- Date: 2026-07-27

## Context

Live task `TASK-33E0EB6476C4` (operation `EXOP-817930F817144E4EA82AC4BE`,
project `test project 5`) exercised Local Power end to end against Laguna
S 2.1 on a from-scratch browser application. The harness behaved correctly at
every boundary it owns: repository preparation, specification approval, the
authorization disclosure, worktree isolation, network denial, mediated writes,
a clean `main`, deterministic verification, and a complete audit trail. It also
produced a `COMPLETE` on an application that does not run.

Two independent defects combined to make that outcome look unremarkable.

### Defect one: a passing check did not end the loop

The model's action sequence was: write `index.html`, write `styles.css`, write
`app.js`, then `run_verification` five times. The first verification passed on
turn 4. Turns 5 through 8 were byte-identical repetitions of the same 13-token
response:

```json
{"action":"run_verification","command_name":"unit-tests"}
```

At temperature zero, with the most recent successful action being
`run_verification`, that is the deterministically correct continuation of the
prompt the model was given. The prompt told it that `finish` ends its turns and
that verification decides acceptance; it never told it that a passing
verification means there is nothing left to do. So the model asked the
harness's question back to the harness, eight times, and the harness answered
by asking for another action.

`LocalPowerSession` accepted every repetition because nothing in the turn loop
treated a passing verification as terminal. Completion happened only when the
model returned `finish` or a budget ran out. Then, at finalization, the harness
ran the same full verification a sixth time.

Two smaller problems were visible in the same trace. `command_results` was a
flat `name -> status` map with no notion of *which code* produced the result,
so a recorded pass could not be distinguished from a stale one; and there was
no equivalent of `BoundedAgentSession`'s verification cache, so an identical
re-run was executed rather than refused.

This is a controller-design defect amplified by an underspecified prompt. It is
not useful model behavior, and it is not fixed by giving the model more turns.

### Defect two: the contract could not tell a product from a pile of files

The owner-written evaluation contract had seven static tests. They checked that
the required files existed; that the title and viewport meta tags were present;
that the expected control ids appeared; that all three mode labels appeared;
that timer-related JavaScript constructs appeared; that `localStorage` was
used; that accessibility markers, reduced-motion CSS, responsive CSS, several
distinct colors, and a nominal circular-progress technique appeared.

All seven passed. The application was inert. `app.js` attached listeners to
`mode-focus`, `mode-short-break`, `mode-long-break`, and `status-live`;
`index.html` defined none of those ids. `styles.css` styled `.progress-ring`,
`.progress-ring__circle`, `.btn`, `.btn-primary`, and `.btn-secondary`; the
markup carried none of those classes. The browser console reported
`TypeError: Cannot read properties of null (reading 'addEventListener')`.
Start relabelled itself to Pause and started nothing; the mode buttons did
nothing; the orbital ring was invisible; the buttons were browser-default.

Every test asked whether a fragment existed. None asked whether the fragments
referred to one another, and none executed the product. The contract was
satisfiable by three files that each looked right in isolation.

The harness disclosed that acceptance coverage was empty and that baseline
completion does not require criteria to be proven. That disclosure was
accurate and insufficient: it was a blank table next to the word `COMPLETE`,
and a blank table is easy to read as "nothing to report".

More turns would not have helped. An ideal "stop as soon as verification
passes" controller would have completed this same broken implementation after
turn 4. The two defects are independent and both are real.

## Decision

### 1. The harness decides sufficiency, and stops

`LocalPowerSession` gains the verification-state machinery the strict loop has
had since ADR 0017:

- `command_results` becomes `digest -> {command name -> status}`, keyed by
  `compute_worktree_fingerprint`, so a result belongs to the exact code that
  produced it.
- `verification_cache` records which command set has already run at which
  state.
- `_verification_sufficient()` is true when the sandbox has changed at least
  one permitted file, at least one command is required, every required command
  has passed *for the current fingerprint*, and — under the strict policy —
  acceptance coverage computed from those same current-state results is
  satisfied.

After every executed action the loop asks that question and breaks when the
answer is yes. The model is never asked whether a passing result is enough.

Turn, time, command, file, and line ceilings are unchanged. They remain the
circuit breakers; this is a stop condition, not a relaxation.

Two consequences follow directly:

- A model-requested verification that is identical to one already run at the
  current fingerprint is **refused**, not executed. Refusal rather than silent
  reuse is deliberate: a refusal lands in `REFUSED_REQUESTS_JSON`, which the
  prompt already instructs the model not to work around, whereas a silently
  reused pass is indistinguishable from a fresh one and invites the same
  request again.
- Finalization **reuses** a current full-required pass instead of re-running
  it. The fingerprint proves nothing changed; a sixth identical run would be
  waste dressed up as rigor. When no such current pass exists — the model
  finished early, or verified piecemeal, or edited after verifying — the
  harness-owned final verification runs exactly as before.

The Local Power prompt is corrected to state that the harness ends the session
on a passing required set, that a result belongs to the state that produced it,
and that a repeated identical check is refused. It also now names the specific
failure class this trial produced: an element the script queries that the
markup never defines, a rule aimed at a class nothing carries.

### 2. Contract strength is computed, recorded, and shown

`apoapsis.verification.contract` grades the configured contract's *evidence
structure* — never its behavior — into four levels: `none`,
`development_only`, `acceptance_designated`, `criterion_mapped`, with typed
findings and remediation.

It is scrupulous about its own limits. It reads whether any command is
required, whether any carries the owner's `acceptance = true` designation, and
whether every active criterion maps to such a command. It does **not** inspect
argv to guess whether a command "really" exercises the product; a command that
greps a file and one that drives a browser are indistinguishable to it, and a
heuristic claiming otherwise would be a fabricated assurance — the same species
of error as the one being fixed. A test asserts that two commands with
identical structure and wildly different argv grade identically.

The assessment appears in four places:

- `apoapsis doctor`, before any model spend;
- the `ExecutionAuthorizationPackage`, inside the authorized and hashed
  content — what a confirmation authorizes is partly *what a success will
  mean*;
- `FinalTaskReport.verification_contract` and the Local Power review package,
  plus its own audit artifact;
- the UI: on the start-coding confirmation and next to the outcome, as a
  sentence rather than an empty table.

The trial's contract grades `development_only`, and a `COMPLETE` produced
under it now carries, in the recorded stop reason, the qualification that
passing it means the configured commands exited zero and nothing more.

**This does not block anything.** Baseline completion semantics are unchanged;
`apoapsis eval` still depends on them for false-success measurement, and a
blank repository with no product yet is a legitimate state. The change is that
the word `COMPLETE` no longer arrives unqualified from a contract that cannot
support it.

### 3. A real check an owner can configure instead

`apoapsis verify-web-product` cross-references a dependency-free browser
product's own files: every id and class a script looks up must be provided by
the markup or created by a script; every CSS rule must be able to match
something; ids must be unique; top-level function names must not collide;
referenced local assets must exist; external resources and network APIs can be
forbidden. Selectors too complex to analyze with confidence are counted and
reported as unchecked rather than assumed fine.

It is stdlib-only, offline, and deterministic — the same properties the
products it verifies are usually required to have. It exits non-zero on error
findings, so it works as a `[[verification.commands]]` entry, and an owner may
mark it `acceptance = true`.

Run against the trial's preserved worktree it reports exactly the four missing
ids and the five dead style rules that the live browser review found, and
nothing else. Run against Apoapsis's own client-rendered UI — a working
product whose entire DOM is built in JavaScript template literals — it passes.
That second result mattered more than the first during development: the initial
implementation reported every id in the UI as unresolved, and a check that
cries wolf on working products is a check owners disable.

Behavioral verification, which needs a real browser, is a **seam with no
provider**, modelled on ADR 0055/0056's official-document search seam.
Requesting it raises `BrowserProbeUnavailableError` and fails the command.
Degrading a requested behavioral check into a silent pass would reintroduce
precisely the failure this ADR exists to address.

## Alternatives considered

**Let the loop run unbounded once verification passes.** This was the
model's implicit proposal and it is wrong. Turns are the mechanism by which
Apoapsis mediates and audits each action; removing the ceiling removes the
mediation. Unbounded turns here would have produced an infinite sequence of
identical passing verification requests.

**Silently reuse a cached result for a repeated model request.** Cheaper, and
worse: the model cannot tell reuse from a fresh run, so nothing discourages the
repetition. Refusal is legible to the model and to a reviewer.

**Block `COMPLETE` when the contract is weak.** Rejected. It changes baseline
semantics that `apoapsis eval` relies on to measure false success, and it
would stop legitimate blank-repository workflows. Disclosure preserves the
owner's authority over what counts as proof while removing the ability to be
misled by silence.

**Infer whether a command "really" tests the product.** Rejected as
unfoundable. Nothing in argv distinguishes a real check from a grep, and a
confident wrong answer here is worse than an honest structural one.

**Ship a headless-browser verifier now.** Rejected as unverifiable from the
current environment. The seam is defined; the claim is not made.

## Consequences

- Local Power sessions stop at the turn the contract is satisfied. On the
  trial's exact sequence that is turn 4 instead of turn 8, with five
  verification runs instead of six.
- A stale result can no longer satisfy anything, in either session class.
- Every report and review package states what its contract could prove.
- Owners of browser products have a configurable check that would have caught
  this failure, and a documented reason to mark it `acceptance = true`.
- The harness still makes no live-browser claim.

## Evidence

- Deterministic fake-provider coverage: `tests.test_local_power_session`
  (`DeterministicTerminationTests`, `ContractDisclosureTests`) and
  `tests.test_verification_contract`.
- Live artifacts inspected: `TASK-33E0EB6476C4`'s `report.json` and the
  preserved worktree at
  `test project 5/.apoapsis/worktrees/33e0eb6476c4`.
- `apoapsis verify-web-product` run against that worktree (fails with the four
  unresolved ids and five dead rules) and against
  `src/apoapsis/ui/static` (passes).
- No live local-model rerun was performed for this ADR. The termination change
  is proven against fakes only; a live rerun remains outstanding.
