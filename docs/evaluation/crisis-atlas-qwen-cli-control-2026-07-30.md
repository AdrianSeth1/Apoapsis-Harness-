# Crisis Atlas unrestricted Qwen CLI control

Date: 2026-07-30
Evidence class: live local inference plus independent host/browser verification

## Question

Would Qwen3.6-27B Q4_K_M do better if it received the complete approved Crisis
Atlas handoff and operated as an autonomous CLI coding agent with a real shell,
instead of working through Apoapsis one slice and one typed action at a time?

This is a control for the model/tool interface, not a safety recommendation and
not a clean measurement of planning versus no planning. The model received the
same harness-produced plan used by the sliced trial. What changed was its action
surface and feedback loop.

## Safety boundary

The agent's shell was unrestricted inside a disposable Docker container, not on
the host:

- the only host mount was a fresh clone of seed commit
  `197b3610e5720cf36718c548fa19c05fe784a978`;
- the container had no network, Docker socket, harness checkout, credentials, or
  other host paths;
- the container was capped at 2 GiB memory, four CPUs, and 256 processes, with
  Linux capabilities dropped and `no-new-privileges`;
- Qwen could inspect, create, overwrite, delete, launch, and test anything under
  `/workspace`;
- the host-side controller alone called the loopback model API and executed each
  requested Bash command inside the container.

The arm is preserved at
`.apoapsis-eval/crisis-atlas-qwen-cli-unrestricted-64k-2026-07-30`.

## Model and budget

- Model: Qwen3.6-27B Q4_K_M
- Server context: 65,536
- Per-response output cap: 16,384
- Temperature: 0
- Wall-clock budget: 1,800 seconds
- Handoff: complete approved plan
  `PLAN-C672117CD8F5/plan-v1.json`
- Shell command timeout: at most 300 seconds per action

The agent received no Apoapsis verification output, Codex repair, held-out
browser result, or human code hint.

## Context rollover

The first context performed 24 successful shell actions. Its attempted README
write ended exactly at 65,536 total tokens:

- provider finish reason: `length`;
- prompt tokens: 64,409;
- completion tokens: 1,127;
- the tool-call JSON string was truncated and could not execute;
- the following request received HTTP 500 because the conversation no longer
  fit the 64K window.

This was a context-ceiling failure, not an output-cap failure and not a model
completion. A CLI agent is expected to compact or roll context, so the evaluator
started one fresh continuation against the unchanged worktree and charged it
only the remaining wall-clock budget. The continuation was told to inspect the
repository and continue the same full handoff; it received no diagnosis of
product bugs. It stopped normally after repairing its own tests and README and
reported completion.

The controller then hit a Windows console-encoding error while printing Qwen's
final check-mark characters. `run-summary.json` had already been written and
records `model_final:stop`; this reporting defect did not change the worktree or
model run.

## Model behavior

Across both contexts Qwen:

1. inspected the seed;
2. implemented domain, persistence, service, export, API, one-process server,
   dashboard, and README layers;
3. added domain, persistence, service, API, behavioral, and launch tests;
4. ran the full suite;
5. diagnosed nine errors in its own stateful integration-test fixture and two
   README failures caused by the truncated write;
6. repaired the fixtures and README;
7. reran the full, behavioral, and launch suites;
8. exercised the real server and checked the required file inventory; and
9. stopped with a concise completion report.

The resulting self-authored suite contains 88 tests, all passing.

## Independent verification

| Check | Result |
| --- | --- |
| `python -m unittest discover -s tests -v` | 88/88 passed |
| `python -m unittest tests.test_behavioral_integration -v` | 14/14 passed |
| `python -m unittest tests.test_launch -v` | 9/9 passed |
| `python -m compileall -q .` | passed |
| `git diff --check` | passed |
| Forbidden offline/browser storage search | no `localStorage`, `sessionStorage`, or `indexedDB` |
| Strict `verify-web-product` | **failed: 10 warnings treated as errors** |
| Independent browser lifecycle | create/select/status/timeline/action/reload passed; **filter failed** |

Nine strict-web warnings concern severity/status classes that JavaScript applies
dynamically; the browser proved those classes render, so they are static
verifier false positives. The tenth warning is a computed fetch target that the
static verifier cannot prove same-origin; source and the browser run show that
it is same-origin. Nevertheless, the plan explicitly requires this configured
command to pass, so the arm is not deliverable under the approved contract.

The browser found a real product defect that Qwen's 88 tests missed. After
creating a critical incident and changing it to `investigating`, selecting the
`Closed` status filter still displayed that incident. Direct requests confirmed
that both:

```text
/api/incidents?status=closed
/api/incidents?status=investigating
```

returned the same incident. `api/server.py` parses the URL but passes only
`parsed.path` to `APIHandler`; `APIHandler` then mistakes regex path parameters
for query parameters. The service's filter implementation is present but never
receives the query.

The UI is usable but visually less polished than the Codex-repaired sliced
candidate: at the tested desktop viewport it occupies a narrow left column with
large unused space. No console errors were observed.

## Telemetry comparison

| Arm | Successful model calls | Input tokens | Output tokens | Provider latency |
| --- | ---: | ---: | ---: | ---: |
| Four sliced Qwen attempts | 19 | 258,632 | 55,364 | 1,467.5 s |
| Unrestricted CLI control | 62 (63 attempted) | 2,080,801 | 35,787 | 1,052.3 s |

The unrestricted arm used about **8.0 times more input tokens**, because the
growing conversation and shell evidence were replayed on every call. It used
about **35% fewer output tokens** and about **28% less provider latency** than
the four sliced Qwen attempts. It therefore did not save local-model input
tokens. A production CLI would need deliberate observation compaction and
stable-prefix caching to avoid this replay cost.

Codex token use during the checkpoint repairs was not recorded in the sliced
provider telemetry, so no honest total-Qwen-plus-Codex token comparison is
available.

## Comparison with the sliced trial

The unrestricted agent was materially better than raw sliced Qwen:

- it did not repeat Slice 2's one-call false completion with an incompatible
  wrong-package skeleton;
- it built and connected every planned layer;
- it added new tests instead of relying only on inherited tests;
- it used test failures to repair its own work; and
- it delivered a near-complete application without Codex editing its files.

That does **not** show that the harness or slicing has no value. It shows that
Qwen's proposal quality is highly sensitive to its action protocol. The
one-action sliced interface and weak acceptance mapping suppressed capability;
the unrestricted shell plus persistent worktree exposed much more of it.

The control also reproduced the reason an independent authority is still
needed. Qwen claimed all acceptance criteria, including browser dashboard
filtering and web integrity, were satisfied. Independent verification disproved
both claims. The earlier sliced harness also had false-green gaps, but Codex
checkpoint review caught and repaired them. The unrestricted arm had no such
review and would have shipped a false success.

## Practical conclusion

The harness-produced handoff was valuable: Qwen used it to build a coherent
whole product. The current bounded action loop was costly in a different way:
it prevented Qwen from expressing the same capability efficiently. A better
design should preserve sandboxing, audit, owner-approved commands, and
independent completion authority while allowing coherent multi-file changes,
real shell-like diagnostic workflows inside the sandbox, and automatic context
compaction.

For a real project, this direct result would have been easier for Codex or a
human to review and finish than the raw per-slice outputs: one coherent product,
one confirmed filtering defect, one strict-gate incompatibility, and visual
polish work. It would not be safe to skip review merely because 88
self-authored tests passed.
