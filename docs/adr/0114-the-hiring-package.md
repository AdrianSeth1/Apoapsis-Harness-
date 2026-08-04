# ADR 0114: The hiring package, and a drift guard for its claims

## Status

Accepted and implemented on 2026-08-03.

## Context

The review's §4 answered "will this get you hired?" with: *not in its current
shape, and yes with two weeks of packaging work* — because the substance is
real but **unsampleable**. Nobody who can hire you reads a 179 KB handoff, a
140 KB README, or 80K lines across 95 test files. A reviewer gives a repo ten
minutes, and those ten minutes were hitting a wall of internal vocabulary, an
ADR changelog, and no way to see the system run without a local Qwen
deployment.

MH-8 asks for four artifacts that convert the work into something reviewable.
None of them change harness behaviour. All of them can be wrong in a way the
test suite would never catch — which is the interesting part of this decision.

## Decision

**Four artifacts, and one test that keeps them honest.**

`README.public.md` (200 lines, ceiling 300), `docs/crisis-atlas-experiment.md`,
`docs/demo-recording-script.md`, `docs/publication-checklist.md`.

### The headline is the result that embarrassed the harness

The lede is not "witness-gated completion". It is: *I measured my harness
against an unrestricted baseline, the baseline beat my first protocol, and I
rebuilt the architecture around that result.* The same run also showed the
control would have shipped a false success its own 88 passing tests missed —
which is the argument **for** the harness, arriving in the run that discredited
its first design.

Leading with the failure is not modesty. It is the most distinguishing thing
here: very few candidates can show they ran the control that could have
invalidated their design, and then published the result.

### Every number traces, and a test enforces it

Each figure was copied from a dated file in `docs/evaluation/`, and
`tests/test_hiring_package.py` asserts that each still appears **in both** the
public document and the evidence file it came from. Prose drifts; evidence gets
edited; a number in a public README that no longer appears anywhere is exactly
the kind of quiet dishonesty this project exists to prevent. When one of those
assertions fails the fix is to re-read the evidence, not to change the
assertion.

The test also pins the qualifications, not just the claims: that Crisis Atlas
is *not held out*, that the 0/6 negative result is still there, that detection
was proven **deterministically and never live**. Those sentences are the most
credible part of the package and the first thing that quietly disappears when a
document is edited for punch.

### No opaque identifiers in reader-facing prose

`EXOP`, `RVOP`, `SXP-`, `DISC-`, `FPKG` are meaningful inside the harness and
meaningless to a reader — the review's "naming tax", asserted. The publication
checklist is explicitly exempt: it is written for the person publishing, not
the person reading, so it names the real artifact it is asking about. The guard
found one live instance on its first run — the demo script's "do not use the
vocabulary" bullet was itself enumerating the vocabulary.

### The quickstart must be runnable by someone with no GPU

Verified, not asserted: 51 tests in about six seconds across
`test_vertical_slice`, `test_capability_sandbox_product` and
`test_workcell_checkpoint`, plus `init` and `doctor` on a bare Git repository.
The test checks those modules exist and that every deep-doc link resolves,
because a copy-pasted quickstart that fails is the first thing a reviewer runs
and the last thing they run.

`doctor` is in the quickstart deliberately. On a fresh project it reports
`warning`, and every warning is a statement about what a passing check would
*not* mean — including `evidence level development_only`. A system that says
that out loud demonstrates more than a green tick does.

### The checklist reports what is actually tracked, not what is ignored

Writing it surfaced a real publication blocker that `.gitignore` hides:

```
tracked under spikes/native-shell-tauri/src-tauri/target/ : 3,101 files, 2,424.9 MB
everything else in the repository                        :   807 files
```

`.gitignore` lists that directory. The files were committed before the rule
existed, and an ignore rule does not untrack. **79% of tracked files and ~2.4 GB
are Rust build output from an abandoned spike.** Also found: a tracked
planning-handoff response in the repository root, and six documents citing
`/home/arya/...` paths. Every one of those is stated as a measured count in the
checklist rather than as a caution.

## Consequences

- The ten-minute path exists: public README → experiment writeup → run 51 tests
  with no GPU.
- The claims cannot rot silently. 13 figures and 6 qualification statements are
  under test.
- The licence question is raised explicitly rather than inherited: `LICENSE.txt`
  is PolyForm Noncommercial 1.0.0, which is a deliberate and defensible choice
  for work meant to be *read*, and the wrong one if adoption is the goal. The
  checklist makes that a decision instead of an accident.
- The 2.4 GB is now a known, quantified, first-in-the-list task rather than
  something a reviewer discovers by waiting for a clone.
- The recording is still the owner's to make. The script makes it repeatable
  and, more usefully, makes the *preparation* explicit — including warming the
  controller image, without which a third of the three-minute budget is a
  motionless spinner (ADR 0113).

## Alternatives rejected

**Rewrite `README.md` in place.** The current README is a genuinely good
operator guide; it is simply not a first contact. Both exist, and the checklist
says which becomes `README.md` at publication so a reviewer is never asked to
choose between two READMEs.

**Cite round numbers.** "~2M tokens" and "8× more input" read better than
2,080,801 and 8.0. But the precision is the point: it signals the number was
measured rather than remembered, and it is what makes the honest qualifications
alongside it believable.

**Leave the numbers untested.** They were correct when written, which is the
same thing every stale document was once. A drift guard costs nine tests.
