# The Crisis Atlas experiment

*How measuring my own harness against an unrestricted baseline made me rebuild
it — and what the rebuilt version can and cannot claim.*

Written for an engineer who has never seen this codebase. Every number here
links to the dated evidence file it came from; nothing is rounded in my favour.

---

## The question

I built a harness that lets a local coding model work on a real repository
without trusting it. The model proposes; the harness owns the filesystem,
verification, and the decision that a task is finished.

The first version enforced that by making the model's action surface very
narrow: **one JSON action per turn**, stateless, prompt rebuilt from scratch
each time — read a file, search, propose a patch, run a check. Safe by
construction, because the model never touches anything directly.

The question I had not answered was the obvious one:

> Is the narrow protocol *costing* me model capability — and if so, how much?

There is an easy way to not find out, which is to never run the control.

## The setup

**The task.** "Crisis Atlas": build an incident-tracking web product — domain
model, persistence, service layer, JSON/Markdown export, HTTP API, single
process serving both UI and API, browser dashboard, README. Non-trivial,
multi-layer, and it has a real acceptance surface: you can drive the finished
thing in a browser and see whether it works.

**The model.** Qwen3.6-27B Q4_K_M on llama.cpp, OpenAI-compatible endpoint,
65,536-token context, temperature 0. Same weights, same server, both arms.

**The plan.** Both arms received the *same* approved plan, produced by a
separate frontier session. This is a control for the **model's action
interface**, not for planning-versus-no-planning.

**Arm A — Apoapsis v1 (bounded).** Four slices, each executed through the
one-action-per-turn protocol. A frontier model (Codex) inspected each slice
after the local model stopped, repaired the checkpoint where necessary, and the
next slice was seeded from that verified commit.

**Arm B — unrestricted control.** The same model, given the same complete plan,
operating as an autonomous CLI agent with a real shell in a disposable Docker
container: no network, no Docker socket, no credentials, no host paths except a
fresh clone of the seed commit; 2 GiB memory, 4 CPUs, 256 processes,
capabilities dropped, `no-new-privileges`. It could create, overwrite, delete,
run and test anything under `/workspace`. It received no Apoapsis verification
output, no repair, no browser result, no human hint.

Evidence: [`crisis-atlas-qwen-cli-control-2026-07-30.md`](evaluation/crisis-atlas-qwen-cli-control-2026-07-30.md),
[`crisis-atlas-64k-codex-frontier-trial-2026-07-30.md`](evaluation/crisis-atlas-64k-codex-frontier-trial-2026-07-30.md).

---

## Result 1: the control beat my protocol

The unrestricted agent inspected the seed, implemented every planned layer,
wrote its own tests across domain, persistence, service, API, behavioural and
launch concerns, ran the suite, **diagnosed nine errors in its own integration
fixture**, repaired them, re-ran, exercised the real server, and stopped with a
completion report. Its self-authored suite: **88 tests, all passing.**

The bounded arm, on the same plan:

| Slice | What the bounded protocol produced |
| --- | --- |
| 1 — domain/persistence | `HUMAN_REVIEW_REQUIRED` after 12 calls; the configured launch check failed because the test file did not exist |
| 2 — services/exports | Reported `COMPLETE` **in one call**. The accepted patch was an incompatible skeleton that no test ever imported |
| 3 — HTTP API | Reported `COMPLETE` in one call. Nonexistent static directory, unreachable export routes, non-serializable responses, crashing traversal handling |
| 4 — dashboard | Reported `COMPLETE`. Browser flow worked; README flags wrong, configured checks did not prove their own labels, mutation failures used `alert()` |

Every slice needed frontier repair. Slice 2's "complete in one call" is the
clearest single data point in the whole experiment: the protocol accepted a
skeleton as a finished slice because the configured checks passed **without
ever importing it**.

The token picture is not flattering to the control, and belongs here:

| Arm | Model calls | Input tokens | Output tokens | Provider latency |
| --- | ---: | ---: | ---: | ---: |
| Four bounded slices | 19 | 258,632 | 55,364 | 1,467.5 s |
| Unrestricted control | 62 | 2,080,801 | 35,787 | 1,052.3 s |

The control used **8× the input tokens** — the growing conversation and shell
output were replayed on every call — for 35% fewer output tokens and 28% less
latency. Better work, dramatically more expensive input. That number is why
prefix-cache behaviour and context compaction later became first-class
concerns rather than optimisations.

### The context ceiling, exactly

The control's first context died in a way worth recording precisely. Its
attempted README write ended at **prompt 64,409 + completion 1,127 = 65,536
tokens exactly**. Finish reason `length`; the tool-call JSON was truncated
mid-string and could not execute; the next request returned HTTP 500 because
the conversation no longer fit the window.

That is a context-ceiling failure, not a model failure and not an output-cap
failure. A CLI agent is expected to compact or roll. The evaluator started one
fresh continuation against the unchanged worktree, charged it only the
remaining wall-clock budget, and it finished normally.

---

## Result 2: the control would have shipped a false success

The control declared every acceptance criterion satisfied, including dashboard
filtering and web integrity. Independent verification — a real browser, driven
by hand, plus the strict static web check — disagreed:

| Check | Result |
| --- | --- |
| The agent's own 88 tests | 88/88 passed |
| `compileall`, `git diff --check` | passed |
| Forbidden offline-storage search | clean |
| Strict `verify-web-product` | **failed** — 10 warnings as errors |
| Independent browser lifecycle | create/select/status/timeline/action/reload passed; **filter failed** |

Creating a critical incident, changing it to `investigating`, then selecting
the `Closed` filter still displayed it. Both `/api/incidents?status=closed` and
`?status=investigating` returned the same incident. Cause: `api/server.py`
parsed the URL but passed only `parsed.path` to the handler, which then mistook
regex path parameters for query parameters. **The service's filter
implementation was present and simply never received the query.**

Eighty-eight self-authored tests, all green, and the product's headline feature
did not work.

Nine of the ten strict-web warnings were verifier false positives — classes
applied dynamically by JavaScript, which the browser proved do render. The
tenth was a computed fetch target the static checker cannot prove same-origin;
source and browser both show it is. I am recording that the static check was
mostly wrong *and* that the arm still fails the approved contract, because the
plan requires that command to pass.

**This is the finding that justifies the harness, and it arrived in the same
run that embarrassed the harness.** The bounded arm also had false-green gaps —
but frontier checkpoint review caught and repaired every one. The unrestricted
arm had no such review and would have shipped.

---

## What I changed because of the evidence

The conclusion was not "the harness was wrong" or "the control was wrong". It
was: **the sandbox boundary was doing useful work; the narrow action protocol
was not.** So the architecture became *native agent inside, deterministic gate
outside*.

- **The agent gets a real shell and a persistent worktree**, inside a
  `--network none` container whose only egress is a controller-owned Unix-socket
  relay. Coherent multi-file changes, real diagnostic workflows, its own
  context management.
- **Completion moved out of the protocol and into evidence.** A slice is
  complete when the project's configured commands pass *and* a coverage trace
  proves they executed the changed lines. This came directly from watching
  Slice 2 pass checks that never imported the code, and later from a live slice
  that wrote 93 passing tests into a directory the test command never collected
  from.
- **The relay records what the model actually cost**, per exchange, because the
  8× input-token result was invisible until something counted it.
- **Context became a measured quantity**, not an assumption — the 65,536-token
  wall is a real boundary that a real run hit exactly.

The v1 bounded protocol still exists for quick non-plan tasks. It is no longer
the path that ships plan work.

---

## Result 3: measuring the rebuild

If the argument is "the sandbox recovers the capability the protocol was
suppressing", that has to be measured, not asserted.

**Design.** Six frozen slots: three repetitions × two arms (unrestricted
control, Apoapsis sandbox), same task, same model, same server. Scored
independently on *first-proposal* quality — the fraction of the authoritative
checkpoint's acceptance criteria satisfied, with repaired quality excluded, so
the harness cannot earn credit for fixing its own arm. The rule is per-pair
non-inferiority; no aggregate is allowed to hide a losing pair.

**Result** ([`slice-7p4-live-pilot-v4-2026-08-01.md`](evaluation/slice-7p4-live-pilot-v4-2026-08-01.md)):

| Repetition | Control | Sandbox | Pair |
| --- | ---: | ---: | --- |
| rep-1 | 1.0 | 1.0 | non-inferior |
| rep-2 | 1.0 | 1.0 | non-inferior |
| rep-3 | 1.0 | 1.0 | non-inferior |

All six first proposals were `COMPLETE` with all three criteria satisfied and
no readiness blocks. No sandbox continuation or external repair was needed.
Across the six slots: 1,166,038 input tokens, 18,039 output tokens, 80 tool
calls, zero malformed responses, zero model errors.

**Detection is a separate claim and I will not merge it into this one.** The
live sandbox proposals were complete, so there was nothing incomplete for the
gate to catch. Under the locked scorer a clean pass on a complete shape scores
1.0 for detection — restating that as "the harness caught defects live" would
be a lie. Detection is established deterministically instead: a zero-model
rehearsal injects 17 controls — incomplete work, stale evidence, truncation,
configuration drift, contamination, and an attempt to hide a pair regression —
and **17/17 fired their mapped detector.**

---

## The negative result I kept

Earlier, on a different local model (`qwen3-coder-next` Q4_K_M), I ran a
six-attempt comparison of monolithic versus plan-then-slices execution, with
the plan produced by a separate Gemini 3.1 Pro session.

**0 of 6 completed.** Every attempt — three monolithic, three planned —
stopped at `HUMAN_REVIEW_REQUIRED` after exhausting its full 12-turn budget
**having called a verification command zero times.** The turn logs show the
same shape in all six: read the target file, make exactly one edit, then spend
every remaining turn re-reading files it had already read.

Every mechanical part of the system behaved correctly. Budgets were counted and
enforced; escalation classified correctly every time; the planned condition
correctly stopped advancing at the first non-complete slice; the held-out
oracle correctly never ran, so false success is recorded as *unmeasured* rather
than zero — there were no claimed successes to evaluate.

It was a model-logic failure, not a harness bug, and I could not explain the
contrast with two later single-slice probes that both completed. It is in the
docs unexplained. Full detail:
[`apoapsis-planning-comparison-2026-07-20.md`](evaluation/apoapsis-planning-comparison-2026-07-20.md).

---

## What this does and does not establish

**Does:**

- The model's action interface materially changes its proposal quality — same
  weights, same plan, very different output.
- A model with a real shell will report success it has not achieved, and its
  own tests will agree with it.
- The rebuilt sandbox path is non-inferior to an unrestricted baseline on this
  benchmark, per pair, on first proposals, independently scored.
- The detection machinery fires on all 17 of its mapped controls.

**Does not:**

- Crisis Atlas is **not held out**. It is a regression benchmark I have looked
  at. This is not broad-corpus superiority and I do not claim it is.
- One model, one quantisation, one context size.
- Three pairs is three pairs. All six proposals scoring 1.0 means the task did
  not discriminate at the top — a harder benchmark would say more.
- No live run has yet produced an incomplete candidate for the gate to catch,
  so live detection is unobserved.
- The 8× input-token cost of the unrestricted shape is real and I have not
  finished paying it down.

---

## If you only remember one thing

I ran the control that could have shown my design was the problem. It did. The
same run also showed that the thing my design was *for* — refusing to let a
model certify its own work — caught a defect that eighty-eight passing tests
missed.

Both of those are in here, dated, with the numbers.
